# -*- coding: utf-8 -*-
"""P2 打包链离线测试（10-10）：dc.csv→v1 双 npz+manifest+方言闸。

合成模型（与 test_interp_check 同族谱）：J(u)=（0.01u²+1e-3)·e^{K|vds|}（A/m），
gm=W·0.02u·e^{K|vds|}，gds=0.01·|ids|，gmb=0.2gm，cgg=W(1e-12+1e-11u)，
cgs=−0.3cgg、cgd=−0.2cgg（IRIS 非对角负号实测形），cds=W·1e-14u。
vstep=0.1、u=0.5 为正格点 ⇒ np.interp/散点在节点处**逐位精确**，可做数值对账；
moi 真值=gm/|ids|=0.02u/(0.01u²+1e-3)，av0=2u/(0.01u²+1e-3)。

方言闸三层各配判别件：键破坏（ColNames/lJ 缺）、首字母反极性拒、vg 格不齐拒、
同点冲突拒、vbs 单点复制+出域响亮拒评、moi 双符号约定（f· 与 |·|）各自命中。
优化器包可导时跑真装载回环（BankJ/BankV），不可导则只验静态层（skipped 呈报）。
"""
import csv
import importlib.util
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from collector import engine, npz_pack  # noqa: E402
from collector.profile import load_profile  # noqa: E402
from collector.qc import parse_len  # noqa: E402

GEN = ROOT / "profiles" / "EXAMPLE_generic_mos_spectre.json"
K = 1.0
HAS_OPT = importlib.util.find_spec("optimization_tool") is not None


def A(u):
    return 0.01 * u * u + 1e-3


def j_true(u, vd):
    return A(u) * math.exp(K * abs(vd))


def moi_true(u):
    return 0.02 * u / A(u) if u > 0 else float("nan")


def av0_true(u):
    return 2.0 * u / A(u) if u > 0 else float("nan")


def rows_full(fet, vds, W, vstep=0.1, n=11, ids_scale=1.0, signed_gmoverid=False):
    """物理自洽合成片行（列名=IRIS 冒号制去前缀形态）。"""
    pol = 1 if fet == "n" else -1
    e = math.exp(K * abs(vds))
    out = []
    for i in range(n):
        v, u = pol * i * vstep, i * vstep
        ids = pol * W * A(u) * e * ids_scale
        gm = W * 0.02 * u * e
        cgg = W * (1e-12 + 1e-11 * u)
        moi = abs(gm) / abs(ids) if ids else 0.0
        if signed_gmoverid and pol < 0:
            moi = -moi                        # 真值=gm/ids raw 负（Cadence 原始比形态）
        out.append({"sweep": v, "g": v, "ids": ids, "gm": gm,
                    "gds": 0.01 * abs(ids),
                    "gmb": 0.2 * gm, "gmoverid": moi,
                    "vth": pol * 0.4, "vdsat": pol * (0.05 + 0.1 * u),
                    "cgg": cgg, "cgs": -0.3 * cgg, "cgd": -0.2 * cgg,
                    "cds": W * 1e-14 * u})
    return out


def write_decks(data_dir: Path, plans, vstep=0.1, ids_scale_by=None,
                signed_gmoverid=False):
    """plans → <data_dir>/<deck_id>/dc.csv 多实例宽表（列名与实弹 IRIS 同构）。"""
    for p in plans:
        d = data_dir / p["deck_id"]
        d.mkdir(parents=True, exist_ok=True)
        n = len(rows_full(p["fet"], p["vds"], 1.0, vstep=vstep))
        table = [dict() for _ in range(n)]
        for c in p["cells"]:
            w = parse_len(c["W"])
            jsc = (ids_scale_by or {}).get(p["deck_id"], 1.0)
            sigs = rows_full(p["fet"], c["vds"], w, vstep=vstep,
                             ids_scale=jsc, signed_gmoverid=signed_gmoverid)
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


def write_plans(plans, path: Path):
    path.write_text("".join(json.dumps({k: v for k, v in p.items() if k != "text"},
                                       ensure_ascii=False) + "\n" for p in plans),
                    encoding="utf-8")
    return path


class PackBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.prof_path = GEN
        cls.prof = load_profile(GEN)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.data = self.tmp / "data" / "pair_01"
        self.root = self.tmp / "_pdk"
        self.lines = []

    def mk_grids(self, fet="n", Ls=("1u", "2u"), Ws=("1u", "2u"),
                 vds=(1.5, 2.5), vstep=0.1):
        return engine.make_plan(self.prof, "pair_01", fet, list(Ls), list(Ws),
                                list(vds), vstep=vstep)

    def pack(self, fet="both", **kw):
        vstep = kw.pop("vstep", 0.1)
        ids_scale_by = kw.pop("ids_scale_by", None)
        signed = kw.pop("signed_gmoverid", False)
        n_plans = self.mk_grids("n")
        p_plans = self.mk_grids("p", vds=(-1.5, -2.5))
        write_decks(self.data, n_plans, vstep=vstep,
                    ids_scale_by=ids_scale_by, signed_gmoverid=signed)
        write_decks(self.data, p_plans, vstep=vstep, signed_gmoverid=signed)
        plans_f = write_plans(n_plans + p_plans, self.tmp / "c.plan.jsonl")
        kw.setdefault("data_root", self.tmp / "data")
        rep = npz_pack.pack_profile(self.prof_path, "pair_01", [plans_f],
                                    fet=fet, pdk_root=self.root,
                                    out=self.lines.append, **kw)
        return rep

    def mem(self):
        return self.root / self.prof["name"] / "mem"


