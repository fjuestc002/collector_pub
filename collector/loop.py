# -*- coding: utf-8 -*-
"""M5 闭环调度：加密待采队列的消费——列账→人签字→发射→复验→事件账（幂等重放）。

闭环位置（设计指令③）：interp-check 超差把提案落 out/queue/densify.jsonl；drain 把
提案拆成下一层加密锚（最大跨先劈、min-gap 防爆密、每轮只劈一层），复用发射原语
（collector.fire），再 qc + interp-check 复验、事件账记状态；复验仍超差就落新提案
（跨度每轮自细分一层，--rounds 无人值守连转）。

人签字位（"攒提案人定期签预算"）：不给采纳令绝不发射——dry-run 只列账；--adopt
选靶（all=全收）、--dismiss 必带理由（销案留痕是最小闭环的账）。

复验的渐进语义：提案兑现 = 新锚真仿到账（真值本身不需再验）；其质量由下一轮
检验——已到账锚升格为粗格（含外来已到账锚的现场反推），本轮新锚作探针，相邻粗锚
中点有新片才对照出误差。于是"拆层→真仿→再检验"逐轮推进，不会无限套娃。

幂等重放：事件账 out/queue/events.jsonl（append-only，状态=各 key 末条）+
累计加密计划 out/queue/densify_plans.jsonl（去重键 deck_id）+ 现场覆盖扫描（数据盘
反推已到账锚）。同一提案重复 drain 不重复开火；开火半途崩的第二轮欠账优先补火、
不再拆层。

预算护栏（指令⑥）：每轮发射 deck 数上限 --max-slots（缺省 64，超出顺延下轮）、
锚距下限 --min-gap（缺省 0.05V，跨度不足两倍下限不再劈）、每提案每轮新锚
--max-anchors（缺省 4，从最大跨先劈）。发射分包/水位/watchdog 细节全在 fire 原语
（≤2000 枪/包）。

台账纪律：out/queue/* 与 out/drain/* 住 gitignore 区（允许派生统计量）；git 面
只增 qc 指纹台账行（qc_ledger 零浮点 schema 原样复用）。纯标准库，PySide6 只在
gui.py，不进本模块。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from . import engine, interp_check, qc, qc_ledger

GAP_TOL = 1e-9
CLOSED = ("resolved", "dismissed")


# ---------- 键与文件 ----------

def key_str(pair: str, fet: str, L: str, a, b) -> str:
    return f"{pair}|{fet}|{L}|{a:g}|{b:g}"


def load_rows(path) -> list[dict]:
    """宽容 JSONL 装载：坏行/非 dict 跳过（账本面自愈，不炸消费者）。"""
    p = Path(path)
    if not p.is_file():
        return []
    rows = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
            if isinstance(r, dict):
                rows.append(r)
        except json.JSONDecodeError:
            continue
    return rows


def append_rows(path, rows: list[dict]) -> int:
    if not rows:
        return 0
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(r, ensure_ascii=False) for r in rows]
    with p.open("a", encoding="utf-8") as f:
        f.write("".join(l + "\n" for l in lines))
    return len(lines)


def load_proposals(queue_path) -> dict[str, dict]:
    """队列 append-only：同 key 取最新行；只认 ev=densify。"""
    out: dict[str, dict] = {}
    for r in load_rows(queue_path):
        if r.get("ev") != "densify":
            continue
        try:
            k = key_str(r["pair"], r["fet"], r["L"], r["span"][0], r["span"][1])
        except (KeyError, TypeError, IndexError):
            continue
        prev = out.get(k)
        if prev is None or r.get("ts", 0) >= prev.get("ts", 0):
            out[k] = r
    return out


def load_last_events(events_path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for r in load_rows(events_path):
        if r.get("ev") == "status" and r.get("key"):
            out[r["key"]] = r
    return out


def open_keys(proposals: dict, events: dict) -> dict:
    """在案提案 = 无关账事件，或队列行比销案事件更新（重新入队）。"""
    res = {}
    for k, row in proposals.items():
        ev = events.get(k)
        if (ev is not None and ev.get("status") in CLOSED
                and ev.get("ts", 0) >= row.get("ts", 0)):
            continue
        res[k] = row
    return res


# ---------- 覆盖与拆锚（纯函数，单测直打） ----------

def _deck_present(pair_dir: Path, deck_id: str) -> bool:
    d = pair_dir / deck_id
    return d.is_dir() and any(f.is_file() and f.stat().st_size > 0 for f in d.iterdir())


def arrived_vds(pair_dir: Path, pair: str, fet: str, L: str) -> set[float]:
    """从数据目录现场反推已到账锚：<pair>.<fet>.L<L>.V<值>；解析失败忽略。"""
    out: set[float] = set()
    if not pair_dir.is_dir():
        return out
    pref = f"{pair}.{fet}.L{L}.V"
    for d in pair_dir.iterdir():
        if not d.is_dir() or not d.name.startswith(pref):
            continue
        try:
            out.add(round(float(d.name[len(pref):]), 9))
        except ValueError:
            continue
    return out


def next_anchors(xs, a, b, min_gap, max_anchors, present) -> list[float]:
    """跨度 [a,b] 内已知锚 xs → 本层加密锚（每轮只劈一层：取现有相邻跨的中点）。
    最大跨先排、至多 max_anchors 个；present(v)=数据盘已有该片 ⇒ 跳过不再劈它。
    跨度 <2*min_gap 不进候选（护栏：真超差由下一轮新提案点名，不靠无限加密）。
    """
    lo, hi = sorted((float(a), float(b)))
    xs0 = sorted({round(x, 9) for x in xs
                  if lo - GAP_TOL <= x <= hi + GAP_TOL} | {round(lo, 9), round(hi, 9)})
    cands = []
    for x, y in zip(xs0, xs0[1:]):
        w = y - x
        if w >= 2 * min_gap - 1e-12:
            cands.append((w, round((x + y) / 2.0, 9)))
    cands.sort(key=lambda t: (-t[0], t[1]))
    chosen = []
    for _w, m in cands:
        if len(chosen) >= max_anchors:
            break
        if present(m):
            continue
        chosen.append(m)
    return sorted(chosen)


def _family_views(coarse_plans: list[dict]) -> dict[tuple, dict]:
    fam: dict[tuple, dict] = {}
    for p in coarse_plans:
        k = (p["pair"], p["fet"], p["L"])
        f = fam.setdefault(k, {"Ws": list(p.get("Ws") or []),
                               "coarse": set(), "tmpl": p})
        f["coarse"].add(round(float(p["vds"]), 9))
    return fam


def _in_span(v: float, span) -> bool:
    lo, hi = sorted((float(span[0]), float(span[1])))
    return lo - GAP_TOL <= v <= hi + GAP_TOL


def _subspan(s2, s1) -> bool:
    """s2 两端都落在 s1 范围内（等宽也算——同跨复提=仍超差的信号）。"""
    try:
        return _in_span(s2[0], s1) and _in_span(s2[1], s1)
    except (TypeError, IndexError):
        return False


def _anchor_rows(fam: dict, data_root: Path, dense_all: list[dict],
                 coarse_plans: list[dict]) -> list[dict]:
    """已到账但不在任何计划里的锚（外来片，如实弹的中点 2.0）→ 合成"粗格升格"行。
    只补身份字段（deck_id/cells 改写 vds），装载仍走盘上 csv；deck 文本不留（复验不发射）。
    """
    planned: dict[tuple, set] = {}
    for p in coarse_plans:
        planned.setdefault((p["pair"], p["fet"], p["L"]), set()).add(
            round(float(p["vds"]), 9))
    for p in dense_all:
        try:
            planned.setdefault((p["pair"], p["fet"], p["L"]), set()).add(
                round(float(p["vds"]), 9))
        except (KeyError, TypeError, ValueError):
            continue
    rows = []
    for k, f in fam.items():
        pair, fet, L = k
        pdir = Path(data_root) / pair
        extra = arrived_vds(pdir, pair, fet, L) - planned.get(k, set())
        for v in sorted(extra):
            did = engine.sanitize_id(f"{pair}.{fet}.L{L}.V{engine.fmtv(v)}")
            if not _deck_present(pdir, did):
                continue
            r = dict(f["tmpl"])
            r["vds"] = v
            r["Ws"] = list(f["Ws"])
            r["deck_id"] = did
            r["cells"] = [dict(c, vds=v) for c in f["tmpl"].get("cells", [])]
            rows.append(r)
    return rows


# ---------- 主流程 ----------

def run_drain(prof, *, coarse_plans, data_root, queue_path, events_path,
              dense_plans_path, vstep, charge, adopt=None, dismiss=None,
              reason="", fire_flag=False, fire_fn=None, fire_kw=None,
              gm_tol=None, ids_tol=None, no_qc=False, no_convert=False,
              qc_ledger_path=None, max_slots=64, min_gap=0.05, max_anchors=4,
              report_dir=None, tag="drain", out=None) -> dict:
    """一圈闭环：列账→（销案）→采纳拆锚→（签字后发射）→qc→复验→销案判定。

    fire_fn(prof, plans, pair_dir, fire_kw, emit) -> {rc, arrived, rejected}；
    缺省走 collector.fire 真发射（yes=True，签字已在 drain 层完成）。
    """
    emit = out or (lambda s: print(s, flush=True))
    fam = _family_views(coarse_plans)
    dense_all = [r for r in load_rows(dense_plans_path) if r.get("deck_id")]
    events = load_last_events(events_path)
    proposals = load_proposals(queue_path)
    rep: dict = {"tag": tag, "dry_run": not fire_flag, "planned_decks": [],
                 "dismissed": [], "orphan": [], "capped": False,
                 "statuses": {}, "fire": None, "fire_rc": 0, "qc": None,
                 "interp": None, "new_queue_rows": 0}

    for k in dismiss or []:
        if not reason:
            raise SystemExit("[err] 驳回必须带理由——销案留痕是最小闭环的账")
        ev = {"ev": "status", "ts": int(time.time()), "key": k,
              "status": "dismissed", "reason": reason}
        append_rows(events_path, [ev])
        events[k] = ev
        rep["dismissed"].append(k)
        emit(f"[drain] 驳回入账：{k}（{reason}）")

    openk = open_keys(proposals, events)
    if adopt == "all":
        sel = sorted(openk)
    elif adopt:
        sel = []
        for k in adopt:
            if k in openk:
                sel.append(k)
            elif k in proposals:
                emit(f"[drain] 采纳对象已销账，跳过：{k}")
            else:
                emit(f"[drain] 采纳对象不在队列：{k}")
    else:
        sel = []
    if not proposals:
        emit("[drain] 队列空，无事办公")

    batch: list[dict] = []
    planned_keys: dict[str, list[str]] = {}
    slots = int(max_slots)
    for k in sel:
        row = openk[k]
        pair, fet, L = row["pair"], row["fet"], row["L"]
        pdir = Path(data_root) / pair
        f = fam.get((pair, fet, L))
        if f is None or not f["Ws"]:
            rep["orphan"].append(k)
            emit(f"[drain] 家族无粗格计划（孤案，需人查）：{k}")
            planned_keys[k] = []
            continue
        dense_vds = {round(float(p["vds"]), 9) for p in dense_all
                     if (p["pair"], p["fet"], p["L"]) == (pair, fet, L)}
        known = (set(f["coarse"]) | dense_vds
                 | arrived_vds(pdir, pair, fet, L))

        def present(v, _p=pdir, _pr=pair, _f=fet, _L=L):
            return _deck_present(_p, engine.sanitize_id(
                f"{_pr}.{_f}.L{_L}.V{engine.fmtv(v)}"))

        new = next_anchors(known, row["span"][0], row["span"][1],
                           min_gap, max_anchors, present)
        missing = [v for v in sorted(dense_vds)
                   if _in_span(v, row["span"]) and not present(v)]
        if missing:
            to_fire = sorted(missing)       # 欠账优先补火，本轮不再拆层
            full = to_fire
        else:
            to_fire = sorted(set(new))
            full = to_fire
        if slots < len(to_fire):
            emit(f"[drain] {k}: 待放 {len(to_fire)} 枪超本轮预算，截 {max(0, slots)}")
            to_fire = to_fire[:max(0, slots)]
            rep["capped"] = True
        slots -= len(to_fire)
        planned_keys[k] = []
        if not to_fire:
            if full:
                emit(f"[drain] {k}: 预算封顶本轮零发射，顺延下轮")
            else:
                emit(f"[drain] {k}: 跨度锚已覆盖且无欠火片，本轮不发射")
            continue
        plans = engine.make_plan(prof, pair, fet, [L], f["Ws"], to_fire,
                                 vstep=vstep, include_charge=charge)
        batch.extend(plans)
        planned_keys[k] = [p["deck_id"] for p in plans]
        emit(f"[drain] {k}: 发射锚 {[engine.fmtv(v) for v in to_fire]}"
             f"{'（补火欠账）' if missing else ''} → {len(plans)} 枪")
    rep["planned_decks"] = [p["deck_id"] for p in batch]

    if fire_flag:
        if batch:
            if len({p["pair"] for p in batch}) > 1:
                raise SystemExit("[err] v1 一轮发射限单器件对（一表一 pair 纪律）")
            known_ids = {p["deck_id"] for p in dense_all}
            add = [{kk: v for kk, v in p.items() if kk != "text"}
                   for p in batch if p["deck_id"] not in known_ids]
            append_rows(dense_plans_path, add)
            dense_all.extend(add)
            t_ev = int(time.time())
            for k in sel:
                decks = planned_keys.get(k) or []
                if not decks:
                    continue
                ev = {"ev": "status", "ts": t_ev, "key": k,
                      "status": "fired", "decks": decks}
                append_rows(events_path, [ev])
                events[k] = ev
            if fire_fn is not None:
                fn = fire_fn
            else:
                def fn(prf, pls, ddir, kw, em):  # 缺省=真发射（签字已在上面完成）
                    from . import fire as fire_mod
                    return fire_mod.fire(prf, pls, ddir, out=em, yes=True,
                                         **(kw or {}))
            fres = fn(prof, batch, Path(data_root) / batch[0]["pair"],
                      dict(fire_kw or {}), emit)
            rep["fire_rc"] = int(fres.get("rc", 0) or 0)
            rep["fire"] = {"arrived": sorted(fres.get("arrived") or []),
                           "rejected": [list(x) if isinstance(x, (list, tuple))
                                        else x for x in fres.get("rejected") or []]}
            if not add:
                emit("[drain] 本批 deck 全部已在累计计划账上（幂等：只补缺，不重记）")
        if batch and not no_qc:
            try:
                qrep = qc.run_qc(prof, batch, Path(data_root) / batch[0]["pair"],
                                 convert=not no_convert)
                rep["qc"] = {"verdict": qrep["verdict"],
                             "loaded": qrep["completeness"]["loaded"],
                             "planned": qrep["completeness"]["planned"]}
                emit(f"[drain] qc 新片：verdict={qrep['verdict']} "
                     f"装载 {rep['qc']['loaded']}/{rep['qc']['planned']}")
                if qc_ledger_path:
                    n = qc_ledger.append(qc_ledger_path, qc_ledger.build_rows(
                        prof, batch, qrep, f"{tag}_qc",
                        plan_hash=engine.plan_hash(batch)))
                    emit(f"[drain] qc 台账 +{n} 行（指纹面，零物理数值）")
            except Exception as e:  # qc 阻塞不了闭环，响亮呈报
                rep["qc"] = {"error": f"{type(e).__name__}: {e}"[:200]}
                emit(f"[drain][呈报] qc 步异常（不阻断闭环）：{type(e).__name__}: {e}")
        irep = None
        if dense_all:
            anchor_extra = _anchor_rows(fam, Path(data_root), dense_all,
                                        coarse_plans)
            irep = interp_check.run_interp_check(
                prof, list(coarse_plans) + anchor_extra, dense_all,
                Path(data_root) / coarse_plans[0]["pair"],
                gm_tol=gm_tol, ids_tol=ids_tol, partial=True,
                convert=not no_convert)
            nq = interp_check.append_queue(queue_path, irep["queue_rows"])
            rep["new_queue_rows"] = nq
            rep["interp"] = {"verdict": irep["verdict"],
                             "pairs_checked": irep["counts"]["pairs_checked"],
                             "hard": len(irep["hard_hits"]),
                             "anchor_upgraded": len(anchor_extra)}
            emit(f"[drain] 复验 interp：verdict={irep['verdict']} "
                 f"对照 {rep['interp']['pairs_checked']} 中点"
                 f"（升格锚 {len(anchor_extra)}）新提案 +{nq}")
            if report_dir:
                rd = Path(report_dir)
                rd.mkdir(parents=True, exist_ok=True)
                (rd / f"{tag}.interp.json").write_text(
                    json.dumps(irep, ensure_ascii=False, indent=1), encoding="utf-8")
        for k in sel:
            row = openk[k]
            pdir = Path(data_root) / row["pair"]
            decks = planned_keys.get(k) or []
            if decks and all(_deck_present(pdir, d) for d in decks):
                requeued = bool(irep) and any(
                    r.get("pair") == row["pair"] and r.get("fet") == row["fet"]
                    and r.get("L") == row["L"] and _subspan(r.get("span"), row["span"])
                    for r in irep["queue_rows"])
                ev = {"ev": "status", "ts": int(time.time()), "key": k,
                      "status": "resolved", "requeued": requeued}
                append_rows(events_path, [ev])
                events[k] = ev
                emit(f"[drain] {k}: 销案（子跨再提案={'是' if requeued else '否'}）")
            elif decks:
                ev = {"ev": "status", "ts": int(time.time()), "key": k,
                      "status": "fire_incomplete"}
                append_rows(events_path, [ev])
                events[k] = ev
                emit(f"[drain] {k}: 片未齐不销案（下轮自动补火欠账）")
    elif batch:
        emit(f"[drain] dry-run：{len(batch)} 枪待发射，人签后加 --yes（缺省不落任何账）")

    for k, row in proposals.items():
        ev = events.get(k)
        st = ev["status"] if ev else ("open" if k in openk else "closed")
        r_note = (ev or {}).get("reason") or ""
        if not r_note and (ev or {}).get("requeued"):
            r_note = "子跨已再提案"
        origin = row.get("origin") or "interp"      # P4.4：旧账缺省=interp
        n_hits = row.get("n_hits")
        rep["statuses"][k] = {"status": st, "gate": row.get("gate"),
                              "span": row.get("span"), "origin": origin,
                              "instances": row.get("instances"),
                              "note": r_note}
        emit(f"[drain] · {k} [{origin}/{row.get('gate')}] span={row.get('span')} "
             f"instances={row.get('instances')}"
             f"{(f' ×{n_hits}' if n_hits else '')} ⇒ {st}"
             f"{('（' + r_note + '）') if r_note else ''}")

    if report_dir:
        rd = Path(report_dir)
        rd.mkdir(parents=True, exist_ok=True)
        (rd / f"{tag}.drain.json").write_text(
            json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
    return rep
