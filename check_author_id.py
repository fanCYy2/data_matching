# -*- coding: utf-8 -*-
"""查找某个/某些 author id 是否在 sciscinet_author_details 数据集中。

用法:
    python check_author_id.py A5074835999
    python check_author_id.py A5074835999 5087654321         # 多个,带不带 A 都行
    python check_author_id.py --file ids.txt                 # 每行一个 id
    python check_author_id.py A5074835999 --show             # 命中就打印整行详情

数据集里 authorid 形如 "A5074835999"(带 A 前缀,字符串)。脚本对输入做归一化:
去掉 URL 前缀和大小写 A,只按末尾数字比较,所以带不带 "A"、带不带
"https://openalex.org/" 前缀都能查。
"""
import sys
import argparse

import duckdb

# Windows 控制台默认 GBK,✓/✗ 之类字符会编码报错,统一切到 utf-8。
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

PARQUET = "sciscinet_author_details.parquet"


def digits(raw: str) -> str:
    """把各种写法(A5074835999 / https://openalex.org/A5074835999 / 5074835999)
    统一成纯数字字符串,作为比较用的 key。"""
    s = raw.strip()
    if not s:
        return s
    s = s.rsplit("/", 1)[-1]          # 去 URL 前缀
    if s[:1] in ("A", "a"):           # 去 OpenAlex 的 A 前缀
        s = s[1:]
    return s


def main() -> int:
    ap = argparse.ArgumentParser(description="查 author id 是否在 sciscinet_author_details 中")
    ap.add_argument("ids", nargs="*", help="一个或多个 author id(带不带 A 都行)")
    ap.add_argument("--file", help="从文件读 id,每行一个")
    ap.add_argument("--show", action="store_true", help="命中时打印整行详情")
    args = ap.parse_args()

    raw_ids = list(args.ids)
    if args.file:
        with open(args.file, encoding="utf-8") as f:
            raw_ids += f.read().splitlines()
    raw_ids = [x for x in raw_ids if x.strip()]

    keys = [digits(x) for x in raw_ids]
    if not keys:
        ap.error("没有提供任何 author id(用位置参数或 --file)")

    # 数据集里可能存 "A123" 也可能存 "123",两种候选都放进 IN 里,命中后再按数字 key 回填。
    candidates = sorted({c for k in keys for c in (k, "A" + k)})

    con = duckdb.connect()
    placeholders = ", ".join(["?"] * len(candidates))
    cols = "*" if args.show else "authorid"
    found = con.execute(
        f"SELECT {cols} FROM '{PARQUET}' WHERE authorid IN ({placeholders})",
        candidates,
    ).df()

    hit_keys = {digits(a) for a in found["authorid"].astype(str)}
    for original, key in zip(raw_ids, keys):
        mark = "[YES] 在  " if key in hit_keys else "[ NO] 不在"
        print(f"{mark}\t{original.strip()}\t(key: {key})")

    if args.show and not found.empty:
        import pandas as pd
        pd.set_option("display.max_columns", None)
        pd.set_option("display.width", 200)
        print("-" * 60)
        print(found.to_string(index=False))

    n_hit = len(hit_keys)
    print("-" * 60)
    print(f"合计: {len(set(keys))} 个不同 id,命中 {n_hit},未命中 {len(set(keys)) - n_hit}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
