# -*- coding: utf-8 -*-
"""M4-4c 插值误差闸：半步加密真仿对照粗格插值（vds 轴）。

闭环定位（10-09 组装令指令③，四证据流之一）：粗格锚点（已采）与相邻中点锚
（加密批真仿）同点对照——插值预测 vs 真值，gm/ids 相对超差 ⇒ 红牌 + 自动
生成加密待采队列（M5 消费形制）。

膝区专项的化解（不是新探针）：中点 signed-log 线性插值误差 ≡ 对数域二阶导
的三点差分估计——gm 误差大的锚段就是"该管膝区所在段"；膝随 L 走
（R-b 教训：绝对 vds 网格≠各管膝区网格），故按 (pair,fet,L) 呈报锚段弯曲
排序。曲率绝对值随模型/vstep 漂移，不设魔法阈值：膝区只进报告呈证据，
红牌与队列提案权全留给误差闸——最弯段若真超差，误差闸自会把该跨度点名下队，
干净网格不刷队列（证据流不造噪声提案）。

口径诚实账：
  * 参考口径 = 沿 vds 的 signed-log 线性插值（与生产 mem_v1 的 log 插值族同形）。
    本闸约束格密度量级，不要求与生产插值器逐位等——生产还含 vgs 向、W log 向
    插值与 logJ 主键选择，那是另一面账。
  * 只考 vds 轴：W log 插值中域无罪 = R8 判决；vgs 系细扫密格，插值风险在锚点向。
  * 显著段：三点（粗格两端 + 中点真值）的 |ids|/W 均 ≥ qc.j_sig_per_w（继承 4a）。
  * 判级粒度 = 实例 × 锚对 median（4a 裁定 + I2 连坐教训：多实例混合 median
    稀释漏检，故按实例判、队列按实例点名）。锚对三片都点名牌（插值坏=格设计
    问题，两粗锚+一加密共担）。
  * 队列落 out/queue（gitignore）：行内含跨度与误差统计（派生量）。台账
    （ledgers/ 零物理数值纪律）将来收它时建议只收计数与牌名，M5 裁。

CLI = cli.py interp-plan（算中点、出加密批计划）/ interp-check（对照+闸+队列）。
纯标准库，零三方依赖。
"""
from __future__ import annotations

import json
import math
import time
from pathlib import Path

from . import qc
from .qc import ensure_csv, instances, load_slice, per_w, relerr, stat, vgs_of

INTERP_DEFAULTS = {
    "gm_tol": 0.01,    # 指令③：gm 相对误差 ≤1%
    "ids_tol": 0.02,   # 指令③：ids 相对误差 ≤2%
}


def interp_thresholds(prof: dict) -> dict:
    t = dict(INTERP_DEFAULTS)
    t.update(((prof.get("qc") or {}).get("interp")) or {})
    for k in ("gm_tol", "ids_tol"):
        if not isinstance(t[k], (int, float)) or not (0 < t[k] < 1):
            raise ValueError(f"qc.interp.{k} 须在 (0,1)，现为 {t[k]!r}")
    return t


# ---------- 网格小工具（纯函数，单测直打） ----------

def midpoint_pairs(anchors) -> list[tuple[float, float, float]]:
    """粗锚表 → [(a, b, 中点)]；升序、去重；单锚返回空。"""
    xs = sorted({float(a) for a in anchors})
    return [(a, b, 0.5 * (a + b)) for a, b in zip(xs, xs[1:])]


def interp_log(xa: float, xb: float):
    """中点 signed-log 线性插值预测（带符号几何均值）；非同号/零/非有限 → None。"""
    if not (math.isfinite(xa) and math.isfinite(xb)):
        return None
    if xa == 0 or xb == 0 or xa * xb <= 0:
        return None
    g = math.exp(0.5 * (math.log(abs(xa)) + math.log(abs(xb))))
    return math.copysign(g, xa)


# ---------- 装载 ----------

def _load_group(prof: dict, plans: list[dict], data_dir: Path, convert: bool):
    """deck 计划 → {deck_id: {plan, v, insts}}；装载失败片点名进 missing 册。"""
    tools = Path(__file__).resolve().parents[1] / "tools"
    ok: dict[str, dict] = {}
    miss: list[str] = []
    for p in plans:
        d = data_dir / p["deck_id"]
        if convert:
            csvp, _rc = ensure_csv(d, tools)
        else:
            f0 = d / "dc.csv"
            csvp = f0 if f0.is_file() else None
        if csvp is None:
            miss.append(p["deck_id"])
            continue
        cols = load_slice(csvp)
        v, _c = vgs_of(cols)
        if not v or len(v) < 2:
            miss.append(p["deck_id"] + "(无扫段列)")
            continue
        ok[p["deck_id"]] = {"plan": p, "v": v,
                            "insts": per_w(instances(cols), p.get("cells", []))}
    return ok, miss


