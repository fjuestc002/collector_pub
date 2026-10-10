# -*- coding: utf-8 -*-
"""M4-4a 质检闸：G7 五闸收集器移植版 + 完整性闸（审出生，不审查询）。

原型 = demo_gs13g2 table_gen/probe_invariants.py（10-08 全量标定件）。
数据面差异与适配决定（诚实账）：

  * 列形制：demo=每片一文件、J 为每宽幅值；收集器=一片一 deck、实例列
    `M{k}:ids` 绝对安培（p 管带负号，10-09 实测定案）。本件按 plan 标称 W
    归一 ids/gm/gds/gmb ⇒ demo 阈值原样继承（J_SIG=1e-6 A/m 显著段口径）。
  * I1 方向子闸锚变：demo 幅值口径(⇒n 升 p 降)；收集器带符号口径下
    **两极性都应有 dJ/dvgs>0**（跨导恒正），翻转检测据此改锚。
  * I3 gmb≈dJ/dvbs：v1 网格 b 绑 s（单 vbs 锚）⇒ 本件记 n/a，
    画像/计划将来给 vbs 锚即自动生效（代码路径与 I2 同构）。
  * I4（qc.ft_source 分轨）：
      none（缺省）：电荷交叉闸 v2（10-10 实弹改判）= 双层判据：
        flat 层（fd 与 c 均近常数）⇒ 裸恒等 relerr 硬闸（常数串列照抓）；
        shell 层（实弹 PDK 单元）⇒ 逐实例去常数 b=median(c−fd) 后判残差，
        qg_cgg 残差硬闸；qd_cgd 的 b 被 vgs 耗尽调制非恒 ⇒ 主判改趋势相关
        rho(fd,c)≥i4_corr 硬闸、残差降软呈报（恒等式不在调制面上玩数字）。
        串列两层必炸。b=寄生栅帽估计，只进 out/ 报告不进台账。
        电荷列缺 ⇒ 该片跳过呈报。
      derived：经典 2π·ft·(cgs+cgd+cgb)≡gm，阈 1e-6（写盘/解析完整性，demo 口径）。
      measured：同式，软阈 0.05 呈报（独立读出=电容族真交叉，效力分轨立场）。
    申报 derived/measured 而列不齐 ⇒ config 级 FAIL 响亮拒，不静默降级。
  * I5 宽无关：同 (pair,fet,L,vds,vgs 索引) 跨 W 实例取 gmoverid、av0=gm/gds
    （+若有 ft）对中位相对漂移；承 G7/I5 口径：>2% 漂移带只呈报（窄宽真物理），
    >50% 混片级硬闸。

完整性闸：网格全到齐（缺片=FAIL，--partial 降呈报）、dc.dc→dc.csv 转换兜底
（tools/iris2csv.py 子进程，rc 0/2 均认产物——降序扫段 WARN 不影响出表）、
行数族内一致、NaN 率、指纹去重呈报、哨兵 gm/|ids|≡gmoverid（实测量级 5e-12，
硬阈 1e-6）。

判定与退出码（承 demo）：0=PASS 或 WARN(soft)，1=硬闸红。
产物：out/qc/<tag>.qc.json（utf-8）；stdout 由调用方负责 ASCII 纪律。
纯标准库，零三方依赖。
"""
from __future__ import annotations

import csv
import math
import re
import statistics
import subprocess
import sys
from pathlib import Path

J_SIG_DEFAULT = 1e-6      # A/m 显著段（demo QC 同源）
_LENSUFFIX = {"": 1.0, "f": 1e-15, "p": 1e-12, "n": 1e-9, "u": 1e-6,
              "µ": 1e-6, "m": 1e-3, "k": 1e3, "meg": 1e6}
_LEN_RE = re.compile(r"^([0-9]*\.?[0-9]+(?:e-?[0-9]+)?)(meg|[a-zµ]?)$", re.I)
_INST_RE = re.compile(r"^M(\d+):(\w+)$")


