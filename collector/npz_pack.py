# -*- coding: utf-8 -*-
"""npz_pack.py — 采集数据 → v1 双 npz + manifest（P2，10-10 采集↔优化闭环管道）。

一句话：把 data/<pair>/<deck_id>/dc.csv 多实例宽表，按严格同名链
（表名=器件名=PDK 模型名=网表模型名）打包成优化器泛化轨可直接吃的
`_pdk/<profile.name>/mem/<dev>_v1J.npz / _v1V.npz` + manifest.json。

**方言契约**（逐字段对齐优化器 `surrogates/gsieve/mem_v1._Pack` 的唯一装载面，
建包数学对齐 gsieve `engine/bank2npy_v1.build_dev`；差一个键/一根轴序=首次查表
即炸，且 `_Pack` 对缺键有 `lJ→vg`、`X→Y` 的**静默回退**——所以本件产完必过
verify_dialect，把"方言不通查表失效"钉死在建表期）：

    <dev>_v1J.npz  L,vds,vbs,W,lJ,X,ColNames   X=(W,L,vds,vbs,NJ,NCOLS) f32
    <dev>_v1V.npz  L,vds,vbs,W,vg,Y            Y=(W,L,vds,vbs,NVG)     f32
    轴义：L/W 米升序；vds/vbs 幅值(=|f·raw|)升序；lJ=linspace(log(1e-13),log(1e4),251)；
          vg=归一 g=f·vgs 升序；W 键必写；allow_pickle=False。
    列语义（与旧 bank 片头注记逐字对账，与生产表实数复核）：
          J,moi,av0,ft,vdsat,vth,vgs 宽无关；电容族 F/m、gmb S/m 每宽归一；
          cap_sign=abs；cgb = cgg−|cgs|−|cgd| 派生（BankJ 恒等式 cgg=Σ 自动闭合）；
          vgs 列留 raw（p 负）；ft = moi·J/(2π·cgg)（qc.i4_ft_points 往返式同构）。
    符号归一：J=f·ids/W>0；moi=f·gmoverid（主号为负⇒判该列已是 abs 约定，
          退 |·| 并记呈报）；av0/gmb/电容/vth/vdsat 取幅（旧表 p 行全正实据）。

退化轴纪律：L/vds/vbs 任一轴只有 1 个实测点 ⇒ 复制成双节点（引擎 _brk+einsum
要求全轴 ≥2；单节点 vbs=[0,0] 的表 = 只吃衬底接源电路，|vbs|>0 查询响亮出域——
诚实语义，不做假覆盖）。

红线：产物只写 MIDAS_PDK_ROOT（或缺省 projects\\_pdk\\）——版权数据永不进收集器
仓库/发行物；本模块纯数值，零凭证零 license 探测；npz 需 numpy（qc 核心纯标准库不变）。

用法（CLI 面见 cli.py pack）:
    pack_profile(prof_path, "pair_01", ["out/plan/m2c_coarse.plan.jsonl"],
                 data_root="data", fet="both", force=True)
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from .profile import (declared_domain, domain_of, get_pair, load_profile)
from .qc import ensure_csv, instances, load_slice, parse_len, per_w, vgs_of
from . import tablelock

#: v1J 列序——与 gsieve bank2npy_v1.COLS 逐字同序（生产表 ColNames 实测一致）
COLS = ["moi", "av0", "ft", "cgs", "cgb", "cgd", "cds", "vdsat", "vth", "gmb", "vgs"]
NJ, JLO, JHI = 251, 1e-13, 1e4
#: 引擎 BankJ.lookup 无条件访问的列（缺一个 = 首查 KeyError/崩，方言硬面）
ENGINE_MIN_COLS = {"moi", "av0", "gmb", "cgs", "cgb", "cgd", "cds"}
TOOL_TAG = "collector pack v0.1"
#: 片目录名：<pair>.<fet>.L<字面量>.V<%g 原样>（engine.make_plan+fmtv 同源）
DECK_NAME_RE = re.compile(
    r"^(?P<pair>.+)\.(?P<fet>[np])\.L(?P<L>[0-9][0-9a-zA-Zµ.]*)\.V(-?[0-9][0-9a-zA-Zµ.]*)$")


class PackError(Exception):
    """打包响亮拒：文案即用户提示，全量点名（缺列/轴不齐/键冲突/方言不过）。"""


def pdk_default_root() -> Path:
    """缺省根——必须与优化器 engine/pdk_tables.pdk_root() 逐字符同约定。"""
    v = (os.environ.get("MIDAS_PDK_ROOT") or "").strip()
    if v:
        return Path(v).expanduser()
    return Path.home() / "optimization_tool" / "projects" / "_pdk"


def _fp(dev: str) -> float:
    """极性首字母判——与 mem_v1._fp 同字复刻。"""
    return -1.0 if dev.lower().startswith("p") else 1.0


# ---------------------------------------------------------------- 计划与片装载

def read_plans(paths) -> dict:
    """plan jsonl 表 → {deck_id: row}（重复 deck_id 后到覆先到=重放幂等）。"""
    out = {}
    for pth in paths:
        pp = Path(pth)
        if not pp.is_file():
            raise PackError(f"计划文件不存在: {pth}（先跑 cli.py plan）")
        for ln in pp.read_text(encoding="utf-8").splitlines():
            if ln.strip():
                row = json.loads(ln)
                if not row.get("deck_id") or not row.get("cells"):
                    raise PackError(f"{pp}: 行缺 deck_id/cells")
                out[row["deck_id"]] = row
    if not out:
        raise PackError("计划为空（--plans 至少一份 plan jsonl）")
    return out


def _deck_name_check(deck_id: str, row: dict) -> None:
    """目录名 ↔ 计划行一致性闸（防混批/错档：pair/fet/L/|vds| 对不上就点名拒）。"""
    m = DECK_NAME_RE.match(deck_id)
    if not m:
        return                                   # 非标准命名不拦（plan 行是正源）
    if m.group("pair") != row["pair"] or m.group("fet") != row["fet"]:
        raise PackError(f"目录 {deck_id} 与计划 pair/fet 不符: "
                        f"{row['pair']}/{row['fet']}")
    v_tok = m.group(4)
    try:
        v_dir = float(v_tok)
    except ValueError:
        return                                   # 名字怪异不拦（plan 行是正源）
    try:
        L_dir = parse_len(m.group("L"))
    except ValueError:
        L_dir = None
    for c in row["cells"]:
        if L_dir is not None:
            Lc = parse_len(c["L"])
            if abs(Lc - L_dir) > 1e-15 * max(1.0, L_dir):
                raise PackError(f"目录 {deck_id} 名 L={L_dir:g} 与 cell "
                                f"{c.get('iname')} L={Lc:g} 不符")
        if abs(float(c["vds"]) - v_dir) > 1e-12:
            raise PackError(f"目录 {deck_id} 名 V={v_dir:g} 与 cell "
                            f"{c.get('iname')} vds={c['vds']} 不符")


def _curve_from_instance(deck_id: str, iname: str, sig: dict, f: float,
                         w: float) -> dict:
    """单实例宽表列（per_w 已把 ids/gm/gds/gmb 除 W）→ 归一曲线 dict。
    电容族 per_w 不归一（交叉闸两侧同量纲），此处补 /W → 表列=F/m、S/m 方言。"""
    need = ("ids", "gm", "gds", "gmb", "vth", "vdsat", "cgg", "cgs", "cgd", "cds")
    miss = [c for c in need if c not in sig]
    if "gmoverid" not in sig and not ("gm" in sig and "ids" in sig):
        miss.append("gmoverid(或 gm+ids 可推)")
    if miss:
        raise PackError(f"片 {deck_id}/{iname} 缺列 {miss}——确认采集是否 --charge、"
                        "冒号制 save 面是否含电容族")
    npts = min([len(sig["vgs"])] + [len(sig[c]) for c in need if c in sig]
               + [len(sig[c]) for c in ("gmoverid",) if c in sig]
               + [len(sig[c]) for c in ("gm", "ids") if "gmoverid" not in sig])
    if npts < 5:
        raise PackError(f"片 {deck_id}/{iname} 短列（{npts} 行），先过 qc 再 pack")
    def arr(k):
        return np.asarray(sig[k][:npts], dtype=np.float64)
    vgs_raw = arr("vgs")
    idsW = arr("ids")                            # A/m（p raw 负，10-09 定案）
    J = f * idsW                                 # 归一电流密度 A/m
    moi_raw = arr("gmoverid") if "gmoverid" in sig else arr("gm") / idsW
    with np.errstate(divide="ignore", invalid="ignore"):
        av0 = np.abs(arr("gm")) / np.abs(arr("gds"))
    cggW = np.abs(arr("cgg")) / w
    cols = {"moi": moi_raw, "av0": av0, "ft": np.full(npts, np.nan),
            "cgs": np.abs(arr("cgs")) / w,
            "cgb": cggW - np.abs(arr("cgs")) / w - np.abs(arr("cgd")) / w,
            "cgd": np.abs(arr("cgd")) / w, "cds": np.abs(arr("cds")) / w,
            "vdsat": np.abs(arr("vdsat")), "vth": np.abs(arr("vth")),
            "gmb": np.abs(arr("gmb")), "vgs": vgs_raw}
    return {"f": f, "J": J, "g": f * vgs_raw, "cols": cols, "npts": npts}


def _finalize_curve(cv: dict, deck_id: str, iname: str) -> dict:
    """moi 符号约定判定 + 逐点有效性掩码过滤（J>0 且全列有限且 moi>0）。"""
    f = cv["f"]
    c = dict(cv["cols"])
    c["moi"] = f * c["moi"]
    conv = "f·gmoverid"
    if np.nanmedian(c["moi"]) <= 0:              # 采集列已是 abs 约定 ⇒ 退 |·|
        c["moi"] = np.abs(c["moi"])
        conv = "|gmoverid|(检测: f· 主号为负)"
    with np.errstate(divide="ignore", invalid="ignore"):
        # cgs/cgd 存 abs、cgb 由 cgg−|cgs|−|cgd| 构造 ⇒ 三者之和精确=cgg（BankJ 恒等式），
        # 分母直接吃这个和（cgb 异常区可为负，abs 会破恒等式）。
        c["ft"] = c["moi"] * cv["J"] / (2.0 * math.pi
                                        * (c["cgs"] + c["cgd"] + c["cgb"]))
    mask = np.isfinite(cv["J"]) & (cv["J"] > 0.0)
    for k in COLS:
        mask &= np.isfinite(c[k])           # 零列值合法(moi=0 平带、cds 下溢)，只滤非有限
    mask &= c["moi"] >= 0.0                 # 负 moi=符号约定破, 必须滤
    if mask.sum() < 3:
        raise PackError(f"片 {deck_id}/{iname} 有效点不足 3（J>0 且全列有限/非零），"
                        f"丢弃 {int((~mask).sum())}/{cv['npts']}——检查该管是否没导通")
    return {"g": cv["g"][mask], "J": cv["J"][mask],
            "cols": {k: v[mask] for k, v in c.items()},
            "dropped": int((~mask).sum()), "moi_conv": conv}


# ---------------------------------------------------------------- P4.3 差额块清单


def gap_blocks_of(domain: dict, cov: dict, f: float) -> list:
    """申报域 − 已覆盖 = 块清单（设计 §2：块粒度 L×vds×W(×vbs)，v1 只出**边块**）。

    轴内稀疏=插值管辖（interp 红牌线，origin=interp 已走），本清单只管申报边界
    之外的空白；vbs 以接法形态计块（body 申报但表只 vbs=0 ⇒ 整片出域源）。
    cov 用 build_dev_npz 的 ws/Ls/vdss/vbss + vg 两端点（未复制退化轴的原值集）。
    """
    if not declared_domain(domain):
        return []
    blocks = []
    axes = (("L", domain.get("L_range"), sorted({v for v in cov.get("Ls") or []})),
            ("W", domain.get("W_range"), sorted({v for v in cov.get("ws") or []})),
            ("vds", domain.get("vds_range"), sorted({v for v in cov.get("vdss") or []})))
    for ax, rng, got in axes:
        if rng is None or not got:
            continue
        lo, hi = float(rng[0]), float(rng[1])
        if ax == "vds":                          # 幅值口径归一（表轴=|vds|）
            lo, hi = min(abs(lo), abs(hi)), max(abs(lo), abs(hi))
        if got[0] - lo > 1e-15 * max(1.0, hi):
            blocks.append({"axis": ax, "kind": "edge", "lo": lo, "hi": got[0]})
        if hi - got[-1] > 1e-15 * max(1.0, hi):
            blocks.append({"axis": ax, "kind": "edge", "lo": got[-1], "hi": hi})
    vg_rng = domain.get("vgs_range")
    vg_lo, vg_hi = cov.get("vg_lo"), cov.get("vg_hi")
    if vg_rng is not None and vg_lo is not None:
        a, b = float(vg_rng[0]), float(vg_rng[1])
        nlo, nhi = sorted((f * a, f * b))        # 归一 g=f·vgs（与 vg 轴同尺）
        if nlo - vg_lo > 1e-12:
            blocks.append({"axis": "vgs", "kind": "edge",
                           "lo": round(nlo / f, 12), "hi": round(vg_lo / f, 12)})
        if nhi - vg_hi > 1e-12:
            blocks.append({"axis": "vgs", "kind": "edge",
                           "lo": round(vg_hi / f, 12), "hi": round(nhi / f, 12)})
    vbss = {round(v, 12) for v in (cov.get("vbss") or [])}
    if domain.get("vbs_topology") == "body" and vbss <= {0.0}:
        blocks.append({"axis": "vbs", "kind": "topology", "lo": 0.0, "hi": None,
                       "note": "申报独立体节点但表 vbs 恒零——源端串退化电阻一类"
                               "电路一进即整片出域；补采须在 pair 加 vbs 扫锚（n 倍"
                               "代价，何时付由签字环节定块）"})
    return blocks


def check_domain_lamp(dev: str, domain: dict, cov: dict, f: float) -> list:
    """灯一（申报域 ⊇ 已覆盖域，设计 §0）的 pack 侧判定：越界点名。

    只查申报了的轴；vbs 单点/双点是覆盖事实不判违规（接法形态走 gap 通道）。
    """
    probs = []
    if not declared_domain(domain):
        return probs

    def _chk(ax, rng, got, conv=None):
        if rng is None or not got:
            return
        lo, hi = float(rng[0]), float(rng[1])
        if conv:
            lo, hi = conv(lo, hi, f)
        bad = [v for v in got if v < lo - 1e-15 or v > hi + 1e-15]
        if bad:
            probs.append("%s: 表轴 %s 越出申报域 [%g,%g]（覆盖点 %s）——"
                         "要么改 domain 申报，要么这是超 SOA 采数，重采修正"
                         % (dev, ax, lo, hi,
                            ", ".join("%g" % v for v in bad[:6])))

    _chk("L", domain.get("L_range"), sorted(set(cov.get("Ls") or [])))
    _chk("W", domain.get("W_range"), sorted(set(cov.get("ws") or [])))
    _chk("vds", domain.get("vds_range"), sorted(set(cov.get("vdss") or [])),
         conv=lambda a, b, s: (min(abs(a), abs(b)), max(abs(a), abs(b))))
    if domain.get("vgs_range") is not None and cov.get("vg_lo") is not None:
        a, b = float(domain["vgs_range"][0]), float(domain["vgs_range"][1])
        nlo, nhi = sorted((f * a, f * b))
        if cov["vg_lo"] < nlo - 1e-12 or cov["vg_hi"] > nhi + 1e-12:
            probs.append("%s: vg 格 [%g,%g] 越出申报 vgs 域（归一后 [%g,%g]）"
                         % (dev, cov["vg_lo"], cov["vg_hi"], nlo, nhi))
    return probs


# ---------------------------------------------------------------- 归一汇集

def collect_curves(prof: dict, pair_name: str, fet: str, data_root, plans: dict,
                   tools_dir) -> tuple[dict, dict]:
    """→ ({dev: {key: curve}}, stats)。key=(L, |vds|, |vbs|, W)；无片返回空 dict。"""
    dec = get_pair(prof, pair_name)[fet]
    dev = dec["model"]
    f = float(dec["polarity"])
    vbs_raw = float(get_pair(prof, pair_name).get("vbs", 0.0))
    data_root = Path(data_root)
    curves: dict = {}
    deck_owner: dict = {}
    stats = {"dev": dev, "f": f, "decks_seen": 0, "decks_other": 0,
             "points_dropped": 0, "slices": 0, "moi_conv": ""}
    for deck_id, row in sorted(plans.items()):
        if row["pair"] != pair_name or row["fet"] != fet:
            stats["decks_other"] += 1
            continue
        ddir = data_root / pair_name / deck_id
        if not ddir.is_dir():
            raise PackError(f"片目录不存在: {ddir}（计划有、数据无——先 run，"
                            "或从 plan 剔除该行）")
        csv_path, rc = ensure_csv(ddir, Path(tools_dir))
        if csv_path is None:
            raise PackError(f"片 {ddir} 无 dc.csv 且补转失败 rc={rc}（先 qc 或手动 iris2csv）")
        _deck_name_check(deck_id, row)
        cols = load_slice(csv_path)
        sweep, scol = vgs_of(cols)
        if not scol:
            raise PackError(f"片 {csv_path} 无扫段列 sweep/g")
        insts = instances(cols)
        byw = per_w(insts, row["cells"])
        stats["decks_seen"] += 1
        for idx, c in enumerate(row["cells"]):
            iname = c.get("iname") or f"M{idx + 1}"
            sig = byw.get(iname)
            if sig is None:
                raise PackError(f"片 {deck_id} 计划标 {iname}，CSV 列族无此前缀")
            w = sig.pop("_W_m")
            sig["vgs"] = sweep
            cv = _finalize_curve(_curve_from_instance(deck_id, iname, sig, f, w),
                                 deck_id, iname)
            if not stats["moi_conv"]:
                stats["moi_conv"] = cv["moi_conv"]
            key = (round(parse_len(c["L"]), 15), round(abs(float(c["vds"])), 15),
                   round(abs(vbs_raw), 15), round(w, 15))
            prev = curves.get(key)
            if prev is not None:
                pj, nj = prev["J"], cv["J"]
                if pj.size == nj.size and float(np.max(np.abs(nj - pj)
                                            / np.maximum(pj, 1e-300))) < 1e-6:
                    continue                     # 同片重放幂等：值一致=保先到
                raise PackError(f"同点冲突 (dev={dev} L={key[0]:g} W={key[3]:g} "
                                f"vds={key[1]:g}): deck {deck_owner[key]} 与 {deck_id} "
                                "J 曲线不一致——查混批/错档")
            cv["deck"] = deck_id
            curves[key] = cv
            deck_owner[key] = deck_id
            stats["slices"] += 1
            stats["points_dropped"] += cv["dropped"]
    return ({dev: curves} if curves else {}), stats


# ---------------------------------------------------------------- 建包数学

def _axes_of(curves: dict) -> tuple:
    """曲线键 → (ws, Ls, vdss, vbss, dup)。单点轴复制成双节点：引擎 _brk 的
    i0=min(...,len-2) 在 len=1 时翻负、einsum 权 2 对轴 1 直接炸（字节级方言）；
    复制后 t 恒 0、值位级等价单读，且 vbs=[0,0] 让 |b|>0 查询响亮出域（诚实）。"""
    ws = sorted({k[3] for k in curves})
    Ls = sorted({k[0] for k in curves})
    vdss = sorted({k[1] for k in curves})
    vbss = sorted({k[2] for k in curves})
    dup = []
    for name, ax in (("L", Ls), ("vds", vdss), ("vbs", vbss)):
        if len(ax) == 1:
            ax.append(ax[0])
            dup.append(name)
    return ws, Ls, vdss, vbss, dup


def _propagate_dup(X, Y, Ls, vdss, vbss) -> None:
    """退化双节点轴的数据传播：li/vi/bi 字典对重复值后索引赢 ⇒ 数据全落在
    idx=1，idx=0 空转 NaN；引擎 _brk 精确命中时读 (0,1) 角块，空半边=四角 NaN
    响亮误杀。逐重复对 1→0 复制（位级同源，无信息增造）。"""
    for ax, dim in ((Ls, 1), (vdss, 2), (vbss, 3)):
        for idx in range(1, len(ax)):
            if ax[idx] != ax[idx - 1]:
                continue
            for A in (X, Y):
                s1 = [slice(None)] * A.ndim
                s0 = [slice(None)] * A.ndim
                s1[dim], s0[dim] = idx, idx - 1
                A[tuple(s0)] = A[tuple(s1)]


def _vg_axis(dev: str, curves: dict) -> np.ndarray:
    """器件统一 vg 格（归一 g）；不齐=响亮拒点名（生产表全等纪律）。"""
    it = iter(curves.values())
    ref = np.asarray(next(it)["g"])
    bad = []
    for cv in it:
        g = np.asarray(cv["g"])
        if g.size != ref.size or float(np.max(np.abs(g - ref))) > 1e-9:
            bad.append(cv["deck"])
    if bad:
        raise PackError(f"器件 {dev}: vgs 格不齐（vstep/段数跨片不一致），冲突片: "
                        f"{bad[:8]}——同器件全部片须同 vstep")
    vg = np.sort(np.asarray(ref, dtype=np.float64))
    if np.any(np.diff(vg) <= 0.0):
        raise PackError(f"器件 {dev}: vgs 格非严格升序")
    return vg


def build_dev_npz(dev: str, curves: dict, mem_dir: Path, write: bool = True) -> dict:
    """归一曲线 → {dev}_v1J.npz + {dev}_v1V.npz（write=False 只算不盘）。"""
    ws, Ls, vdss, vbss, dup = _axes_of(curves)
    vg = _vg_axis(dev, curves)
    li = {v: i for i, v in enumerate(Ls)}
    vi = {v: i for i, v in enumerate(vdss)}
    bi = {v: i for i, v in enumerate(vbss)}
    wi = {v: i for i, v in enumerate(ws)}
    lJ = np.linspace(math.log(JLO), math.log(JHI), NJ)
    X = np.full((len(ws), len(Ls), len(vdss), len(vbss), NJ, len(COLS)),
                np.nan, dtype=np.float32)
    Y = np.full((len(ws), len(Ls), len(vdss), len(vbss), len(vg)),
                np.nan, dtype=np.float32)
    nfill = 0
    badmono = 0
    for key, cv in sorted(curves.items()):
        i, j, k, m = li[key[0]], vi[key[1]], bi[key[2]], wi[key[3]]
        J = np.asarray(cv["J"])
        g = np.asarray(cv["g"])
        order = np.argsort(J, kind="stable")
        dd = np.diff(J[order])
        if (dd <= 0).any():
            badmono += 1
            keep = np.concatenate(([True], dd > 0))
            if keep.sum() < 3:
                continue
            order = order[keep]
        Js = J[order]
        gs = g[order]
        idxs = np.clip(np.searchsorted(vg, gs), 0, len(vg) - 1)
        if float(np.max(np.abs(vg[idxs] - gs))) > 1e-9:
            raise PackError(f"器件 {dev} 片 {key}: g 值落不进 vg 格（内部错误）")
        Y[m, i, j, k, idxs] = Js.astype(np.float32)
        row = Y[m, i, j, k]
        fin = np.flatnonzero(np.isfinite(row))
        if fin.size:
            if fin[0] > 0:
                row[:fin[0]] = row[fin[0]]
            row[fin[-1] + 1:] = row[fin[-1]]
        lc = np.log(Js)
        for ci, c in enumerate(COLS):
            X[m, i, j, k, :, ci] = np.interp(lJ, lc, np.asarray(cv["cols"][c])[order])
        nfill += NJ
    _propagate_dup(X, Y, Ls, vdss, vbss)
    nanfrac = float(np.mean(~np.isfinite(X)))
    if write:
        os.makedirs(mem_dir, exist_ok=True)
        W_arr = np.array(ws, dtype=np.float64)
        np.savez(str(mem_dir / f"{dev}_v1J"), allow_pickle=False,
                 L=np.array(Ls), vds=np.array(vdss), vbs=np.array(vbss),
                 W=W_arr, lJ=lJ, X=X, ColNames=np.array(COLS, dtype="U8"))
        np.savez(str(mem_dir / f"{dev}_v1V"), allow_pickle=False,
                 L=np.array(Ls), vds=np.array(vdss), vbs=np.array(vbss),
                 W=W_arr, vg=vg, Y=Y)
    return {"dev": dev, "axes": (len(ws), len(Ls), len(vdss), len(vbss), len(vg)),
            "dup_axes": dup, "curves": len(curves), "badmono": badmono,
            "fill%": round(100.0 * nfill / max(1, len(ws) * len(Ls) * len(vdss)
                                               * len(vbss) * NJ), 1),
            "nan%": round(100.0 * nanfrac, 1),
            "ws": ws, "Ls": Ls, "vdss": vdss, "vbss": vbss,
            "vg_lo": float(vg[0]), "vg_hi": float(vg[-1])}


# ---------------------------------------------------------------- manifest

def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def table_digest_of(mem_dir: Path) -> str:
    """P4.1 内容指纹：表目录全部 npz（文件名排序+字节）的 sha256。

    优化器台账（ga_state #META + evaluations 列）逐行携带它的短形——表被
    pack 更新 ⇒ 指纹必变 ⇒ 旧账继承被版本闸拦下（设计 §1）。覆写留代
    （_archive_prev_gen）保证旧指纹仍有实体可追溯。"""
    h = hashlib.sha256()
    for fn in sorted(os.listdir(mem_dir)):
        if not fn.endswith(".npz"):
            continue
        h.update(fn.encode("utf-8"))
        h.update((mem_dir / fn).read_bytes())
    return h.hexdigest()


def _archive_prev_gen(pdk_dir: Path, mem_dir: Path, keep: int = 1,
                      out=print) -> str:
    """覆写留代：把 mem 下现有 npz（+旧 manifest）整体挪进
    <pdk>/archive/<旧指纹8>/，只保最近 keep 代（按目录 mtime 剪）。

    返回归档目录名（旧指纹8）；mem 无 npz ⇒ 空串（首建无代可留）。
    动机=设计 §1"指纹可追溯"：不留代，旧 ga_state 的表指纹将指回一个
    已不存在的表面，审计链断。
    """
    olds = [f for f in os.listdir(mem_dir) if f.endswith(".npz")] \
        if mem_dir.is_dir() else []
    if not olds:
        return ""
    dg8 = table_digest_of(mem_dir)[:8]
    arch = pdk_dir / "archive" / dg8
    if arch.exists():
        shutil.rmtree(arch)
    arch.mkdir(parents=True)
    for fn in olds:
        shutil.move(str(mem_dir / fn), str(arch / fn))
    mf = pdk_dir / "manifest.json"
    if mf.is_file():
        shutil.move(str(mf), str(arch / "manifest.json"))
    # 剪代：只留最近 keep 个（含刚写入的这代）
    try:
        gens = sorted((pdk_dir / "archive").iterdir(),
                      key=lambda p: p.stat().st_mtime, reverse=True)
        for stale in gens[max(0, keep):]:
            if stale.is_dir():
                shutil.rmtree(stale, ignore_errors=True)
    except OSError:
        pass
    out(f"[pack][留代] 旧表已归档 archive/{dg8}/（保留最近 {keep} 代）")
    return dg8


def write_manifest(pdk_dir: Path, prof: dict, pair_name: str, built: dict,
                   notes: list, force: bool, mem_dir: Path = None) -> Path:
    """manifest.json 合册：多 pair 分批 pack 保留既有 devices；--force 本批全重写。

    P4.1：落表时写入 table_digest（对 mem 全部 npz 内容做 sha256）——
    优化器与台账以此为版本闸的权威锚（缺 manifest 时优化器回落元数据指纹，
    pdk_tables.table_digest）。"""
    f = pdk_dir / "manifest.json"
    old = None
    if f.is_file():
        try:
            old = json.loads(f.read_text(encoding="utf-8"))
        except ValueError:
            old = None
        if old is not None and old.get("kind") != "midas-pdk" and not force:
            raise PackError(f"{f} 非 midas-pdk manifest（kind={old.get('kind')!r}），"
                            "不敢覆写——人工处置或 --force")
    doc = {"kind": "midas-pdk", "v": 1,
           "pdk": prof["name"], "created_utc": _now_utc(),
           "source": {"tool": TOOL_TAG, "profile": prof["name"]},
           "pair": pair_name, "corner": prof.get("corner", "tt"),
           "temp_c": prof.get("temp_c"),
           "devices": dict((old or {}).get("devices") or {}),
           "notes": list((old or {}).get("notes") or [])}
    for dev, b in built.items():
        ent = {"polarity": 1 if b["f"] >= 0 else -1, "pair": pair_name,
               "corner": prof.get("corner", "tt"), "temp_c": prof.get("temp_c"),
               "L_range": [b["Ls"][0], b["Ls"][-1]], "W_nodes": b["ws"],
               "vds_anchors": b["vdss"], "vbs_nodes": b["vbss"],
               "n_slices": b["curves"], "built_utc": _now_utc()}
        if declared_domain(b.get("domain") or {}):
            ent["domain"] = dict(b["domain"])          # P4.3 申报域入册
            ent["gap_blocks"] = b.get("gap_blocks") or []
        doc["devices"][dev] = ent if force else {**doc["devices"].get(dev, {}), **ent}
    for n in notes:
        if n not in doc["notes"]:
            doc["notes"].append(n)
    mem = Path(mem_dir) if mem_dir is not None else pdk_dir / "mem"
    doc["table_digest"] = (table_digest_of(mem)
                           if mem.is_dir() and os.listdir(mem) else "")
    f.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    return f


# ---------------------------------------------------------------- 方言验证闸

def _shape_bad(A, want) -> bool:
    return tuple(A.shape) != tuple(want)


def verify_dialect(mem_dir: Path, devices: dict, live: bool = True) -> dict:
    """P2 的生死闸——回答"新建 npz 是否存在方言、导致查表失效"。

    devices: {dev: 申报极性 ±1}。三层：
      静态 schema（必做，纯 numpy）：键齐/形状=轴积/轴升序且幅值非负/
        ColNames=契约列/引擎硬需列齐/首字母极性与申报一致；
      真装载回环（优化器包可导则必做）：BankV/BankJ 直读 mem_dir，中位坐标
        逐管 J_grad+lookup，断言 J>0、moi/av0 有限、W 出域必响亮 OutOfDomain
        （引擎"不外推"纪律的正脸测试）；
      不可导 ⇒ live=skipped 呈报降级（发行机场景），静态层仍全跑。
    """
    lines, problems = [], []
    ok = True
    for dev, fexp in sorted(devices.items()):
        pJ, pV = mem_dir / f"{dev}_v1J.npz", mem_dir / f"{dev}_v1V.npz"
        if not (pJ.is_file() and pV.is_file()):
            problems.append(f"{dev}: vJ/vV 不齐（{pJ.name} / {pV.name}）")
            ok = False
            continue
        z = np.load(pJ, allow_pickle=False)
        for k in ("L", "vds", "vbs", "W", "lJ", "X", "ColNames"):
            if k not in z.files:
                problems.append(f"{dev}_v1J 缺键 {k!r}（_Pack 装载面硬依赖；"
                                "lJ/X 缺失会被静默回退成 vg/Y——正是方言暗坑）")
                ok = False
        zz = np.load(pV, allow_pickle=False)
        for k in ("L", "vds", "vbs", "W", "vg", "Y"):
            if k not in zz.files:
                problems.append(f"{dev}_v1V 缺键 {k!r}")
                ok = False
        cols = [str(c) for c in z["ColNames"]] if "ColNames" in z.files else []
        if cols and set(cols) != set(COLS):
            problems.append(f"{dev}_v1J ColNames={cols} ≠ 契约列集")
            ok = False
        miss = ENGINE_MIN_COLS - set(cols)
        if miss:
            problems.append(f"{dev}_v1J 缺引擎硬需列 {sorted(miss)}（BankJ.lookup "
                            "无条件取列=首查 KeyError）")
            ok = False
        if ok:
            if _shape_bad(z["X"], (len(z["W"]), len(z["L"]), len(z["vds"]),
                                   len(z["vbs"]), NJ, len(COLS))):
                problems.append(f"{dev}_v1J X.shape={z['X'].shape} ≠ 轴积")
                ok = False
            if _shape_bad(zz["Y"], (len(zz["W"]), len(zz["L"]), len(zz["vds"]),
                                    len(zz["vbs"]), len(zz["vg"]))):
                problems.append(f"{dev}_v1V Y.shape={zz['Y'].shape} ≠ 轴积")
                ok = False
            for ax in ("L", "vds", "vbs"):
                v = z[ax]
                if len(v) < 2 or np.any(np.diff(v) < 0):
                    problems.append(f"{dev}: 轴 {ax} 须 ≥2 节点且升序（_brk/einsum 方言），"
                                    f"现为 {list(v)}")
                    ok = False
            if np.any(z["vds"] < 0) or np.any(z["vbs"] < 0) or np.any(z["W"] <= 0):
                problems.append(f"{dev}: vds/vbs 须幅值(≥0)、W 须正")
                ok = False
            if np.any(np.diff(z["lJ"]) <= 0) or np.any(np.diff(zz["vg"]) <= 0):
                problems.append(f"{dev}: lJ/vg 非严格升序")
                ok = False
            if np.any(zz["vg"] < 0):
                problems.append(f"{dev}: vg 须归一 f·vgs（全 ≥0）——p 表存负格=坐标面反了")
                ok = False
            if z["X"].dtype != np.float32 or zz["Y"].dtype != np.float32:
                problems.append(f"{dev}: X/Y 须 float32（与生产表字节同型）")
                ok = False
        if _fp(dev) != float(fexp):
            problems.append(f"{dev}: 引擎首字母判极性={_fp(dev):+.0f} 与申报 {fexp:+.0f} "
                            "不符——泛化轨取号必错（表名=PDK 模型名纪律的反例）")
            ok = False
        if ok:
            lines.append(f"{dev}: 静态方言 PASS（axes W{len(z['W'])}×L{len(z['L'])}"
                         f"×vds{len(z['vds'])}×vbs{len(z['vbs'])}, lJ {len(z['lJ'])}, "
                         f"vg {len(zz['vg'])}）")
    if not ok:
        return {"ok": False, "lines": lines, "problems": problems, "live": "skipped"}
    if not live:
        return {"ok": True, "lines": lines, "problems": [], "live": "off"}
    try:
        from optimization_tool.surrogates.gsieve.bank_loader import OutOfDomain
        from optimization_tool.surrogates.gsieve.mem_v1 import BankJ, BankV
    except Exception as exc:
        return {"ok": True, "lines": lines, "problems": [],
                "live": f"skipped（优化器包不可导入: {type(exc).__name__}）"}
    try:
        bv = BankV(str(mem_dir))
        bj = BankJ(str(mem_dir))
    except Exception as exc:
        return {"ok": False, "lines": lines,
                "problems": [f"真装载失败: {type(exc).__name__}: {exc}"], "live": "FAIL"}
    for dev in sorted(devices):
        p = bj.pk.get(dev)
        pv = bv.pk.get(dev)
        if p is None or pv is None:
            return {"ok": False, "lines": lines,
                    "problems": [f"{dev}: BankJ/BankV 装载后不在包集（文件名方言面）"],
                    "live": "FAIL"}
        try:
            L = float(p.L[len(p.L) // 2])
            a = float(p.vds[len(p.vds) // 2])
            w = float(p.W[min(1, len(p.W) - 1)])    # 主宽优先取第 2 节点（1µm 语义位）
            g = float(pv.grid[len(pv.grid) // 2])
            f = _fp(dev)
            Jv, dg, dd_, db = bv.J_grad(dev, L, f * a, -0.0, f * g, w=w)
            if not (Jv > 0.0 and all(math.isfinite(x) for x in (Jv, dg, dd_, db))):
                raise ValueError(f"J_grad 读数非正/非有限 J={Jv!r}")
            q = bj.lookup(dev, L, w, Jv, f * a, -0.0)
            bad = [k for k in ("moi", "av0", "gm", "gds")
                   if not math.isfinite(q.get(k, float("nan")))]
            if bad or q["moi"] <= 0.0:
                raise ValueError(f"lookup 列失格 {bad or 'moi<=0'}")
            try:
                bj.lookup(dev, L, float(p.W[-1]) * 10.0, Jv, f * a, -0.0)
                raise ValueError("W 出域未拒评（外推纪律破=幻影读数入口）")
            except OutOfDomain:
                pass
        except Exception as exc:
            return {"ok": False, "lines": lines,
                    "problems": [f"{dev}: 真装载回环炸出 {type(exc).__name__}: {exc}"],
                    "live": "FAIL"}
        lines.append(f"{dev}: 真装载回环 PASS（J_grad={Jv:.3g} A/m, moi={q['moi']:.3g}, "
                     f"av0={q['av0']:.3g}）")
    return {"ok": True, "lines": lines, "problems": [], "live": "PASS"}


# ---------------------------------------------------------------- 顶层驱动

def pack_profile(prof_path, pair_name: str, plan_paths, data_root="data",
                 fet: str = "both", pdk_root=None, force: bool = False,
                 dry_run: bool = False, live_verify: bool = True,
                 qc_report: str = "", interp_report: str = "",
                 force_unlock: bool = False, keep_archive: int = 1,
                 out=print) -> dict:
    """采集数据 → <根>/<profile.name>/mem 双包+manifest+方言闸。返回汇总报告。

    fet: "n"|"p"|"both"；plan_paths: plan jsonl 路径表（多份合账，重放幂等）。
    失败路径全部 PackError（启动即响亮，绝不留半套表）。
    P4.2 表锁：非 dry-run 抢写锁后落盘（活读者=优化器在跑 ⇒ TableLocked 拒单；
    --force-unlock 人工强解留痕）；P4.1 留代：--force 覆写前把旧表整体挪进
    archive/<旧指纹8>/（只保最近 keep_archive 代）。
    """
    prof = load_profile(prof_path)
    pair = get_pair(prof, pair_name)
    root = Path(pdk_root).expanduser() if pdk_root else pdk_default_root()
    pdk_dir = root / prof["name"]
    mem_dir = pdk_dir / "mem"
    plans = read_plans(list(plan_paths) if isinstance(plan_paths, (list, tuple))
                       else [plan_paths])
    locked = False
    if not dry_run:
        tablelock.begin_write(pdk_dir, force_unlock=force_unlock, out=out)
        locked = True
    try:
        return _pack_body(prof, pair, pair_name, plans, mem_dir, pdk_dir,
                          data_root=data_root, fet=fet,
                          force=force, dry_run=dry_run,
                          live_verify=live_verify, qc_report=qc_report,
                          interp_report=interp_report,
                          keep_archive=keep_archive, out=out)
    finally:
        if locked:
            tablelock.end_write(pdk_dir)


def _pack_body(prof, pair, pair_name, plans, mem_dir, pdk_dir,
               data_root, fet, force, dry_run,
               live_verify, qc_report, interp_report, keep_archive, out):
    """pack_profile 的落盘主体（写锁已由外层持有；异常直穿 finally 放锁）。"""
    tools_dir = Path(__file__).resolve().parent.parent / "tools"
    fets = ("n", "p") if fet == "both" else (fet,)
    built: dict = {}
    notes: list = []
    _arch_done = False
    for fe in fets:
        curves, stats = collect_curves(prof, pair_name, fe, Path(data_root), plans,
                                       tools_dir)
        if not curves:
            notes.append(f"{get_pair(prof, pair_name)[fe]['model']}: {fe} 侧无在账片"
                         "（计划/目录不含），跳过")
            out(f"[pack] {notes[-1]}")
            continue
        dev, cmap = next(iter(curves.items()))
        if not dry_run and not force:
            for suf in ("_v1J.npz", "_v1V.npz"):
                tgt = mem_dir / (dev + suf)
                if tgt.exists():
                    raise PackError(f"{tgt} 已存在——整目录覆写是版本纪律，"
                                    "确认请 --force")
        if not dry_run and force and not _arch_done:
            _arch_done = True
            _archive_prev_gen(pdk_dir, mem_dir, keep_archive, out)
        b = build_dev_npz(dev, cmap, mem_dir, write=not dry_run)
        b["f"] = stats["f"]
        b["points_dropped"] = stats["points_dropped"]
        # ── P4.3 域申报半边：灯一硬闸（申报 ⊉ 覆盖 ⇒ 响亮拒）+ 差额块清单 ──
        b["domain"] = domain_of(prof, pair_name, fe)
        if declared_domain(b["domain"]):
            probs = check_domain_lamp(dev, b["domain"], b, stats["f"])
            if probs:
                raise PackError("域申报不过灯一（申报域必须 ⊇ 已覆盖域）——\n"
                                + "\n".join(probs))
        b["gap_blocks"] = gap_blocks_of(b["domain"], b, stats["f"])
        built[dev] = b
        tag = "dry" if dry_run else "ok"
        out(f"[pack] {dev}: 轴 W{len(b['ws'])}×L{len(b['Ls'])}×vds{len(b['vdss'])}"
            f"×vbs{len(b['vbss'])}×g{b['axes'][4]}  片 {b['curves']}  "
            f"填充 {b['fill%']}%  X NaN {b['nan%']}%  丢点 {b['points_dropped']}"
            f"  非单调片 {b['badmono']}  moi={stats['moi_conv'] or '—'}  [{tag}]")
        if declared_domain(b["domain"]):
            d_ = b["domain"]
            rng_ = " ".join("%s=[%g,%g]" % (k, d_[k][0], d_[k][1])
                            for k in ("L_range", "W_range", "vds_range",
                                      "vgs_range") if d_[k])
            out(f"[pack][域申报] {dev}: {rng_} vbs={d_['vbs_topology']} "
                f"source={d_['source']}（灯一 ⊇ 覆盖 PASS）")
        if b["gap_blocks"]:
            out(f"[pack][差额块] {dev}: {len(b['gap_blocks'])} 块入 manifest"
                f"（边块 {sum(1 for g in b['gap_blocks'] if g['kind']=='edge')}"
                f" / 接法 {sum(1 for g in b['gap_blocks'] if g['kind']=='topology')}）"
                "——按电路负载踩块再下队，不必全域一次付")
        for ax in b["dup_axes"]:
            notes.append(f"{dev}: {ax} 轴单点 ⇒ 复制双节点（引擎 _brk 须 ≥2；"
                         "vbs 单点表只吃衬底接源电路，|vbs|>0 查询响亮出域）")
    if not built:
        raise PackError("无器件可打包（两侧均无片——检查 --fet/--plans/--data）")
    rep = {"pdk": prof["name"], "dir": str(pdk_dir), "built": built, "notes": notes}
    if dry_run:
        out(f"[pack] dry-run：判式如上，未落盘（目标 {mem_dir}）")
        rep["verify"] = {"ok": True, "lines": [], "problems": [], "live": "dry"}
        return rep
    rep["manifest"] = str(write_manifest(pdk_dir, prof, pair_name, built, notes,
                                         force, mem_dir=mem_dir))
    rep["table_digest"] = str(table_digest_of(mem_dir))
    out(f"[pack] 表指纹 {rep['table_digest'][:8]}（manifest.table_digest）")
    if qc_report:
        rep["qc_report"] = qc_report
    if interp_report:
        rep["interp_report"] = interp_report
    v = verify_dialect(mem_dir, {d: b["f"] for d, b in built.items()},
                       live=live_verify)
    for ln in v["lines"]:
        out(f"[pack][方言] {ln}")
    for pr_ in v["problems"]:
        out(f"[pack][方言][红] {pr_}")
    if isinstance(v["live"], str):
        out(f"[pack][方言] 真装载: {v['live']}")
    rep["verify"] = v
    if not v["ok"]:
        raise PackError(f"方言验证不过——产物已落 {mem_dir}，修复后 --force 重打："
                        + "；".join(v["problems"]))
    out(f"[pack] 入库 {pdk_dir}（npz×{2 * len(built)} + manifest）；优化器侧 "
        f"midas_pdk=auto 应能点名 {prof['name']}")
    return rep