def _fam(plans: list[dict], loaded: dict) -> dict[tuple, dict[float, str]]:
    """(pair, fet, L) → {vds: deck_id}（只收装载成功片）。"""
    fam: dict[tuple, dict[float, str]] = {}
    for p in plans:
        if p["deck_id"] in loaded:
            fam.setdefault((p["pair"], p["fet"], p["L"]), {})[float(p["vds"])] = p["deck_id"]
    return fam


def _align(vA: list[float], vB: list[float], vP: list[float]) -> bool:
    """三片 vgs 扫段向量逐位贴合（加密批 vstep 须与粗格一致的机器判据）。"""
    n = len(vA)
    if n != len(vB) or n != len(vP):
        return False
    return max(abs(vA[i] - vB[i]) for i in range(n)) < 1e-9 \
        and max(abs(vA[i] - vP[i]) for i in range(n)) < 1e-9


def _pair_errors(sa: dict, sb: dict, sp: dict, jsig: float) -> dict[str, list[float]]:
    """单实例三片对照：返回 {"gm": [...], "ids": [...]} 相对误差点集。"""
    out: dict[str, list[float]] = {"gm": [], "ids": []}
    # 显著段=直接比（per_w 已把 ids 除过 W，再除 _W_m 即双重除法——4c 测试
    # 复现件当场炸出该继承自 4a i5 的口径错，两处同修 10-09）。
    n = min(len(sa["ids"]), len(sb["ids"]), len(sp["ids"]),
            len(sa["gm"]), len(sb["gm"]), len(sp["gm"]))
    for i in range(n):
        xa, xb, xm = sa["ids"][i], sb["ids"][i], sp["ids"][i]
        if all(map(math.isfinite, (xa, xb, xm))) \
                and min(abs(xa), abs(xb), abs(xm)) >= jsig:
            pred = interp_log(xa, xb)
            if pred is not None:
                out["ids"].append(relerr(abs(pred), abs(xm)))
        ga, gb, gm_ = sa["gm"][i], sb["gm"][i], sp["gm"][i]
        if all(map(math.isfinite, (ga, gb, gm_))) and min(ga, gb, gm_) > 0:
            pred = interp_log(ga, gb)          # gm 恒正口径（10-09 实测定案）
            if pred is not None:
                out["gm"].append(relerr(abs(pred), abs(gm_)))
    return out


# ---------- 主流程 ----------

