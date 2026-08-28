# -*- coding: utf-8 -*-
"""
从 92GB 的 sciscinet_papertitleabstract.parquet 里,只抽出【US 层3/层4 候选作者】论文的
标题+摘要,产出小 parquet 供 US 层3(v2 abstract-vs-abstract)用。

结构与 EU 版 extract_abstracts.py 一致,只是候选池换成 US 的 match_3_us.csv、输出加 _us 后缀。

filter-first:先把"需要的 paperid"缩到几万,再对 92GB 做【一次】半连接扫描。
- 候选作者 = match_3_us.csv 里的 authorid(US 层3 候选池,已含流向层4 的)
- 每作者封顶 K 篇(默认 30,给 language='en' 过滤留余量)
- 只留 language='en'
- 摘要在库里是 OpenAlex 倒排索引(JSON:词->位置),抽出后还原成纯文本

前置:先跑一次 us_match.py(无 author_vectors_us.npz 时层3 自动走 v1)生成 match_3_us.csv。

产出:
- cand_papers_us.parquet     (authorid, paperid, rnk)   capped 映射
- cand_abstracts_us.parquet  (paperid, title, abstract, language)  已还原摘要
"""
import os
import json
import time

import duckdb

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)

K = int(os.environ.get("US_ABS_K", "30"))          # 每作者封顶论文数(pre-scan)
ABS_FILE = "../sciscinet_papertitleabstract.parquet"
CAND_CSV = os.environ.get("US_CAND_CSV", "match_3_us.csv")

if not os.path.exists(CAND_CSV):
    raise SystemExit(f"[error] 找不到候选池 {CAND_CSV};请先跑一次 us_match.py 生成它。")

con = duckdb.connect()
con.sql("SET temp_directory='.duckdb_tmp'")
con.sql("SET memory_limit='12GB'")
con.sql("SET preserve_insertion_order=false")

t0 = time.time()

# 1) 候选作者的论文,按 authorid 分组封顶 K 篇(row_number 排序仅为确定性,非重要性)
print(f"[1/4] 构建候选 paperid 集合 (K={K}/作者) ...", flush=True)
con.sql(f"""
    CREATE TEMP TABLE cand_papers AS
    WITH cand AS (SELECT DISTINCT authorid FROM '{CAND_CSV}' WHERE authorid IS NOT NULL),
    ap AS (
        SELECT p.authorid, p.paperid,
               row_number() OVER (PARTITION BY p.authorid ORDER BY p.paperid) AS rnk
        FROM '../sciscinet_authors_paperid.parquet' p
        JOIN cand c ON p.authorid = c.authorid
    )
    SELECT authorid, paperid, rnk FROM ap WHERE rnk <= {K}
""")
n_map, n_pids = con.sql(
    "SELECT count(*), count(DISTINCT paperid) FROM cand_papers").fetchone()
con.sql("COPY cand_papers TO 'cand_papers_us.parquet' (FORMAT parquet)")
print(f"      映射行 {n_map}, 需要的 distinct paperid {n_pids}  ({time.time()-t0:.1f}s)")

# 2) 对 92GB 做一次半连接扫描:只取需要的 paperid 且 language='en'
print(f"[2/4] 扫描 {ABS_FILE} (122M 行) 抽取匹配 ... 这一步最慢", flush=True)
ts = time.time()
raw = con.sql(f"""
    SELECT a.paperid, a.title, a.abstract_inverted_index, a.language
    FROM '{ABS_FILE}' a
    SEMI JOIN (SELECT DISTINCT paperid FROM cand_papers) k
      ON a.paperid = k.paperid
    WHERE a.language = 'en'
""").df()
print(f"      扫描完成:命中 en 论文 {len(raw)} / 需要 {n_pids}  "
      f"(scan {time.time()-ts:.1f}s)", flush=True)

# 3) 还原倒排索引 -> 纯文本摘要
print(f"[3/4] 还原 {len(raw)} 条摘要(倒排索引 -> 文本) ...", flush=True)


def deinvert(s):
    if not isinstance(s, str) or not s:
        return None
    try:
        d = json.loads(s)
    except Exception:
        return None
    pairs = [(p, tok) for tok, ps in d.items() for p in ps]
    if not pairs:
        return None
    pairs.sort()
    return " ".join(t for _, t in pairs)


raw["abstract"] = raw["abstract_inverted_index"].map(deinvert)
out = raw[["paperid", "title", "abstract", "language"]]
n_abs = out["abstract"].notna().sum()

# 4) 落盘
out.to_parquet("cand_abstracts_us.parquet", index=False)
print(f"[4/4] 写出 cand_abstracts_us.parquet  行 {len(out)}, 有摘要 {n_abs} "
      f"({n_abs/max(len(out),1)*100:.0f}%)")
print(f"总用时 {time.time()-t0:.1f}s")