def parse_len(s: str) -> float:
    """SPICE 长度字面量 → 米。'1u'→1e-6，'10n'→1e-8，裸数=米。"""
    m = _LEN_RE.match(str(s).strip())
    if not m:
        raise ValueError(f"无法解析长度字面量: {s!r}")
    return float(m.group(1)) * _LENSUFFIX[m.group(2).lower()]


QC_DEFAULTS = {
    "ft_source": "none",          # none|derived|measured（I4 效力分轨）
    "j_sig_per_w": J_SIG_DEFAULT,
    "i1_hard": 0.05,              # I1 幅值 median 硬阈（demo 定标继承）
    "i2_soft": 0.30,              # I2 弦差软阈
    "i4_hard_none": 0.05,         # 电荷交叉残差 median 硬阈（flat 裸恒等/shell 去常数）
    "i4_corr": 0.95,              # shell 层趋势 Spearman 硬阈（串列/腐坏必塌）
    "i4_flat_relstd": 0.02,       # 系列相对散布低于此 ⇒ flat 层（无寄生可藏）
    "i4_hard_derived": 1e-6,
    "i4_soft_measured": 0.05,
    "i5_report": 0.02, "i5_hard": 0.50,
    "sentinel_hard": 1e-6,
    "nan_max": 0.5,               # 单列 NaN 占比上限（超=残片）
    "min_rows": 5,                # 短于此判残片
}


def qc_thresholds(prof: dict) -> dict:
    t = dict(QC_DEFAULTS)
    t.update(prof.get("qc", {}) or {})
    if t["ft_source"] not in ("none", "derived", "measured"):
        raise ValueError(f"qc.ft_source 须为 none|derived|measured，现为 {t['ft_source']!r}")
    return t


# ---------- CSV 装载与转换 ----------

def ensure_csv(deck_dir: Path, tools_dir: Path) -> tuple[Path | None, int]:
    """缺 dc.csv 且有 dc.dc ⇒ 调 tools/iris2csv.py 补转（rc 0/2 均认产物）。"""
    dc_csv = deck_dir / "dc.csv"
    if dc_csv.is_file():
        return dc_csv, 0
    dc = deck_dir / "dc.dc"
    if not dc.is_file():
        return None, -1
    try:
        rc = subprocess.run([sys.executable, str(tools_dir / "iris2csv.py"),
                             str(dc), str(dc_csv)],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, timeout=120).returncode
    except Exception:
        return None, -2
    return (dc_csv if rc in (0, 2) and dc_csv.is_file() else None), rc


def load_slice(csv_path: Path) -> dict[str, list[float]]:
    """CSV → {col: [float]}；空串/解析失败=NaN。"""
    with csv_path.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return {}
    cols: dict[str, list[float]] = {}
    for k in rows[0]:
        if k is None:
            continue
        vals = []
        for r in rows:
            v = (r.get(k) or "").strip()
            try:
                vals.append(float(v))
            except ValueError:
                vals.append(float("nan"))
        cols[k] = vals
    return cols


def vgs_of(cols: dict) -> tuple[list[float], str]:
    """扫段轴：优先自动识别列 'sweep'，退栅节点电压 'g'（b=s=0 ⇒ V_g=Vgs）。"""
    for c in ("sweep", "g"):
        if c in cols and len(cols[c]) >= 2:
            return cols[c], c
    return [], ""


def instances(cols: dict) -> dict[str, dict[str, list[float]]]:
    """`M1:ids` 列名拆开 → {iname: {signal: 列}}。"""
    out: dict[str, dict[str, list[float]]] = {}
    for k, v in cols.items():
        m = _INST_RE.match(k)
        if m:
            out.setdefault(f"M{m.group(1)}", {})[m.group(2)] = v
    return out


def per_w(insts: dict, cells: list[dict]) -> dict:
    """按标称 W 归一 ids/gm/gds/gmb（q/C/ft 族保持绝对——交叉闸两侧同量纲）。"""
    wmap = {c.get("iname", f"M{i + 1}"): parse_len(c["W"])
            for i, c in enumerate(cells)}
    out = {}
    for iname, sig in insts.items():
        w = wmap.get(iname, 1e-6)
        s2 = dict(sig)
        s2["_W_m"] = w
        for k in ("ids", "gm", "gds", "gmb"):
            if k in s2:
                s2[k] = [x / w for x in s2[k]]
        out[iname] = s2
    return out


