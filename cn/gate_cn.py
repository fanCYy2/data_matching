# -*- coding: utf-8 -*-
"""Confidence gating for CN matching (§6.1 of HANDOFF).

Goal: user picked "95% = auto-subset precision" (route hard rows to manual).
Find a confidence rule whose AUTO-accepted subset hits >=95% precision on gold,
and report the coverage (fraction of gold rows auto-accepted) at that point.

Reads existing pipeline outputs only (no retrain / no embedding):
  matched_final_cn.csv (the pick + source + match_type + n_papers + host_id)
  match_1_cn.csv       (candidate pool per rid)
  cn_host_ids_bridge.csv (rid -> host institution)
  ../sciscinet_paper_author_affiliation.parquet (host-survivor recompute)
  ../sciscinet_authors_paperid.parquet          (n_papers per candidate, for margins)

Per gold row we compute candidate-agnostic confidence features, then sweep rules.
"""
import sys, time
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import duckdb, numpy as np, pandas as pd
import cn_match

T0 = time.time()
def el(): return "[%5.0fs]" % (time.time() - T0)

con = duckdb.connect()
con.sql("SET temp_directory='.duckdb_tmp'"); con.sql("SET memory_limit='12GB'")
con.sql("SET preserve_insertion_order=false")

# ---- gold ----
cn_df, _ = cn_match.load_cn()
gold = cn_df[cn_df["gold_authorid"].notna()][["rid", "gold_authorid"]].copy()
gold["rid"] = gold["rid"].astype(int)
con.register("gold", gold)

# ---- pipeline pick ----
fin = pd.read_csv("matched_final_cn.csv", dtype=str)
fin["rid"] = fin["rid"].astype(int)
fin = fin[["rid", "authorid", "source", "match_type", "host_id", "n_papers"]].rename(
    columns={"authorid": "pick", "n_papers": "pick_np"})
fin["pick_np"] = pd.to_numeric(fin["pick_np"], errors="coerce").fillna(0).astype(int)

# ---- candidate pool (match_1), restricted to gold rids ----
con.sql("""CREATE TABLE cand AS
    SELECT DISTINCT CAST(rid AS INTEGER) AS rid, authorid, match_type
    FROM read_csv('match_1_cn.csv', types={'rid':'INTEGER','authorid':'VARCHAR','match_type':'VARCHAR'})
    WHERE CAST(rid AS INTEGER) IN (SELECT rid FROM gold)""")
ncand = con.sql("SELECT rid, count(DISTINCT authorid) n_cand FROM cand GROUP BY rid").df()

# ---- host per rid ----
con.sql("""CREATE TABLE hostb AS
    SELECT CAST(row_id AS INTEGER) AS rid, host_openalex_id AS host
    FROM 'cn_host_ids_bridge.csv'
    WHERE host_openalex_id IS NOT NULL AND host_openalex_id <> ''""")

# ---- host survivors: candidates with a paper at the row's host ----
con.sql("""CREATE TABLE aff AS
    SELECT DISTINCT authorid, institutionid FROM '../sciscinet_paper_author_affiliation.parquet'
    WHERE institutionid IN (SELECT DISTINCT host FROM hostb)
      AND authorid IN (SELECT DISTINCT authorid FROM cand)""")
con.sql("""CREATE TABLE hs AS
    SELECT DISTINCT c.rid, c.authorid
    FROM cand c JOIN hostb h ON c.rid = h.rid
    JOIN aff a ON a.authorid = c.authorid AND a.institutionid = h.host""")
nhs = con.sql("SELECT rid, count(DISTINCT authorid) n_hs FROM hs GROUP BY rid").df()

# gold recalled / gold survives host
gold_recalled = con.sql("""SELECT g.rid FROM gold g
    JOIN cand c ON c.rid=g.rid AND c.authorid=g.gold_authorid""").df()["rid"]
gold_in_host = con.sql("""SELECT g.rid FROM gold g
    JOIN hs h ON h.rid=g.rid AND h.authorid=g.gold_authorid""").df()["rid"]

# ---- n_papers of every candidate in gold pools (for margins / global-max-papers) ----
con.sql("""CREATE TABLE cnp AS
    SELECT authorid, count(DISTINCT paperid) n_papers
    FROM '../sciscinet_authors_paperid.parquet'
    WHERE authorid IN (SELECT DISTINCT authorid FROM cand)
    GROUP BY authorid""")
# among host survivors: top1/top2 papers -> margin; among all cands: global max papers id
hs_np = con.sql("""SELECT h.rid, h.authorid, COALESCE(p.n_papers,0) n_papers
    FROM hs h LEFT JOIN cnp p ON p.authorid=h.authorid""").df()
