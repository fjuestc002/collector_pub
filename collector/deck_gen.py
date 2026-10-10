# -*- coding: utf-8 -*-
"""曲线 deck 生成器（M1，画像驱动）。

单锚形制 = render_curve_deck：一条曲线 = 一个器件实例 (model,L,W) 的 Vgs 直流扫描。
与 VM 实跑过的 P2a.scs 逐行对齐（tests/test_deck_gen.py 金标准钉死），
且 10-09 实机端到端自证（502ms/0 err/哨兵 5.3e-12，见 README）。

多锚形制 = render_batch_deck（M1-v2）：多实例单分析——N 个器件拷贝各挂独立漏网
d{k} 与固定漏压源 V{k}，共栅节点 g 由单一源 V{n+1} 扫一遍。
背景：多锚 V1#dc 覆写已实机判死 (SFE-691)；本形制同时实现"全 W×锚组一 deck"
的发射摊销。理想压源零内阻 ⇒ 各实例电气解耦，组内曲线应与单跑逐位一致（哨兵）。
命名约定：实例漏网=d{k}；漏压源=V{k}；栅扫源=V{N+1}；分析名恒 "dc"。
"""
from __future__ import annotations

import re

from .profile import sha256_text

_LEAD_M = re.compile(r"^M\d+")
_LEAD_V = re.compile(r"^V\d+")


def fmtv(x: float) -> str:
    s = f"{x:g}"
    return "0" if s in ("-0",) else s


def default_ramp(pair: dict, fet: str, vstep: float) -> tuple[float, float, float]:
    """按极性生成 Vgs 扫段：n 从 0→+vmax，p 从 0→-vmax。"""
    vmax = pair["vmax"]
    sign = pair[fet]["polarity"]
    return 0.0, sign * vmax, sign * vstep


def _options_line(prof: dict) -> str:
    fixed = [f"reltol={prof['options']['reltol']}",
             f"vabstol={prof['options']['vabstol']}",
             f"iabstol={prof['options']['iabstol']}"]
    rest = [f"temp={fmtv(prof['temp_c'])}", f"tnom={fmtv(prof['tnom_c'])}",
            f"gmin={prof['options']['gmin']}"]
    extra = [f"{k}={v}" for k, v in prof["options"].items()
             if k not in ("reltol", "vabstol", "iabstol", "gmin")]
    return "simulatorOptions options " + " ".join(fixed + rest + extra)


def render_curve_deck(prof: dict, pair_name: str, fet: str, L: str, W: str,
                      vds_anchors: list[float], vstep: float = 0.01,
                      ramp: tuple[float, float, float] | None = None,
                      include_charge: bool = False,
                      save_options: bool = True) -> dict:
    """渲染一条曲线 deck。返回 {text, analyses:[{name,vds}], deck_sha256}."""
    pair = prof["pairs"][pair_name]
    mos = prof["mos"]
    if not vds_anchors:
        raise ValueError("vds_anchors 不能为空")
    if ramp is None:
        ramp = default_ramp(pair, fet, vstep)
    v0, v1, vs = ramp

    lines: list[str] = ["simulator lang=spectre"]
    for inc in prof["model_includes"]:
        for sec in inc["sections"]:
            lines.append(f'include "{inc["file"]}" section={sec}')

    lines.append(mos["instance"].format(model=pair[fet]["model"], W=W, L=L,
                                        d="d", g="g", b="0", s="0"))
    lines.append(mos["vds_source"].format(d="d", b="0",
                                          vds_fixed=fmtv(vds_anchors[0])))
    lines.append(mos["vgs_source"].format(g="g", b="0", vgs_start=fmtv(v0)))
    lines.append(_options_line(prof))
    if save_options:
        lines.append("saveOptions options save=allpub")

    sigs = list(prof["signals_dc"])
    if include_charge:
        sigs += prof.get("signals_charge", [])
    lines.append("save " + " ".join(f"M1:{s}" for s in sigs))

    analyses = []
    sweep = mos["sweep"]
    if len(vds_anchors) > 1:
        raise NotImplementedError(
            "多锚点 V1#dc 覆写已被实机判死 (SFE-691, 10-09 判别)。"
            "多锚请走 render_batch_deck(多实例单分析形制 M1-v2)。")
    base = (f"dev={sweep['dev']} param={sweep['param']} "
            f"start={fmtv(v0)} stop={fmtv(v1)} step={fmtv(vs)}")
    for i, vd in enumerate(vds_anchors):
        name = "dc" if i == 0 else f"dc{i + 1}"
        lines.append(f"dc {name} {base}")
        analyses.append({"name": name, "vds": vd})

    text = "\n".join(lines) + "\n"
    return {"text": text, "analyses": analyses, "deck_sha256": sha256_text(text)}


def _signals(prof: dict, include_charge: bool) -> list[str]:
    sigs = list(prof["signals_dc"])
    if include_charge:
        sigs += prof.get("signals_charge", [])
    return sigs


def render_batch_deck(prof: dict, pair_name: str, fet: str,
                      cells: list[dict], vstep: float = 0.01,
                      ramp: tuple[float, float, float] | None = None,
                      include_charge: bool = False,
                      save_options: bool = True) -> dict:
    """多实例单分析形制（M1-v2）。

    cells: [{"L": "1u", "W": "2u", "vds": 2.5}, ...]，同画像同 pair 同极性。
    返回 {text, cells:[{iname, dnet, vds_src, L, W, vds}], sweep_dev, deck_sha256}。
    """
    pair = prof["pairs"][pair_name]
    mos = prof["mos"]
    if not cells:
        raise ValueError("cells 不能为空")
    n = len(cells)
    if ramp is None:
        ramp = default_ramp(pair, fet, vstep)
    v0, v1, vs = ramp
    gate_src = f"V{n + 1}"
    model = pair[fet]["model"]
    sigs = _signals(prof, include_charge)

    lines: list[str] = ["simulator lang=spectre"]
    for inc in prof["model_includes"]:
        for sec in inc["sections"]:
            lines.append(f'include "{inc["file"]}" section={sec}')

    cell_meta = []
    for k, c in enumerate(cells, start=1):
        dnet = f"d{k}"
        inst = _LEAD_M.sub(f"M{k}", mos["instance"].format(
            model=model, W=c["W"], L=c["L"], d=dnet, g="g", b="0", s="0"))
        vsrc = _LEAD_V.sub(f"V{k}", mos["vds_source"].format(
            d=dnet, b="0", vds_fixed=fmtv(float(c["vds"]))))
        lines.append(inst)
        cell_meta.append({"iname": f"M{k}", "dnet": dnet, "vds_src": f"V{k}",
                          "L": c["L"], "W": c["W"], "vds": float(c["vds"])})
        lines.append(vsrc)
    lines.append(_LEAD_V.sub(gate_src, mos["vgs_source"].format(
        g="g", b="0", vgs_start=fmtv(v0))))
    lines.append(_options_line(prof))
    if save_options:
        lines.append("saveOptions options save=allpub")
    lines.append("save " + " ".join(f"{cm['iname']}:{s}"
                                    for cm in cell_meta for s in sigs))
    lines.append(f"dc dc dev={gate_src} param={mos['sweep']['param']} "
                 f"start={fmtv(v0)} stop={fmtv(v1)} step={fmtv(vs)}")

    text = "\n".join(lines) + "\n"
    return {"text": text, "cells": cell_meta, "sweep_dev": gate_src,
            "deck_sha256": sha256_text(text)}
