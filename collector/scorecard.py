# -*- coding: utf-8 -*-
"""scorecard.py — 数据集达标判据单（P4.5，设计 §0）。

四栏全连续量、**不设 PM 栏**（用户裁定原话存档："PM 的不确定性，是爹妈生出
就有的"）：
  ① 域覆盖：申报域（manifest devices.*.domain）vs 已覆盖（表轴）vs 电路负载
     （backflow origin=domain 命中块数）+ 差额块数；
  ② interp 误差分布：最近一次 interp-check 报告的 gm/ids median 谱+红牌数；
  ③ 金对子残差 Sp：优化器 verify_ledger.csv 导出件按目标列算 Spearman rho+
     |Δ| 中位（无件=无账，不造数）；
  ④ 五闸通过率：qc 指纹台账（ledgers/qc.jsonl）deck 级 gate 命中统计。

三盏灯（全绿 ⇒ 呈"可冻结 vN"）：
  L1 申报域 ⊇ 已覆盖域；L2 interp 红牌清零（且队列 origin=interp 在案=0）；
  L3 金对子 rho ≥ 门槛（缺账 ⇒ 灯不亮=灰，不冒充绿）。

纪律：零新物理量计算——全部数字来自已落盘台账/报告；缺源如实"无账"。
纯标准库（csv/json/statistics），零 numpy。产物落 out/report（gitignore 区，
允许派生统计量）。
"""
from __future__ import annotations

import csv
import json
import statistics
from pathlib import Path

from .backflow import load_rows      # 宽容 JSONL 装载（loop 同源）


