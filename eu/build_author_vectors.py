# -*- coding: utf-8 -*-
"""
离线预计算:候选作者(层3/层4 候选池)的"论文文本质心"向量。
输入 cand_papers.parquet + cand_abstracts.parquet(由 extract_abstracts.py 产出),
每个作者取 <= K 篇英文论文的 (title. abstract),一次性批量嵌入,按作者取质心。
产物 author_vectors.npz 供 data_match.py 层3(v2)用;作者不在此表 -> 层3 回退到子学科名。
"""
import os
import numpy as np
import pandas as pd
import duckdb

from erc_embed import embed, centroid, AUTHORVEC_FILE

K = int(os.environ.get("AUTHORVEC_K", "20"))   # 每作者最多用多少篇英文论文建质心

con = duckdb.connect()
df = con.sql(f"""
    WITH picked AS (
        SELECT cp.authorid, cp.paperid,
               row_number() OVER (PARTITION BY cp.authorid ORDER BY cp.paperid) AS rk
        FROM 'cand_papers.parquet' cp
        JOIN 'cand_abstracts.parquet' ca ON cp.paperid = ca.paperid
    )
    SELECT p.authorid,
           coalesce(ca.title, '') || '. ' || coalesce(ca.abstract, '') AS doc
    FROM picked p
    JOIN 'cand_abstracts.parquet' ca ON p.paperid = ca.paperid
    WHERE p.rk <= {K}
""").df()
print(f"待嵌入文档 {len(df)} 篇,覆盖作者 {df['authorid'].nunique()}(K={K})")

# 一次性批量嵌入所有文档(比按作者逐个循环快得多),再按作者取质心
V = embed(df["doc"].tolist(), maxlen=256)      # (N, 384),与 df 行对齐

buckets = {}
for a, row in zip(df["authorid"].values, V):
    buckets.setdefault(a, []).append(row)

ids = list(buckets)
mat = np.vstack([centroid(np.vstack(buckets[a])) for a in ids]).astype("float32")
ndoc = np.array([len(buckets[a]) for a in ids], dtype="int32")

np.savez(AUTHORVEC_FILE, ids=np.array(ids, dtype=object), mat=mat, ndoc=ndoc)
print(f"已保存 {AUTHORVEC_FILE}:{len(ids)} 个作者向量,维度 {mat.shape}")
