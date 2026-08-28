# -*- coding: utf-8 -*-
"""Internal ORCID harness for CN matching (§6.2, candidate-verification strategy).

Strategy chosen by user: ORCID public-API + candidate verification. Rather than
find each NSFC awardee's ORCID from scratch, we enumerate the recalled same-name
candidates that ALREADY carry an ORCID (the true candidate carries one ~88% of the
time), then (later, in the API stage) fetch each such ORCID's public profile and
verify its self-declared name/affiliation against the NSFC row to pick the winner.

This harness is the pure-internal part (no network):
  1. orcid key: (authorid -> orcid), verified 1:1 for gold; restricted to candidate pool.
  2. worklist: for EVERY rid, the candidates-with-ORCID to fetch/verify  -> cand_orcids_cn.csv
  3. ceiling + free-signal analysis on gold, and a reusable evaluator:
       - CEILING of candidate-verification = P(gold recalled AND gold has orcid)
       - how often exactly ONE recalled candidate carries an orcid (free pick, no API)
       - does adding host-survivor filter collapse multi-orcid rows to 1
       - oracle: pick candidate whose orcid == gold's orcid  (upper bound precision/cov)
Run:  python orcid_harness_cn.py     (cwd-independent; chdir to cn inside)
"""
import os, sys, time
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
os.chdir("d:/data_matching/cn"); sys.path.insert(0, "d:/data_matching/cn")
import duckdb, numpy as np, pandas as pd
import cn_match

T0 = time.time()
def el(): return "[%5.0fs]" % (time.time() - T0)

DET = "'../sciscinet_author_details.parquet'"
PAP = "'../sciscinet_authors_paperid.parquet'"
AFF = "'../sciscinet_paper_author_affiliation.parquet'"

con = duckdb.connect()
con.sql("SET temp_directory='.duckdb_tmp'"); con.sql("SET memory_limit='12GB'")
con.sql("SET preserve_insertion_order=false")

# ---- gold + candidate pool (all rids) ----
cn_df, _ = cn_match.load_cn()
gold = cn_df[cn_df["gold_authorid"].notna()][["rid", "gold_authorid"]].copy()
gold["rid"] = gold["rid"].astype(int)
con.register("gold", gold)

con.sql("""CREATE TABLE cand AS
    SELECT DISTINCT CAST(rid AS INTEGER) AS rid, authorid, match_type
    FROM read_csv('match_1_cn.csv', types={'rid':'INTEGER','authorid':'VARCHAR','match_type':'VARCHAR'})""")
n_rids = con.sql("SELECT count(DISTINCT rid) FROM cand").fetchone()[0]

# ---- orcid key (authorid -> orcid), restricted to candidate pool ----
con.sql(f"""CREATE TABLE okey AS
    SELECT DISTINCT d.authorid, d.orcid
    FROM {DET} d
    WHERE d.orcid IS NOT NULL AND d.orcid <> ''
      AND d.authorid IN (SELECT DISTINCT authorid FROM cand)""")

# ---- candidates carrying an orcid (the API worklist), + n_papers + host-survivor flag ----
con.sql("""CREATE VIEW hostb AS
    SELECT CAST(row_id AS INTEGER) AS rid, host_openalex_id AS host
    FROM 'cn_host_ids_bridge.csv'
    WHERE host_openalex_id IS NOT NULL AND host_openalex_id <> ''""")
con.sql(f"""CREATE TABLE cand_orc AS
    SELECT c.rid, c.authorid, c.match_type, k.orcid
    FROM cand c JOIN okey k ON c.authorid = k.authorid""")
con.sql(f"""CREATE TABLE conp AS
    SELECT authorid, count(DISTINCT paperid) AS n_papers FROM {PAP}
    WHERE authorid IN (SELECT DISTINCT authorid FROM cand_orc) GROUP BY 1""")
con.sql(f"""CREATE TABLE aff AS
    SELECT DISTINCT authorid, institutionid FROM {AFF}
    WHERE institutionid IN (SELECT DISTINCT host FROM hostb)
      AND authorid IN (SELECT DISTINCT authorid FROM cand_orc)""")
