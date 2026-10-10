# -*- coding: utf-8 -*-
"""M4-4a 质检闸离线测试：合成自洽网格 PASS + 六类埋雷必炸。

合成模型（每宽量, pol=±1）：
  ids/W = pol·(α·u² + β·u·vds_abs + c)，u=pol·vgs（n:u=v, p:u=−v, vds_abs=|vds|）
  gm    = W·(2α·u + β·vds_abs)   ⇒ d(ids/W)/dvgs 中心差分**精确**等于 gm（方向恒正）
  gds   = W·β·u                  ⇒ 邻 vds 锚弦差**精确**等于 gds
  gmoverid = gm/|ids|（逐行精确） ⇒ 哨兵 0 误差
  qg = W·(γg·u + q0), cgg = W·γg；qd = W·(γd·u + q1), cgd = W·γd（γd<0）
  ⇒ 电荷交叉 |d(q)/dvgs|=|c| 逐行精确
"""
import csv
import json
import math
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from collector import engine, qc  # noqa: E402
from collector.profile import load_profile  # noqa: E402

GEN = ROOT / "profiles" / "EXAMPLE_generic_mos_spectre.json"
ALPHA, BETA, C0 = 0.1, 0.02, 1e-3
GG, GD, Q0, Q1 = 1e-9, -2e-12, 5e-18, -8e-19


def synth_rows(fet, vds, W, vstep=0.1, n=11):
    pol = 1 if fet == "n" else -1
    vd = abs(vds)
    out = []
    for i in range(n):
        v = pol * i * vstep          # n: 0→+1, p: 0→−1
        u = pol * v                  # = i*vstep，恒非负
        jp = ALPHA * u * u + BETA * u * vd + C0
        ids = pol * W * jp
        gm = W * (2 * ALPHA * u + BETA * vd)
        gds = W * BETA * u
        gmb = W * 0.005
        qg = W * (GG * u + Q0)
        qd = W * (GD * u + Q1)
        cgg, cgd = W * GG, W * GD
        row = {"sweep": v, "g": v,
               "ids": ids, "gm": gm, "gds": gds, "gmb": gmb,
               "vth": 0.35, "vdsat": 0.2, "gmoverid": gm / abs(ids),
               "qg": qg, "cgg": cgg, "qd": qd, "cgd": cgd}
        out.append(row)
    return out


def write_slices(data_dir: Path, plans, charge=True, ft=False):
    """plans → <data_dir>/<deck_id>/dc.csv（列名带实例前缀）。"""
    for p in plans:
        d = data_dir / p["deck_id"]
        d.mkdir(parents=True, exist_ok=True)
        rows_n = len(synth_rows(p["fet"], p["vds"], 1.0))
        table = [dict() for _ in range(rows_n)]
        for ci, c in enumerate(p["cells"]):
            w = qc.parse_len(c["W"])
            sigs = synth_rows(p["fet"], c["vds"], w)
            for i, r in enumerate(sigs):
                for k, v in r.items():
                    key = k if k in ("sweep", "g") else f"{c['iname']}:{k}"
                    if key.endswith(("qg", "qd", "cgg", "cgd")) and not charge:
                        continue
                    table[i][key] = v
            if ft:
                for r in table:
                    cgs, cgb = w * GG * 0.8, w * GG * 0.2
                    cgdv = w * abs(GD)
                    r[f"{c['iname']}:cgs"], r[f"{c['iname']}:cgd"], r[f"{c['iname']}:cgb"] = cgs, cgdv, cgb
                    r[f"{c['iname']}:ft"] = r[f"{c['iname']}:gm"] / (
                        2 * math.pi * (cgs + cgdv + cgb))
        cols = list(table[0].keys())
        with (d / "dc.csv").open("w", newline="", encoding="utf-8") as f:
            wr = csv.writer(f)
            wr.writerow(cols)
            for r in table:
                wr.writerow([f"{r[k]:.12g}" for k in cols])


