#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
把两批 US 匹配结果纵向拼成一份全量表。

两批的来源不同、rid 各自从 0 起, 直接 concat 会串号, 所以:
  - 加 `dataset` 列区分来源(拼接后 rid 只在 dataset 内唯一);
  - 从各自源 CSV 回填 AwardNumber / start_year, 让合并表能自证行来源;
  - 原有 7 列(rid..n_papers)顺序不动, 新列一律追加在后面。

两批数据:
  us_since1990   1995-2023 的 NSF CAREER 奖(17400 行, 源 ../us_since1990.csv)
  us_2024plus    2024-2027 的新 award  (3000 行,  源 US.csv)
时间上基本不重叠(since1990 只有 4 条 2024 年边界行), 但 PI 会重复出现,
同一个人两边可能配到不同 authorid -> 单独出冲突审计, 不在此处调和。
"""
import os
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# (dataset 名, 结果目录, 源 CSV)
BATCHES = [
    ("us_since1990", HERE, os.path.join(ROOT, "us_since1990.csv")),
    ("us_2024plus", os.path.join(HERE, "_backup_US3000"), os.path.join(HERE, "US.csv")),
]
OUT_COLS = ["dataset", "rid", "us_name", "authorid", "match_type", "host_id",
            "source", "n_papers", "award_number", "start_year"]


def load_src(path):
    """源 CSV -> rid 索引的 (AwardNumber, start_year), rid 定义同 us_match.py: 行号从 0 起。"""
    df = pd.read_csv(path, dtype=str, encoding="utf-8-sig")
    df["rid"] = range(len(df))
    yr = pd.to_datetime(df["StartDate"], errors="coerce", format="%m/%d/%Y").dt.year
    return pd.DataFrame({"rid": df["rid"],
                         "award_number": df["AwardNumber"],
                         "start_year": yr.astype("Int64")})


def merge(fname, out_name):
    parts = []
    for ds, resdir, src in BATCHES:
        p = os.path.join(resdir, fname)
        if not os.path.exists(p):
            print(f"[skip] {ds}: 缺 {p}")
            continue
        d = pd.read_csv(p, dtype=str, encoding="utf-8-sig")
        d["rid"] = d["rid"].astype(int)
        d = d.merge(load_src(src), on="rid", how="left")
        d.insert(0, "dataset", ds)
        parts.append(d)
        print(f"[load] {ds:14s} {fname:24s} {len(d):6d} 行")

    out = pd.concat(parts, ignore_index=True)
    # held 表没有 source/n_papers 之外的差异, 但两表列集合可能不同, 统一按 OUT_COLS 对齐
    for c in OUT_COLS:
        if c not in out.columns:
            out[c] = pd.NA
    extra = [c for c in out.columns if c not in OUT_COLS]
    out = out[OUT_COLS + extra]

    dst = os.path.join(HERE, out_name)
    out.to_csv(dst, index=False, encoding="utf-8-sig")
    print(f"[write] {dst}  {len(out)} 行 / {out.authorid.nunique()} 个唯一 authorid")
    return out


def audit(m):
    """同一个 PI 姓名在两批里配到完全不同的 authorid -> 逐条列出, 供人工复核。"""
    m = m.copy()
    m["n"] = m["us_name"].str.strip()
    g = {ds: sub.groupby("n").authorid.agg(set) for ds, sub in m.groupby("dataset")}
    a, b = g["us_since1990"], g["us_2024plus"]
    rows = []
    for name in sorted(set(a.index) & set(b.index)):
        if not (a[name] & b[name]):
            rows.append({"us_name": name,
                         "authorid_since1990": ";".join(sorted(a[name])),
                         "authorid_2024plus": ";".join(sorted(b[name]))})
    dst = os.path.join(HERE, "merge_conflicts_us.csv")
    pd.DataFrame(rows, columns=["us_name", "authorid_since1990", "authorid_2024plus"]) \
        .to_csv(dst, index=False, encoding="utf-8-sig")
    print(f"[write] {dst}  {len(rows)} 个同名不同 authorid 的 PI")


if __name__ == "__main__":
    matched = merge("matched_final_us.csv", "matched_final_us_all.csv")
    merge("held_lowpaper_us.csv", "held_lowpaper_us_all.csv")
    audit(matched)
