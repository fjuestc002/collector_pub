# -*- coding: utf-8 -*-
"""backflow.py — 优化器回流清单入队（P4.4 收集器半边，设计 §3）。

优化器 run 尾产 `<run>/backflow.jsonl`（行形制见 optimization_tool
engine/backflow.py 头注——双仓协议面，改动须两侧对表）；本模块把它并进
单队列：

  · kind=densify 且 (pair,fet,L,span) 集齐 ⇒ 追加 densify.jsonl——与 interp
    提案同键制（pair|fet|L|a|b）、同幂等律（在案未销不重复入队）、同签名/
    劈层/开火/复验机器（run_drain 零新码）；origin 带 domain 入账。
  · 其余（gap_block / model 对不上画像 / 几何缺项）⇒ out/queue/gap_blocks.jsonl
    候块账：L/W/vgs/J/vbs 轴与残差整点在现行"家族固定 W 档"射程之外——
    n 倍代价何时付由签字环节定块（设计 §2），机器不代裁。

model→(pair,fet) 走画像 pairs.*.{n,p}.model 反查（表名=器件名=PDK 模型名
严格同名链的回程）。一个 model 命中多 pair ⇒ 响亮点名不猜。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from .loop import (key_str, load_last_events, load_proposals, open_keys,
                   append_rows, load_rows)

ORIGINS = ("interp", "domain", "residual")


def fmt_len_token(m) -> str:
    """米 → L 档字面量（"1.2u"/"130n"）；与 plan/drain 的 L token 口径互认用。"""
    try:
        f = float(m)
    except (TypeError, ValueError):
        return ""
    if f <= 0:
        return ""
    if f >= 1e-6:
        u, suf = f * 1e6, "u"
    else:
        u, suf = f * 1e9, "n"
    s = ("%.6f" % u).rstrip("0").rstrip(".")
    return (s or "0") + suf


def model_map(prof: dict) -> dict:
    """{model: (pair, fet)}；同 model 多挂 ⇒ 值置 None（歧义点名拒配）。"""
    out: dict = {}
    for pname, pair in (prof.get("pairs") or {}).items():
        for fet in ("n", "p"):
            mdl = ((pair or {}).get(fet) or {}).get("model")
            if not mdl:
                continue
            out[mdl] = None if mdl in out else (str(pname), fet)
    return out


def _gap_row(r: dict, reason: str) -> dict:
    g = {"ev": "gap_block", "ts": int(time.time()), "origin": r.get("origin"),
         "reason": reason, "model": r.get("model") or "",
         "axis": r.get("axis"), "L": r.get("L"), "W": r.get("W"),
         "vds": r.get("vds"), "span": r.get("span") or [None, None],
         "n": r.get("n") or 1, "run_id": r.get("run_id") or "",
         "detail": (r.get("detail") or "")[:160]}
    return g


def ingest_backflow(prof: dict, src, queue_path, gap_path=None, out=None) -> dict:
    """backflow.jsonl → 单队列并轨。返回 {added, skipped_open, gap, bad}。"""
    emit = out or (lambda s: print(s, flush=True))
    src = Path(src)
    rows = load_rows(src)
    if not rows:
        emit("[ingest] 回流文件为空/不存在：%s" % src)
        return {"added": 0, "skipped_open": 0, "gap": 0, "bad": 0,
                "queue": str(queue_path), "gap_file": str(gap_path or "")}
    mm = model_map(prof)
    proposals = load_proposals(queue_path)
    events = load_last_events(Path(queue_path).parent / "events.jsonl")
    added, gap_rows, skipped, bad = [], [], [], []
    for r in rows:
        if r.get("ev") != "backflow" or r.get("origin") not in ORIGINS:
            bad.append(r)
            continue
        model = r.get("model") or ""
        tgt = mm.get(model)
        why = None
        if not model:
            why = "model 缺（多器件网表无法归件）"
        elif model not in mm:
            why = "model 无画像配对（严格同名链反查落空）"
        elif tgt is None:
            why = "model 挂多 pair 歧义（画像 pairs.*.model 重复）"
        if (why or r.get("kind") != "densify" or r.get("axis") != "vds"
                or r.get("L") is None):
            rr = dict(r)
            rr.setdefault("reason", why or "kind/axis 不在开火射程")
            gap_rows.append(_gap_row(rr, rr["reason"]))
            continue
        span = r.get("span") or [None, None]
        try:
            a, b = float(span[0]), float(span[1])
        except (TypeError, ValueError):
            gap_rows.append(_gap_row(r, "span 不可数"))
            continue
        if not (b > a):
            gap_rows.append(_gap_row(r, "span 零跨/反序"))
            continue
        pair, fet = tgt
        Ltok = fmt_len_token(r["L"])
        k = key_str(pair, fet, Ltok, a, b)
        if k in open_keys(proposals, events):
            skipped.append(k)
            continue
        added.append({"ev": "densify", "ts": int(time.time()),
                      "origin": r["origin"], "reason": r.get("reason")
                      or "domain_out", "pair": pair, "fet": fet,
                      "L": Ltok, "vds": round((a + b) / 2.0, 9),
                      "span": [round(a, 9), round(b, 9)],
                      "gate": "vds", "instances": [], "decks": [],
                      "n_hits": r.get("n") or 1,
                      "detail": (r.get("detail") or "")[:160]})
        proposals[k] = added[-1]            # 本批内同键去重
    n = append_rows(queue_path, added)
    gap_file = Path(gap_path) if gap_path else (
        Path(queue_path).parent / "gap_blocks.jsonl")
    seen_gap = {(g.get("origin"), g.get("model"), g.get("axis"), g.get("L"),
                 g.get("W")) for g in load_rows(gap_file)}
    gap_rows = [g for g in gap_rows
                if (g["origin"], g["model"], g["axis"], g["L"], g["W"])
                not in seen_gap]
    ng = append_rows(gap_file, gap_rows)
    emit("[ingest] 入队 %d 键（域/残差可执行块）；候块账 %d 行 → %s；"
         "在案未销跳过 %d；坏行 %d" % (n, ng, gap_file, len(skipped), len(bad)))
    for k in skipped[:6]:
        emit("[ingest] · 已在案：%s" % k)
    return {"added": n, "skipped_open": len(skipped), "gap": ng,
            "bad": len(bad), "queue": str(queue_path),
            "gap_file": str(gap_file)}