class TestParseLen(unittest.TestCase):
    def test_units(self):
        self.assertAlmostEqual(qc.parse_len("1u"), 1e-6)
        self.assertAlmostEqual(qc.parse_len("0.18u"), 1.8e-7)
        self.assertAlmostEqual(qc.parse_len("10n"), 1e-8)
        self.assertAlmostEqual(qc.parse_len("2e-6"), 2e-6)
        self.assertAlmostEqual(qc.parse_len("3"), 3.0)
        with self.assertRaises(ValueError):
            qc.parse_len("abc")


class TestQcBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.prof = load_profile(GEN)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def plans_n(self, charge=True):
        plans = engine.make_plan(self.prof, "pair_01", "n",
                                 ["1u", "2u"], ["1u", "2u"], [1.5, 2.5],
                                 vstep=0.1, include_charge=charge)
        write_slices(self.tmp, plans, charge=charge)
        return plans

    def run_qc(self, plans, **kw):
        return qc.run_qc(self.prof, plans, self.tmp, convert=False, **kw)


class TestCleanPass(TestQcBase):
    def test_n_grid_pass(self):
        rep = self.run_qc(self.plans_n())
        self.assertEqual(rep["completeness"]["loaded"], 4)
        self.assertEqual(rep["verdict"], "PASS", rep["hard_hits"] + rep["soft_notes"])
        for key in ("I1_gm_vs_dJ_dvgs", "I2_gds_vs_dJ_dvds"):
            self.assertGreater(rep[key]["n"], 0)
            self.assertLess(rep[key]["max"], 1e-10, key)
        self.assertLess(rep["sentinel_gmoverid"]["stat"]["max"], 1e-10)
        self.assertLess(rep["I4_charge_cross"]["qg_cgg"]["max"], 1e-10)
        self.assertIn("skipped", rep["I3_gmb"])

    def test_p_grid_pass(self):
        plans = engine.make_plan(self.prof, "pair_01", "p",
                                 ["1u"], ["1u", "2u"], [-1.5, -2.5], vstep=0.1)
        write_slices(self.tmp, plans)
        rep = self.run_qc(plans)
        self.assertEqual(rep["verdict"], "PASS", rep["hard_hits"])
        self.assertGreater(rep["I1_gm_vs_dJ_dvgs"]["n"], 0)  # 方向锚=带符号恒正, 不误杀 p
        self.assertLess(rep["I2_gds_vs_dJ_dvds"]["max"], 1e-10)  # 负锚弦差同闸
        self.assertLess(rep["I4_charge_cross"]["qg_cgg"]["max"], 1e-10)

    def test_ft_measured_mode(self):
        plans = self.plans_n()
        for p in plans:  # 补 ft/C 族列
            write_slices(self.tmp, [p], ft=True)
        prof = json.loads(json.dumps(self.prof))
        prof["qc"] = {"ft_source": "measured"}
        rep = qc.run_qc(prof, plans, self.tmp, convert=False)
        self.assertEqual(rep["verdict"], "PASS", rep["hard_hits"])
        s = rep["I4_ft_measured"]
        self.assertGreater(s["n"], 0)
        self.assertLess(s["max"], 1e-6)

    def test_ft_measured_config_mismatch(self):
        plans = self.plans_n()  # 无 ft/cgb 列
        prof = json.loads(json.dumps(self.prof))
        prof["qc"] = {"ft_source": "measured"}
        rep = qc.run_qc(prof, plans, self.tmp, convert=False)
        self.assertEqual(rep["verdict"], "FAIL")
        self.assertTrue(any("申报与列面不符" in h for h in rep["hard_hits"]))


