# -*- coding: utf-8 -*-
"""M1 金标准离线测试（发表安全版）。

主测试用 profiles/EXAMPLE_generic_mos_spectre.json（虚构器件名/路径，可随发行物）。
真厂商画像金标准=集成测试 test_vendor_golden，仅当本地 profiles_local/ 在位才跑，
公开仓无该目录即自动 skip，真实 PDK 字符串不进发行物。
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from collector.deck_gen import default_ramp, render_batch_deck, render_curve_deck  # noqa: E402
from collector.profile import ProfileError, load_profile, validate_profile  # noqa: E402

GEN = ROOT / "profiles" / "EXAMPLE_generic_mos_spectre.json"
LIB = "/path/to/pdk/models/spe/generic_mos.lib"

GOLDEN_GENERIC = f"""simulator lang=spectre
include "{LIB}" section=setup
include "{LIB}" section=tt
M1 (d g 0 0) nch_example w=1u l=1u m=1 nf=1
V1 (d 0) vsource dc=2.5
V2 (g 0) vsource dc=0
simulatorOptions options reltol=1e-3 vabstol=1e-6 iabstol=1e-12 temp=27 tnom=27 gmin=1e-12 maxwarns=5
saveOptions options save=allpub
save M1:ids M1:gm M1:gds M1:gmb M1:gmoverid M1:vth M1:vdsat
dc dc dev=V2 param=dc start=0 stop=5 step=0.1
"""


class TestMultiInstance(unittest.TestCase):
    """M1-v2 多实例单分析形制（SFE-691 改道）金标准。"""

    @classmethod
    def setUpClass(cls):
        cls.prof = load_profile(GEN)

    def test_batch_golden(self):
        res = render_batch_deck(self.prof, "pair_01", "n",
                                [{"L": "1u", "W": "1u", "vds": 2.5},
                                 {"L": "1u", "W": "2u", "vds": 1.5}],
                                ramp=(0, 5, 0.1))
        golden = f"""simulator lang=spectre
include "{LIB}" section=setup
include "{LIB}" section=tt
M1 (d1 g 0 0) nch_example w=1u l=1u m=1 nf=1
V1 (d1 0) vsource dc=2.5
M2 (d2 g 0 0) nch_example w=2u l=1u m=1 nf=1
V2 (d2 0) vsource dc=1.5
V3 (g 0) vsource dc=0
simulatorOptions options reltol=1e-3 vabstol=1e-6 iabstol=1e-12 temp=27 tnom=27 gmin=1e-12 maxwarns=5
saveOptions options save=allpub
save M1:ids M1:gm M1:gds M1:gmb M1:gmoverid M1:vth M1:vdsat M2:ids M2:gm M2:gds M2:gmb M2:gmoverid M2:vth M2:vdsat
dc dc dev=V3 param=dc start=0 stop=5 step=0.1
"""
        self.assertEqual(res["text"], golden)
        self.assertEqual(res["sweep_dev"], "V3")
        self.assertEqual([c["iname"] for c in res["cells"]], ["M1", "M2"])
        self.assertEqual([c["dnet"] for c in res["cells"]], ["d1", "d2"])
        self.assertEqual([c["vds_src"] for c in res["cells"]], ["V1", "V2"])

    def test_batch_polarity_and_charge(self):
        res = render_batch_deck(self.prof, "pair_01", "p",
                                [{"L": "1u", "W": "1u", "vds": -2.5},
                                 {"L": "1u", "W": "1u", "vds": -0.5}],
                                vstep=0.01, include_charge=True)
        self.assertIn("dc dc dev=V3 param=dc start=0 stop=-5 step=-0.01", res["text"])
        self.assertIn("V1 (d1 0) vsource dc=-2.5", res["text"])
        save = [l for l in res["text"].splitlines() if l.startswith("save ")][0]
        self.assertEqual(save.count(":"), 30)  # 2 实例 × (7dc+8charge)

    def test_batch_empty_cells(self):
        with self.assertRaises(ValueError):
            render_batch_deck(self.prof, "pair_01", "n", [])


class TestCollector(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.prof = load_profile(GEN)

    def test_golden_generic(self):
        res = render_curve_deck(self.prof, "pair_01", "n", "1u", "1u",
                                [2.5], ramp=(0, 5, 0.1))
        self.assertEqual(res["text"], GOLDEN_GENERIC)
        self.assertEqual(res["analyses"], [{"name": "dc", "vds": 2.5}])

    def test_multi_anchor_rejected(self):
        with self.assertRaises(NotImplementedError):
            render_curve_deck(self.prof, "pair_01", "n", "1u", "1u",
                              [2.5, 1.5, 0.5], ramp=(0, 5, 0.1))

    def test_polarity_p(self):
        v0, v1, vs = default_ramp(self.prof["pairs"]["pair_01"], "p", 0.01)
        self.assertEqual((v0, v1, vs), (0, -5, -0.01))
        res = render_curve_deck(self.prof, "pair_01", "p", "1u", "1u", [-2.5],
                                vstep=0.01, include_charge=True)
        self.assertIn("M1 (d g 0 0) pch_example w=1u l=1u m=1 nf=1", res["text"])
        save = [l for l in res["text"].splitlines() if l.startswith("save ")][0]
        self.assertEqual(save.count(":"), 15)
        self.assertIn("dc dc dev=V2 param=dc start=0 stop=-5 step=-0.01", res["text"])

    def test_profile_rejects_missing_key(self):
        bad = dict(self.prof)
        bad.pop("pairs")
        with self.assertRaises(ProfileError):
            validate_profile(bad, source="mem")

    def test_no_secret_literals_in_public_profile(self):
        # 发表红线自检·两路:
        # ① PDK 指纹=全文扫描。清单本身也是情报, 故住 gitignore 的
        #    profiles_local/fingerprint_terms.txt(一行一词); 公开仓无此文件 ⇒ 该路 skip。
        text = GEN.read_text(encoding="utf-8").lower()
        terms_file = ROOT / "profiles_local" / "fingerprint_terms.txt"
        if terms_file.is_file():
            for fp in (w.strip().lower() for w in terms_file.read_text(encoding="utf-8").splitlines() if w.strip()):
                self.assertNotIn(fp, text, f"通用画像泄露 PDK 指纹: {fp}")
        # ② 连接身份=字段级校验(必须仍是占位符, 不依赖指纹清单)
        import json
        conn = json.loads(GEN.read_text(encoding="utf-8"))["connection"]
        self.assertEqual(conn["host"], "YOUR_SIM_HOST")
        self.assertEqual(conn["user"], "YOUR_USER")
        self.assertNotIn(":", conn["host"])  # 不得含真实 IP/端口


if __name__ == "__main__":
    unittest.main()
