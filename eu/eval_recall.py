# -*- coding: utf-8 -*-
"""召回 POC 验收:与 matched_final.csv 逐 source 对比(pair-completeness = 旧管线命中是否进了新候选池),
目标双姓案例 rid 1175 是否召回真人,以及扇出统计。不看 ORCID。"""
import sys
import numpy as np
import pandas as pd

raw = pd.read_parquet("recall_ngram_raw.parquet")          # 第一段(a)原始召回(未过 fuzzyname)
cand = pd.read_csv("candidates_poc.csv")                   # 过 fuzzyname 后的候选
final = pd.read_csv("matched_final.csv")                   # 旧管线最终结果(每 rid 唯一 authorid)

raw_pairs = set(zip(raw["rid"], raw["authorid"]))
cand_pairs = set(zip(cand["rid"], cand["authorid"]))
final_pairs = list(zip(final["rid"], final["authorid"]))

print("=== 规模 ===")
print("raw 召回对: %d | 过 fuzzyname: %d | matched_final: %d" % (len(raw_pairs), len(cand_pairs), len(final)))

print("\n=== pair-completeness:旧 matched_final 的 (rid,authorid) 是否进了新候选池 ===")
def cover(pairs, pool):
    hit = sum((r, a) in pool for r, a in pairs)
    return hit, len(pairs), 100.0 * hit / max(1, len(pairs))
for src, g in final.groupby("source"):
    fp = list(zip(g["rid"], g["authorid"]))
    hr, nr, pr = cover(fp, raw_pairs)
    hc, nc, pc = cover(fp, cand_pairs)
    print("  %-18s n=%-5d | raw池 %4d/%-4d=%5.1f%% | 过fuzzy %4d/%-4d=%5.1f%%"
          % (src, nr, hr, nr, pr, hc, nc, pc))
hr, nr, pr = cover(final_pairs, raw_pairs)
hc, nc, pc = cover(final_pairs, cand_pairs)
print("  %-18s n=%-5d | raw池 %4d/%-4d=%5.1f%% | 过fuzzy %4d/%-4d=%5.1f%%"
      % ("(全部)", nr, hr, nr, pr, hc, nc, pc))

# 未被 raw 池覆盖的旧命中:逐 match_type 看,判断缺口性质(是否单字母缩写类,归 branch-b)
miss = final[[ (r, a) not in raw_pairs for r, a in zip(final["rid"], final["authorid"]) ]]
print("\n=== raw 池未覆盖的旧命中: %d 个,按旧 match_type 分 ===" % len(miss))
if len(miss):
    print(miss["match_type"].value_counts().to_string())
    print("\n  抽样(前 20):")
    print(miss[["rid", "eu_name", "authorid", "match_type", "source"]].head(20).to_string(index=False))

print("\n=== 目标双姓案例 rid 1175 (Julián Valero Moreno) ===")
tgt = raw[raw["rid"] == 1175].sort_values("score", ascending=False)
print(tgt.to_string(index=False))
print("真人 A5049884091 在 raw 池:", (1175, "A5049884091") in raw_pairs,
      "| 过 fuzzyname:", (1175, "A5049884091") in cand_pairs)

print("\n=== 扇出 ===")
for nm, d in [("raw", raw), ("过fuzzy", cand)]:
    s = d.groupby("rid").size()
    print("  %-8s EU名有候选=%d | 每名候选 mean %.1f p50 %.0f p95 %.0f max %d"
          % (nm, len(s), s.mean(), s.median(), s.quantile(.95), s.max()))
