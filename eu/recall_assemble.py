# -*- coding: utf-8 -*-
"""离线装配:从两段扫描的原始召回(recall_ngram_raw.parquet + recall_initial_raw.parquet)
重跑【并集 + branch-b rarity-cap + fuzzyname 过滤 + 输出】,免去重扫 100M。用于调 BKEY_CAP。
用法: BKEY_CAP=50 python recall_assemble.py"""
import os
import time
import numpy as np
import pandas as pd
from fuzzyname_vendored import names_match

BKEY_CAP = int(os.environ.get("BKEY_CAP", "50"))
raw_a = pd.read_parquet("recall_ngram_raw.parquet")            # rid, authorid, sci_name, score
raw_b_full = pd.read_parquet("recall_initial_raw.parquet")     # rid, eu_name, authorid, sci_name, key, key_n
raw_b = raw_b_full[raw_b_full["key_n"] <= BKEY_CAP].drop_duplicates(["rid", "authorid"])

a = raw_a[["rid", "authorid", "sci_name", "score"]].copy()
b = raw_b[["rid", "authorid", "sci_name"]].copy()
union = a.merge(b, on=["rid", "authorid"], how="outer", suffixes=("_a", "_b"))
union["sci_name"] = union["sci_name_a"].fillna(union["sci_name_b"])
in_a, in_b = union["sci_name_a"].notna(), union["sci_name_b"].notna()
union["source"] = np.where(in_a & in_b, "ngram+initial", np.where(in_a, "ngram", "initial"))
print("BKEY_CAP=%d | branch-b capped %d 对(全量 %d) | 并集 %d(仅a %d/仅b %d/两者 %d)"
      % (BKEY_CAP, len(raw_b), len(raw_b_full), len(union),
         int((union.source == "ngram").sum()), int((union.source == "initial").sum()),
         int((union.source == "ngram+initial").sum())), flush=True)

# eu_name(fuzzyname 左侧):branch-b 带了 eu_name,补给 branch-a-only 行
name_of = dict(zip(raw_b_full["rid"], raw_b_full["eu_name"]))
import duckdb
eu = duckdb.sql("""SELECT (row_number() OVER ())-1 AS rid, "Researcher(s)" AS eu_name
                   FROM read_xlsx('EU.xlsx', all_varchar=true)""").df()
name_of.update(dict(zip(eu["rid"], eu["eu_name"])))
union["eu_name"] = union["rid"].map(name_of)

t = time.time()
pairs = union[["eu_name", "sci_name"]].drop_duplicates()
verdict = {(en, sn): names_match(en, sn) for en, sn in pairs.itertuples(index=False)}
union["fuzzy_ok"] = [verdict[(en, sn)] for en, sn in zip(union["eu_name"], union["sci_name"])]
print("fuzzyname: %d 去重对, %.0fs" % (len(pairs), time.time() - t), flush=True)

out = union[union["fuzzy_ok"]][["rid", "eu_name", "authorid", "sci_name", "source", "score"]].sort_values(
    ["rid", "score"], ascending=[True, False], na_position="last").reset_index(drop=True)
out.to_csv("candidates_poc.csv", index=False, encoding="utf-8-sig")
fo = out.groupby("rid").size()
print("过 fuzzyname 候选 %d 对,%d EU 名 | 扇出 mean %.1f p50 %.0f p95 %.0f max %d | 来源 %s"
      % (len(out), out["rid"].nunique(), fo.mean(), fo.median(), fo.quantile(.95), fo.max(),
         dict(out["source"].value_counts())))
