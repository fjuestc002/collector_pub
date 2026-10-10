# -*- coding: utf-8 -*-
"""M4-4d QC 指纹台账测试：行形制、零浮点自守、片级旗标贯通。

复用 test_qc 的合成网格夹具（n 管 2L×2W×2锚 → 4 片×11 行）。
"""
import csv
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from collector import engine, qc, qc_ledger  # noqa: E402
from collector.profile import load_profile  # noqa: E402
from test_qc import write_slices  # noqa: E402  (discover 会把 tests/ 入 path)

GEN = ROOT / "profiles" / "EXAMPLE_generic_mos_spectre.json"


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.prof = load_profile(GEN)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.plans = engine.make_plan(self.prof, "pair_01", "n",
                                      ["1u", "2u"], ["1u", "2u"], [1.5, 2.5],
                                      vstep=0.1, include_charge=True)
        write_slices(self.tmp, self.plans)

    def report(self):
        return qc.run_qc(self.prof, self.plans, self.tmp, convert=False)

    def rows(self, rep=None):
        return qc_ledger.build_rows(self.prof, self.plans, rep or self.report(),
                                    "t", plan_hash="ph123")


class TestRows(Base):
    def test_shape_and_counts(self):
        rows = self.rows()
        self.assertEqual(len(rows), 1 + len(self.plans))
        run = rows[0]
        self.assertEqual(run["ev"], "qc_run")
        self.assertEqual(run["verdict"], "PASS")
        self.assertEqual((run["planned"], run["loaded"]), (4, 4))
        for k in ("missing", "no_csv", "corrupt", "short", "hard", "soft"):
            self.assertEqual(run[k], 0, k)
        for d in rows[1:]:
            self.assertEqual(d["ev"], "qc_deck")
            self.assertEqual(d["status"], "inspected")
            self.assertEqual(d["rows"], 11)
            self.assertEqual(d["gates_hard"], [])
            self.assertEqual(d["deck_sha256"],
                             next(p["deck_sha256"] for p in self.plans
                                  if p["deck_id"] == d["deck_id"]))

    def test_profile_fingerprint_not_leaked(self):
        """run 行禁带 profile 原名（10-09 实弹自咬案的回归锁）。
        词面运行期拼接，防本测试文件被红线扫描器自咬。"""
        zx = "vx" + "d0r" + "_9p9"
        prof = json.loads(json.dumps(self.prof))
        prof["name"] = zx
        rep = qc.run_qc(prof, self.plans, self.tmp, convert=False)
        rows = qc_ledger.build_rows(prof, self.plans, rep, "t")
        blob = json.dumps(rows)
        self.assertNotIn(zx, blob)
        self.assertNotIn(chr(34) + "profile" + chr(34) + ":", blob)
        d = rows[0]["profile_digest"]
        self.assertEqual(len(d), 16)
        import hashlib
        self.assertEqual(d, hashlib.sha256(zx.encode("utf-8")).hexdigest()[:16])

    def test_zero_float_guard(self):
        rows = self.rows()
        qc_ledger._no_float(rows[0])                       # 应无声通过
        bad = dict(rows[0]); bad["leak"] = 0.5
        with self.assertRaises(ValueError):
            qc_ledger._no_float(bad)
        with self.assertRaises(ValueError):
            qc_ledger.append(self.tmp / "x.jsonl", [bad])
        self.assertFalse((self.tmp / "x.jsonl").exists())  # 响亮拒=不落半行

    def test_thresholds_sha_stable(self):
        t = qc.qc_thresholds(self.prof)
        self.assertEqual(qc_ledger.thresholds_sha(t), qc_ledger.thresholds_sha(dict(reversed(list(t.items())))))

    def test_append_roundtrip(self):
        p = self.tmp / "ledgers" / "qc.jsonl"
        self.assertEqual(qc_ledger.append(p, self.rows()), 5)
        qc_ledger.append(p, self.rows())
        lines = p.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 10)
        import json
        self.assertEqual(json.loads(lines[1])["ev"], "qc_deck")


class TestStatusFlow(Base):
    def test_missing_deck_status(self):
        shutil.rmtree(self.tmp / self.plans[3]["deck_id"])
        rows = {r["deck_id"]: r for r in self.rows() if r.get("ev") == "qc_deck"}
        self.assertEqual(rows[self.plans[3]["deck_id"]]["status"], "missing")
        self.assertIsNone(rows[self.plans[3]["deck_id"]]["rows"])
        self.assertEqual(self.rows(self.report())[0]["verdict"], "FAIL")

    def test_sentinel_gate_names_deck(self):
        f = self.tmp / self.plans[1]["deck_id"] / "dc.csv"
        rows = list(csv.reader(f.open(encoding="utf-8")))
        j = rows[0].index("M1:gmoverid")
        for r in rows[1:]:
            r[j] = f"{float(r[j]) * 1.001:.12g}"
        with f.open("w", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerows(rows)
        rep = self.report()
        dr = {r["deck_id"]: r for r in self.rows(rep) if r.get("ev") == "qc_deck"}
        self.assertIn("SENT", dr[self.plans[1]["deck_id"]]["gates_hard"])
        self.assertNotIn("SENT", dr[self.plans[0]["deck_id"]]["gates_hard"])
        self.assertTrue(any("哨兵" in h and self.plans[1]["deck_id"] in h
                            for h in rep["hard_hits"]))

    def test_i2_gate_names_both_anchor_decks(self):
        # 整片 gds 列位移（全部实例 ×2）=串列案典型；I2 判级=相邻锚对，
        # 锚对命中把两片都点名（含健康伙伴片=连坐，弦差本质跨片故无法单方归因）
        f = self.tmp / self.plans[2]["deck_id"] / "dc.csv"
        rows = list(csv.reader(f.open(encoding="utf-8")))
        js = [j for j, h in enumerate(rows[0]) if h.endswith(":gds")]
        for r in rows[1:]:
            for j in js:
                r[j] = f"{float(r[j]) * 2:.12g}"
        with f.open("w", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerows(rows)
        rep = self.report()
        dr = {r["deck_id"]: r for r in self.rows(rep) if r.get("ev") == "qc_deck"}
        self.assertIn("I2", dr[self.plans[2]["deck_id"]]["gates_hard"])
        self.assertIn("I2", dr[self.plans[3]["deck_id"]]["gates_hard"])
        self.assertTrue(any("I2 邻弦" in h for h in rep["hard_hits"]))


if __name__ == "__main__":
    unittest.main()