# ---------- 统计小工具 ----------

def relerr(a: float, b: float) -> float:
    return abs(a - b) / max(abs(a), abs(b), 1e-300)


def stat(vals: list[float]) -> dict:
    if not vals:
        return {"n": 0}
    x = sorted(vals)
    i90 = max(0, math.ceil(0.9 * len(x)) - 1)
    return {"n": len(x), "median": statistics.median(x), "p90": x[i90], "max": x[-1]}


# ---------- 片内逐点闸 ----------

def i1_points(v: list[float], insts: dict, t: dict) -> tuple[list[float], int, int]:
    """gm ≡ dJ/dvgs：返回 (相对误差点集, 显著点数, 方向翻转点数)。"""
    rels, nsig, nflip = [], 0, 0
    for iname, sig in sorted(insts.items()):
        if "ids" not in sig or "gm" not in sig:
            continue
        n = min(len(v), len(sig["ids"]), len(sig["gm"]))
        for i in range(1, n - 1):
            dv = v[i + 1] - v[i - 1]
            if dv == 0:
                continue
            dj = (sig["ids"][i + 1] - sig["ids"][i - 1]) / dv
            if not math.isfinite(dj) or abs(dj) < t["j_sig_per_w"]:
                continue
            rels.append(relerr(abs(dj), abs(sig["gm"][i])))
            nsig += 1
            if dj < 0:                      # 收集器带符号口径：两极性均应 dj>0
                nflip += 1
    return rels, nsig, nflip


def sentinel_points(insts: dict, t: dict) -> list[float]:
    """gm/|ids| ≡ gmoverid（每实例独立，防列名串位；p 管符号定案 |ids|）。"""
    rels = []
    for iname, sig in sorted(insts.items()):
        if not all(k in sig for k in ("ids", "gm", "gmoverid")):
            continue
        for i, ids in enumerate(sig["ids"]):
            mo = sig["gmoverid"][i]
            if not (abs(ids) > 1e-21 and math.isfinite(mo)):
                continue
            rels.append(relerr(abs(sig["gm"][i] / ids), mo))
    return rels


def _relstd(x: list[float]) -> float:
    if len(x) < 4:
        return math.inf
    m = sum(x) / len(x)
    if m == 0:
        return math.inf
    return statistics.pstdev(x) / abs(m)


def _spearman(xs: list[float], ys: list[float]):
    """Spearman 秩相关；点数<10 或任一侧零方差 ⇒ None（不判）。"""
    if len(xs) < 10:
        return None
    def rank(z):
        r = []
        for xv in z:
            lt = sum(1 for y in z if y < xv)
            eq = sum(1 for y in z if y == xv)
            r.append(lt + (eq + 1) / 2.0)
        return r
    rx, ry = rank(xs), rank(ys)
    n = len(rx)
    mx, my = sum(rx) / n, sum(ry) / n
    sxx = sum((a - mx) ** 2 for a in rx)
    syy = sum((b - my) ** 2 for b in ry)
    if sxx == 0 or syy == 0:
        return None
    sxy = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    return sxy / math.sqrt(sxx * syy)


