# -*- coding: utf-8 -*-
"""收集器 CLI（GUI 前的驱动面；M2 挂 plan/run，GUI 并入后退居调试入口）。

用法示例:
  py -3.14 cli.py deck --profile profiles/EXAMPLE_generic_mos_spectre.json --pair pair_01 \
      --fet n --L 1u --W 1u --vds 2.5 --vstep 0.01 --out out/decks --tag smoke
  # 多锚/多 W = 多实例单分析形制 (M1-v2):
  py -3.14 cli.py deck ... --vds 2.5,1.5,0.5 --tag multi        # 同 L,W 三锚一枪
  py -3.14 cli.py deck ... --cells "1u,1u,2.5;1u,2u,2.5;1u,1u,1.5"  # L,W,vds 分号表
  # M2 批量执行（先 --plan-only 看账，再 --yes 开火）:
  py -3.14 cli.py run --profile profiles_local/vendor_a.json --pair pair_01 --fet n \
      --Ls 1u,2u --Ws 1u,2u --vds 2.5 --vstep 0.1 --plan-only
  py -3.14 cli.py run ... --yes
  # M4 质检闸（对已到账网格跑五闸+完整性，只读零派发；台账行落 ledgers/，零物理数值可进 git）:
  py -3.14 cli.py qc --profile profiles/EXAMPLE_generic_mos_spectre.json \
      --plan out/plan/m2_smoke.plan.jsonl --data data
  # M4-4c 插值误差闸（加密真仿 vs 粗格插值对照，只读零派发）:
  py -3.14 cli.py interp-plan --profile profiles/EXAMPLE_generic_mos_spectre.json       --pair pair_01 --fet n --Ls 1u,2u --Ws 1u,2u --vds 1.5,2.5 --vstep 0.1
  py -3.14 cli.py run ... --vds 2.0 --yes        # 加密中点批开火（run 原语）
  py -3.14 cli.py interp-check --profile ...       --coarse-plan out/plan/m2_smoke.plan.jsonl --probe-plan out/plan/densify.plan.jsonl
  # M5 闭环调度（缺省 dry-run 列账；采纳+确认才开火；幂等可重放）:
  py -3.14 cli.py drain --profile profiles_local/vendor_a.json --coarse-plan out/plan/m2c_coarse.plan.jsonl --data data_charge --adopt all --vstep 0.1 --charge --yes --disk-check 20000
  # P2 打包链（数据表→v1 双 npz+manifest→_pdk 表目录；方言闸=静态+优化器真装载回环）:
  py -3.14 cli.py pack --profile profiles_local/vendor_a.json --pair pair_01 \
      --plans out/plan/m2c_coarse.plan.jsonl --data data_charge --fet both --force
  py -3.14 cli.py pack ... --dry-run            # 只出判式不落盘
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from collector import engine, fire as fire_mod, interp_check, loop, npz_pack, qc, qc_ledger  # noqa: E402
from collector import backflow as backflow_mod, scorecard as sc_mod  # noqa: E402
from collector.deck_gen import render_batch_deck, render_curve_deck  # noqa: E402
from collector.profile import get_pair, load_profile  # noqa: E402


def pr(s: str) -> None:
    try:
        print(s, flush=True)
    except UnicodeEncodeError:
        print(s.encode("gbk", "replace").decode("gbk"), flush=True)


def _parse_cells(spec: str) -> list[dict]:
    cells = []
    for tok in spec.split(";"):
        tok = tok.strip()
        if not tok:
            continue
        parts = tok.replace(",", " ").split()
        if len(parts) != 3:
            raise SystemExit(f"[err] --cells 项须为 L,W,vds 三段: {tok!r}")
        cells.append({"L": parts[0], "W": parts[1], "vds": float(parts[2])})
    if not cells:
        raise SystemExit("[err] --cells 解析为空")
    return cells


def cmd_deck(a: argparse.Namespace) -> int:
    prof = load_profile(a.profile)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    scs = out / f"{a.tag}.scs"
    form = "single"
    if a.cells:
        cells = _parse_cells(a.cells)
        res = render_batch_deck(prof, a.pair, a.fet, cells, vstep=a.vstep,
                                include_charge=a.charge)
        form = "batch"
        n_units = len(res["cells"])
    else:
        anchors = [float(x) for x in a.vds.split(",") if x.strip()] if a.vds else \
            [prof["pairs"][a.pair]["vmax"] * prof["pairs"][a.pair][a.fet]["polarity"]]
        if len(anchors) > 1:
            cells = [{"L": a.L, "W": a.W, "vds": v} for v in anchors]
            res = render_batch_deck(prof, a.pair, a.fet, cells, vstep=a.vstep,
                                    include_charge=a.charge)
            form = "batch"
            n_units = len(res["cells"])
        else:
            res = render_curve_deck(prof, a.pair, a.fet, a.L, a.W, anchors,
                                    vstep=a.vstep, include_charge=a.charge)
            n_units = 1
    scs.write_text(res["text"], encoding="utf-8")
    meta = {"tag": a.tag, "form": form, "profile": prof["_path"],
            "profile_sha256": prof["_hash"], "deck_sha256": res["deck_sha256"],
            "pair": a.pair, "fet": a.fet, "vstep": a.vstep,
            "charge_cols": a.charge,
            "created": datetime.now().isoformat(timespec="seconds")}
    if form == "batch":
        meta["cells"] = res["cells"]
        meta["sweep_dev"] = res["sweep_dev"]
    else:
        meta["L"] = a.L
        meta["W"] = a.W
        meta["vds_anchors"] = anchors
        meta["analyses"] = res["analyses"]
    (out / f"{a.tag}.meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[ok] {scs}  ({len(res['text'].splitlines())} 行, {form}, {n_units} 实例)"
          .encode("gbk", "replace").decode("gbk"))
    return 0


def _grid_args(a):
    Ls = [x.strip() for x in a.Ls.split(",") if x.strip()]
    Ws = [x.strip() for x in a.Ws.split(",") if x.strip()]
    vds = [float(x) for x in a.vds.split(",") if x.strip()]
    if not (Ls and Ws and vds):
        raise SystemExit("[err] --Ls/--Ws/--vds 均不可空")
    sign = get_pair(load_profile(a.profile), a.pair)[a.fet]["polarity"]
    for v in vds:
        if v * sign < 0:
            raise SystemExit(f"[err] vds={v} 与 {a.fet} 管极性不符（须同号）——响亮拒")
    return Ls, Ws, vds


def cmd_plan(a: argparse.Namespace) -> int:
    prof = load_profile(a.profile)
    Ls, Ws, vds = _grid_args(a)
    plans = engine.make_plan(prof, a.pair, a.fet, Ls, Ws, vds,
                             vstep=a.vstep, include_charge=a.charge)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    with (out / f"{a.tag}.plan.jsonl").open("w", encoding="utf-8") as f:
        for p in plans:
            f.write(json.dumps({k: v for k, v in p.items() if k != "text"},
                               ensure_ascii=False) + "\n")
    pr(f"[plan] {len(plans)} 张 deck（片=器件×L×vds，内含 {len(Ws)} W × 曲线），"
       f"分 {len(engine.chunk(plans, a.batch_size))} 包，plan_hash="
       f"{engine.plan_hash(plans)[:12]}")
    return 0


def cmd_run(a: argparse.Namespace) -> int:
    """批量执行（M2）——本体抽至 collector.fire，与 drain/GUI 同径复用，预算分包语义全同。"""
    prof = load_profile(a.profile)
    Ls, Ws, vds = _grid_args(a)
    plans = engine.make_plan(prof, a.pair, a.fet, Ls, Ws, vds,
                             vstep=a.vstep, include_charge=a.charge)
    res = fire_mod.fire(prof, plans, Path(a.data) / a.pair, host=a.host,
                        wd=a.wd, jobs=a.jobs, thread_reserve=a.thread_reserve,
                        rss_floor=a.rss_floor, batch_size=a.batch_size,
                        watchdog=a.watchdog, disk_check=a.disk_check,
                        disk_floor=a.disk_floor, stale_h=a.stale_h,
                        keep_remote=a.keep_remote, yes=a.yes,
                        plan_only=a.plan_only, out=pr)
    return res["rc"]


def cmd_interp_plan(a: argparse.Namespace) -> int:
    """粗锚表 → 相邻中点锚加密批计划；发射复用 run 原语（同 vstep 纪律由 check 端机器判）。"""
    prof = load_profile(a.profile)
    Ls, Ws, vds = _grid_args(a)
    pairs = interp_check.midpoint_pairs(vds)
    if not pairs:
        raise SystemExit("[err] 单锚不产生中点；粗格需 ≥2 锚")
    mids = [m for _a, _b, m in pairs]
    plans = engine.make_plan(prof, a.pair, a.fet, Ls, Ws, mids,
                             vstep=a.vstep, include_charge=a.charge)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    fp = out / f"{a.tag}.plan.jsonl"
    with fp.open("w", encoding="utf-8") as f:
        for p in plans:
            f.write(json.dumps({k: v for k, v in p.items() if k != "text"},
                               ensure_ascii=False) + "\n")
    pr(f"[interp-plan] 粗锚 {sorted(vds)} → 中点锚 {mids}；"
       f"{len(Ls)} L × {len(mids)} 中点 = {len(plans)} 枪")
    runv = ",".join(f"{m:g}" for m in sorted(mids))
    charge_s = " --charge" if a.charge else ""
    pr(f"[interp-plan] 开火: py -3.14 cli.py run --profile {a.profile} "
       f"--pair {a.pair} --fet {a.fet} --Ls {a.Ls} --Ws {a.Ws} "
       f"--vds {runv} --vstep {a.vstep}{charge_s} --yes")
    pr(f"[interp-plan] 对照: py -3.14 cli.py interp-check --profile {a.profile} "
       f"--coarse-plan <粗格plan> --probe-plan {fp}")
    return 0


def cmd_interp_check(a: argparse.Namespace) -> int:
    prof = load_profile(a.profile)
    for pth in (a.coarse_plan, a.probe_plan):
        if not Path(pth).is_file():
            raise SystemExit(f"[err] 计划文件不存在: {pth}（先跑 plan / interp-plan）")
    coarse = [json.loads(l) for l in
              Path(a.coarse_plan).read_text(encoding="utf-8").splitlines() if l.strip()]
    probe = [json.loads(l) for l in
             Path(a.probe_plan).read_text(encoding="utf-8").splitlines() if l.strip()]
    if not coarse or not probe:
        raise SystemExit("[err] 粗格/加密计划均不可为空")
    pairs = {pl["pair"] for pl in coarse} | {pl["pair"] for pl in probe}
    if len(pairs) != 1:
        raise SystemExit(f"[err] v1 一表一器件对，计划含多个 pair: {sorted(pairs)}")
    rep = interp_check.run_interp_check(prof, coarse, probe,
                                        Path(a.data) / coarse[0]["pair"],
                                        gm_tol=a.gm_tol, ids_tol=a.ids_tol,
                                        partial=a.partial, convert=not a.no_convert)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    rfp = out / f"{a.tag}.interp.json"
    rfp.write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
    c = rep["counts"]
    pr(f"[interp] 对照锚对 {c['pairs_checked']} 中点未到 {c['probe_missing']} "
       f"vgs错位 {c['vgs_misalign']}（粗格装载 {c['coarse_loaded']}/{c['coarse_planned']}"
       f" 加密 {c['probe_loaded']}/{c['probe_planned']}）")
    for e in rep["pair_stats"]:
        g, i = e["gm_all"], e["ids_all"]
        fired = ",".join(sorted(e["fired"])) or "-"
        pr(f"[interp] {'/'.join(e['key'])} vds {e['span'][0]:g}→{e['span'][1]:g}: "
           f"gm median={g.get('median', float('nan')):.3g} "
           f"ids median={i.get('median', float('nan')):.3g} n={g.get('n', 0)} 触发=[{fired}]")
    for k, lst in rep["knee_by_family"].items():
        if lst:
            top = lst[0]
            pr(f"[interp] 膝区 {k}: 最弯锚段 {top['span'][0]:g}→{top['span'][1]:g}"
               f"（gm median={top['gm_median']}）")
    nq = interp_check.append_queue(a.queue, rep["queue_rows"])
    for h in rep["hard_hits"]:
        pr(f"[interp][硬] {h}")
    for s_ in rep["soft_notes"]:
        pr(f"[interp][呈报] {s_}")
    pr(f"[interp] verdict={rep['verdict']}  gm_tol={rep['gm_tol']} "
       f"ids_tol={rep['ids_tol']}  报告: {rfp}")
    if nq:
        pr(f"[interp] 加密待采队列 +{nq} 行 → {a.queue}（派生统计住 out/ 区，不进 git）")
    return 1 if rep["verdict"] == "FAIL" else 0


def cmd_qc(a: argparse.Namespace) -> int:
    prof = load_profile(a.profile)
    plan_path = Path(a.plan)
    if not plan_path.is_file():
        raise SystemExit(f"[err] 计划文件不存在: {plan_path}（先跑 cli.py plan）")
    plans = [json.loads(l) for l in plan_path.read_text(encoding="utf-8").splitlines()
             if l.strip()]
    if not plans:
        raise SystemExit("[err] 计划为空")
    pairs = {p["pair"] for p in plans}
    if len(pairs) != 1:
        raise SystemExit(f"[err] v1 一表一器件对，计划含多个 pair: {sorted(pairs)}")
    data_dir = Path(a.data) / plans[0]["pair"]
    rep = qc.run_qc(prof, plans, data_dir, partial=a.partial,
                    convert=not a.no_convert)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    rfp = out / f"{a.tag}.qc.json"
    rfp.write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
    n_led = 0
    if not a.no_ledger:
        rows = qc_ledger.build_rows(prof, plans, rep, a.tag,
                                    plan_hash=engine.plan_hash(plans))
        n_led = qc_ledger.append(a.ledger, rows)
    c = rep["completeness"]
    pr(f"[qc] 完整性: 装载 {c['loaded']}/{c['planned']} 缺片 {len(c['missing'])} "
       f"未转 {len(c['no_csv'])} NaN残片 {len(c['nan_reject'])} 短片 {len(c['short'])}"
       f"（扫段列 {c['vgs_col_usage']}）")
    for key in ("I1_gm_vs_dJ_dvgs", "I2_gds_vs_dJ_dvds", "sentinel_gmoverid"):
        s_ = rep.get(key, {})
        s2_ = s_.get("stat", s_)
        pr(f"[qc] {key}: n={s2_.get('n', 0)} median={s2_.get('median', float('nan')):.3g} "
           f"p90={s2_.get('p90', float('nan')):.3g} max={s2_.get('max', float('nan')):.3g}")
    if "I4_charge_cross" in rep:
        m4 = rep["I4_charge_cross"]
        pr(f"[qc] I4 电荷交叉: qg/cgg n={m4['qg_cgg'].get('n', 0)} "
           f"median={m4['qg_cgg'].get('median', float('nan')):.3g} | "
           f"qd/cgd n={m4['qd_cgd'].get('n', 0)} "
           f"median={m4['qd_cgd'].get('median', float('nan')):.3g} "
           f"跳过片={m4['skipped_slices']}")
    for k in list(rep):
        if k.startswith("I4_ft_"):
            s_ = rep[k]
            pr(f"[qc] {k}: mode={s_.get('mode')} n={s_.get('n', 0)} "
               f"median={s_.get('median', float('nan')):.3g}")
    for col, s_ in rep.get("I5_width_independence", {}).items():
        pr(f"[qc] I5 {col}: n={s_.get('n', 0)} median={s_.get('median', float('nan')):.3g} "
           f"max={s_.get('max', float('nan')):.3g}")
    if rep.get("I3_gmb"):
        pr(f"[qc] I3: {rep['I3_gmb']['skipped']}")
    for h in rep["hard_hits"]:
        pr(f"[qc][硬] {h}")
    for s in rep["soft_notes"]:
        pr(f"[qc][呈报] {s}")
    pr(f"[qc] verdict={rep['verdict']}  报告: {rfp}")
    if n_led:
        pr(f"[qc] 台账 +{n_led} 行 → {a.ledger}（指纹/计数/状态零物理数值，可进 git）")
    return 1 if rep["verdict"] == "FAIL" else 0


def _read_plan(pth) -> list:
    pp = Path(pth)
    if not pp.is_file():
        raise SystemExit(f"[err] 计划文件不存在: {pth}（先跑 cli.py plan）")
    rows = [json.loads(l) for l in
            pp.read_text(encoding="utf-8").splitlines() if l.strip()]
    if not rows:
        raise SystemExit(f"[err] 计划为空: {pth}")
    return rows


def cmd_drain(a: argparse.Namespace) -> int:
    """M5 闭环调度：消费加密待采队列——列账→人签→拆锚发射→复验→销案（幂等重放）。"""
    prof = load_profile(a.profile)
    coarse = _read_plan(a.coarse_plan)
    pairs = {pl["pair"] for pl in coarse}
    if len(pairs) != 1:
        raise SystemExit(f"[err] v1 一表一器件对，粗格计划含多 pair: {sorted(pairs)}")
    if a.dismiss and not a.reason:
        raise SystemExit("[err] --dismiss 必须配 --reason（销案留痕是最小闭环的账）")
    adopt = None
    if a.adopt:
        adopt = "all" if a.adopt.strip() == "all" else [
            x.strip() for x in a.adopt.split(",") if x.strip()]
    dismiss = ([x.strip() for x in a.dismiss.split(",") if x.strip()]
               if a.dismiss else None)
    fire_kw = dict(host=a.host, wd=a.wd, jobs=a.jobs, batch_size=a.batch_size,
                   watchdog=a.watchdog, disk_check=a.disk_check)
    n_rounds = max(1, a.rounds)
    rep = None
    fire_rc = 0
    for r in range(1, n_rounds + 1):
        adopt_r = adopt if r == 1 else ("all" if (a.yes and adopt) else None)
        tag = a.tag if n_rounds == 1 else f"{a.tag}_r{r}"
        rep = loop.run_drain(prof, coarse_plans=coarse, data_root=a.data,
                             queue_path=a.queue, events_path=a.events,
                             dense_plans_path=a.plans, vstep=a.vstep,
                             charge=a.charge, adopt=adopt_r,
                             dismiss=dismiss if r == 1 else None,
                             reason=a.reason, fire_flag=a.yes, fire_kw=fire_kw,
                             gm_tol=a.gm_tol, ids_tol=a.ids_tol,
                             no_qc=a.no_qc, no_convert=a.no_convert,
                             qc_ledger_path=a.ledger or None,
                             max_slots=a.max_slots, min_gap=a.min_gap,
                             max_anchors=a.max_anchors,
                             report_dir=a.out, tag=tag, out=pr)
        fire_rc = rep["fire_rc"] or fire_rc
        if not (rep["planned_decks"] or rep["new_queue_rows"]):
            break
    open_ct = sum(1 for st in rep["statuses"].values()
                  if st["status"] in ("open", "fired", "fire_incomplete"))
    if fire_rc not in (0, 1):
        pr(f"[drain] 发射 rc={fire_rc}（水位/拒档语义见 fire 账），未销案 {open_ct} 件")
        return fire_rc
    pr(f"[drain] 未销案提案 {open_ct} 件；事件账: {a.events}")
    return 1 if open_ct else 0


def cmd_pack(a: argparse.Namespace) -> int:
    """P2 打包链：采集数据→v1 双 npz+manifest→_pdk 表目录，方言验证闸内置。"""
    plans = [x.strip() for x in a.plans.split(",") if x.strip()]
    if not plans:
        raise SystemExit("[err] --plans 为空（逗号表：可多份 plan jsonl 合账）")
    try:
        rep = npz_pack.pack_profile(a.profile, a.pair, plans,
                                    data_root=a.data, fet=a.fet,
                                    pdk_root=a.pdk_root or None, force=a.force,
                                    dry_run=a.dry_run, live_verify=not a.no_live,
                                    qc_report=a.qc_report,
                                    interp_report=a.interp_report,
                                    force_unlock=a.force_unlock,
                                    keep_archive=a.keep_archive, out=pr)
    except npz_pack.tablelock.TableLocked as exc:
        for ln in str(exc).splitlines():
            pr(f"[err][pack][表锁] {ln}")
        return 3
    except npz_pack.PackError as exc:
        for ln in str(exc).splitlines():
            pr(f"[err][pack] {ln}")
        return 2
    return 0 if rep["verify"]["ok"] else 2


def cmd_ingest(a: argparse.Namespace) -> int:
    """P4.4 回流并轨：优化器 run 尾产的 backflow.jsonl → 单队列。"""
    prof = load_profile(a.profile)
    rep = backflow_mod.ingest_backflow(prof, a.file, a.queue,
                                       gap_path=a.gap_out or None, out=pr)
    return 0


def cmd_scorecard(a: argparse.Namespace) -> int:
    """P4.5 达标判据单：读现成台账/报告出四栏+三盏灯（零新物理计算，不设 PM 栏）。"""
    mf = Path(a.manifest)
    if mf.is_dir():
        mf = mf / "manifest.json"
    if not mf.is_file():
        raise SystemExit(f"[err] manifest 不存在: {a.manifest}（给表目录或其下 "
                         "manifest.json；缺先跑 pack）")
    rep = sc_mod.build_scorecard(
        manifest_path=mf, interp_path=a.interp or None,
        qc_ledger_path=a.ledger or None, verify_path=a.verify or None,
        backflow_path=a.backflow or None, queue_path=a.queue or None,
        events_path=a.events or None, gap_path=a.gap_out or None,
        rho_min=a.rho_min)
    md = sc_mod.render_md(rep)
    outd = Path(a.out)
    outd.mkdir(parents=True, exist_ok=True)
    (outd / (a.tag + ".scorecard.md")).write_text(md, encoding="utf-8")
    (outd / (a.tag + ".scorecard.json")).write_text(
        json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
    pr(md)
    pr(f"[scorecard] 三灯 {sum(1 for v in rep['lamps'].values() if v is True)}"
       f"/3 绿；freeze_ready={rep['freeze_ready']} → {outd / (a.tag + '.scorecard.md')}")
    return 0 if rep["freeze_ready"] else 1


def main() -> int:
    ap = argparse.ArgumentParser(prog="collector")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("deck", help="生成一条曲线的 spectre deck")
    d.add_argument("--profile", required=True)
    d.add_argument("--pair", required=True)
    d.add_argument("--fet", choices=["n", "p"], required=True)
    d.add_argument("--L", default="1u", help="单锚形制用;--cells 时以 cells 表为准")
    d.add_argument("--W", default="1u", help="单锚形制用;--cells 时以 cells 表为准")
    d.add_argument("--cells", default="", help='多实例表 "L,W,vds;L,W,vds"')
    d.add_argument("--vds", default="", help="Vds 锚点逗号表(缺省=极性顶格单点;>1 个走多实例形制)")
    d.add_argument("--vstep", type=float, default=0.01)
    d.add_argument("--charge", action="store_true", help="附带电荷/电容八列")
    d.add_argument("--out", default="out/decks")
    d.add_argument("--tag", default="deck")
    d.set_defaults(fn=cmd_deck)

    def add_grid(p):
        p.add_argument("--profile", required=True)
        p.add_argument("--pair", required=True)
        p.add_argument("--fet", choices=["n", "p"], required=True)
        p.add_argument("--Ls", required=True, help="L 档逗号表，如 0.18u,1u,2u")
        p.add_argument("--Ws", required=True, help="W 档逗号表（每张 deck 内含全部 W）")
        p.add_argument("--vds", required=True, help="漏压锚点逗号表（须与器件极性同号）")
        p.add_argument("--vstep", type=float, default=0.01)
        p.add_argument("--charge", action="store_true")

    pl = sub.add_parser("plan", help="只排计划开账，不开火")
    add_grid(pl)
    pl.add_argument("--batch-size", type=int, default=2000)
    pl.add_argument("--out", default="out/plan")
    pl.add_argument("--tag", default="plan")
    pl.set_defaults(fn=cmd_plan)

    r = sub.add_parser("run", help="批量执行（探测→预算→确认→分包并发→清理→断点续采）")
    add_grid(r)
    r.add_argument("--host", default="", help="覆盖画像 connection.host")
    r.add_argument("--jobs", type=int, default=0, help="人工钉并发(让路模式=--jobs 1)")
    r.add_argument("--thread-reserve", type=int, default=4)
    r.add_argument("--rss-floor", type=int, default=200, help="单枪内存下限 MB(实测 91MB 取裕量)")
    r.add_argument("--batch-size", type=int, default=2000)
    r.add_argument("--wd", default="~/collector_run", help="远端工作目录")
    r.add_argument("--data", default="data", help="本地样本根目录")
    r.add_argument("--watchdog", type=int, default=300, help="单枪远端 timeout 秒")
    r.add_argument("--disk-check", type=int, default=0,
                   help="spectre +disk_check(官方=字节;本参数按 MB 换算,0=不加)")
    r.add_argument("--disk-floor", type=int, default=2048, help="引擎级盘水位中止线 MB")
    r.add_argument("--stale-h", type=float, default=24.0, help="dispatched 超期重派阈值 h")
    r.add_argument("--plan-only", action="store_true", help="只排计划+续采核对，不开火")
    r.add_argument("--yes", action="store_true", help="确认资源占用声明，允许开火")
    r.add_argument("--keep-remote", action="store_true", help="包间不清清远端(调试)")
    r.set_defaults(fn=cmd_run)

    ip = sub.add_parser("interp-plan",
                        help="M4-4c：粗锚表算中点，出加密批计划（发射复用 run）")
    add_grid(ip)
    ip.add_argument("--out", default="out/plan")
    ip.add_argument("--tag", default="densify")
    ip.set_defaults(fn=cmd_interp_plan)

    ic = sub.add_parser("interp-check",
                        help="M4-4c：加密真仿对照粗格插值——gm/ids 误差闸+膝区呈报+加密待采队列（只读零派发）")
    ic.add_argument("--profile", required=True)
    ic.add_argument("--coarse-plan", required=True, help="粗格 plan jsonl")
    ic.add_argument("--probe-plan", required=True, help="加密中点批 plan jsonl（interp-plan 产物）")
    ic.add_argument("--data", default="data")
    ic.add_argument("--out", default="out/interp")
    ic.add_argument("--tag", default="interp")
    ic.add_argument("--gm-tol", type=float, default=None,
                    help="gm 相对误差上限（缺省=画像 qc.interp.gm_tol 或 0.01；GUI 可调走画像覆写）")
    ic.add_argument("--ids-tol", type=float, default=None,
                    help="ids 相对误差上限（缺省 0.02）")
    ic.add_argument("--queue", default="out/queue/densify.jsonl",
                    help="加密待采队列 JSONL（out/ 区）")
    ic.add_argument("--partial", action="store_true", help="在途核验：装载缺降呈报")
    ic.add_argument("--no-convert", action="store_true")
    ic.set_defaults(fn=cmd_interp_check)

    q = sub.add_parser("qc", help="M4 质检闸：G7 五闸收集器版+完整性/哨兵/去重（只读 data，零派发）")
    q.add_argument("--profile", required=True)
    q.add_argument("--plan", required=True, help="cli.py plan 产物 jsonl（含 cells/deck_sha256）")
    q.add_argument("--data", default="data", help="本地样本根目录（其下 <pair>/<deck_id>/）")
    q.add_argument("--out", default="out/qc")
    q.add_argument("--tag", default="qc")
    q.add_argument("--partial", action="store_true",
                   help="在途核验：缺片降为呈报不判红")
    q.add_argument("--no-convert", action="store_true",
                   help="不调 iris2csv 补转 dc.dc→dc.csv（只认已转好的）")
    q.add_argument("--ledger", default="ledgers/qc.jsonl",
                   help="QC 指纹台账(append-only JSONL, 零物理数值, git 跟踪区)")
    q.add_argument("--no-ledger", action="store_true", help="不写台账（调试）")
    q.set_defaults(fn=cmd_qc)
    dr = sub.add_parser("drain",
                        help="M5 闭环：消费加密待采队列（列账/人签/拆锚发射/qc+复验/事件账销案，幂等重放）")
    dr.add_argument("--profile", required=True)
    dr.add_argument("--coarse-plan", required=True, help="粗格 plan jsonl（家族 W 档与锚基准）")
    dr.add_argument("--data", default="data", help="本地样本根（其下 <pair>/<deck_id>/）")
    dr.add_argument("--queue", default="out/queue/densify.jsonl")
    dr.add_argument("--events", default="out/queue/events.jsonl",
                    help="提案事件账（out/ 区：派生统计允许，不进 git）")
    dr.add_argument("--plans", default="out/queue/densify_plans.jsonl",
                    help="累计加密计划（幂等去重键 deck_id）")
    dr.add_argument("--vstep", type=float, default=0.1, help="须与粗格一致（vgs 对齐机器判）")
    dr.add_argument("--charge", action="store_true")
    dr.add_argument("--adopt", default="",
                    help="人签：队列键逗号表（key=pair|fet|L|a|b）或 all=全收；缺省=只列账")
    dr.add_argument("--dismiss", default="", help="驳回键逗号表（必须配 --reason）")
    dr.add_argument("--reason", default="")
    dr.add_argument("--yes", action="store_true", help="确认发射（配合 --adopt 方开火）")
    dr.add_argument("--max-slots", type=int, default=64, help="本轮发射枪数上限（超出顺延下轮）")
    dr.add_argument("--min-gap", type=float, default=0.05, help="加密锚距下限 V（防无限劈）")
    dr.add_argument("--max-anchors", type=int, default=4, help="每提案每轮新锚上限（大跨先劈）")
    dr.add_argument("--gm-tol", type=float, default=None)
    dr.add_argument("--ids-tol", type=float, default=None)
    dr.add_argument("--rounds", type=int, default=1,
                    help="连转圈数（第 2 轮起自动采纳在案提案=无人值守全签模式）")
    dr.add_argument("--no-qc", action="store_true", help="发射后跳过 qc 新片步")
    dr.add_argument("--no-convert", action="store_true")
    dr.add_argument("--ledger", default="ledgers/qc.jsonl",
                    help="qc 指纹台账（git 面，零物理数值）；空串=不写")
    dr.add_argument("--out", default="out/drain", help="报告目录")
    dr.add_argument("--tag", default="drain")
    dr.add_argument("--host", default="")
    dr.add_argument("--wd", default="~/collector_run")
    dr.add_argument("--jobs", type=int, default=0)
    dr.add_argument("--batch-size", type=int, default=2000)
    dr.add_argument("--watchdog", type=int, default=300)
    dr.add_argument("--disk-check", type=int, default=0, help="spectre +disk_check MB 换算")
    dr.set_defaults(fn=cmd_drain)

    pk = sub.add_parser("pack",
                        help="P2 闭环打包：dc.csv→v1 双 npz+manifest→_pdk 表目录（内置方言验证闸，"
                             "表名=器件名=PDK 模型名严格同名）")
    pk.add_argument("--profile", required=True)
    pk.add_argument("--pair", required=True)
    pk.add_argument("--fet", choices=["n", "p", "both"], default="both")
    pk.add_argument("--plans", required=True, help="plan jsonl 逗号表（多份合账=重放幂等）")
    pk.add_argument("--data", default="data", help="本地样本根（其下 <pair>/<deck_id>/）")
    pk.add_argument("--pdk-root", default="",
                    help="表目录根；缺省=MIDAS_PDK_ROOT 或 %%HOME%%\\optimization_tool\\projects\\_pdk"
                         "（与优化器同约定）")
    pk.add_argument("--force", action="store_true",
                    help="允许覆写既有同器件 npz/manifest 条目（版本刷新=整目录覆写纪律）")
    pk.add_argument("--dry-run", action="store_true", help="只出判式，不落盘不验证")
    pk.add_argument("--no-live", action="store_true",
                    help="跳过优化器真装载回环（发行机无优化器场景；静态方言层仍全跑）")
    pk.add_argument("--qc-report", default="", help="误差报告路径注记（写入 manifest，可选）")
    pk.add_argument("--interp-report", default="", help="插值报告路径注记（可选）")
    pk.add_argument("--force-unlock", action="store_true",
                    help="人工强解表锁：清除既有租约后抢写锁（动作呈报留痕，"
                         "P4.2；仅当确认持约进程已死时使用）")
    pk.add_argument("--keep-archive", type=int, default=1,
                    help="覆写留代代数（P4.1，archive/<指纹8>/ 只保最近 N 代；"
                         "0=不留代）")
    pk.set_defaults(fn=cmd_pack)

    ig = sub.add_parser("ingest",
                        help="P4.4 回流并轨：优化器 backflow.jsonl → 单队列（domain 可执行块进 densify，"
                             "其余进候块账 gap_blocks.jsonl；幂等去重）")
    ig.add_argument("--profile", required=True, help="画像（model→pair/fet 反查 + L 档字面量口径）")
    ig.add_argument("--file", required=True, help="优化器 <run>/backflow.jsonl")
    ig.add_argument("--queue", default="out/queue/densify.jsonl",
                    help="加密待采队列（事件账取同目录 events.jsonl 判在案）")
    ig.add_argument("--gap-out", default="out/queue/gap_blocks.jsonl",
                    help="候块账（L/W/vgs/J/vbs 轴 + 残差整点，n 倍代价候人定块）")
    ig.set_defaults(fn=cmd_ingest)

    sc = sub.add_parser("scorecard",
                        help="P4.5 达标判据单：四栏（域覆盖/interp误差/金对子Sp/五闸通过率）+三盏灯，"
                             "只读现成台账，零新物理计算，不设 PM 栏（全绿=可冻结 vN）")
    sc.add_argument("--manifest", required=True,
                    help="pack 产物表目录（其下 manifest.json）或 manifest 文件")
    sc.add_argument("--interp", default="", help="最近一次 interp-check 报告 json")
    sc.add_argument("--ledger", default="ledgers/qc.jsonl", help="qc 指纹台账")
    sc.add_argument("--verify", default="", help="优化器 verify_ledger.csv（金对子账）")
    sc.add_argument("--backflow", default="", help="优化器 backflow.jsonl（域负载命中计数）")
    sc.add_argument("--queue", default="out/queue/densify.jsonl",
                    help="加密待采队列（interp 在案提案计数）")
    sc.add_argument("--events", default="out/queue/events.jsonl")
    sc.add_argument("--gap-out", default="out/queue/gap_blocks.jsonl")
    sc.add_argument("--rho-min", type=float, default=0.9, help="金对子 Sp 过线门槛")
    sc.add_argument("--out", default="out/report")
    sc.add_argument("--tag", default="scorecard")
    sc.set_defaults(fn=cmd_scorecard)

    a = ap.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())
