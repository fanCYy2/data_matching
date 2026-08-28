# -*- coding: utf-8 -*-
"""Probe 4: ceiling of a field-consistency filter.
Among same-host candidates, keep only those whose top-3 subfields overlap the
'true' research field, THEN argmax papers. Uses gold's own subfields as an ORACLE
target => upper bound of what a perfect field/topic layer (used as a FILTER, not a
rare decider) could achieve on the addressable set. Compares to current baseline.
"""
import time
import duckdb
import pandas as pd

import cn_match

T0 = time.time()
def el(): return "[%5.0fs]" % (time.time() - T0)

EDGES = "'../sciscinet_paper_author_affiliation.parquet'"
PAPID = "'../sciscinet_authors_paperid.parquet'"
PF = "'../sciscinet_paperfields.parquet'"
FLD = "'../sciscinet_fields.parquet'"

con = duckdb.connect()
con.sql("SET temp_directory='.duckdb_tmp'")
con.sql("SET memory_limit='12GB'")
con.sql("SET preserve_insertion_order=false")

cn_df, _ = cn_match.load_cn()
gold = cn_df[cn_df["gold_authorid"].notna()][["rid", "gold_authorid"]].copy()
con.register("gold_raw", gold)
con.sql("""CREATE TABLE gold AS
    SELECT g.rid, g.gold_authorid, h.host_openalex_id AS host
    FROM gold_raw g JOIN 'cn_host_ids_bridge.csv' h ON g.rid = h.row_id
    WHERE h.host_openalex_id IS NOT NULL AND h.host_openalex_id <> ''""")
con.sql("""CREATE TABLE ehost AS
    SELECT DISTINCT authorid, institutionid FROM %s
    WHERE institutionid IN (SELECT DISTINCT host FROM gold)""" % EDGES)
# addressable candidates: same-host candidates for rids where gold also survives host
con.sql("""CREATE TABLE cah AS
    SELECT DISTINCT m.rid, m.authorid, g.host, g.gold_authorid
    FROM 'match_1_cn.csv' m JOIN gold g ON m.rid = g.rid
    JOIN ehost e ON e.authorid = m.authorid AND e.institutionid = g.host
    WHERE EXISTS (SELECT 1 FROM ehost e2
                  WHERE e2.authorid = g.gold_authorid AND e2.institutionid = g.host)""")
con.sql("""CREATE TABLE gpc AS
    SELECT authorid, count(DISTINCT paperid) AS n_papers FROM %s
    WHERE authorid IN (SELECT DISTINCT authorid FROM cah) GROUP BY 1""" % PAPID)

# top-3 level-1 subfields for every candidate authorid
con.sql("""CREATE TABLE top3 AS
    WITH ids AS (SELECT DISTINCT authorid FROM cah),
    papers AS (SELECT authorid, paperid FROM %s WHERE authorid IN (SELECT authorid FROM ids)),
    lvl1 AS (SELECT fieldid FROM %s WHERE level = 1),
    fc AS (SELECT p.authorid, pf.fieldid, count(*) AS n
           FROM papers p JOIN %s pf ON p.paperid = pf.paperid
           WHERE pf.fieldid IN (SELECT fieldid FROM lvl1) GROUP BY 1,2),
    ranked AS (SELECT authorid, fieldid,
                      row_number() OVER (PARTITION BY authorid ORDER BY n DESC, fieldid) rnk
               FROM fc)
    SELECT authorid, list(fieldid) AS fields FROM ranked WHERE rnk <= 3 GROUP BY authorid""" % (PAPID, FLD, PF))
print("%s subfields computed" % el(), flush=True)

feat = con.sql("""
    SELECT c.rid, c.authorid, c.gold_authorid, COALESCE(p.n_papers,0) AS n_papers,
           (c.authorid = c.gold_authorid) AS is_gold, t.fields
    FROM cah c LEFT JOIN gpc p ON p.authorid = c.authorid
    LEFT JOIN top3 t ON t.authorid = c.authorid""").df()
import numpy as np
def to_set(x):
    if isinstance(x, np.ndarray):
        return set(x.tolist())
    if isinstance(x, list):
        return set(x)
    return set()
feat["fields"] = feat["fields"].apply(to_set)

def run(field_filter):
    hit = tot = 0
    for rid, g in feat.groupby("rid"):
        gold_fields = set().union(*g.loc[g["is_gold"], "fields"]) if g["is_gold"].any() else set()
        cand = g
        if field_filter and gold_fields:
            keep = g["fields"].apply(lambda s: len(s & gold_fields) > 0)
            cand = g[keep] if keep.any() else g
        pick = cand.sort_values(["n_papers", "authorid"], ascending=[False, True]).iloc[0]
        hit += bool(pick["is_gold"]); tot += 1
    return hit, tot, 100 * hit / tot

for name, ff in [("baseline: argmax papers", False),
                 ("ORACLE field-filter -> argmax papers", True)]:
    h, t, a = run(ff)
    print("  %-38s %4d/%d = %.1f%%" % (name, h, t, a))
print("%s done" % el(), flush=True)