def i4_charge_check(v: list[float], insts: dict, t: dict) -> dict:
    """电荷交叉闸 v2（10-10 实弹改判，替换 v1"严格恒等"）。

    实弹判词：q 列=本征端子电荷（qg+qd+qs+qb≡0 且不随扫漂），c 列=器件单元
    边界电容——qg_cgg 差一个 ∝W 固定几何寄生帽（散布 0.002%），qd_cgd 还叠
    vgs 耗尽调制（非常数）。v1 把寄生当数据误差，八片全误判。双层判据：
      * flat 层：fd 与 c 两系列相对散布均 < i4_flat_relstd ⇒ 寄生常数无处藏
        ⇒ 裸 relerr(fd, c) 硬闸（裸模型/合成网走此层，与 v1 全等向后兼容）；
      * shell 层（真实 PDK 壳）：b=median(c−fd) 去常数后 relerr；
        qg_cgg 残差硬闸（干净数据 0.02% 量级）；qd_cgd 的 b 本身被 vgs 调制
        ⇒ 主判换 Spearman rho(fd,c) ≥ i4_corr（串列/腐坏必塌），
        去常数残差降为软呈报——不在调制面上硬判恒等式。
    副产物：qg_cgg 的 b = 该片寄生栅帽估计（随 W 线性可校验），入 out/ 报告，
    台账零物理数值纪律不破。
    """
    res = {"qg_cgg": [], "qd_cgd": [], "rho_qg": [], "rho_qd": [],
           "mode": {"qg_cgg": [], "qd_cgd": []}, "paras": []}
    for qcol, ccol, key in (("qg", "cgg", "qg_cgg"), ("qd", "cgd", "qd_cgd")):
        rk = "rho_qg" if key == "qg_cgg" else "rho_qd"
        for iname, sig in sorted(insts.items()):
            if qcol not in sig or ccol not in sig:
                continue
            n = min(len(v), len(sig[qcol]), len(sig[ccol]))
            fd, cc = [], []
            for i in range(1, n - 1):
                dv = v[i + 1] - v[i - 1]
                if dv == 0:
                    continue
                # 幅值口径（承 v1）：pFET 电荷沿负向 vgs 求导与电容读出反号，
                # 两侧取 |·| 后 flat/shell/rho 全在幅值面上——p 格回归测试自咬实证。
                d = abs((sig[qcol][i + 1] - sig[qcol][i - 1]) / dv)
                c = abs(sig[ccol][i])
                if not (math.isfinite(d) and math.isfinite(c)):
                    continue
                if c < 1e-20 and d < 1e-20:              # 两端皆零=无信息
                    continue
                fd.append(d)
                cc.append(c)
            if not fd:
                continue
            if _relstd(fd) < t["i4_flat_relstd"] and _relstd(cc) < t["i4_flat_relstd"]:
                res["mode"][key].append("flat")
                res[key] += [relerr(d, c) for d, c in zip(fd, cc)]
                continue
            res["mode"][key].append("shell")
            b = statistics.median([c - d for d, c in zip(fd, cc)])
            res[key] += [relerr(d + b, c) for d, c in zip(fd, cc)]
            if key == "qg_cgg":
                res["paras"].append({"inst": iname, "b": b})
            rho = _spearman(fd, cc)
            if rho is not None:
                res[rk].append(rho)
    return res


def i4_ft_points(insts: dict) -> list[float]:
    """经典往返 2π·ft·(cgs+cgd+cgb) ≡ gm（电容族绝对量 ⇒ gm 用绝对口径还原）。"""
    rels = []
    for iname, sig in sorted(insts.items()):
        need = ("ft", "cgs", "cgd", "cgb", "gm")
        if not all(c in sig for c in need):
            continue
        w = sig.get("_W_m", 1e-6)
        for i in range(min(len(sig[c]) for c in need)):
            csum = sig["cgs"][i] + sig["cgd"][i] + sig["cgb"][i]
            gm_abs = sig["gm"][i] * w
            if csum > 0 and sig["ft"][i] > 0 and gm_abs > 1e-15:
                rels.append(relerr(2 * math.pi * sig["ft"][i] * csum, gm_abs))
    return rels


