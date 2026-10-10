# -*- coding: utf-8 -*-
"""单枪 VM 执行器（M2 引擎的人类手工入口/调试件；批量走 cli.py run）。

认证与运输逻辑住 collector/remote.py（凭证三级，单一出处）。
每发命令与输出追加记 out/vm_run_manifest.log（存单纪律同 ocn_probes）。

用法: py -3.14 tools/vm_run.py --deck out/decks/smoke.scs --profile profiles_local/vendor_a.json
"""
from __future__ import annotations

import argparse
import posixpath
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from collector.profile import load_profile  # noqa: E402
from collector.remote import connect  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "out" / "vm_run_manifest.log"


def log(s: str) -> None:
    MANIFEST.parent.mkdir(exist_ok=True)
    with MANIFEST.open("a", encoding="utf-8") as f:
        f.write(s + "\n")
    try:
        print(s, flush=True)
    except UnicodeEncodeError:
        print(s.encode("gbk", "replace").decode("gbk"), flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--deck", required=True)
    ap.add_argument("--profile", required=True)
    ap.add_argument("--workdir", default="~/collector_run")
    ap.add_argument("--sample-dir", default="out/samples")
    a = ap.parse_args()
    deck = Path(a.deck)
    name = deck.stem

    prof = load_profile(a.profile)
    conn = prof.get("connection")
    if not conn or "host" not in conn or "user" not in conn:
        log("[err] 画像缺 connection.host/user——响亮拒")
        return 4
    t0 = time.time()
    rem = connect(conn)
    wd = a.workdir.replace("~", f"/home/{conn['user']}")

    def run(cmd, timeout=900):
        log(f"$ {cmd}")
        rc, out = rem.run(cmd, timeout=timeout)
        if out.strip():
            log(out.rstrip())
        log(f"[rc={rc}]")
        return rc, out

    run(f"mkdir -p {wd}")
    rem.put_text(deck.read_text(encoding="utf-8"), posixpath.join(wd, deck.name))
    log(f"[upload] {deck} -> {wd}/{deck.name}")
    # log 由 spectre 自动产 <deck>.log 与 <deck>_spectre.out，勿传 -log（SPECTRE-132）
    run(f"cd {wd} && {conn.get('shell_wrap', 'bash -lc')} "
        f"'(spectre -64 {deck.name} > {name}_console.log 2>&1; echo SPECTRE_RC=$?)'")
    _, out = run(f"tail -3 {wd}/{name}_spectre.out 2>/dev/null || "
                 f"tail -5 {wd}/{name}_console.log")
    m = re.findall(r"(\d+)\s+error", out)
    if m and any(int(x) for x in m):
        log("[health] spectre.out 报 errors>0 ⇒ 响亮拒")
        rem.close()
        return 2
    _, listing = run(f"find {wd}/{name}.raw -type f 2>/dev/null | head -40")
    files = [f for f in listing.split() if f.strip()]
    if not files:
        log("[health] 未找到 .raw 产物 ⇒ 拒")
        rem.close()
        return 3
    sdir = ROOT / a.sample_dir / name
    sdir.mkdir(parents=True, exist_ok=True)
    for rf in files:
        lp = sdir / posixpath.relpath(rf, f"{wd}/{name}.raw").replace("..", "up")
        lp.parent.mkdir(parents=True, exist_ok=True)
        log(f"[pull] {rf} ({rem.get(rf, str(lp))} B)")
    log(f"[done] {time.time() - t0:.1f}s")
    rem.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