def run_interp_check(prof: dict, coarse_plans: list[dict], probe_plans: list[dict],
                     data_dir: str | Path, gm_tol: float | None = None,
                     ids_tol: float | None = None, partial: bool = False,
                     convert: bool = True) -> dict:
    thr = interp_thresholds(prof)
    if gm_tol is not None:
        thr["gm_tol"] = gm_tol
    if ids_tol is not None:
        thr["ids_tol"] = ids_tol
    jsig = qc.qc_thresholds(prof)["j_sig_per_w"]
    data_dir = Path(data_dir)

    coarse, c_miss = _load_group(prof, coarse_plans, data_dir, convert)
    probe, p_miss = _load_group(prof, probe_plans, data_dir, convert)

    hard: list[str] = []
    soft: list[str] = []
    mark = (hard if not partial else soft)
    if c_miss:
        mark.append(f"粗格片装载缺 {len(c_miss)}: {c_miss[:8]}"
                    + ("[partial 降呈报]" if partial else ""))
    if p_miss:
        mark.append(f"加密片装载缺 {len(p_miss)}: {p_miss[:8]}"
                    + ("[partial 降呈报]" if partial else ""))

    fam_c = _fam(coarse_plans, coarse)
    fam_p = _fam(probe_plans, probe)

    pair_stats: list[dict] = []
    queue_rows: list[dict] = []
    seen_queue: set[tuple] = set()
    n_probe_missing = 0
    n_vgs_misalign = 0
    n_pairs_checked = 0

    for key, byvds in sorted(fam_c.items()):
        pair, fet, L = key
        anchors = sorted(byvds)
        if len(anchors) < 2:
            soft.append(f"{key}: 单 vds 锚，插值闸不适用（待加密批）")
            continue
        pby = fam_p.get(key, {})
        for a, b, mid in midpoint_pairs(anchors):
            pid = next((d for v, d in pby.items() if abs(v - mid) < 1e-9), None)
            if pid is None:
                n_probe_missing += 1
                mark.append(f"加密中点片未到 {key} vds {a:g}→{b:g} 中点 {mid:g}")
                continue
            ca, cb = coarse[byvds[a]], coarse[byvds[b]]
            cp = probe[pid]
            if not _align(ca["v"], cb["v"], cp["v"]):
                n_vgs_misalign += 1
                soft.append(f"vgs 向量错位 {key} vds {a:g}→{b:g}"
                            f"（加密批 vstep 须与粗格一致）")
                continue
            n_pairs_checked += 1
            entry = {"key": list(key), "span": [a, b], "mid": mid,
                     "decks": [byvds[a], byvds[b]], "probe": pid,
                     "insts": {}, "fired": {}}
            agg = {"gm": [], "ids": []}
            common = sorted(set(ca["insts"]) & set(cb["insts"]) & set(cp["insts"]))
            for iname in common:
                sa, sb, sp = ca["insts"][iname], cb["insts"][iname], cp["insts"][iname]
                if not all(c in s for s in (sa, sb, sp) for c in ("ids", "gm")):
                    continue
                errs = _pair_errors(sa, sb, sp, jsig)
                sgm, sid = stat(errs["gm"]), stat(errs["ids"])

                def _r(s_):
                    return {k: (round(v, 6) if k != "n" else v) for k, v in s_.items()}

                entry["insts"][iname] = {"gm": _r(sgm), "ids": _r(sid)}
                agg["gm"] += errs["gm"]
                agg["ids"] += errs["ids"]
                for col, s_, tkey in (("gm", sgm, "gm_tol"), ("ids", sid, "ids_tol")):
                    if s_.get("n") and s_["median"] > thr[tkey]:
                        entry["fired"].setdefault(col, []).append(iname)
                        hard.append(
                            f"插值误差 {col} {key} vds {a:g}→{b:g} 实例 {iname} "
                            f"median {s_['median']:.3g} > {thr[tkey]}")
            gagg, iagg = stat(agg["gm"]), stat(agg["ids"])
            entry["gm_all"], entry["ids_all"] = _r(gagg), _r(iagg)
            for col, names in sorted(entry["fired"].items()):
                qkey = (pair, fet, L, round(a, 9), round(b, 9))
                if qkey not in seen_queue:
                    seen_queue.add(qkey)
                    queue_rows.append({
                        "ev": "densify", "ts": int(time.time()),
                        "origin": "interp",       # P4.4 三源并轨（缺省=interp 旧账兼容）
                        "reason": f"interp_{col}", "pair": pair, "fet": fet,
                        "L": L, "vds": mid, "span": [a, b], "gate": col,
                        "instances": names,
                        "decks": [byvds[a], byvds[b], pid]})
            if not gagg.get("n") and not iagg.get("n"):
                soft.append(f"零显著样本 {key} vds {a:g}→{b:g}")
            pair_stats.append(entry)

    # 膝区呈报（只进报告不进队列）：每 (pair,fet,L) 把已对照锚对按 gm 聚合
    # 误差 median 降序——误差=对数域二阶导差分估计，最弯段=膝区所在。
    by_fam: dict[tuple, list[dict]] = {}
    for e in pair_stats:
        by_fam.setdefault(tuple(e["key"]), []).append(e)
    knee_by_family: dict[str, list[dict]] = {}
    for key, es in sorted(by_fam.items()):
        ranked = sorted(es, key=lambda e: -(e["gm_all"].get("median") or -1.0))
        knee_by_family["|".join(map(str, key))] = [
            {"span": e["span"], "mid": e["mid"],
             "gm_median": e["gm_all"].get("median"),
             "ids_median": e["ids_all"].get("median")} for e in ranked]

    if n_pairs_checked == 0 and not hard:
        soft.append("无可对照锚对（加密批未跑或锚点不足）")

    verdict = "FAIL" if hard else ("WARN" if soft else "PASS")
    return {
        "verdict": verdict, "profile": prof.get("name", ""),
        "gm_tol": thr["gm_tol"], "ids_tol": thr["ids_tol"],
        "j_sig_per_w": jsig,
        "counts": {"pairs_checked": n_pairs_checked,
                   "probe_missing": n_probe_missing,
                   "vgs_misalign": n_vgs_misalign,
                   "coarse_loaded": len(coarse), "probe_loaded": len(probe),
                   "coarse_planned": len(coarse_plans),
                   "probe_planned": len(probe_plans)},
        "pair_stats": pair_stats,
        "knee_by_family": knee_by_family,
        "queue_rows": queue_rows,
        "hard_hits": hard, "soft_notes": soft,
    }


# ---------- 队列落盘 ----------

def append_queue(path: str | Path, rows: list[dict]) -> int:
    """JSONL append（out/queue 区，允许派生统计量）；先全量序列化再落，不落半行。"""
    if not rows:
        return 0
    lines = [json.dumps(r, ensure_ascii=False) for r in rows]
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write("".join(l + "\n" for l in lines))
    return len(lines)
