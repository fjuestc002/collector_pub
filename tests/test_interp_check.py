# -*- coding: utf-8 -*-
"""M4-4c 插值误差闸离线测试。

合成模型取 ids/W = pol·A(u)·e^{k·vds}、gm = W·A'(u)·e^{k·vds}
（u=|vgs|，A=0.01u²+1e-3）——两列对 log|·| 都是 vds 的严格线性函数，
signed-log 中点插值**逐位精确**（干净 PASS 基线）；埋雷=对中点片按实例乘
因子（模拟粗格曲率不可忽略），超差必点名实例、跨度进队列。
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

from collector import engine, interp_check, qc  # noqa: E402
from collector.profile import load_profile  # noqa: E402

GEN = ROOT / "profiles" / "EXAMPLE_generic_mos_spectre.json"
K = 1.0


def rows_exp(fet, vds, W, vstep=0.1, n=11, gm_scale=1.0, ids_scale=1.0):
    pol = 1 if fet == "n" else -1
    vd = abs(vds)
    out = []
    for i in range(n):
        v = pol * i * vstep
        u = i * vstep
        A = 0.01 * u * u + 1e-3
        e = math.exp(K * vd)
        ids = pol * W * A * e * ids_scale
        gm = W * 0.02 * u * e * gm_scale
        out.append({"sweep": v, "g": v, "ids": ids, "gm": gm,
                    "gmoverid": abs(gm) / abs(ids) if ids else 0.0})
    return out


def write_exp(data_dir: Path, plans, scale_by_vds=None, vstep=0.1):
    """scale_by_vds: {round(vds,6): {iname: (gm_scale, ids_scale)}}。"""
    for p in plans:
        d = data_dir / p["deck_id"]
        d.mkdir(parents=True, exist_ok=True)
        sc = (scale_by_vds or {}).get(round(float(p["vds"]), 6), {})
        n = len(rows_exp(p["fet"], p["vds"], 1.0, vstep=vstep))
        table = [dict() for _ in range(n)]
        for c in p["cells"]:
            w = qc.parse_len(c["W"])
            gs, jss = sc.get(c["iname"], (1.0, 1.0))
            sigs = rows_exp(p["fet"], c["vds"], w, vstep=vstep,
                            gm_scale=gs, ids_scale=jss)
            for i, r in enumerate(sigs):
                for k, v in r.items():
                    key = k if k in ("sweep", "g") else f"{c['iname']}:{k}"
                    table[i][key] = v
        cols = list(table[0].keys())
        with (d / "dc.csv").open("w", newline="", encoding="utf-8") as f:
            wr = csv.writer(f)
            wr.writerow(cols)
            for r in table:
                wr.writerow([f"{r[k]:.14g}" for k in cols])


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.prof = load_profile(GEN)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def grids(self, coarse_anchors=(1.5, 2.5), Ls=("1u",), Ws=("1u", "2u"),
              fet="n", probe_scale=None, probe=True, vstep=0.1):
        coarse = engine.make_plan(self.prof, "pair_01", fet, list(Ls), list(Ws),
                                  list(coarse_anchors), vstep=vstep)
        write_exp(self.tmp, coarse, vstep=vstep)
        probe_plans = []
        if probe:
            mids = [m for _a, _b, m in interp_check.midpoint_pairs(coarse_anchors)]
            probe_plans = engine.make_plan(self.prof, "pair_01", fet, list(Ls),
                                           list(Ws), mids, vstep=vstep)
            write_exp(self.tmp, probe_plans, scale_by_vds=probe_scale, vstep=vstep)
        return coarse, probe_plans

    def check(self, coarse, probe_plans, **kw):
        return interp_check.run_interp_check(self.prof, coarse, probe_plans,
                                             self.tmp, convert=False, **kw)


class TestPureFuncs(Base):
    def test_midpoint_pairs(self):
        self.assertEqual(interp_check.midpoint_pairs([1.5, 2.5]), [(1.5, 2.5, 2.0)])
        self.assertEqual(interp_check.midpoint_pairs([2.5, 1.5, 1.5]),
                         [(1.5, 2.5, 2.0)])                       # 排序+去重
        self.assertEqual(interp_check.midpoint_pairs([-2.5, -1.5]),
                         [(-2.5, -1.5, -2.0)])                    # p 管负锚同式
        self.assertEqual(len(interp_check.midpoint_pairs([1.0, 1.5, 2.5])), 2)
        self.assertEqual(interp_check.midpoint_pairs([2.0]), [])

    def test_interp_log(self):
        self.assertAlmostEqual(interp_check.interp_log(2.0, 8.0), 4.0, places=9)
        self.assertAlmostEqual(interp_check.interp_log(-2.0, -8.0), -4.0, places=9)
        self.assertIsNone(interp_check.interp_log(2.0, -8.0))     # 异号拒
        self.assertIsNone(interp_check.interp_log(0.0, 8.0))
        self.assertIsNone(interp_check.interp_log(float("nan"), 1.0))

    def test_thresholds(self):
        self.assertEqual(interp_check.interp_thresholds(self.prof),
                         {"gm_tol": 0.01, "ids_tol": 0.02})
        prof = json.loads(json.dumps(self.prof))
        prof["qc"]["interp"] = {"gm_tol": 0.05}
        self.assertEqual(interp_check.interp_thresholds(prof)["gm_tol"], 0.05)
        prof["qc"]["interp"] = {"gm_tol": 5}                      # 越界=响亮拒
        with self.assertRaises(ValueError):
            interp_check.interp_thresholds(prof)


class TestCleanPass(Base):
    def test_n_grid_exact_log_linear_passes(self):
        coarse, probe_plans = self.grids()
        rep = self.check(coarse, probe_plans)
        self.assertEqual(rep["verdict"], "PASS", rep["hard_hits"] + rep["soft_notes"])
        self.assertEqual(rep["counts"]["pairs_checked"], 1)
        for e in rep["pair_stats"]:
            self.assertLess(e["gm_all"]["max"], 1e-12)
            self.assertLess(e["ids_all"]["max"], 1e-12)
        self.assertEqual(rep["queue_rows"], [])                   # 干净网格不刷队列

    def test_p_negative_grid_passes(self):
        coarse, probe_plans = self.grids((-2.5, -1.5), fet="p")
        rep = self.check(coarse, probe_plans)
        self.assertEqual(rep["verdict"], "PASS", rep["hard_hits"])
        self.assertLess(rep["pair_stats"][0]["ids_all"]["max"], 1e-12)

    def test_knee_ranking_without_magic_threshold(self):
        coarse, probe_plans = self.grids((1.0, 1.5, 2.5))
        rep = self.check(coarse, probe_plans)
        self.assertEqual(rep["verdict"], "PASS", rep["hard_hits"] + rep["soft_notes"])
        kf = rep["knee_by_family"]["pair_01|n|1u"]
        self.assertEqual(len(kf), 2)                              # 全锚对排序呈报
        self.assertEqual(rep["queue_rows"], [])                   # 膝区只进报告


class TestMinefields(Base):
    def test_gm_gate_fires_names_instance(self):
        coarse, probe_plans = self.grids(probe_scale={2.0: {"M1": (1.03, 1.0)}})
        rep = self.check(coarse, probe_plans)
        self.assertEqual(rep["verdict"], "FAIL")
        self.assertTrue(any("插值误差 gm" in h and "M1" in h for h in rep["hard_hits"]))
        self.assertFalse(any("M2" in h for h in rep["hard_hits"]
                             if "插值误差" in h))                 # 实例级判级不连坐
        q = rep["queue_rows"]
        self.assertEqual(len(q), 1)
        self.assertEqual(q[0]["reason"], "interp_gm")
        self.assertEqual(q[0]["instances"], ["M1"])
        self.assertEqual(q[0]["span"], [1.5, 2.5])
        self.assertEqual(q[0]["vds"], 2.0)
        self.assertEqual(q[0]["decks"][2], probe_plans[0]["deck_id"])

    def test_ids_gate_fires_gm_stays_clean(self):
        coarse, probe_plans = self.grids(probe_scale={2.0: {"M1": (1.0, 1.05)}})
        rep = self.check(coarse, probe_plans)
        self.assertEqual(rep["verdict"], "FAIL")
        self.assertTrue(any("插值误差 ids" in h for h in rep["hard_hits"]))
        self.assertFalse(any("插值误差 gm" in h for h in rep["hard_hits"]))
        self.assertEqual(rep["queue_rows"][0]["reason"], "interp_ids")

    def test_tolerance_override_silences(self):
        coarse, probe_plans = self.grids(probe_scale={2.0: {"M1": (1.03, 1.0)}})
        rep = self.check(coarse, probe_plans, gm_tol=0.10)
        self.assertEqual(rep["verdict"], "PASS", rep["hard_hits"])

    def test_probe_missing_hard_then_partial(self):
        coarse, probe_plans = self.grids(probe=False)
        rep = self.check(coarse, probe_plans)
        self.assertEqual(rep["verdict"], "FAIL")
        self.assertTrue(any("加密中点片未到" in h for h in rep["hard_hits"]))
        rep2 = self.check(coarse, probe_plans, partial=True)
        self.assertEqual(rep2["verdict"], "WARN")

    def test_single_anchor_na(self):
        coarse, probe_plans = self.grids((2.5,), probe=False)
        rep = self.check(coarse, probe_plans)
        self.assertEqual(rep["verdict"], "WARN")
        self.assertTrue(any("单 vds 锚" in s for s in rep["soft_notes"]))

    def test_vgs_misalign_skips_pair(self):
        # 加密批 vstep 与粗格不符（人祸场景）：机器判错位、呈报不硬炸
        coarse = engine.make_plan(self.prof, "pair_01", "n", ["1u"], ["1u", "2u"],
                                  [1.5, 2.5], vstep=0.1)
        write_exp(self.tmp, coarse, vstep=0.1)
        probe_plans = engine.make_plan(self.prof, "pair_01", "n", ["1u"], ["1u", "2u"],
                                       [2.0], vstep=0.1)
        write_exp(self.tmp, probe_plans, vstep=0.2)
        rep = self.check(coarse, probe_plans)
        self.assertEqual(rep["counts"]["vgs_misalign"], 1)
        self.assertEqual(rep["counts"]["pairs_checked"], 0)
        self.assertTrue(any("vgs 向量错位" in s for s in rep["soft_notes"]))

    def test_knee_orders_by_curvature(self):
        coarse, probe_plans = self.grids(
            (1.0, 1.5, 2.5), probe_scale={1.25: {"M1": (1.08, 1.0)},
                                          2.0: {"M1": (1.02, 1.0)}})
        rep = self.check(coarse, probe_plans)
        self.assertEqual(rep["verdict"], "FAIL")
        kf = rep["knee_by_family"]["pair_01|n|1u"]
        self.assertEqual(kf[0]["span"], [1.0, 1.5])               # 最弯段=埋雷更重段
        spans = {tuple(q["span"]) for q in rep["queue_rows"]}
        self.assertEqual(spans, {(1.0, 1.5), (1.5, 2.5)})         # 误差闸各自下队

    def test_zero_significance_soft(self):
        # 中点片 gm 置零 + ids 掉出显著段 ⇒ 该片该对零样本，呈报不硬炸
        coarse, probe_plans = self.grids()
        f = self.tmp / probe_plans[0]["deck_id"] / "dc.csv"
        rows = list(csv.reader(f.open(encoding="utf-8")))
        hdr = rows[0]
        for inst in ("M1", "M2"):
            jg, ji = hdr.index(f"{inst}:gm"), hdr.index(f"{inst}:ids")
            for r in rows[1:]:
                r[jg] = "0"
                r[ji] = f"{float(r[ji]) * 1e-9:.6g}"
        with f.open("w", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerows(rows)
        rep = self.check(coarse, probe_plans)
        self.assertTrue(any("零显著样本" in s for s in rep["soft_notes"]))
        self.assertEqual(rep["verdict"], "WARN")


class TestQueueAppend(Base):
    def test_roundtrip_and_shape(self):
        coarse, probe_plans = self.grids(probe_scale={2.0: {"M1": (1.03, 1.0)}})
        rep = self.check(coarse, probe_plans)
        p = self.tmp / "queue" / "densify.jsonl"
        self.assertEqual(interp_check.append_queue(p, rep["queue_rows"]), 1)
        interp_check.append_queue(p, rep["queue_rows"])
        lines = p.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        r = json.loads(lines[0])
        self.assertEqual(r["ev"], "densify")
        self.assertIsInstance(r["ts"], int)
        self.assertEqual(r["decks"][2], probe_plans[0]["deck_id"])
        json.dumps(rep)                                           # 报告整体可序列化

    def test_empty_rows_no_file(self):
        p = self.tmp / "queue" / "x.jsonl"
        self.assertEqual(interp_check.append_queue(p, []), 0)
        self.assertFalse(p.exists())


if __name__ == "__main__":
    unittest.main()