def i5_drift_points(insts: dict, t: dict) -> dict[str, list[float]]:
    """跨 W 实例漂移：gmoverid、av0=gm/gds、(若有)ft —— 同 vgs 索引对跨实例中位。"""
    series_map = {
        "gmoverid": lambda s: s.get("gmoverid"),
        "av0": lambda s: [g / d if d > 0 else float("nan")
                          for g, d in zip(s.get("gm", []), s.get("gds", []))],
        "ft": lambda s: s.get("ft"),
    }
    out = {}
    for col, getter in series_map.items():
        per_inst = {iname: getter(sig) for iname, sig in insts.items()}
        per_inst = {k: x for k, x in per_inst.items() if x is not None}
        if len(per_inst) < 2:
            continue
        drift = []
        n = min((len(x) for x in per_inst.values()), default=0)
        for i in range(n):
            vals = []
            for iname, x in per_inst.items():
                val = x[i]
                ids = insts[iname].get("ids", [])
                # ids 已经 per_w 归一（除以 W），显著段直比 j_sig_per_w；
                # 旧式 abs(ids)/_W_m 系双重除法=过滤虚设（10-09 4c 测试复现
                # 件暴露，与 interp_check 同源同修）。
                if (i >= len(ids) or not math.isfinite(val) or val <= 0
                        or abs(ids[i]) < t["j_sig_per_w"]):
                    continue
                vals.append(val)
            if len(vals) >= 2:
                m = statistics.median(vals)
                if m > 0:
                    drift.extend(abs(x - m) / m for x in vals)
        if drift:
            out[col] = drift
    return out


# ---------- 跨片闸 ----------

def i2_family_points(byvds: dict[float, dict], t: dict) -> list[float]:
    """gds ≈ ΔJ/Δvds 相邻锚弦差（byvds: vds → {iname: per-W 列}）。"""
    rels = []
    anchors = sorted(byvds)
    for a, b in zip(anchors, anchors[1:]):
        d = b - a
        if d == 0:
            continue
        for iname in set(byvds[a]) & set(byvds[b]):
            s1, s2 = byvds[a][iname], byvds[b][iname]
            if not all(k in s1 and k in s2 for k in ("ids", "gds")):
                continue
            n = min(len(s1["ids"]), len(s2["ids"]))
            for i in range(n):
                j1, j2 = s1["ids"][i], s2["ids"][i]
                if not (abs(j1) >= t["j_sig_per_w"] and abs(j2) >= t["j_sig_per_w"]):
                    continue
                avg = 0.5 * (s1["gds"][i] + s2["gds"][i])
                if abs(avg) < 1e-300:
                    continue
                rels.append(relerr(abs((j2 - j1) / d), abs(avg)))
    return rels


# ---------- 主流程 ----------