class TestMinefields(TestQcBase):
    def _tamper(self, plan, dst, src):
        """把 src 列值抄进 dst 列（模拟列位串扰）。"""
        f = self.tmp / plan["deck_id"] / "dc.csv"
        with f.open(encoding="utf-8") as fh:
            rows = list(csv.reader(fh))
        hdr = rows[0]
        i, j = hdr.index(src), hdr.index(dst)
        for r in rows[1:]:
            r[j] = r[i]
        with f.open("w", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerows(rows)

    def test_swap_gm_gds_breaks_i1(self):
        plans = self.plans_n()
        self._tamper(plans[0], "M1:gm", "M1:gds")   # gm 列被 gds 值顶掉
        rep = self.run_qc(plans)
        self.assertEqual(rep["verdict"], "FAIL")
        self.assertTrue(any("I1" in h for h in rep["hard_hits"]))
        self.assertTrue(any("片级" in h and plans[0]["deck_id"] in h for h in rep["hard_hits"]))

    def test_sentinel_tamper(self):
        plans = self.plans_n()
        f = self.tmp / plans[1]["deck_id"] / "dc.csv"
        with f.open(encoding="utf-8") as fh:
            rows = list(csv.reader(fh))
        j = rows[0].index("M1:gmoverid")
        for r in rows[1:]:
            r[j] = f"{float(r[j]) * 1.001:.12g}"
        with f.open("w", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerows(rows)
        rep = self.run_qc(plans)
        self.assertTrue(any("哨兵" in h for h in rep["hard_hits"]))

    def test_width_mixslice_breaks_i5(self):
        plans = self.plans_n()
        f = self.tmp / plans[0]["deck_id"] / "dc.csv"
        with f.open(encoding="utf-8") as fh:
            rows = list(csv.reader(fh))
        src, dst = rows[0].index("M1:gmoverid"), rows[0].index("M2:gmoverid")
        for r in rows[1:]:
            r[dst] = f"{float(r[src]) * 4:.12g}"  # 整列跳位=漂移 0.6 ⇒ 混片级
        with f.open("w", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerows(rows)
        rep = self.run_qc(plans)
        self.assertTrue(any("I5 混片级" in h for h in rep["hard_hits"]))

    def test_missing_slice_fail_then_partial_warn(self):
        plans = self.plans_n()
        shutil.rmtree(self.tmp / plans[3]["deck_id"])
        rep = self.run_qc(plans)
        self.assertEqual(rep["verdict"], "FAIL")
        rep2 = self.run_qc(plans, partial=True)
        self.assertEqual(rep2["verdict"], "WARN")

    def test_nan_corrupt_reject(self):
        plans = self.plans_n()
        f = self.tmp / plans[0]["deck_id"] / "dc.csv"
        with f.open(encoding="utf-8") as fh:
            rows = list(csv.reader(fh))
        j = rows[0].index("M1:gm")
        for r in rows[1:7]:
            r[j] = ""
        with f.open("w", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerows(rows)
        rep = self.run_qc(plans)
        self.assertTrue(any("NaN" in h for h in rep["hard_hits"]))

    def test_dedup_collision_reported(self):
        plans = self.plans_n()
        dup = dict(plans[0])
        dup["deck_id"] = plans[0]["deck_id"] + ".dup"
        plans.append(dup)
        shutil.copytree(self.tmp / plans[0]["deck_id"], self.tmp / dup["deck_id"])
        rep = self.run_qc(plans)
        self.assertEqual(rep["verdict"], "WARN")
        self.assertTrue(any("指纹撞车" in s for s in rep["soft_notes"]))

    def test_rowcount_mismatch(self):
        plans = self.plans_n()
        f = self.tmp / plans[2]["deck_id"] / "dc.csv"
        with f.open(encoding="utf-8") as fh:
            rows = list(csv.reader(fh))
        with f.open("w", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerows(rows[:-2])
        rep = self.run_qc(plans)
        self.assertTrue(any("行数不齐" in h for h in rep["hard_hits"]))


QCP, QGP, QBETA, QEPS = 0.888e-9, -0.44e-9, 5e-9, -3e-12  # 勿撞 ALPHA/BETA


def write_shell_slices(data_dir, plans, mode="normal", vstep=0.04, n=26):
    """壳层形制切片：q 系列含曲率、c 系列=q 斜率+寄生(±调制)。

    normal: qg=W(γu+βu²+q0), cgg=W(γ+2βu+cp) ⇒ |c−dq/dv|≡W·cp 常数寄生；
            qd 同式(ε<0 ⇒ 斜率恒负不穿零), cgd=斜率+gp ⇒ rho(|fd|,|c|)=+1。
    mirror: cgd 幅值随 u 递减（与 |fd| 反向）⇒ rho=−1 串列案。
    flat_cross: 线性族+cgg=3Wγ 常数错位 ⇒ flat 层裸恒等抓（v1 向后兼容）。
    """
    flat = mode == "flat_cross"
    if flat:
        vstep, n = 0.1, 11
    for pl in plans:
        d = data_dir / pl["deck_id"]
        d.mkdir(parents=True, exist_ok=True)
        table = [dict() for _ in range(n)]
        for c in pl["cells"]:
            w = qc.parse_len(c["W"])
            sigs = synth_rows(pl["fet"], c["vds"], w, vstep=vstep, n=n)
            for i, r in enumerate(sigs):
                u = abs(r["g"])
                # 二次电荷的中心差分精确=一次斜率式（等步长网格）
                sl_g = GG + (0 if flat else 2 * QBETA * u)
                sl_d = GD + (0 if flat else 2 * QEPS * u)
                r["qg"] = w * (GG * u + (0 if flat else QBETA * u * u) + Q0)
                r["qd"] = w * (GD * u + (0 if flat else QEPS * u * u) + Q1)
                r["cgg"] = w * (3 * GG if flat else sl_g + QCP)
                r["cgd"] = w * (GD if flat else
                                (-(2.2e-11 - 6e-12 * u) if mode == "mirror"
                                 else sl_d + QGP))
                for k, v in r.items():
                    key = k if k in ("sweep", "g") else f"{c['iname']}:{k}"
                    table[i][key] = v
        cols = list(table[0].keys())
        with (d / "dc.csv").open("w", newline="", encoding="utf-8") as f:
            wr = csv.writer(f)
            wr.writerow(cols)
            for row in table:
                wr.writerow([f"{row[k]:.14g}" for k in cols])


class TestI4Shell(TestQcBase):
    def shell_run(self, mode="normal"):
        plans = engine.make_plan(self.prof, "pair_01", "n", ["1u"], ["1u", "2u"],
                                 [1.5] if mode == "flat_cross" else [1.5, 2.5],
                                 vstep=0.1 if mode == "flat_cross" else 0.04,
                                 include_charge=True)
        write_shell_slices(self.tmp, plans, mode=mode)
        return plans, qc.run_qc(self.prof, plans, self.tmp, convert=False)

    def test_shell_parasitic_constant_passes(self):
        """实弹回归锁：∝W 固定寄生帽不再被当数据误差（10-09 八片误判案）。"""
        plans, rep = self.shell_run()
        self.assertEqual(rep["verdict"], "PASS", rep["hard_hits"])
        g = rep["I4_charge_cross"]["qg_cgg"]
        self.assertLess(g["max"], 1e-8)
        self.assertGreater(rep["I4_charge_cross"]["rho_qg"]["median"], 0.99)
        par = {q["inst"]: q["b"] for q in rep["I4_charge_cross"]["parasitic_gg"]}
        self.assertAlmostEqual(par["M2"] / par["M1"], 2.0, places=9)   # W 线性签名

    def test_shell_mirror_qd_rho_collapse_fails(self):
        """壳层串列：|fd| 升 vs |cgd| 降 ⇒ rho=−1 ⇒ I4qr 硬闸必炸。"""
        plans, rep = self.shell_run(mode="mirror")
        self.assertEqual(rep["verdict"], "FAIL")
        self.assertTrue(any("趋势相关性塌" in h and "qd_cgd" in h
                            for h in rep["hard_hits"]), rep["hard_hits"])
        self.assertIn("I4qr", rep["deck_flags"][plans[0]["deck_id"]]["hard"])

    def test_flat_raw_const_cross_still_caught(self):
        """flat 层向后兼容：两系列皆常数、cgg 错位 3× ⇒ 裸 relerr 抓 I4q。"""
        plans, rep = self.shell_run(mode="flat_cross")
        self.assertEqual(rep["verdict"], "FAIL")
        self.assertTrue(any("qg_cgg" in h for h in rep["hard_hits"]))
        self.assertIn("I4q", rep["deck_flags"][plans[0]["deck_id"]]["hard"])


if __name__ == "__main__":
    unittest.main()
