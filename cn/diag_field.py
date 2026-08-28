# -*- coding: utf-8 -*-
"""Why does Chinese field_text -> subfield mapping fail? Show, for wrong-pick gold rids:
   cn_name | NSFC field_text | gold's actual top-3 subfields | bge-m3 top-3 mapped subfields
"""
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import duckdb, numpy as np, pandas as pd
import cn_match

cn_df, _ = cn_match.load_cn()
gold = cn_df[cn_df["gold_authorid"].notna()][["rid", "cn_name", "field_text", "gold_authorid"]]
gold_map = dict(zip(gold["rid"].astype(int), gold["gold_authorid"].astype(str)))
ftext_map = dict(zip(gold["rid"].astype(int), gold["field_text"]))
name_map = dict(zip(gold["rid"].astype(int), gold["cn_name"]))

con = duckdb.connect(); con.sql("SET memory_limit='8GB'"); con.sql("SET temp_directory='.duckdb_tmp'")
# gold authors' actual top-3 level-1 subfields
con.register("g", gold[["gold_authorid"]].dropna().drop_duplicates().rename(columns={"gold_authorid":"authorid"}))
gsub = con.sql("""
  WITH papers AS (SELECT authorid,paperid FROM '../sciscinet_authors_paperid.parquet'
                  WHERE authorid IN (SELECT authorid FROM g)),
  lvl1 AS (SELECT fieldid,display_name FROM '../sciscinet_fields.parquet' WHERE level=1),
  fc AS (SELECT p.authorid,pf.fieldid,count(*) n FROM papers p
         JOIN '../sciscinet_paperfields.parquet' pf ON p.paperid=pf.paperid
         WHERE pf.fieldid IN (SELECT fieldid FROM lvl1) GROUP BY 1,2),
  r AS (SELECT authorid,fieldid,row_number() OVER(PARTITION BY authorid ORDER BY n DESC,fieldid) rk FROM fc)
  SELECT r.authorid, list(l.display_name ORDER BY r.rk) fields
  FROM r JOIN lvl1 l ON r.fieldid=l.fieldid WHERE r.rk<=3 GROUP BY r.authorid""").df()
gsub_map = dict(zip(gsub["authorid"], gsub["fields"]))

# bge-m3 map field_text -> nearest level-1 subfields
vocab = con.sql("SELECT DISTINCT display_name FROM '../sciscinet_fields.parquet' WHERE level=1 AND display_name IS NOT NULL").df()["display_name"].tolist()
embed = cn_match._get_embedder()
texts = sorted({t for t in ftext_map.values() if isinstance(t, str) and t.strip()})
V = embed(vocab); F = embed(texts)
order = np.argsort(-(F @ V.T), axis=1); tix = {t:i for i,t in enumerate(texts)}
va = np.array(vocab)

# show 25 examples where gold has subfields and field_text present
shown = 0
for rid in sorted(gold_map):
    ft = ftext_map.get(rid); gid = gold_map[rid]
    if not isinstance(ft, str) or gid not in gsub_map: continue
    gfields = list(gsub_map[gid])
    mapped = va[order[tix[ft]][:3]].tolist() if ft in tix else []
    hit = "OK " if set(gfields) & set(mapped) else "MISS"
    print("[%s] %s | 领域=%s" % (hit, str(name_map.get(rid))[:6], str(ft)[:34]))
    print("       gold真实子学科: %s" % gfields)
    print("       bge-m3映射到  : %s" % mapped)
    shown += 1
    if shown >= 25: break
