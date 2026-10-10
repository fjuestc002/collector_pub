# -*- coding: utf-8 -*-
"""M5 发射原语：从 cli.cmd_run 抽出的唯一开火路径（run/drain/GUI 共用，防两套预算/分包走样）。

语义与 M2 全同：探测→预算→分包(≤2000)→并发派发→水位中止→清远端→阶梯回退；
账本重放续采在 engine.Ledger.resume_plan 里，重复调用不重火。
凭证不经参数：remote.connect 按画像 password_env→环境→getpass 三级取，本模块零凭证面。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from . import engine


def fire(prof: dict, plans: list[dict], data_dir: str | Path, *,
         host: str = "", wd: str = "~/collector_run", jobs: int = 0,
         thread_reserve: int = 4, rss_floor: int = 200, batch_size: int = 2000,
         watchdog: int = 300, disk_check: int = 0, disk_floor: int = 2048,
         stale_h: float = 24.0, keep_remote: bool = False,
         yes: bool = False, plan_only: bool = False, out=None) -> dict:
    """开火一批 plans（data_dir=pair 级）。返回 {rc, arrived, rejected, held, ...}。

    rc 约定同旧 cmd_run：0 完赛 / 1 有拒档 / 5 未确认(--yes) / 6 盘水位中止。
    """
    emit = out or (lambda s: print(s, flush=True))
    data_dir = Path(data_dir)
    led = engine.Ledger(data_dir / "ledger.jsonl")
    rp = led.resume_plan(plans, data_dir, stale_h=stale_h)
    emit(f"[fire] {len(plans)} 张 deck | 已到账 {len(rp['done'])} | 拒档 {len(rp['rejected'])} "
         f"| 他进程持有 {len(rp['held'])} | 待发 {len(rp['to_dispatch'])}"
         f"（其中超期重派 {len(rp['stale_redo'])}）")
    for p in plans:
        led.append(p["deck_id"], "planned", deck_sha256=p["deck_sha256"])
    res = {"rc": 0, "arrived": list(rp["done"]),
           "rejected": [[d, "ledger"] for d in rp["rejected"]],
           "held": list(rp["held"]), "planned": len(plans),
           "to_dispatch": len(rp["to_dispatch"]), "batches": 0}
    if plan_only or not rp["to_dispatch"]:
        return res
    from .remote import connect          # 真开火才要 paramiko
    probe_remote = connect(prof["connection"], host=host)
    probe = engine.measure(probe_remote)
    probe_remote.close()
    budget = engine.compute_jobs(probe, prof, thread_reserve=thread_reserve,
                                 rss_floor_mb=rss_floor, jobs_override=jobs or None)
    batches = engine.chunk(rp["to_dispatch"], batch_size)
    lic = prof.get("license", {})
    emit(f"[probe] 逻辑线程={probe['nproc']} 可用内存={probe['mem_avail_mb']}MB "
         f"盘可用={probe['disk_free_mb']}MB")
    emit(f"[budget] 并发 N={budget['jobs']}（约束项={budget['binding']}，"
         f"上限明细 {budget['caps']}；license 申报 "
         f"{lic.get('max_concurrent_spectre', '未设')} 预留同事 {lic.get('reserve_for_others', 0)}）")
    emit(f"[警告] 收集器将占用 VM 最多 {budget['jobs']} 核与约 "
         f"{budget['jobs'] * rss_floor} MB 内存，可能影响其他工作；"
         f"共 {len(batches)} 包 × ≤{batch_size} 枪。确认请加 --yes。")
    if not yes:
        res["rc"] = 5
        return res
    conn = dict(prof["connection"])
    if host:
        conn["host"] = host
    factory = lambda: connect(conn)  # noqa: E731  每工作线程独立短连接
    wd = wd.replace("~", f"/home/{conn['user']}")
    n_jobs = budget["jobs"]
    done_ct, rejected_ct = 0, 0
    res["batches"] = len(batches)
    t_start = datetime.now()
    for bi, batch in enumerate(batches, 1):
        rem = factory()
        probe = engine.measure(rem)
        rem.close()
        if probe["disk_free_mb"] < disk_floor:
            emit(f"[水位] 盘可用 {probe['disk_free_mb']}MB < 下限 {disk_floor}MB"
                 f" ⇒ 中止剩余包（响亮拒）")
            res["rc"] = 6
            return res
        budget = engine.compute_jobs(prof=prof, probe=probe,
                                     thread_reserve=thread_reserve,
                                     rss_floor_mb=rss_floor,
                                     jobs_override=min(jobs, n_jobs) if jobs else n_jobs)
        n_jobs = budget["jobs"]
        el = (datetime.now() - t_start).total_seconds()
        eta = (len(batches) - bi + 1) * len(batch) / max(1, n_jobs) * 0.6
        emit(f"[包 {bi}/{len(batches)}] 开火 {len(batch)} 枪 × {n_jobs} 并发 "
             f"（已运行 {el / 60:.1f}min，ETA≈{eta / 60:.1f}min）")
        rep = engine.run_batch(factory, prof, batch, wd, data_dir, n_jobs, led,
                               watchdog_s=watchdog, disk_check_mb=disk_check,
                               on_event=lambda ev: emit(
                                   f"  · {ev['ev']} {ev.get('deck', '')} "
                                   f"{ev.get('reason', '')} {ev.get('secs', 0):.1f}s"))
        done_ct += len(rep["arrived"])
        rejected_ct += len(rep["rejected"])
        res["arrived"] += list(rep["arrived"])
        res["rejected"] += list(rep["rejected"])
        emit(f"[包 {bi}/{len(batches)}] 到 {len(rep['arrived'])} 拒 {len(rep['rejected'])} "
             f"背压={'是' if rep['backpressure'] else '否'} 单枪均值 {rep['per_deck_secs']}s "
             f"（累计 {done_ct}/{len(rp['to_dispatch'])}，"
             f"ETA≈{(len(rp['to_dispatch']) - done_ct - rejected_ct) / max(1, n_jobs) * rep['per_deck_secs'] / 60:.1f}min）")
        if not keep_remote:
            rem = factory()
            engine.clean_remote(rem, prof, wd,
                                [p["deck_id"] for p in batch]
                                + [d for d, _ in rep["rejected"]])
            rem.close()
        n_jobs = engine.ladder(n_jobs, rep)  # 阶梯回退，下包生效
    emit(f"[收官] 到 {done_ct} 拒 {rejected_ct}（拒档=dead-letter 不自动重派，见账本）")
    res["rc"] = 0 if rejected_ct == 0 else 1
    return res
