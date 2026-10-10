# -*- coding: utf-8 -*-
"""M2 执行引擎：分包派发 / 动态并发 / 水位背压 / journal 断点续采 / watchdog。

派发单元 = deck。片完整性红线：一 deck = 一片(器件×L×vds 锚，内含全 W×全 Vgs)
= 多实例单分析形制 M1-v2（实机判活，解耦哨兵逐位一致）。

并发三层取小（用户令：机器规格禁写死，每包重探）：
  N = min(逻辑线程 − 线程预留, license 申报上限 − 为同事预留, 内存预算/单枪RSS)
license 只用 profile 申报值，绝不探测 license 文件；教学 VM uncounted 结论不作设计依据。

背压：只停派新枪，绝不杀在跑；连续失败/内存吃紧 ⇒ 本包收枪、下包阶梯减半（最低 1）。
让路模式 = 人工 --jobs 1 + --mem-floor 抬高。
断点续采 = 账本重放：missing ∪ 超期 dispatched 重派；rejected 默认 dead-letter 不自动重派。
防 3600 死机三件套：每包 ≤2000 枪、包间清远端 .raw/日志、spectre +disk_check 开关（单位候标定）。
"""
from __future__ import annotations

import json
import queue
import re
import threading
import time
from pathlib import Path

from .deck_gen import fmtv, render_batch_deck
from .profile import sha256_text

# ---------- 计划层 ----------

def sanitize_id(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.\-]", "_", s)


def make_plan(prof: dict, pair: str, fet: str, Ls: list[str], Ws: list[str],
              vds_anchors: list[float], vstep: float = 0.01,
              include_charge: bool = False) -> list[dict]:
    """网格 → deck 计划表。一 (L, vds) 一 deck，deck 内全 W 多实例。"""
    plans = []
    for L in Ls:
        for vds in vds_anchors:
            cells = [{"L": L, "W": w, "vds": vds} for w in Ws]
            deck = render_batch_deck(prof, pair, fet, cells, vstep=vstep,
                                     include_charge=include_charge)
            did = sanitize_id(f"{pair}.{fet}.L{L}.V{fmtv(vds)}")
            plans.append({"deck_id": did, "pair": pair, "fet": fet,
                          "L": L, "vds": vds, "Ws": list(Ws),
                          "text": deck["text"], "cells": deck["cells"],
                          "deck_sha256": deck["deck_sha256"]})
    return plans


def chunk(items: list, size: int) -> list[list]:
    """分包：缺省 ≤2000（硬顶 3000，用户令防资源累积死机）。"""
    size = 2000 if size <= 0 else min(size, 3000)
    return [items[i:i + size] for i in range(0, len(items), size)]


def plan_hash(plans: list[dict]) -> str:
    return sha256_text("".join(p["deck_sha256"] for p in plans))


# ---------- 探测与并发预算 ----------

PROBE_CMD = ("nproc; free -m | awk '/Mem:/{print $7}'; "
             "df -m --output=avail . | awk 'NR==2{print $1}'")


def measure(remote) -> dict:
    """每包三探测：逻辑线程 / 可用内存 MB / 工作目录所在盘可用 MB。

    走 run 不走 run_b：探测命令自带 awk 单引号，经 bash -lc '…' 二次包裹会被打碎
    （实机踩坑 10-09），而 nproc/free/df 无需登录环境。
    """
    rc, out = remote.run(PROBE_CMD, timeout=60)
    lines = [l.strip() for l in out.splitlines() if l.strip()]
    try:
        return {"nproc": int(lines[0]), "mem_avail_mb": int(lines[1]),
                "disk_free_mb": int(lines[2]), "rc": rc}
    except (ValueError, IndexError):
        raise RuntimeError(f"资源探测输出不可解析: {out[:200]!r}")


