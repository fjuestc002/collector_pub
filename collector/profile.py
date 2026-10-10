# -*- coding: utf-8 -*-
"""PDK 画像装载与校验（M0）。

画像=域与通道的事实清单（路径/段名/选项串/冒号制列/器件对），换 PDK 只加画像不改码。
装载失败=响亮拒（缺键点名），不做静默兜默认值。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

REQUIRED_KEYS = [
    "name", "backend", "cadence", "model_includes", "temp_c", "tnom_c",
    "options", "save_style", "signals_dc", "mos", "pairs",
]
REQUIRED_MOS = ["pins", "instance", "vds_source", "vgs_source", "sweep"]

#: P4.3 域申报（设计 §2，source=manual 定案：SOA 人工填写，软件不解析各家
#: PDK 方言）。挂在 pairs.<pair>.<fet>.domain，随 pack 进 manifest 供优化器
#: 启动夹紧与差额块清单消费。
DOMAIN_AXES = ("L_range", "W_range", "vds_range", "vgs_range")
#: 已实现的 source 值；"soa"=留位不填实现（用户裁：PDK 方言穷不尽，人工填写定案）
DOMAIN_SOURCES = ("manual",)
VBS_TOPOLOGIES = ("tied", "body")       # tied=衬底接源(vbs≡0)；body=独立体节点


class ProfileError(Exception):
    pass


def _need(cond: bool, msg: str) -> None:
    if not cond:
        raise ProfileError(msg)


def load_profile(path: str | Path) -> dict:
    p = Path(path)
    _need(p.is_file(), f"画像文件不存在: {p}")
    try:
        prof = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ProfileError(f"画像 JSON 解析失败: {p}: {e}") from e
    validate_profile(prof, source=str(p))
    prof["_path"] = str(p)
    prof["_hash"] = sha256_text(p.read_text(encoding="utf-8"))
    return prof


def validate_profile(prof: dict, source: str = "<dict>") -> None:
    for k in REQUIRED_KEYS:
        _need(k in prof, f"[{source}] 画像缺必填键: {k}")
    _need(prof["backend"] == "spectre_batch",
          f"[{source}] backend 未支持: {prof['backend']} (v1 只认 spectre_batch)")
    _need(prof["save_style"] == "colon",
          f"[{source}] save_style=v1 只实现冒号制, 现为 {prof['save_style']}")
    for i, inc in enumerate(prof["model_includes"]):
        _need("file" in inc and "sections" in inc,
              f"[{source}] model_includes[{i}] 缺 file/sections")
    mos = prof["mos"]
    for k in REQUIRED_MOS:
        _need(k in mos, f"[{source}] mos 缺字段: {k}")
    for pname, pair in prof["pairs"].items():
        for fet in ("n", "p"):
            _need(fet in pair and "model" in pair[fet],
                  f"[{source}] pairs.{pname}.{fet} 缺 model")
            _need(pair[fet].get("polarity") in (1, -1),
                  f"[{source}] pairs.{pname}.{fet}.polarity 须为 ±1")
            if "domain" in (pair.get(fet) or {}):     # P4.3：申报了才校形
                validate_domain(pair[fet]["domain"],
                                f"[{source}] pairs.{pname}.{fet}.domain")
        _need(pair.get("vmax", 0) > 0, f"[{source}] pairs.{pname}.vmax 须 >0")


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


# ---------------------------------------------------------- P4.3 域申报（domain）

def _len_m(v, ctx: str) -> float:
    """长度申报值 → 米。裸数字按米；带后缀（130n/1.2u…）走 qc.parse_len 同尺。"""
    from .qc import parse_len                      # 延迟导入防环
    if isinstance(v, str):
        try:
            return parse_len(v)
        except Exception as e:
            raise ProfileError(f"{ctx}: 长度 {v!r} 解析失败（{e}）") from e
    if isinstance(v, (int, float)):
        return float(v)
    raise ProfileError(f"{ctx}: 长度申报值类型不合法 {v!r}")


def _pair_rng(v, ctx: str, kind: str) -> list:
    if not isinstance(v, (list, tuple)) or len(v) != 2:
        raise ProfileError(f"{ctx}: {kind} 须为 [lo, hi] 两端表，收到 {v!r}")
    if kind in ("L_range", "W_range"):
        lo, hi = _len_m(v[0], ctx), _len_m(v[1], ctx)
    else:
        try:
            lo, hi = float(v[0]), float(v[1])
        except (TypeError, ValueError) as e:
            raise ProfileError(f"{ctx}: {kind} 电压端点须数字，收到 {v!r}") from e
    if not (hi > lo):
        raise ProfileError(f"{ctx}: {kind} 须 hi>lo（收到 [{lo:g}, {hi:g}]）")
    return [lo, hi]


def validate_domain(dom: dict, ctx: str) -> None:
    """domain 块形状校验（值域合理性归人工，软件只查形）。"""
    if not isinstance(dom, dict):
        raise ProfileError(f"{ctx}: domain 须为对象")
    for k in dom:
        if (k not in DOMAIN_AXES and k not in ("vbs_topology", "corner",
                                               "temp_c", "source", "note")):
            raise ProfileError(f"{ctx}: domain 未知键 {k!r}"
                               f"（已知 {list(DOMAIN_AXES)}"
                               "+vbs_topology/corner/temp_c/source/note）")
    for kind in DOMAIN_AXES:
        if kind in dom and dom[kind] is not None:
            dom[kind] = _pair_rng(dom[kind], ctx, kind)
    if "vbs_topology" in dom:
        if dom["vbs_topology"] not in VBS_TOPOLOGIES:
            raise ProfileError(f"{ctx}: vbs_topology 须 ∈ {VBS_TOPOLOGIES}，"
                               f"收到 {dom['vbs_topology']!r}")
    src = dom.get("source", "manual")
    if src == "soa":
        raise ProfileError(f"{ctx}: source=soa 为留位（不实现）——SOA 人工填写定案，"
                           "请把值抄进各 *_range 并填 source=manual")
    if src not in DOMAIN_SOURCES:
        raise ProfileError(f"{ctx}: domain.source 须 ∈ {DOMAIN_SOURCES}，收到 {src!r}")
    if "temp_c" in dom:
        try:
            dom["temp_c"] = float(dom["temp_c"])
        except (TypeError, ValueError):
            raise ProfileError(f"{ctx}: domain.temp_c 须数字")


def domain_of(prof: dict, pair_name: str, fet: str) -> dict:
    """pairs.<pair>.<fet>.domain 的规范化读取（未申报 ⇒ {}，绝不 None 渗漏）。

    缺省补齐：vbs_topology=tied（在役采集常态：衬底接源、vbs 单点）、source=manual、
    corner/temp_c 缺省回落到画像顶层。返回面键齐（缺的轴=None），消费方免判键。
    """
    raw = ((prof.get("pairs") or {}).get(pair_name) or {}).get(fet) or {}
    dom = dict(raw.get("domain") or {})
    out = {"source": dom.get("source", "manual"),
           "vbs_topology": dom.get("vbs_topology", "tied"),
           "corner": dom.get("corner", prof.get("corner", "tt")),
           "temp_c": float(dom.get("temp_c", prof.get("temp_c", 27)) or 27)}
    for kind in DOMAIN_AXES:                      # 幂等归一：字符串档→米、伏档→float
        if dom.get(kind) is not None:
            out[kind] = _pair_rng(dom[kind], f"pairs.{pair_name}.{fet}.domain", kind)
        else:
            out[kind] = None
    if dom.get("note"):
        out["note"] = str(dom["note"])
    return out


def declared_domain(dom: dict) -> bool:
    """是否申报了任何可夹紧/可查差的轴（全 None 的 domain 块=空申报）。"""
    return any(dom.get(k) is not None for k in DOMAIN_AXES)


def get_pair(prof: dict, name: str) -> dict:
    _need(name in prof["pairs"],
          f"画像无器件对 '{name}', 已知: {sorted(prof['pairs'])}")
    return prof["pairs"][name]
