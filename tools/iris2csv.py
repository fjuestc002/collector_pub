# -*- coding: utf-8 -*-
"""iris2csv.py —— Cadence IRIS(psfbin) → CSV 最小解析器 v0.4（10-09 建，同日降序修）

实证格式(samples/P3_dc.dc 帧解剖, 大端):
  元信息: 33=串属性 [33][keylen][key补4][vallen][val补4] / 34=int / 35=double
  数据区: 定长 16B 流 [int32 16][int32 idx][double]，逐点行优先(sweep 列先出)
  向量表: 部分形态 [namelen][name补8][unit][16][idx]，其余 idx 以列值自动补名
v0.3 策略: 数据区不依赖头区——以 "最长连续 16B 记录流" 定位;
           sweep 列由值序列(min→max 按 points)自动识别; 未知 idx 命名 col{idx}。
v0.4 变更: sweep 判据改 "单调(含降)+端点贴合"。实证 p 管 0→−5 批件:
           PSF 的 "sweep min/max" 字段系扫段起/止(实测 min=0.0 max=-5.0)而非
           算术最小/大 ⇒ 端点式对升降两向同形，仅方向判据须接受非递增。
用法: py -3.14 iris2csv.py <dc.dc> [out.csv]
自检: 行数==PSF sweep points & sweep 列单调(升或降)且贴合起止 → 否则返回码 2。
"""
import struct
import sys


def pad(n, m):
    return n + (-n % m)


def rdstr(b):
    return b.split(b"\x00")[0].decode("ascii", "replace")


def walk_meta(data, pos):
    meta = {}
    n = len(data)
    while pos + 8 <= n:
        typ, ln = struct.unpack_from(">ii", data, pos)
        if not (1 <= ln <= 100):
            break
        key = rdstr(data[pos + 8: pos + 8 + ln])
        if not key:
            break
        vp = pos + 8 + pad(ln, 4)
        try:
            if typ == 33:
                (vln,) = struct.unpack_from(">i", data, vp)
                if not (0 <= vln <= 4096):
                    break
                meta[key] = rdstr(data[vp + 4: vp + 4 + vln])
                pos = vp + 4 + pad(vln, 4)
            elif typ == 34:
                (meta[key],) = struct.unpack_from(">i", data, vp)
                pos = vp + 4
            elif typ == 35:
                (meta[key],) = struct.unpack_from(">d", data, vp)
                pos = vp + 8
            else:
                break
        except struct.error:
            break
    return meta


def scan_names(data, upto):
    """宽容收集 (unit,16,idx) 向量表条目: [namelen][name补8][unit][16][idx]。"""
    NAMECHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789:_.-")
    vecs = {}
    p = 0
    n = min(upto, len(data))
    while p + 12 <= n:
        (ln,) = struct.unpack_from(">i", data, p)
        if 1 <= ln <= 31:
            raw = data[p + 4: p + 4 + ln]
            name = raw.split(b"\x00")[0].decode("ascii", "replace")
            if name and all(c in NAMECHARS for c in name):
                q = p + 4 + pad(ln, 4)
                if q + 12 <= n:
                    unit, k16, idx = struct.unpack_from(">iii", data, q)
                    if k16 == 16 and 0 <= unit <= 255 and 2 <= idx < 65535:
                        vecs.setdefault(idx - 1, name)  # 实证: 该字段=后继 idx(整体+1), 物理交叉核定案
                        p = q + 12
                        continue
        p += 4
    return vecs


def find_data_stream(data, vecs):
    """找最长连续 16B [16][idx][finite dbl] 记录流。返回 (start, records list)。"""
    n = len(data)
    best_start, best = -1, []
    p = 0
    while p + 16 <= n:
        if struct.unpack_from(">i", data, p)[0] != 16:
            p += 4
            continue
        start = p
        recs = []
        while p + 16 <= n:
            typ, idx = struct.unpack_from(">ii", data, p)
            (val,) = struct.unpack_from(">d", data, p + 8)
            if typ != 16 or not (1 <= idx < 65535) or val != val or abs(val) > 1e35:
                break
            recs.append((idx, val))
            p += 16
        if len(recs) > len(best):
            best_start, best = start, recs
        p = start + 4
    return best_start, best


def sweep_match(vals, start, end):
    """sweep 列判据（v0.4）：单调（升或降）且端点贴合扫段起/止。

    PSF 的 "sweep min/max" 字段系起/止值非算术 min/max——降序批件实测
    min=0.0 max=-5.0（p 管 0→−5）⇒ 端点式 (vals[0]≈start, vals[-1]≈end)
    对升降两向同形，方向判据接受非递增即可。浮点容差沿用 1e-9。"""
    if start is None or end is None or len(vals) < 2:
        return False
    asc = all(vals[i + 1] >= vals[i] for i in range(len(vals) - 1))
    desc = all(vals[i + 1] <= vals[i] for i in range(len(vals) - 1))
    if not (asc or desc):
        return False
    return abs(vals[0] - start) < 1e-9 and abs(vals[-1] - end) < 1e-9


def main():
    path = sys.argv[1]
    out = sys.argv[2] if len(sys.argv) > 2 else None
    data = open(path, "rb").read()
    meta = walk_meta(data, 12)
    npts = meta.get("PSF sweep points")
    vecs = scan_names(data, len(data))
    dstart, recs = find_data_stream(data, vecs)
    if not recs:
        sys.exit("[FATAL] 未找到数据流")
    # 行切分: idx 重复即新行
    rows, cur = [], {}
    for idx, val in recs:
        if idx in cur:
            rows.append(cur)
            cur = {}
        cur[idx] = val
    if cur:
        rows.append(cur)
    cols = sorted({i for r in rows for i in r})
    names = {c: vecs.get(c, f"col{c}") for c in cols}
    # sweep 列自动识别: 单调(升或降)且端点贴合扫段起/止
    smin, smax = meta.get("PSF sweep min"), meta.get("PSF sweep max")  # 实证=起/止值
    sweep_col = None
    for c in cols:
        vals = [r[c] for r in rows if c in r]
        if len(vals) == len(rows) and sweep_match(vals, smin, smax):
            sweep_col = c
            if names[c].startswith("col"):
                names[c] = "sweep"
            break
    print(f"file={path}")
    print(f"meta: points={npts} traces={meta.get('PSF traces')} min={smin} max={smax} "
          f"sim={meta.get('simulator')}/{meta.get('version')}")
    print(f"data@{hex(dstart)} recs={len(recs)} rows={len(rows)}")
    print(f"cols: {[(c, names[c]) for c in cols]}")
    ok = True
    if isinstance(npts, int) and len(rows) != npts:
        print(f"[WARN] 行数{len(rows)}!=points{npts}")
        ok = False
    if sweep_col is None:
        print("[WARN] sweep 列未自动识别")
        ok = False
    else:
        print(f"[CHECK] sweep 列 idx={sweep_col} 单调(含降)且贴合起止 OK")
    if out and rows:
        import csv as _csv
        with open(out, "w", newline="", encoding="utf-8") as f:
            w = _csv.writer(f)
            w.writerow([names[c] for c in cols])
            for r in rows:
                w.writerow([f"{r.get(c, float('nan')):.12g}" for c in cols])
        print(f"[OUT] {out}")
    elif rows:
        for r in rows[:3]:
            print("  ", {names[c]: r.get(c) for c in cols})
    sys.exit(0 if ok else 2)


if __name__ == "__main__":
    main()