def compute_jobs(probe: dict, prof: dict, thread_reserve: int = 4,
                 rss_floor_mb: int = 200, jobs_override: int | None = None) -> dict:
    """N = min(线程−预留, license申报−为同事预留, 可用内存/RSS下限)。"""
    lic = prof.get("license", {})
    caps = {"n_thread": max(1, probe["nproc"] - thread_reserve),
            "n_mem": max(1, probe["mem_avail_mb"] // rss_floor_mb)}
    cap = lic.get("max_concurrent_spectre")
    if cap:
        caps["n_lic"] = max(1, int(cap) - int(lic.get("reserve_for_others", 0)))
    binding = min(caps, key=caps.get)
    n = caps[binding]
    if jobs_override:
        n, binding = max(1, jobs_override), "override"
    return {"jobs": n, "binding": binding, "caps": caps, "probe": probe}


# ---------- 账本（journal，append-only JSONL） ----------

class Ledger:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def append(self, deck_id: str, state: str, **fields) -> None:
        rec = {"ts": round(time.time(), 3), "deck_id": deck_id, "state": state}
        rec.update({k: v for k, v in fields.items() if v is not None})
        with self._lock, self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def last_states(self) -> dict:
        out = {}
        if not self.path.is_file():
            return out
        with self.path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        r = json.loads(line)
                        out[r["deck_id"]] = r
                    except (json.JSONDecodeError, KeyError):
                        continue
        return out

    def resume_plan(self, plans: list[dict], data_dir: str | Path,
                    stale_h: float = 24.0) -> dict:
        """账本重放：返回 {to_dispatch, done, held, rejected, stale_redo}。
        arrived 但以本地文件为准（账本说到了、盘上没有 ⇒ 重派）。"""
        data_dir = Path(data_dir)
        last = self.last_states()
        now = time.time()
        res = {"to_dispatch": [], "done": [], "held": [], "rejected": [],
               "stale_redo": []}
        for p in plans:
            e = last.get(p["deck_id"])
            if e is None or e["state"] == "planned":
                res["to_dispatch"].append(p)
            elif e["state"] == "arrived":
                if self._local_ok(data_dir, p["deck_id"]):
                    res["done"].append(p["deck_id"])
                else:
                    res["to_dispatch"].append(p)  # 空口无凭，重派
            elif e["state"] == "rejected":
                res["rejected"].append(p["deck_id"])  # dead-letter，不自动重派
            elif e["state"] == "dispatched":
                if now - e["ts"] > stale_h * 3600:
                    res["stale_redo"].append(p)
                    res["to_dispatch"].append(p)
                else:
                    res["held"].append(p["deck_id"])  # 他进程在跑，不动
        return res

    @staticmethod
    def _local_ok(data_dir: Path, deck_id: str) -> bool:
        d = data_dir / deck_id
        return d.is_dir() and any(f.stat().st_size > 0 for f in d.iterdir()
                                  if f.is_file())


# ---------- 单 deck 执行 ----------

ERROR_RE = re.compile(r"(\d+)\s+error")
RC_RE = re.compile(r"SPECTRE_RC=(\d+)")


def spectre_cmd(prof: dict, name: str, wd: str, watchdog_s: int,
                disk_check_mb: int | None = None) -> str:
    cad = prof["cadence"]
    flags = [cad.get("arch", "-64")] + list(cad.get("extra_flags", []))
    if disk_check_mb:
        # 官方帮助实机定案(10-09): +disk_check 阈值单位=字节("less than N bytes,
        # 例 +disk_check=1e10")，引擎对外接口按 MB，入令换算 ×1e6。
        flags.append(f"+disk_check={int(disk_check_mb * 1_000_000)}")
    inner = (f"cd {wd} && (timeout {watchdog_s}s {cad['spectre_bin']} "
             f"{' '.join(flags)} {name}.scs > {name}_console.log 2>&1; "
             f"echo SPECTRE_RC=$?)")
    return f"{prof['connection'].get('shell_wrap', 'bash -lc')} '{inner}'"


def run_deck(remote, prof: dict, plan: dict, wd: str, data_dir: Path,
             watchdog_s: int = 300, disk_check_mb: int | None = None) -> dict:
    """上传→仿真(远端 timeout 兜 watchdog)→健康闸→拉样。返回 {ok, reason, files}。"""
    name = plan["deck_id"]
    sdir = Path(data_dir) / name
    sdir.mkdir(parents=True, exist_ok=True)
    remote.run_b(f"mkdir -p {wd}", timeout=60)
    remote.put_text(plan["text"], f"{wd}/{name}.scs")
    rc, out = remote.run(spectre_cmd(prof, name, wd, watchdog_s, disk_check_mb),
                         timeout=watchdog_s + 120)
    m = RC_RE.search(out)
    src = int(m.group(1)) if m else -1
    if src == 124:
        return {"ok": False, "reason": "timeout", "files": []}
    # 健康闸：spectre.out 报 errors>0 响亮拒（SFE-691 那次就是这闸拦的）
    _, tail = remote.run_b(
        f"tail -3 {wd}/{name}_spectre.out 2>/dev/null || tail -5 {wd}/{name}_console.log",
        timeout=60)
    errs = [int(x) for x in ERROR_RE.findall(tail)]
    if errs and any(errs):
        return {"ok": False, "reason": f"errors:{max(errs)}", "files": [],
                "health": tail[-400:]}
    if src != 0:
        return {"ok": False, "reason": f"rc:{src}", "files": [],
                "health": tail[-400:]}
    _, listing = remote.run_b(f"find {wd}/{name}.raw -type f 2>/dev/null | head -200",
                              timeout=60)
    files = [f.strip() for f in listing.splitlines() if f.strip()]
    if not files:
        return {"ok": False, "reason": "no_raw", "files": []}
    got = []
    for rf in files:
        rel = rf.split(f"{name}.raw/", 1)[-1]
        rel = rel.split("..", 1)[-1].lstrip("/")  # psf/../x → x；防越界
        lp = sdir / rel
        lp.parent.mkdir(parents=True, exist_ok=True)
        got.append(rel)
        remote.get(rf, str(lp))
    return {"ok": True, "reason": "", "files": got, "src_rc": src}


# ---------- 包级并发派发（线程池 + 背压） ----------

def run_batch(transport_factory, prof: dict, plans: list[dict], wd: str,
              data_dir: str | Path, jobs: int, ledger: Ledger,
              watchdog_s: int = 300, disk_check_mb: int | None = None,
              fail_stop: int = 4, on_event=None) -> dict:
    """一包内并发 jobs 条枪。背压=连续 fail_stop 败 ⇒ 停派新枪(不杀在跑)。"""
    q = queue.Queue()
    for p in plans:
        q.put(p)
    results = {"arrived": [], "rejected": [], "held_back": []}
    lock = threading.Lock()
    stop = threading.Event()
    streak = [0]

    def emit(ev):
        if on_event:
            on_event(ev)

    def worker():
        remote = transport_factory()
        try:
            while not stop.is_set():
                try:
                    p = q.get_nowait()
                except queue.Empty:
                    return
                ledger.append(p["deck_id"], "dispatched", wd=wd)
                t0 = time.time()
                try:
                    r = run_deck(remote, prof, p, wd, Path(data_dir),
                                watchdog_s, disk_check_mb)
                except Exception as e:  # 连接断等：记败不炸池
                    r = {"ok": False, "reason": f"exc:{type(e).__name__}:{e}",
                         "files": []}
                dt = time.time() - t0
                with lock:
                    if r["ok"]:
                        streak[0] = 0
                        results["arrived"].append(p["deck_id"])
                        ledger.append(p["deck_id"], "arrived",
                                      deck_sha256=p["deck_sha256"],
                                      files=len(r["files"]), secs=round(dt, 2))
                        emit({"ev": "arrived", "deck": p["deck_id"], "secs": dt})
                    else:
                        streak[0] += 1
                        results["rejected"].append((p["deck_id"], r["reason"]))
                        ledger.append(p["deck_id"], "rejected",
                                      deck_sha256=p["deck_sha256"],
                                      reason=r["reason"], secs=round(dt, 2))
                        emit({"ev": "rejected", "deck": p["deck_id"],
                              "reason": r["reason"], "secs": dt})
                        if streak[0] >= fail_stop:
                            stop.set()
                            emit({"ev": "backpressure", "streak": streak[0]})
                q.task_done()
        finally:
            remote.close()

    ths = [threading.Thread(target=worker, daemon=True) for _ in range(jobs)]
    t0 = time.time()
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    n_ok = len(results["arrived"])
    return {"arrived": results["arrived"], "rejected": results["rejected"],
            "backpressure": stop.is_set(),
            "unconsumed": q.qsize() if hasattr(q, "qsize") else 0,
            "batch_secs": round(time.time() - t0, 1),
            "per_deck_secs": round((time.time() - t0) / max(1, n_ok + len(results["rejected"])), 2)}


def clean_remote(remote, prof: dict, wd: str, deck_ids: list[str]) -> None:
    """包间清理：清远端 .raw/日志/scs（治 3600 死机的存储侧）。失败不炸循环。"""
    if not deck_ids:
        return
    paths = []
    for d in deck_ids[:400]:
        paths += [f"{wd}/{d}.raw", f"{wd}/{d}.scs", f"{wd}/{d}.log",
                  f"{wd}/{d}_spectre.out", f"{wd}/{d}_console.log"]
    remote.run_b("rm -rf " + " ".join(paths), timeout=300)


def ladder(prev_jobs: int, batch_report: dict) -> int:
    """阶梯回退：本包背压 ⇒ 下包减半(最低 1)。"""
    return max(1, prev_jobs // 2) if batch_report.get("backpressure") else prev_jobs