class TestPackRoundTrip(PackBase):
    def test_full_build_and_manifest(self):
        rep = self.pack(force=True)
        mem = self.mem()
        for dev in ("nch_example", "pch_example"):
            for suf in ("_v1J.npz", "_v1V.npz"):
                self.assertTrue((mem / (dev + suf)).is_file(), dev + suf)
        man = json.loads((mem.parent / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(man["kind"], "midas-pdk")
        self.assertEqual(man["devices"]["nch_example"]["polarity"], 1)
        self.assertEqual(man["devices"]["pch_example"]["polarity"], -1)
        self.assertEqual(man["devices"]["nch_example"]["W_nodes"], [1e-6, 2e-6])
        self.assertEqual(man["devices"]["nch_example"]["vds_anchors"], [1.5, 2.5])
        self.assertEqual(man["devices"]["nch_example"]["vbs_nodes"], [0.0, 0.0])
        self.assertTrue(any("vbs" in n for n in man["notes"]))
        self.assertTrue(rep["verify"]["ok"])
        # 曲线数 = 2 L × 2 vds 锚 × 2 W 实例 = 8 片/器件
        self.assertTrue(all(b["curves"] == 8 for b in rep["built"].values()))

    def test_numeric_fidelity_bankj_bankv(self):
        rep = self.pack(force=True)
        if not HAS_OPT:
            self.skipTest("优化器包不可导，真装载数值对账跳过（静态层已验）")
        from optimization_tool.surrogates.gsieve.mem_v1 import BankJ, BankV
        mem = str(self.mem())
        bv = BankV(mem)
        bj = BankJ(mem)
        J = j_true(0.5, 1.5)
        g1 = bv.J_grad("nch_example", 1e-6, 1.5, 0.0, 0.5, w=2e-6)
        self.assertAlmostEqual(g1[0], J, delta=1e-4 * J)
        q = bj.lookup("nch_example", 1e-6, 1e-6, J, 1.5, 0.0)
        self.assertAlmostEqual(q["moi"], moi_true(0.5), delta=2e-2 * moi_true(0.5))
        self.assertAlmostEqual(q["av0"], av0_true(0.5), delta=2e-2 * av0_true(0.5))
        self.assertAlmostEqual(q["cgs"], 0.3 * 6e-12, delta=2e-2 * 0.3 * 6e-12)
        self.assertAlmostEqual(q["cgg_F"], 6e-12 * 1e-6, delta=2e-2 * 6e-18)
        qp = bj.lookup("pch_example", 2e-6, 2e-6, j_true(0.5, 1.5), -1.5, 0.0)
        self.assertGreater(qp["moi"], 0.0)
        self.assertTrue(any("真装载回环 PASS" in s for s in rep["verify"]["lines"]))

    def test_moi_conventions_both_paths(self):
        self.pack(force=True)                       # 默认 gmoverid=abs 形
        self.assertTrue(any("|gmoverid|(检测" in s for s in self.lines))
        self.assertTrue(any("moi=f·gmoverid" in s for s in self.lines))
        # signed 形：p 列存原始负比 ⇒ f· 路径直接命中
        self.lines.clear()
        self.pack(force=True, signed_gmoverid=True)
        self.assertTrue(not any("|gmoverid|(检测" in s for s in self.lines))

    def test_dry_run_writes_nothing(self):
        rep = self.pack(dry_run=True)
        self.assertFalse((self.root / self.prof["name"] / "mem").exists())
        self.assertEqual(rep["verify"]["live"], "dry")

    def test_overwrite_guard_then_force(self):
        self.pack(force=True)
        with self.assertRaises(npz_pack.PackError) as cm:
            self.pack()                              # 无 force ⇒ 拒
        self.assertIn("--force", str(cm.exception))

    def test_replay_idempotent(self):
        rep1 = self.pack(force=True)
        rep = self.pack(force=True)                  # 重放：同计划同数据，再验全过
        self.assertTrue(rep["verify"]["ok"])
        self.assertEqual({d: b["curves"] for d, b in rep1["built"].items()},
                         {d: b["curves"] for d, b in rep["built"].items()})


class TestLoudRejects(PackBase):
    def test_missing_deck_dir(self):
        n_plans = self.mk_grids("n")
        write_decks(self.data, n_plans[:-1], vstep=0.1)
        pf = write_plans(n_plans, self.tmp / "c.jsonl")
        with self.assertRaises(npz_pack.PackError) as cm:
            npz_pack.pack_profile(self.prof_path, "pair_01", [pf],
                                  data_root=self.tmp / "data", fet="n",
                                  pdk_root=self.root, out=self.lines.append)
        self.assertIn("片目录不存在", str(cm.exception))

    def test_vgs_grid_mismatch(self):
        n_plans = self.mk_grids("n")
        write_decks(self.data, n_plans[:2], vstep=0.1)
        write_decks(self.data, n_plans[2:], vstep=0.2)   # 后两 L 片换步长
        pf = write_plans(n_plans, self.tmp / "c.jsonl")
        with self.assertRaises(npz_pack.PackError) as cm:
            npz_pack.pack_profile(self.prof_path, "pair_01", [pf],
                                  data_root=self.tmp / "data", fet="n",
                                  pdk_root=self.root, out=self.lines.append)
        self.assertIn("vgs 格不齐", str(cm.exception))

    def test_conflict_same_point(self):
        """不同 deck_id、同 (L,|vds|,W) 键 ⇒ 同点两条互斥曲线=混批，必须点名拒。"""
        n_plans = self.mk_grids("n")
        write_decks(self.data, n_plans, vstep=0.1)
        pf1 = write_plans(n_plans, self.tmp / "a.jsonl")
        d2 = json.loads(json.dumps(n_plans[0]))
        d2["deck_id"] = "pair_01.n.L1u.V1.5b"          # 同键新名（名字怪异⇒名检跳过）
        write_decks(self.data, [d2], vstep=0.1,
                    ids_scale_by={d2["deck_id"]: 1.05})
        pf2 = write_plans([d2], self.tmp / "b.jsonl")
        with self.assertRaises(npz_pack.PackError) as cm:
            npz_pack.pack_profile(self.prof_path, "pair_01", [pf1, pf2],
                                  data_root=self.tmp / "data", fet="n",
                                  pdk_root=self.root, out=self.lines.append)
        self.assertIn("同点冲突", str(cm.exception))

    def test_polarity_letter_mismatch_refused(self):
        prof = json.loads(GEN.read_text(encoding="utf-8"))
        prof["pairs"]["pair_01"]["p"]["model"] = "mos_pex"   # p 管但不以 p 开头
        pf_path = self.tmp / "prof.json"
        pf_path.write_text(json.dumps(prof), encoding="utf-8")
        n_plans = engine.make_plan(load_profile(pf_path), "pair_01", "n",
                                   ["1u"], ["1u"], [1.5], vstep=0.1)
        p_plans = engine.make_plan(load_profile(pf_path), "pair_01", "p",
                                   ["1u"], ["1u"], [-1.5], vstep=0.1)
        write_decks(self.data, n_plans + p_plans, vstep=0.1)
        pf = write_plans(n_plans + p_plans, self.tmp / "c.jsonl")
        with self.assertRaises(npz_pack.PackError) as cm:
            npz_pack.pack_profile(pf_path, "pair_01", [pf],
                                  data_root=self.tmp / "data", fet="both",
                                  pdk_root=self.root, force=True,
                                  out=self.lines.append)
        self.assertIn("首字母判极性", str(cm.exception))

    def test_plan_file_missing(self):
        with self.assertRaises(npz_pack.PackError) as cm:
            npz_pack.read_plans([self.tmp / "ghost.jsonl"])
        self.assertIn("计划文件不存在", str(cm.exception))


class TestDialectGate(PackBase):
    def test_static_catches_key_mutilation(self):
        """方言破坏者：删 ColNames ⇒ 静默回退暗坑必须被静态层点名（引擎装载面
        lJ 缺失会回退 vg、X 缺失回退 Y——正是"查表失效"事故形态）。"""
        n_plans = self.mk_grids("n")
        write_decks(self.data, n_plans, vstep=0.1)
        pf = write_plans(n_plans, self.tmp / "c.jsonl")
        npz_pack.pack_profile(self.prof_path, "pair_01", [pf],
                              data_root=self.tmp / "data", fet="n",
                              pdk_root=self.root, force=True,
                              out=self.lines.append)
        good = np.load(self.mem() / "nch_example_v1J.npz", allow_pickle=False)
        broken = self.tmp / "mem_broken"
        broken.mkdir()
        kw = {k: good[k] for k in good.files if k != "ColNames"}
        np.savez(str(broken / "nch_example_v1J"), allow_pickle=False, **kw)
        shutil.copy(self.mem() / "nch_example_v1V.npz",
                    broken / "nch_example_v1V.npz")
        v = npz_pack.verify_dialect(broken, {"nch_example": 1.0}, live=False)
        self.assertFalse(v["ok"])
        self.assertTrue(any("ColNames" in p for p in v["problems"]))

    def test_static_catches_negative_vg(self):
        """p 表误存 raw vgs（负格）⇒ vg 归一面违例必须点名（坐标面反了=全表错位）。"""
        n_plans = self.mk_grids("n")
        write_decks(self.data, n_plans, vstep=0.1)
        pf = write_plans(n_plans, self.tmp / "c.jsonl")
        npz_pack.pack_profile(self.prof_path, "pair_01", [pf],
                              data_root=self.tmp / "data", fet="n",
                              pdk_root=self.root, force=True,
                              out=self.lines.append)
        good = np.load(self.mem() / "nch_example_v1V.npz", allow_pickle=False)
        broken = self.tmp / "mem_neg"
        broken.mkdir()
        kw = {k: good[k] for k in good.files}
        kw["vg"] = -kw["vg"]
        kw["Y"] = kw["Y"][..., ::-1]
        np.savez(str(broken / "nch_example_v1V"), allow_pickle=False, **kw)
        shutil.copy(self.mem() / "nch_example_v1J.npz",
                    broken / "nch_example_v1J.npz")
        v = npz_pack.verify_dialect(broken, {"nch_example": 1.0}, live=False)
        self.assertFalse(v["ok"])
        self.assertTrue(any("vg" in p for p in v["problems"]))

    def test_degenerate_vbs_out_of_domain(self):
        """vbs 单点表：|b|=0 能吃、|b|>0 必响亮 OutOfDomain（不做假覆盖）。"""
        n_plans = self.mk_grids("n", Ls=("1u",), Ws=("1u",), vds=(1.5,))
        write_decks(self.data, n_plans, vstep=0.1)
        pf = write_plans(n_plans, self.tmp / "c.jsonl")
        rep = npz_pack.pack_profile(self.prof_path, "pair_01", [pf],
                                    data_root=self.tmp / "data", fet="n",
                                    pdk_root=self.root, force=True,
                                    out=self.lines.append)
        self.assertTrue(rep["verify"]["ok"])
        z = np.load(self.mem() / "nch_example_v1J.npz", allow_pickle=False)
        self.assertEqual(list(z["vbs"]), [0.0, 0.0])
        self.assertEqual(list(z["L"]), [1e-6, 1e-6])
        self.assertEqual(list(z["vds"]), [1.5, 1.5])
        if not HAS_OPT:
            self.skipTest("优化器包不可导")
        from optimization_tool.surrogates.gsieve.bank_loader import OutOfDomain
        from optimization_tool.surrogates.gsieve.mem_v1 import BankJ
        bj = BankJ(str(self.mem()))
        q = bj.lookup("nch_example", 1e-6, 1e-6, j_true(0.5, 1.5), 1.5, 0.0)
        self.assertGreater(q["moi"], 0.0)
        with self.assertRaises(OutOfDomain):
            bj.lookup("nch_example", 1e-6, 1e-6, j_true(0.5, 1.5), 1.5, -0.5)
        with self.assertRaises(OutOfDomain):       # W 出节点域
            bj.lookup("nch_example", 1e-6, 1e-5, j_true(0.5, 1.5), 1.5, 0.0)


class TestCliSurface(unittest.TestCase):
    def test_pack_dry_and_err_rc(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        data = tmp / "data" / "pair_01"
        prof = load_profile(GEN)
        n_plans = engine.make_plan(prof, "pair_01", "n", ["1u"], ["1u", "2u"],
                                   [1.5, 2.5], vstep=0.1)
        write_decks(data, n_plans, vstep=0.1)
        pf = write_plans(n_plans, tmp / "c.jsonl")
        r = subprocess.run([sys.executable, str(ROOT / "cli.py"), "pack",
                            "--profile", str(GEN), "--pair", "pair_01",
                            "--plans", str(pf), "--data", str(tmp / "data"),
                            "--fet", "n", "--pdk-root", str(tmp / "_pdk"),
                            "--dry-run"], capture_output=True, text=True,
                           encoding="utf-8",
                           env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("[pack]", r.stdout)
        self.assertFalse((tmp / "_pdk").exists())
        r2 = subprocess.run([sys.executable, str(ROOT / "cli.py"), "pack",
                             "--profile", str(GEN), "--pair", "pair_01",
                             "--plans", str(tmp / "ghost.jsonl"),
                             "--data", str(tmp / "data"), "--fet", "n",
                             "--pdk-root", str(tmp / "_pdk")],
                            capture_output=True, text=True,
                            encoding="utf-8",
                            env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        self.assertEqual(r2.returncode, 2)
        self.assertIn("[err][pack]", r2.stdout)


class TestP41DigestP42Lock(PackBase):
    """P4.1 表内容指纹/覆写留代 + P4.2 写锁互斥与强解（10-10 设计 §1/§4）。"""

    def test_manifest_table_digest_tracks_bytes(self):
        rep = self.pack(force=True)
        dg = rep["table_digest"]
        self.assertEqual(len(dg), 64)
        man = json.loads((self.mem().parent / "manifest.json").read_text(
            encoding="utf-8"))
        self.assertEqual(man["table_digest"], dg)
        # 指纹=npz 内容：事后动一个字节 ⇒ 指纹必变（版本闸的度量基础）
        f = self.mem() / "nch_example_v1J.npz"
        f.write_bytes(f.read_bytes() + b"\x00")
        self.assertNotEqual(npz_pack.table_digest_of(self.mem()), dg)

    def test_repack_force_archives_prev_gen_and_prunes(self):
        rep1 = self.pack(force=True)
        dg1 = npz_pack.table_digest_of(self.mem())[:8]
        self.pack(force=True)                        # 第二次：留第一代
        arch_root = self.root / self.prof["name"] / "archive"
        a1 = arch_root / dg1
        self.assertTrue(a1.is_dir(),
                        "留代目录缺失——lines=%s" % self.lines)
        self.assertTrue((a1 / "nch_example_v1J.npz").is_file())
        self.assertTrue((a1 / "manifest.json").is_file())
        self.pack(force=True)                        # 第三次：剪回最近 1 代
        gens = [p for p in arch_root.iterdir() if p.is_dir()]
        self.assertEqual(len(gens), 1)
        # 主面完好且指纹与 manifest 自洽
        rep3 = self.pack(force=True)
        man = json.loads((self.mem().parent / "manifest.json").read_text(
            encoding="utf-8"))
        self.assertEqual(man["table_digest"], rep3["table_digest"])

    def test_writer_lock_blocks_run_lease_then_force_unlock(self):
        self.pack(force=True)
        lk = self.root / self.prof["name"] / ".tablelock"
        dg_before = npz_pack.table_digest_of(self.mem())
        # 模拟优化器活读租约（本进程 pid=活着）⇒ pack 抢写锁必须拒
        lk.write_text(json.dumps({"writers": [], "readers": [
            {"pid": os.getpid(), "tool": "optimizer run", "run_id": "gui",
             "digest": "ab" * 32, "ts": time.time()}]}), encoding="utf-8")
        from collector.tablelock import TableLocked
        with self.assertRaises(TableLocked) as cm:
            self.pack(force=True)
        self.assertIn("表锁", str(cm.exception))
        self.assertIn("gui", str(cm.exception))
        # 表未被碰：拒单后主面字节指纹与抢锁前逐位一致
        self.assertEqual(npz_pack.table_digest_of(self.mem()), dg_before)
        # 人工强解：清场后抢锁成功，收尾自释，呈报留痕
        rep = self.pack(force=True, force_unlock=True)
        self.assertTrue(rep["verify"]["ok"])
        doc = json.loads(lk.read_text(encoding="utf-8"))
        self.assertEqual(doc["readers"], [])
        self.assertEqual(doc["writers"], [])
        self.assertTrue(any("force-unlock" in s for s in self.lines))


if __name__ == "__main__":
    unittest.main()