work = con.sql(f"""
    SELECT co.rid, co.authorid, co.orcid, co.match_type,
           COALESCE(p.n_papers,0) AS n_papers,
           (h.host IS NOT NULL AND a.authorid IS NOT NULL) AS host_survivor
    FROM cand_orc co
    LEFT JOIN conp p ON p.authorid = co.authorid
    LEFT JOIN hostb h ON h.rid = co.rid
    LEFT JOIN aff a ON a.authorid = co.authorid AND a.institutionid = h.host
    ORDER BY co.rid, host_survivor DESC, n_papers DESC""").df()
work.to_csv("cand_orcids_cn.csv", index=False, encoding="utf-8-sig")
print("%s worklist: %d (rid,cand-with-orcid) rows over %d rids -> cand_orcids_cn.csv"
      % (el(), len(work), work["rid"].nunique()), flush=True)

# ---- gold analysis ----
gold_orcid = set(con.sql("""SELECT g.gold_authorid FROM gold g JOIN okey k ON g.gold_authorid=k.authorid""").df()["gold_authorid"])
gset = dict(zip(gold["rid"], gold["gold_authorid"]))
w_by_rid = {rid: g for rid, g in work.groupby("rid")}
N = len(gold)

rows = []
for rid, gaid in gset.items():
    g = w_by_rid.get(rid)
    cand_orcs = [] if g is None else list(g["authorid"])
    hs_orcs = [] if g is None else list(g.loc[g["host_survivor"], "authorid"])
    gold_has_orcid = gaid in gold_orcid
    gold_in_candorc = gaid in cand_orcs
    rows.append(dict(rid=rid, gaid=gaid, n_candorc=len(cand_orcs), n_hs_candorc=len(hs_orcs),
                     gold_has_orcid=gold_has_orcid, gold_in_candorc=gold_in_candorc,
                     sole=cand_orcs[0] if len(cand_orcs) == 1 else None,
                     sole_hs=hs_orcs[0] if len(hs_orcs) == 1 else None))
G = pd.DataFrame(rows)

def pc(name, mask, pick_col):
    sub = G[mask]
    if len(sub) == 0:
        print("  %-46s cov 0" % name); return
    hit = (sub[pick_col] == sub["gaid"]).sum()
    print("  %-46s prec %5.1f%%  cov %4d/%d = %4.1f%%%s"
          % (name, 100*hit/len(sub), hit, N, 100*len(sub)/N, "   <<< >=95%" if hit/len(sub) >= 0.95 else ""))

print("=" * 84)
print("CEILING of candidate-verification (need true cand recalled AND carrying orcid):")
print("  gold has orcid (in dataset)        : %4d/%d = %.1f%%" % (G["gold_has_orcid"].sum(), N, 100*G["gold_has_orcid"].mean()))
print("  gold IS among candidates-with-orcid: %4d/%d = %.1f%%  <-- realistic ceiling"
      % (G["gold_in_candorc"].sum(), N, 100*G["gold_in_candorc"].mean()))
print("-" * 84)
print("per-row count of candidates-carrying-orcid:")
print("  " + G["n_candorc"].value_counts().sort_index().head(12).to_string().replace("\n", "\n  "))
print("-" * 84)
print("FREE signals (no API): pick the sole candidate-with-orcid")
pc("exactly 1 cand-with-orcid -> pick it", G["n_candorc"] == 1, "sole")
pc("exactly 1 HOST-survivor-with-orcid -> pick it", G["n_hs_candorc"] == 1, "sole_hs")
print("-" * 84)
print("ORACLE (upper bound if API verification is perfect):")
G["oracle_pick"] = np.where(G["gold_in_candorc"], G["gaid"], None)
pc("pick gold-orcid candidate when present", G["gold_in_candorc"], "oracle_pick")
print("=" * 84)
print("%s done" % el(), flush=True)