allc_np = con.sql("""SELECT c.rid, c.authorid, COALESCE(p.n_papers,0) n_papers
    FROM cand c LEFT JOIN cnp p ON p.authorid=c.authorid""").df()
print("%s features built" % el(), flush=True)

# per-rid: host-survivor argmax-papers margin, and global argmax-papers authorid
def top2margin(df):
    s = df.sort_values("n_papers", ascending=False)["n_papers"].values
    if len(s) == 0: return np.nan
    if len(s) == 1: return s[0]  # sole survivor: margin = its own count (large)
    return s[0] - s[1]
hs_margin = hs_np.groupby("rid").apply(top2margin).rename("hs_margin").reset_index()
glob_max = (allc_np.sort_values(["rid", "n_papers", "authorid"], ascending=[True, False, True])
            .groupby("rid").first().reset_index()[["rid", "authorid"]]
            .rename(columns={"authorid": "gmax_pick"}))

# ---- assemble per-gold-row feature frame ----
d = gold.merge(fin, on="rid", how="left").merge(ncand, on="rid", how="left") \
        .merge(nhs, on="rid", how="left").merge(hs_margin, on="rid", how="left") \
        .merge(glob_max, on="rid", how="left")
d["n_cand"] = d["n_cand"].fillna(0).astype(int)
d["n_hs"] = d["n_hs"].fillna(0).astype(int)
d["recalled"] = d["rid"].isin(set(gold_recalled))
d["gold_in_host"] = d["rid"].isin(set(gold_in_host))
d["correct"] = d["pick"] == d["gold_authorid"]
d["pick_is_gmax"] = d["pick"] == d["gmax_pick"]
d["has_host"] = d["host_id"].notna() & (d["host_id"].astype(str) != "") & (d["host_id"].astype(str) != "nan")
N = len(d)
print("%s gold rows=%d | recalled=%.1f%% | gold_in_host=%.1f%%"
      % (el(), N, 100*d["recalled"].mean(), 100*d["gold_in_host"].mean()), flush=True)

def rule(name, mask):
    acc = d[mask]; n = len(acc)
    if n == 0:
        print("  %-52s cov 0" % name); return
    p = acc["correct"].mean()
    print("  %-52s auto-prec %5.1f%%  cov %4d/%d = %4.1f%%%s"
          % (name, 100*p, acc["correct"].sum(), N, 100*n/N, "   <<< >=95%" if p >= 0.95 else ""))

print("=" * 84)
print("baseline (accept ALL rows, argmax pipeline):")
rule("all rows", pd.Series(True, index=d.index))
print("-" * 84)
print("single-signal slices:")
rule("source=round2_inst (host unique)", d["source"] == "round2_inst")
rule("n_hs==1 (recomputed host-unique survivor)", d["n_hs"] == 1)
rule("n_hs==1 AND pick match_type=exact", (d["n_hs"] == 1) & (d["match_type"] == "exact"))
rule("source=round4_maxpapers", d["source"] == "round4_maxpapers")
rule("source=round4_semtie", d["source"] == "round4_semtie")
rule("source=round3_semantic", d["source"] == "round3_semantic")
print("-" * 84)
print("agreement / composite slices (candidates for the 95% auto set):")
rule("n_hs==1 AND pick_is_gmax", (d["n_hs"] == 1) & d["pick_is_gmax"])
rule("n_hs==1 AND exact AND pick_is_gmax", (d["n_hs"] == 1) & (d["match_type"] == "exact") & d["pick_is_gmax"])
rule("n_hs==1 AND n_cand<=5", (d["n_hs"] == 1) & (d["n_cand"] <= 5))
rule("n_hs==1 AND n_cand<=10", (d["n_hs"] == 1) & (d["n_cand"] <= 10))
rule("n_hs==1 AND exact AND n_cand<=20", (d["n_hs"] == 1) & (d["match_type"] == "exact") & (d["n_cand"] <= 20))
rule("has_host AND pick_is_gmax AND exact", d["has_host"] & d["pick_is_gmax"] & (d["match_type"] == "exact"))
rule("pick_is_gmax AND exact (no host req)", d["pick_is_gmax"] & (d["match_type"] == "exact"))
print("=" * 84)

# margin sweep on host-unique-ish rows: require host survivor argmax to lead by >= m papers
print("host-multi (n_hs>=2): require papers margin>=m among survivors + exact:")
sub = d[(d["n_hs"] >= 2) & (d["match_type"] == "exact")]
for m in [0, 20, 50, 100, 200, 500]:
    s = sub[sub["hs_margin"] >= m]
    if len(s):
        print("    margin>=%-4d  auto-prec %5.1f%%  cov(of full) %4.1f%%  (n=%d)"
              % (m, 100*s["correct"].mean(), 100*len(s)/N, len(s)))
print("%s done" % el(), flush=True)
