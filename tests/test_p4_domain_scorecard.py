# -*- coding: utf-8 -*-
"""P4.3/P4.4/P4.5 收集器侧离线测试（10-10 "把 P4 做完"令，设计 §2/§3/§0）。

判别面：
  · profile domain：形状校验（soa 拒/反序拒/未知键拒）、domain_of 归一
    （后缀→米、缺省 tied/manual）、EXAMPLE 在案；
  · pack 半边纯函数：gap_blocks_of（边块+vbs 接法块）、check_domain_lamp
    （覆盖越申报=响亮点名）、write_manifest 入册（domain+gap_blocks）；
  · backflow ingest：densify 并轨（origin=domain、L token、在案去重幂等）、
    gap 候块账（model 无配/多 pair 歧义/gap kind 三路）、坏行计数；
  · scorecard：四栏装配+三盏灯（合成账全绿⇒freeze_ready；红牌>0⇒灯二灭；
    缺源=无账不冒充绿）；spearman 对已知秩对判 1/-1。
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from collector import npz_pack, scorecard as sc          # noqa: E402
from collector.backflow import fmt_len_token, ingest_backflow  # noqa: E402
from collector.profile import (ProfileError, declared_domain,  # noqa: E402
                               domain_of, load_profile, validate_domain)

GEN = ROOT / "profiles" / "EXAMPLE_generic_mos_spectre.json"


class TestDomainProfile(unittest.TestCase):
    def test_example_in_manifest_shape(self):
        prof = load_profile(GEN)
        d = domain_of(prof, "pair_01", "n")
        self.assertTrue(declared_domain(d))
        self.assertAlmostEqual(d["L_range"][0], 130e-9)
        self.assertAlmostEqual(d["L_range"][1], 10e-6)   # 后缀归一=米
        self.assertEqual(d["vbs_topology"], "tied")
        self.assertEqual(d["source"], "manual")

    def test_undeclared_is_empty_shape(self):
        prof = load_profile(GEN)
        d = domain_of(prof, "pair_01", "p")               # p 侧没填
        self.assertFalse(declared_domain(d))
        self.assertEqual(d["vbs_topology"], "tied")       # 缺省形态齐

    def test_rejects(self):
        base = {"L_range": ["130n", "10u"]}
        with self.assertRaises(ProfileError):
            validate_domain(dict(base, source="soa"), "t")
        with self.assertRaises(ProfileError):
            validate_domain({"L_range": [10e-6, 130e-9]}, "t")   # 反序
        with self.assertRaises(ProfileError):
            validate_domain(dict(base, bogus=1), "t")            # 未知键
        with self.assertRaises(ProfileError):
            validate_domain(dict(base, vbs_topology="floating"), "t")

    def test_bad_profile_file_rejected(self):
        tmp = Path(tempfile.mkdtemp())
        prof = json.loads(GEN.read_text(encoding="utf-8"))
        prof["pairs"]["pair_01"]["n"]["domain"]["W_range"] = ["50u", "0.29u"]
        f = tmp / "bad.json"
        f.write_text(json.dumps(prof), encoding="utf-8")
        with self.assertRaises(ProfileError):
            load_profile(f)


class TestGapAndLamp(unittest.TestCase):
    DOM = {"L_range": [100e-9, 20e-6], "W_range": [0.2e-6, 100e-6],
           "vds_range": [0.0, 3.3], "vgs_range": [0.0, 3.3],
           "vbs_topology": "tied", "source": "manual",
           "corner": "tt", "temp_c": 27.0}
    COV = {"Ls": [1e-6, 2e-6], "ws": [1e-6, 2e-6], "vdss": [1.5, 2.5],
           "vbss": [0.0, 0.0], "vg_lo": 0.0, "vg_hi": 1.0}

    def test_gap_blocks_edges_and_topology(self):
        g = npz_pack.gap_blocks_of(self.DOM, self.COV, 1.0)
        ax = {(x["axis"], x["kind"]) for x in g}
        self.assertIn(("L", "edge"), ax)          # 100n–1µ 与 2µ–20µ 两头
        self.assertIn(("vds", "edge"), ax)        # 0–1.5 与 2.5–3.3
        self.assertIn(("vgs", "edge"), ax)        # 1.0–3.3
        self.assertNotIn(("vbs", "topology"), ax)  # tied 申报无 vbs 块
        body = dict(self.DOM, vbs_topology="body")
        g2 = npz_pack.gap_blocks_of(body, self.COV, 1.0)
        self.assertIn(("vbs", "topology"),
                      {(x["axis"], x["kind"]) for x in g2})

    def test_lamp_one_violation_named(self):
        cov = dict(self.COV, Ls=[1e-6, 30e-6])   # 30µ > 申报 20µ
        probs = npz_pack.check_domain_lamp("dev", self.DOM, cov, 1.0)
        self.assertEqual(len(probs), 1)
        self.assertIn("L", probs[0])
        self.assertEqual(npz_pack.check_domain_lamp("dev", self.DOM,
                                                    self.COV, 1.0), [])
        self.assertEqual(npz_pack.check_domain_lamp(
            "dev", {}, self.COV, 1.0), [])       # 未申报⇒不判

    def test_manifest_carries_domain_and_gaps(self):
        tmp = Path(tempfile.mkdtemp())
        mem = tmp / "mem"
        mem.mkdir()
        (mem / "nch_example_v1J.npz").write_bytes(b"z")
        prof = load_profile(GEN)
        b = {"f": 1.0, "Ls": [1e-6, 2e-6], "ws": [1e-6, 2e-6],
             "vdss": [1.5, 2.5], "vbss": [0.0, 0.0], "curves": 4,
             "domain": domain_of(prof, "pair_01", "n")}
        b["gap_blocks"] = npz_pack.gap_blocks_of(b["domain"], b, 1.0)
        f = npz_pack.write_manifest(tmp, prof, "pair_01",
                                    {"nch_example": b}, [], True,
                                    mem_dir=mem)
        mf = json.loads(f.read_text(encoding="utf-8"))
        ent = mf["devices"]["nch_example"]
        self.assertAlmostEqual(ent["domain"]["L_range"][0], 130e-9)
        self.assertAlmostEqual(ent["domain"]["L_range"][1], 10e-6)
        self.assertTrue(ent["gap_blocks"])


class TestIngest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.prof = load_profile(GEN)
        self.q = self.tmp / "densify.jsonl"
        self.g = self.tmp / "gap_blocks.jsonl"

    def _bf(self, rows):
        f = self.tmp / "backflow.jsonl"
        f.write_text("".join(json.dumps(r) + "\n" for r in rows),
                     encoding="utf-8")
        return f

    def test_densify_and_gap_routing_and_idempotent(self):
        f = self._bf([
            {"ev": "backflow", "origin": "domain", "kind": "densify",
             "model": "nch_example", "axis": "vds", "L": 1e-6, "W": 2e-6,
             "span": [3.3, 3.9], "n": 2, "detail": "vds=3.9 出轴 [0.1,3.3]"},
            {"ev": "backflow", "origin": "domain", "kind": "gap_block",
             "model": "nch_example", "axis": "W", "L": 1.2e-6, "W": 12.3e-6,
             "span": [None, None], "n": 1},
            {"ev": "backflow", "origin": "residual", "kind": "gap_block",
             "model": "nosuch_model", "axis": "point", "L": 1e-6, "W": 2e-6,
             "span": [None, None], "n": 1},
            {"ev": "junk"},
        ])
        rep = ingest_backflow(self.prof, f, self.q, gap_path=self.g,
                              out=lambda s: None)
        self.assertEqual(rep["added"], 1)
        self.assertEqual(rep["gap"], 2)
        self.assertEqual(rep["bad"], 1)
        rows = [json.loads(l) for l in
                self.q.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(rows[0]["ev"], "densify")
        self.assertEqual(rows[0]["origin"], "domain")
        self.assertEqual(rows[0]["pair"], "pair_01")
        self.assertEqual(rows[0]["fet"], "n")
        self.assertEqual(rows[0]["L"], "1u")            # 米→L token
        self.assertEqual(rows[0]["span"], [3.3, 3.9])
        self.assertEqual(rows[0]["n_hits"], 2)
        # 幂等：重放不重复入队、候块也去重
        rep2 = ingest_backflow(self.prof, f, self.q, gap_path=self.g,
                               out=lambda s: None)
        self.assertEqual(rep2["added"], 0)
        self.assertEqual(rep2["skipped_open"], 1)
        self.assertEqual(rep2["gap"], 0)

    def test_fmt_len_token(self):
        self.assertEqual(fmt_len_token(1e-6), "1u")
        self.assertEqual(fmt_len_token(1.2e-6), "1.2u")
        self.assertEqual(fmt_len_token(130e-9), "130n")
        self.assertEqual(fmt_len_token(None), "")


class TestScorecard(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def _files(self, *, hard=0, rho_ok=True):
        prof = load_profile(GEN)
        dom = domain_of(prof, "pair_01", "n")
        dom.update({"L_range": [0.5e-6, 4e-6], "W_range": [0.5e-6, 4e-6],
                    "vds_range": [0.5, 3.0], "vgs_range": [-0.2, 3.3]})
        mf = {"kind": "midas-pdk", "pdk": "example_pdk", "table_digest": "d" * 64,
              "devices": {"nch_example": {
                  "polarity": 1, "L_range": [1e-6, 2e-6], "W_nodes": [1e-6, 2e-6],
                  "vds_anchors": [1.5, 2.5], "vbs_nodes": [0.0],
                  "domain": dom, "gap_blocks": []}}}
        mfp = self.tmp / "manifest.json"
        mfp.write_text(json.dumps(mf), encoding="utf-8")
        interp = {"verdict": "PASS" if not hard else "FAIL",
                  "counts": {"pairs_checked": 2},
                  "hard_hits": ["x"] * hard,
                  "pair_stats": [{"key": ["pair_01", "n", "1u"],
                                  "gm_all": {"n": 5, "median": 0.004},
                                  "ids_all": {"n": 5, "median": 0.007}}]}
        ip = self.tmp / "i.interp.json"
        ip.write_text(json.dumps(interp), encoding="utf-8")
        led = self.tmp / "qc.jsonl"
        led.write_text("\n".join([
            json.dumps({"ev": "qc_run", "verdict": "PASS"}),
            json.dumps({"ev": "qc_deck", "deck_id": "a", "status": "inspected",
                        "gates_hard": [], "gates_soft": ["I4qdS"]}),
            json.dumps({"ev": "qc_deck", "deck_id": "b", "status": "inspected",
                        "gates_hard": [], "gates_soft": []}),
        ]) + "\n", encoding="utf-8")
        v = self.tmp / "verify_ledger.csv"
        a, b = (1, 2, 3, 4), (10, 20, 30, 40)
        if not rho_ok:
            b = (40, 30, 20, 10)
        v.write_text("gen,var_lm,obj_gain_sieve_raw,obj_gain_real_raw\n" +
                     "\n".join("%d,1e-6,%s,%s" % (i, s, r)
                               for i, (s, r) in enumerate(zip(a, b))) + "\n",
                     encoding="utf-8")
        return mfp, ip, led, v

    def test_all_green(self):
        mfp, ip, led, v = self._files(hard=0, rho_ok=True)
        rep = sc.build_scorecard(manifest_path=mfp, interp_path=ip,
                                 qc_ledger_path=led, verify_path=v)
        self.assertTrue(rep["cols"]["domain"]["lamp1"])
        self.assertTrue(rep["cols"]["interp"]["have"])
        self.assertEqual(rep["cols"]["goldpairs"]["objs"][0]["rho"], 1.0)
        self.assertTrue(rep["freeze_ready"], rep["lamps"])
        md = sc.render_md(rep)
        for kw in ("三盏灯", "① 域覆盖", "② interp", "③ 金对子", "④ 五闸",
                   "PM 不入判据"):
            self.assertIn(kw, md)
        self.assertNotIn("pm_deg", md.lower().replace(" ", ""))

    def test_lamp2_red_when_hard(self):
        mfp, ip, led, v = self._files(hard=1)
        rep = sc.build_scorecard(manifest_path=mfp, interp_path=ip,
                                 qc_ledger_path=led, verify_path=v)
        self.assertFalse(rep["lamps"]["L2_interp清零"])
        self.assertFalse(rep["freeze_ready"])

    def test_missing_sources_are_gray(self):
        mfp = self._files()[0]
        rep = sc.build_scorecard(manifest_path=mfp)
        self.assertFalse(rep["cols"]["interp"]["have"])
        self.assertFalse(rep["cols"]["goldpairs"]["have"])
        self.assertIsNone(rep["lamps"]["L3_金对子Sp过线"])
        self.assertFalse(rep["freeze_ready"])

    def test_spearman_anticorrelated(self):
        rho, n = sc.spearman([1, 2, 3, 4], [40, 30, 20, 10])
        self.assertAlmostEqual(rho, -1.0)
        self.assertIsNone(sc.spearman([1, 2], [1, 2])[0])
        self.assertIsNone(sc.spearman([1, 1, 1], [1, 2, 3])[0])   # 零方差


if __name__ == "__main__":
    unittest.main()