def spearman(xs: list, ys: list):
    """Spearman rho（秩+并列均值秩→Pearson）。样本<3 ⇒ None（不冒充统计量）。"""
    pts = [(a, b) for a, b in zip(xs, ys)
           if a is not None and b is not None]
    if len(pts) < 3:
        return None, len(pts)

    def ranks(vals):
        order = sorted(range(len(vals)), key=lambda i: vals[i])
        rk = [0.0] * len(vals)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and vals[order[j + 1]] == vals[order[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1.0
            for k in range(i, j + 1):
                rk[order[k]] = avg
            i = j + 1
        return rk
    rx, ry = ranks([p[0] for p in pts]), ranks([p[1] for p in pts])
    n = len(rx)
    mx, my = sum(rx) / n, sum(ry) / n
    sxx = sum((a - mx) ** 2 for a in rx)
    syy = sum((b - my) ** 2 for b in ry)
    if sxx <= 0 or syy <= 0:
        return None, n
    sxy = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    return sxy / (sxx * syy) ** 0.5, n


def _fmt(v, nd=3):
    if v is None:
        return "—"
    if isinstance(v, float):
        return ("%.{}g".format(nd)) % v
    return str(v)


def _axis_covered_subset(domain: dict, dev: dict) -> tuple:
    """灯一机器判：表轴 ⊆ 申报域。返回 (ok, 越界轴列表)。"""
    bad = []

    def chk(ax, rng, pts):
        if not rng or not pts:
            return
        lo, hi = float(rng[0]), float(rng[1])
        if ax == "vds":                         # 幅值口径（表轴=|vds|，申报可负）
            pts = [abs(p) for p in pts]
            lo, hi = min(abs(lo), abs(hi)), max(abs(lo), abs(hi))
        if min(pts) < lo - 1e-15 or max(pts) > hi + 1e-15:
            bad.append(ax)
    chk("L", domain.get("L_range"), dev.get("L_range_nodes")
        or ([dev["L_range"][0], dev["L_range"][1]]
            if dev.get("L_range") else []))
    chk("W", domain.get("W_range"), dev.get("W_nodes"))
    chk("vds", domain.get("vds_range"), dev.get("vds_anchors"))
    vbss = [abs(v) for v in (dev.get("vbs_nodes") or [])]
    if domain.get("vbs_topology") == "tied" and any(v > 1e-12 for v in vbss):
        bad.append("vbs(tied 申报但有非零锚)")
    return (not bad), bad


# ---------------------------------------------------------------- 各栏装配

def col_domain(manifest: dict, backflow_rows: list) -> dict:
    devs = (manifest or {}).get("devices") or {}
    load = {}
    for r in backflow_rows or []:
        if r.get("origin") == "domain":
            m = r.get("model") or "?"
            load[m] = load.get(m, 0) + int(r.get("n") or 1)
    rows, lamp1, lamps1_bad = [], True, []
    any_domain = False
    for dev, meta in sorted(devs.items()):
        dom = meta.get("domain") or {}
        if not any(dom.get(k) for k in ("L_range", "W_range", "vds_range",
                                       "vgs_range")):
            continue
        any_domain = True
        ok, bad = _axis_covered_subset(dom, meta)
        if not ok:
            lamp1 = False
            lamps1_bad.append("%s: %s" % (dev, ",".join(bad)))
        rows.append({"dev": dev, "domain": dom,
                     "covered": {"L": meta.get("L_range"),
                                 "W": meta.get("W_nodes"),
                                 "vds": meta.get("vds_anchors"),
                                 "vbs": meta.get("vbs_nodes")},
                     "gap_blocks": meta.get("gap_blocks") or [],
                     "load_hits": load.get(dev, 0)})
    return {"have": bool(rows), "any_declared": any_domain,
            "lamp1": lamp1 if any_domain else None,
            "lamp1_bad": lamps1_bad, "rows": rows,
            "digest": (manifest or {}).get("table_digest") or ""}


def col_interp(interp: dict, open_interp_rows: int) -> dict:
    if not interp:
        return {"have": False}
    med_g, med_i, hard = [], [], interp.get("hard_hits") or []
    for e in interp.get("pair_stats") or []:
        if (e.get("gm_all") or {}).get("n"):
            med_g.append(e["gm_all"]["median"])
        if (e.get("ids_all") or {}).get("n"):
            med_i.append(e["ids_all"]["median"])

    def _spec(vals):
        if not vals:
            return {"n": 0, "median": None, "p90": None, "max": None}
        s = sorted(vals)
        k = min(len(s) - 1, int(round(0.9 * (len(s) - 1))))
        return {"n": len(s), "median": statistics.median(s),
                "p90": s[k], "max": s[-1]}
    return {"have": True,
            "verdict": interp.get("verdict"),
            "gm": _spec(med_g), "ids": _spec(med_i),
            "hard": len(hard), "open_proposals": open_interp_rows,
            "pairs_checked": (interp.get("counts") or {}).get("pairs_checked")}


def col_goldpairs(verify_rows: list, rho_min: float) -> dict:
    if not verify_rows:
        return {"have": False}
    objs = []
    for k in (verify_rows[0].keys() if verify_rows else []):
        if k.endswith("_sieve_raw"):
            objs.append(k[: -len("_sieve_raw")])
    stats, n_pts = [], 0
    for obj in objs:
        xs, ys = [], []
        for r in verify_rows:
            try:
                a = float(r.get(obj + "_sieve_raw") or "nan")
                b = float(r.get(obj + "_real_raw") or "nan")
            except ValueError:
                continue
            if a == a and b == b:              # NaN 跳（读不到=不计）
                xs.append(a)
                ys.append(b)
        rho, n = spearman(xs, ys)
        deltas = sorted(abs(a - b) for a, b in zip(xs, ys))
        stats.append({"obj": obj, "rho": rho, "n": n,
                      "d_med": (statistics.median(deltas) if deltas else None)})
        n_pts = max(n_pts, n)
    lamps = [s["rho"] for s in stats if s["rho"] is not None]
    return {"have": True, "rows": len(verify_rows), "n_pts": n_pts,
            "objs": stats, "rho_min": rho_min,
            "lamp3": (all(v >= rho_min for v in lamps) if lamps else None),
            "worst": (min(lamps) if lamps else None)}


def col_gates(qc_rows: list) -> dict:
    decks = [r for r in qc_rows or [] if r.get("ev") == "qc_deck"]
    runs = [r for r in qc_rows or [] if r.get("ev") == "qc_run"]
    if not decks:
        return {"have": bool(runs), "decks": 0, "gates": {}, "status": {},
                "runs": len(runs)}
    gates, status = {}, {}
    for r in decks:
        st = str(r.get("status") or "unjudged")
        status[st] = status.get(st, 0) + 1
        for g in set(r.get("gates_hard") or []):
            gates[g] = gates.get(g, 0) + 1
    n = len(decks)
    return {"have": True, "decks": n,
            "gate_fail": {g: {"n": c, "rate": round(100.0 * c / n, 1)}
                          for g, c in sorted(gates.items())},
            "status": status, "runs": len(runs),
            "last_verdict": (runs[-1].get("verdict") if runs else None)}


# ---------------------------------------------------------------- 主装配

def build_scorecard(*, manifest_path=None, interp_path=None, qc_ledger_path=None,
                    verify_path=None, backflow_path=None, queue_path=None,
                    events_path=None, gap_path=None, rho_min=0.9) -> dict:
    """各源缺省 None/缺档 ⇒ 该栏 have=False（如实"无账"）。零新物理计算。"""
    manifest = None
    if manifest_path:
        p = Path(manifest_path)
        if p.is_dir():
            p = p / "manifest.json"
        if p.is_file():
            try:
                manifest = json.loads(p.read_text(encoding="utf-8"))
            except ValueError:
                manifest = None
    interp = None
    if interp_path and Path(interp_path).is_file():
        try:
            interp = json.loads(Path(interp_path).read_text(encoding="utf-8"))
        except ValueError:
            interp = None
    qc_rows = load_rows(qc_ledger_path) if qc_ledger_path else []
    verify_rows = []
    if verify_path and Path(verify_path).is_file():
        with open(verify_path, newline="", encoding="utf-8") as f:
            verify_rows = list(csv.DictReader(f))
    bf_rows = load_rows(backflow_path) if backflow_path else []
    open_interp = 0
    if queue_path and Path(queue_path).is_file():
        from .loop import load_last_events, load_proposals, open_keys
        ev_path = events_path or (Path(queue_path).parent / "events.jsonl")
        for k, row in open_keys(load_proposals(queue_path),
                                load_last_events(ev_path)).items():
            if (row.get("origin") or "interp") == "interp":
                open_interp += 1
    c1 = col_domain(manifest or {}, bf_rows)
    c2 = col_interp(interp, open_interp)
    c3 = col_goldpairs(verify_rows, rho_min)
    c4 = col_gates(qc_rows)
    lamps = {
        "L1_申报⊇覆盖": c1["lamp1"] if c1["have"] else None,
        "L2_interp清零": (c2["hard"] == 0 and c2["open_proposals"] == 0)
        if c2["have"] else None,
        "L3_金对子Sp过线": c3["lamp3"] if c3["have"] else None,
    }
    green = all(v is True for v in lamps.values())
    gap_p = Path(gap_path) if gap_path else (
        (Path(backflow_path).parent / "gap_blocks.jsonl")
        if backflow_path else None)
    n_gap = sum(1 for r in load_rows(gap_p)
                if r.get("ev") == "gap_block") if gap_p else 0
    return {"pdk": (manifest or {}).get("pdk") or "",
            "table_digest": c1.get("digest") or "",
            "cols": {"domain": c1, "interp": c2, "goldpairs": c3, "gates": c4},
            "lamps": lamps, "freeze_ready": green,
            "lamp1_bad": c1.get("lamp1_bad") or [],
            "backflow_gap_blocks": n_gap}


def render_md(rep: dict) -> str:
    L = ["# 数据集达标判据单（P4.5）", ""]
    L.append("- PDK: `%s`　表指纹: `%s`" % (rep["pdk"] or "（无 manifest）",
                                          (rep["table_digest"] or "—")[:16]))
    lamps = rep["lamps"]

    def _lamp(v):
        return "🟢" if v is True else ("🔴" if v is False else "⚪ 无账")
    L += ["", "## 三盏灯（全绿⇒可冻结 v%s）" % ((rep["table_digest"] or "?")[:6]),
          "- L1 申报域 ⊇ 已覆盖域：%s%s" % (
              _lamp(lamps["L1_申报⊇覆盖"]),
              ("（越界: " + "; ".join(rep["lamp1_bad"]) + "）")
              if rep["lamp1_bad"] else ""),
          "- L2 interp 红牌清零：%s" % _lamp(lamps["L2_interp清零"]),
          "- L3 金对子残差 Sp 过线：%s" % _lamp(lamps["L3_金对子Sp过线"]),
          "- **结论：%s**" % ("三灯全绿，建议冻结发布（新需求攒 vN+1）"
                              if rep["freeze_ready"] else "未达标/账不全——照各栏欠账补采"),
          "", "## ① 域覆盖（申报 vs 已覆盖 vs 电路负载）"]
    c1 = rep["cols"]["domain"]
    if not c1["have"]:
        L.append("- 无申报域（profile domain 块未填或 manifest 未产——灯一灰）")
    for r in c1["rows"]:
        d = r["domain"]
        rng = " ".join("%s=[%s,%s]" % (k.split("_")[0], _fmt(d[k][0]), _fmt(d[k][1]))
                       for k in ("L_range", "W_range", "vds_range", "vgs_range")
                       if d.get(k))
        L.append("- `%s`：申报 %s（vbs=%s, source=%s）｜负载命中 %d 点｜"
                 "差额块 %d%s" % (r["dev"], rng or "（轴缺省）",
                                  d.get("vbs_topology"), d.get("source"),
                                  r["load_hits"], len(r["gap_blocks"]),
                                  ("｜" + "; ".join(
                                      "%s 边块 [%s,%s]" % (g["axis"],
                                                          _fmt(g.get("lo")),
                                                          _fmt(g.get("hi")))
                                      for g in r["gap_blocks"])) if r["gap_blocks"] else ""))
    c2 = rep["cols"]["interp"]
    L += ["", "## ② interp 误差分布"]
    if not c2["have"]:
        L.append("- 无 interp-check 报告（--interp 未给/缺档）")
    else:
        L.append("- verdict=%s 对照中点 %s；gm median/p90/max = %s/%s/%s；"
                 "ids = %s/%s/%s；红牌 %d；在案 interp 提案 %d"
                 % (c2["verdict"], _fmt(c2["pairs_checked"]),
                    _fmt(c2["gm"]["median"]), _fmt(c2["gm"]["p90"]),
                    _fmt(c2["gm"]["max"]), _fmt(c2["ids"]["median"]),
                    _fmt(c2["ids"]["p90"]), _fmt(c2["ids"]["max"]),
                    c2["hard"], c2["open_proposals"]))
    c3 = rep["cols"]["goldpairs"]
    L += ["", "## ③ 金对子残差（verify_ledger 导出）"]
    if not c3["have"]:
        L.append("- 无金对子账（run 未开 sieve_verify 或 --verify 未给）")
    else:
        for s in c3["objs"]:
            L.append("- `%s`: rho=%s（n=%s）｜|Δ| median=%s"
                     % (s["obj"], _fmt(s["rho"]), s["n"], _fmt(s["d_med"])))
        L.append("- 门槛 rho≥%s；最差 %s" % (_fmt(c3["rho_min"]),
                                             _fmt(c3["worst"])))
    c4 = rep["cols"]["gates"]
    L += ["", "## ④ 五闸通过率（qc 指纹台账）"]
    if not c4["decks"]:
        L.append("- 台账无 deck 行（--ledger 未给/未跑 qc）")
    else:
        L.append("- deck 数 %d；最近 qc_run verdict=%s；状态分布 %s"
                 % (c4["decks"], c4.get("last_verdict"),
                    json.dumps(c4["status"], ensure_ascii=False)))
        L.append("- 闸命中（FAIL deck 数/占比%%）：%s"
                 % (json.dumps(c4["gate_fail"], ensure_ascii=False)
                    if c4["gate_fail"] else "零命中（全过）"))
    L += ["", "## 附：候块账", "- backflow gap_block 在账 %d 行（L/W/vgs/J/vbs/"
          "残差——n 倍代价定块权在人，设计 §2）" % rep["backflow_gap_blocks"],
          "", "> 本单不触物理仿真、不新算物理量；数字全部读自台账。PM 不入"
          "判据（用户裁定）：其断续是目标定义的性质，不是数据的罪。"]
    return "\n".join(L) + "\n"
