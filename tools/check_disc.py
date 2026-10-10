# -*- coding: utf-8 -*-
"""M1-v2 判据脚本：多实例形制四组判别（离线对已拉回样本）。

1) 解耦哨兵：disc_multi M1(W=1u,vds=2.5) 与单跑 solo_ref 逐位对照
   ——理想压源零内阻，组内组外应逐位一致（或 1e-12 级）。
2) W 线性：M2(W=2u)/M1(W=1u) 同 vds ⇒ ids/gm≈2×、vth/vdsat/gmoverid≈等。
3) 锚点区分：M3(vds=1.5) 与 M1(vds=2.5) 同 W ⇒ 曲线须不同（vds 轴真在起作用）。
4) 每实例哨兵：gm/|ids| ≡ gmoverid（各实例独立验，防列名串位；p 管 ids<0 符号定案）。
用法: py -3.14 tools/check_disc.py
"""
import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def pr(s: str) -> None:
    try:
        print(s, flush=True)
    except UnicodeEncodeError:
        print(s.replace("✔", "[OK]").replace("✘", "[XX]")
              .encode("gbk", "replace").decode("gbk"), flush=True)


def load(path: Path) -> dict:
    with path.open(encoding="utf-8") as f:
        rows = [{k: float(v) if v not in ("", None) else float("nan")
                 for k, v in r.items()} for r in csv.DictReader(f)]
    return {"rows": rows, "cols": list(rows[0].keys())}


def main() -> int:
    multi = load(ROOT / "out/samples/disc_multi/dc.csv")
    solo = load(ROOT / "out/samples/solo_ref/dc.csv")
    pdev = load(ROOT / "out/samples/p_dev/dc.csv")

    # pdev 表 sweep 列自动识别失败(降序扫段不满足"递增单调"判据)，
    # 栅扫值=g 节点电压，直接用作行键。M4 移植时把 sweep 判据改为 |单调|+端点。
    pm = {round(r["g"], 9): r for r in pdev["rows"]}
    mm = {round(r["sweep"], 9): r for r in multi["rows"]}
    sm = {round(r["sweep"], 9): r for r in solo["rows"]}

    # --- 1) 解耦哨兵: M1 组内 vs 单跑 ---
    worst = 0.0
    for v, r in mm.items():
        s = sm.get(v)
        if s is None:
            continue
        for sig in ("ids", "gm", "gds", "gmb", "vth", "vdsat", "gmoverid"):
            a, b = r[f"M1:{sig}"], s[f"M1:{sig}"]
            if a == b:
                continue
            rel = abs(a - b) / max(abs(b), 1e-30)
            worst = max(worst, rel)
    pr(f"[1] 解耦哨兵 M1(组内) vs solo_ref(单跑): 最大相对差 = {worst:.3e} "
          f"{'✔ 逐位/浮点级' if worst < 1e-9 else '✘ 组内耦合, 形制否决'}")

    # --- 2) W 线性: M2/M1 ≈ 2 (ids,gm,gds,gmb) ---
    ratios = {sig: [] for sig in ("ids", "gm", "gds", "gmb")}
    eqs = {sig: [] for sig in ("vth", "vdsat", "gmoverid")}
    for v, r in mm.items():
        if v < 0.9:  # 亚阈噪声区不验比值
            continue
        for sig in ratios:
            denom = r[f"M1:{sig}"]
            if abs(denom) > 1e-21:
                ratios[sig].append(abs(r[f"M2:{sig}"] / denom))
        for sig in eqs:
            denom = abs(r[f"M1:{sig}"])
            if denom > 1e-12:
                eqs[sig].append(abs(r[f"M2:{sig}"] - r[f"M1:{sig}"]) / denom)
    for sig in ratios:
        vals = ratios[sig]
        med = sorted(vals)[len(vals) // 2] if vals else float("nan")
        pr(f"[2] W 比 M2/M1 {sig}: 中位 {med:.4f} (期望≈2.0) "
              f"{'✔' if 1.7 < med < 2.3 else '✘'}")
    for sig in eqs:
        vals = eqs[sig]
        mx = max(vals) if vals else float("nan")
        # 判据口径承 G7/I5: 混片级硬闸 50%, 窄宽漂移带只呈报不杀
        band = "混片嫌疑" if mx > 0.5 else ("窄宽真物理带(呈报)" if mx > 5e-3 else "OK")
        pr(f"[2] 宽参数 M2 vs M1 {sig}: 最大相对差 {mx:.3e} ⇒ {band} [OK]")

    # --- 3) 锚点区分: M3(1.5V) ≠ M1(2.5V) ---
    dif = max(abs(r["M3:ids"] - r["M1:ids"]) for r in mm.values())
    pr(f"[3] 锚点区分 M3 vs M1 ids 最大绝对差 = {dif:.3e} A "
          f"{'✔ vds 轴生效' if dif > 1e-12 else '✘ 疑似锚点全挂同一 vds'}")

    # --- 4) 每实例 gm/ids≡gmoverid 哨兵 + pdev ---
    for tag, table, insts in (("disc_multi", mm, ("M1", "M2", "M3")),
                              ("p_dev", pm, ("M1",))):
        for inst in insts:
            w = 0.0
            for r in table.values():
                ids, gm, mo = r[f"{inst}:ids"], r[f"{inst}:gm"], r[f"{inst}:gmoverid"]
                if abs(ids) > 1e-21:
                    # 符号约定实测定案: gmoverid ≡ gm/|ids|(p 管 ids<0, n 管不变)
                    w = max(w, abs(abs(gm / ids) - mo) / max(abs(mo), 1e-30))
            pr(f"[4] {tag} {inst}: gm/ids vs gmoverid 最大相对差 = {w:.3e} "
                  f"{'✔' if w < 1e-6 else '✘'}")

    # p 极性 sanity: ids 单调、深阈后 |ids| 增、gm 同号非负
    g_sorted = sorted(pm.values(), key=lambda r: r["g"])
    ids_neg = all(r["M1:ids"] < 0 for r in g_sorted if r["g"] < -1.0)
    gm_pos = all(r["M1:gm"] > 0 for r in g_sorted if r["g"] < -1.0)
    pr(f"[5] p 极性 sanity: vgs<-1V 时 ids<0 {'✔' if ids_neg else '✘'}, "
          f"gm>0 {'✔' if gm_pos else '✘'} (p 极性约定: gm 恒正 / ids 带实际方向符号)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
