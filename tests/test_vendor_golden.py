# -*- coding: utf-8 -*-
"""真实厂商 PDK 金标准（集成测试，非发行物）。

仅当本地存在 profiles_local/vendor_a.* 时运行；公开仓无此目录 ⇒ skip。
金标准与画像都住 gitignore 的 profiles_local/，本测试代码不含任何真实 PDK 字面量。
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from collector.deck_gen import render_curve_deck  # noqa: E402
from collector.profile import load_profile  # noqa: E402

LOCAL_PROF = ROOT / "profiles_local" / "vendor_a.json"
LOCAL_GOLDEN = ROOT / "profiles_local" / "vendor_a_golden.scs"


@unittest.skipUnless(LOCAL_PROF.is_file() and LOCAL_GOLDEN.is_file(),
                     "本地无厂商画像/金标准(非发行环境) ⇒ skip")
class TestVendorGolden(unittest.TestCase):
    def test_golden_matches_vm_p2a(self):
        prof = load_profile(LOCAL_PROF)
        res = render_curve_deck(prof, "pair_01", "n", "1u", "1u",
                                [2.5], ramp=(0, 5, 0.1))
        golden = LOCAL_GOLDEN.read_text(encoding="utf-8")
        self.assertEqual(res["text"], golden)


if __name__ == "__main__":
    unittest.main()