def run_qc(prof: dict, plans: list[dict], data_dir: Path | str,
           partial: bool = False, convert: bool = True) -> dict:
    """全网格质检。plans=make_plan/cmd_plan 产物（含 cells/deck_sha256）。
    convert=False 时只认已转好的 dc.csv（离线测试用）。"""
    t = qc_thresholds(prof)
    tools = Path(__file__).resolve().parents[1] / "tools"
    data_dir = Path(data_dir)
    hard: list[str] = []
    soft: list[str] = []

    # --- 完整性 ---
    slices: dict[str, dict] = {}
    missing, no_csv, corrupt, short = [], [], [], []
    deck_status: dict[str, str] = {}     # 未入检片点名册（台账数据源；inspected 由片级循环登记）
    for p in plans:
        d = data_dir / p["deck_id"]
        if convert:
            csvp, _rc = ensure_csv(d, tools)
        else:
            f0 = d / "dc.csv"
            csvp = f0 if f0.is_file() else None
        if csvp is None:
            has_raw = (d / "dc.dc").is_file()
            (no_csv if has_raw else missing).append(p["deck_id"])
            deck_status[p["deck_id"]] = "no_csv" if has_raw else "missing"
            continue
        cols = load_slice(csvp)
        v, vcol = vgs_of(cols)
        if not v:
            no_csv.append(p["deck_id"] + "(无扫段列 sweep/g)")
            deck_status[p["deck_id"]] = "no_sweep"
            continue
        inst_cols = [c for c in cols if _INST_RE.match(c)]
        nan_bad = [c for c in inst_cols
                   if sum(1 for x in cols[c] if not math.isfinite(x)) / len(v)
                   > t["nan_max"]]
        if nan_bad:
            corrupt.append({"deck": p["deck_id"], "cols": nan_bad[:8]})
            deck_status[p["deck_id"]] = "nan_reject"
            continue
        if len(v) < t["min_rows"]:
            short.append({"deck": p["deck_id"], "rows": len(v)})
            deck_status[p["deck_id"]] = "short"
            continue
        slices[p["deck_id"]] = {"plan": p, "cols": cols, "v": v, "vcol": vcol}
    if missing or no_csv:
        msg = (f"网格缺片 {len(missing) + len(no_csv)}"
               f"（missing={missing[:8]} no_csv={no_csv[:8]}）")
        (hard if not partial else soft).append(msg + ("[partial 降呈报]" if partial else ""))
    if corrupt:
        hard.append(f"NaN 残片 {len(corrupt)}: {corrupt[:5]}")
    if short:
        soft.append(f"短扫段片 {len(short)}: {short[:5]}")
    fam_rows: dict[tuple, dict[int, int]] = {}
    for s in slices.values():
        key = (s["plan"]["pair"], s["plan"]["fet"])
        fam_rows.setdefault(key, {})
        fam_rows[key][len(s["v"])] = fam_rows[key].get(len(s["v"]), 0) + 1
    for key, byn in fam_rows.items():
        if len(byn) > 1:
            hard.append(f"行数不齐 {key}: {byn}")
    report: dict = {"verdict": "", "profile": prof.get("name", ""),
                    "ft_source": t["ft_source"],
                    "thresholds": {k: t[k] for k in QC_DEFAULTS},
                    "completeness": {"planned": len(plans), "loaded": len(slices),
                                     "missing": missing, "no_csv": no_csv,
                                     "nan_reject": corrupt, "short": short,
                                     "vgs_col_usage": sorted({s["vcol"] for s in slices.values()})}}

    # --- 指纹去重（呈报级）---
    by_hash: dict[str, list[str]] = {}
    for p in plans:
        by_hash.setdefault(p["deck_sha256"], []).append(p["deck_id"])
    collisions = {h: ids for h, ids in by_hash.items() if len(ids) > 1}
    if collisions:
        soft.append(f"内容指纹撞车(同 deck_sha256 异 deck_id) {len(collisions)} 组：{collisions}")
    report["dedup"] = {"unique": len(by_hash),
                       "collisions": {h[:12]: ids for h, ids in list(collisions.items())[:5]}}

    # --- 片内闸逐点收集（判级=片级：审出生以片为对象，单坏片不稀释、不连坐）---
    i1_all, i2_all = [], []
    i1_flip = []            # 整片级翻转计数（显著点占比>50% 判 hard）
    sent_all = []
    i4c_all = {"qg_cgg": [], "qd_cgd": []}
    i4_rho = {"qg_cgg": [], "qd_cgd": []}
    i4_paras: list[dict] = []
    i4c_skipped = 0
    i4f_all = []
    i4_config = []
    i5_all: dict[str, list[float]] = {}
    fam: dict[tuple, dict[float, dict]] = {}
    deck_flags: dict[str, dict] = {}
    for did, s in slices.items():
        p, v = s["plan"], s["v"]
        df = deck_flags.setdefault(did, {"status": "inspected", "hard": [],
                                         "soft": [], "rows": len(v)})
        insts = per_w(instances(s["cols"]), p.get("cells", []))
        r1, nsig, nflip = i1_points(v, insts, t)
        i1_all += r1
        s1_sl = stat(r1)
        if s1_sl.get("n") and s1_sl["median"] > t["i1_hard"]:
            hard.append(f"I1 片级 median {s1_sl['median']:.3g} > {t['i1_hard']} @ {did}")
            df["hard"].append("I1")
        if nflip:
            ratio = nflip / max(1, nsig)
            rec = {"deck": did, "flipped": nflip, "sig": nsig, "frac": round(ratio, 3)}
            i1_flip.append(rec)
            if ratio >= 0.5:
                hard.append(f"I1 整片方向翻转 {did}: {nflip}/{nsig}")
                df["hard"].append("I1dir")
        s_pt = sentinel_points(insts, t)
        sent_all += s_pt
        s_sl = stat(s_pt)
        if s_sl.get("n") and s_sl["max"] > t["sentinel_hard"]:   # 判级上收片级：坏行列点名片
            hard.append(f"哨兵 gm/|ids|≡gmoverid 片级 max {s_sl['max']:.3g} "
                        f"> {t['sentinel_hard']} @ {did}")
            df["hard"].append("SENT")
        if t["ft_source"] == "none":
            if any("qg" in x and "cgg" in x for x in insts.values()):
                chk = i4_charge_check(v, insts, t)
                for k in ("qg_cgg", "qd_cgd"):
                    i4c_all[k] += chk[k]
                    i4_rho[k] += chk["rho_qg" if k == "qg_cgg" else "rho_qd"]
                i4_paras += [{"deck": did, "inst": q["inst"], "b": q["b"]}
                             for q in chk["paras"]]
                sk = stat(chk["qg_cgg"])
                if sk.get("n") and sk["median"] > t["i4_hard_none"]:
                    hard.append(f"I4 电荷交叉 qg_cgg 片级 median {sk['median']:.3g} "
                                f"> {t['i4_hard_none']} @ {did}")
                    df["hard"].append("I4q")
                for k in ("qg_cgg", "qd_cgd"):
                    for rho in chk["rho_qg" if k == "qg_cgg" else "rho_qd"]:
                        if rho < t["i4_corr"]:
                            hard.append(f"I4 电荷交叉 {k} 趋势相关性塌 rho {rho:.3f} "
                                        f"< {t['i4_corr']} @ {did}")
                            df["hard"].append("I4qr")
                sq = stat(chk["qd_cgd"])
                if sq.get("n") and sq["median"] > t["i4_hard_none"]:
                    soft.append(f"I4 qd_cgd 去常数残差片级 median {sq['median']:.3g} "
                                f"@ {did}（壳层耗尽调制，只呈报不判）")
                    df["soft"].append("I4qdS")
            else:
                i4c_skipped += 1
        else:
            need = ("ft", "cgs", "cgd", "cgb")
            if not all(any(c in x for x in insts.values()) for c in need):
                i4_config.append(f"{did}: 缺 {'/'.join(c for c in need if not any(c in x for x in insts.values()))}")
                df["hard"].append("I4cfg")
            else:
                pts = i4_ft_points(insts)
                i4f_all += pts
                sf = stat(pts)
                if t["ft_source"] == "derived" and sf.get("n") and sf["median"] > t["i4_hard_derived"]:
                    hard.append(f"I4 ft 往返片级 median {sf['median']:.3g} "
                                f"> {t['i4_hard_derived']:.0e} @ {did}")
                    df["hard"].append("I4ft")
                elif t["ft_source"] == "measured" and sf.get("n") and sf["median"] > t["i4_soft_measured"]:
                    soft.append(f"I4 ft 交叉(measured) 片级 median {sf['median']:.3g} @ {did}")
                    df["soft"].append("I4ftS")
        for col, pts in i5_drift_points(insts, t).items():
            i5_all.setdefault(col, []).extend(pts)
            sk = stat(pts)
            if sk.get("n") and sk["median"] > t["i5_hard"]:
                hard.append(f"I5 混片级 {col} 片级 median {sk['median']:.3g} "
                            f"> {t['i5_hard']} @ {did}")
                df["hard"].append("I5")
            elif sk.get("n") and sk["median"] > t["i5_report"]:
                soft.append(f"I5 漂移带 {col} 片级 median {sk['median']:.3g}"
                            f"（窄宽真物理口径） @ {did}")
                df["soft"].append("I5drift")
        key = (p["pair"], p["fet"], p["L"])
        fam.setdefault(key, {})[float(p["vds"])] = {"deck": did, "insts": insts}
    # I2 跨锚弦：判级=相邻锚对（一片坏只点名该片所在的对，不稀释全局）
    for key, byvds in fam.items():
        anchors = sorted(byvds)
        for a, b in zip(anchors, anchors[1:]):
            pts = i2_family_points({a: byvds[a]["insts"], b: byvds[b]["insts"]}, t)
            i2_all += pts
            sp = stat(pts)
            if sp.get("n") and sp["median"] > t["i2_soft"]:
                hard.append(f"I2 邻弦 {key} vds {a:g}→{b:g} median "
                            f"{sp['median']:.3g} > {t['i2_soft']}")
                for dref in (byvds[a]["deck"], byvds[b]["deck"]):
                    deck_flags[dref]["hard"].append("I2")

    # --- 全局统计存档呈报（判定已在片/锚对级完成）---
    report["I1_gm_vs_dJ_dvgs"] = stat(i1_all)
    report["I1_direction_flips"] = i1_flip[:20]
    if slices and not report["I1_gm_vs_dJ_dvgs"].get("n"):
        hard.append("I1 零样本（ids/gm 列缺或无显著段）")

    report["I2_gds_vs_dJ_dvds"] = stat(i2_all)
    multi_anchor = sum(1 for byvds in fam.values() if len(byvds) >= 2)
    if multi_anchor == 0:
        report["I2_skipped"] = "每 (pair,fet,L) 仅单 vds 锚，无邻弦可验（呈报）"
        soft.append("I2 n/a：单 vds 锚网格")

    report["I3_gmb"] = {"skipped": "v1 网格单 vbs 锚（b 绑 s），闸与 I2 同构候 vbs 轴"}

    if t["ft_source"] == "none":
        merged = {k: stat(v) for k, v in i4c_all.items()}
        merged["rho_qg"] = stat(i4_rho["qg_cgg"])
        merged["rho_qd"] = stat(i4_rho["qd_cgd"])
        merged["skipped_slices"] = i4c_skipped
        merged["thr_hard"] = t["i4_hard_none"]
        merged["thr_corr"] = t["i4_corr"]
        merged["caliber"] = ("flat=裸恒等硬闸 / shell: qg=去常数残差硬闸, "
                             "qd=rho≥thr_corr 硬闸+残差软呈报")
        merged["parasitic_gg"] = i4_paras   # 副产物: 寄生栅帽估计（out/ 区专用）
        report["I4_charge_cross"] = merged
        if i4c_skipped == len(slices) and slices:
            soft.append("I4 电荷交叉全跳：deck 生成未带 --charge（charge 列缺）")
    else:
        s_ = stat(i4f_all)
        s_["mode"] = t["ft_source"]
        report[f"I4_ft_{t['ft_source']}"] = s_
        if i4_config:
            report["I4_config_mismatch"] = i4_config[:10]
            hard.append(f"I4 ft_source={t['ft_source']} 申报与列面不符: {i4_config[:3]}")
        elif s_.get("n", 0) == 0:
            soft.append("I4 ft 恒等零样本")

    i5 = {col: stat(pts) for col, pts in i5_all.items()}
    report["I5_width_independence"] = i5
    report["I5_note"] = ("漂移带>2%只呈报(窄宽真物理, 承 G7/I5)；>50%=混片级硬闸；"
                         "判定按片级 median（单坏片不稀释）")

    ss = stat(sent_all)
    report["sentinel_gmoverid"] = {"stat": ss, "hard_thr": t["sentinel_hard"],
                                   "note": "判级在片级(片级 max)，此处仅全局存档"}
    if slices and ss.get("n", 0) == 0:
        hard.append("哨兵零样本（gm/ids/gmoverid 列不齐）")

    # --- 片旗标合册：未入检片也点名（missing/no_csv/no_sweep/nan_reject/short）---
    for did, st in deck_status.items():
        deck_flags.setdefault(did, {"status": st, "hard": [], "soft": []})
    report["deck_flags"] = deck_flags

    report["hard_hits"] = hard
    report["soft_notes"] = soft
    report["verdict"] = "FAIL" if hard else ("WARN" if soft else "PASS")
    return report
