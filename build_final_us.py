# -*- coding: utf-8 -*-
"""把两批 US 匹配结果落成 final/ 的交付格式。

final/ 的既有约定(见 final/eu_final.csv、final/aus_final.csv):
  **源名单原样保留全部列与行序, 末尾追加一列 `author_id`**。
  只有进了 matched_final 的行才填 id; 低产出队列(held)与未匹配行留空,
  这样交付表的行数 == 源名单行数, 下游能直接按行对齐回原始 award。

三份产物:
  us_final.csv           2024-2027 的新 award (源 us/US.csv, 3000 行) —— 重现既有文件
  us_since1990_final.csv 1995-2023 的 NSF CAREER 奖 (源 us_since1990.csv, 17400 行)
  us_all_final.csv       上面两批纵向拼接 (20400 行), 按时间先后 since1990 在前

合并表额外追加一列 `dataset`: 两批源列完全相同, 拼起来就分不出行的来源, 而两批
**奖项口径并不同**(since1990 只筛 CAREER, 2024plus 是全类型新 award), 必须能区分。
"""
import os

import pandas as pd

os.chdir(os.path.dirname(os.path.abspath(__file__)))

OUT_DIR = "final"

# (dataset 名, 源 CSV, 该批的 matched_final)
BATCHES = [
    ("us_since1990", "us_since1990.csv", "us/matched_final_us.csv"),
    ("us_2024plus", "us/US.csv", "us/_backup_US3000/matched_final_us.csv"),
]


def build(src_csv, matched_csv, dataset):
    """源名单 + author_id。rid 定义同 us_match.py: 源 CSV 行号, 0 起。"""
    src = pd.read_csv(src_csv, dtype=str, encoding="utf-8-sig")
    m = pd.read_csv(matched_csv, dtype=str, encoding="utf-8-sig")
    assert not m["rid"].duplicated().any(), f"{matched_csv}: rid 有重复, 无法按行对齐"

    aid = dict(zip(m["rid"].astype(int), m["authorid"]))
    src["author_id"] = [aid.get(i) for i in range(len(src))]
    src["dataset"] = dataset

    n = src["author_id"].notna().sum()
    print(f"[{dataset:14s}] {len(src):6d} 行 | 有 author_id {n:6d} ({n / len(src):.1%}) "
          f"| 空 {len(src) - n}")
    return src


def write(df, name, drop_dataset=False):
    out = df.drop(columns="dataset") if drop_dataset else df
    dst = os.path.join(OUT_DIR, name)
    out.to_csv(dst, index=False, encoding="utf-8-sig")
    print(f"[write] {dst}  {len(out)} 行 x {len(out.columns)} 列")


if __name__ == "__main__":
    parts = {ds: build(src, mt, ds) for ds, src, mt in BATCHES}

    # 单批: 保持 final/ 原有的"源列 + author_id", 不带 dataset
    write(parts["us_2024plus"], "us_final.csv", drop_dataset=True)
    write(parts["us_since1990"], "us_since1990_final.csv", drop_dataset=True)

    # 合并: 时间靠前的在前, 保留 dataset 以区分口径
    allrows = pd.concat([parts["us_since1990"], parts["us_2024plus"]], ignore_index=True)
    write(allrows, "us_all_final.csv")
