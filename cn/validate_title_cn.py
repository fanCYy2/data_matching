# -*- coding: utf-8 -*-
"""Validate TITLE-based field matching on gold, BEFORE wiring into the pipeline.

Idea: OpenAlex subfield TAGS are noisy (tag-based filter regressed to ~63%).
Match the NSFC Chinese field text against each candidate's actual paper TITLES
(clean content) via bge-m3 cross-lingual cosine instead.

Population: addressable set = gold rows whose gold id survives the host filter,
with the host-surviving candidate pool (same setup as the 94% oracle probe).
Sample S rids for speed. For each candidate: concat up to K titles -> 1 bge-m3 vector.
Score cosine(title_vec, field_text_vec). Compare:
  baseline argmax papers  |  argmax title-sim  |  filter(sim>=floor)+argmax papers
  vs oracle(subfield overlap) for reference.
"""
import os, sys, time
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import duckdb, numpy as np, pandas as pd
import cn_match

T0 = time.time()
def el(): return "[%5.0fs]" % (time.time() - T0)

S = int(os.environ.get("S", "600"))     # sampled addressable rids
K = int(os.environ.get("K", "16"))      # titles per candidate
EDGES = "'../sciscinet_paper_author_affiliation.parquet'"
PAPID = "'../sciscinet_authors_paperid.parquet'"
TITLE = "'../sciscinet_papertitleabstract.parquet'"
PF = "'../sciscinet_paperfields.parquet'"; FLD = "'../sciscinet_fields.parquet'"

con = duckdb.connect()
con.sql("SET temp_directory='.duckdb_tmp'"); con.sql("SET memory_limit='12GB'")
con.sql("SET preserve_insertion_order=false")

cn_df, _ = cn_match.load_cn()
gold = cn_df[cn_df["gold_authorid"].notna()][["rid", "gold_authorid", "field_text"]].copy()
con.register("gold_raw", gold[["rid", "gold_authorid"]])
con.sql("""CREATE TABLE gold AS
    SELECT g.rid, g.gold_authorid, h.host_openalex_id AS host
    FROM gold_raw g JOIN 'cn_host_ids_bridge.csv' h ON g.rid = h.row_id
    WHERE h.host_openalex_id IS NOT NULL AND h.host_openalex_id <> ''""")
con.sql("""CREATE TABLE ehost AS
    SELECT DISTINCT authorid, institutionid FROM %s
    WHERE institutionid IN (SELECT DISTINCT host FROM gold)""" % EDGES)
# addressable candidates (gold survives host filter), sampled to S rids
con.sql("""CREATE TABLE cah AS
    SELECT DISTINCT m.rid, m.authorid, g.host, g.gold_authorid
    FROM 'match_1_cn.csv' m JOIN gold g ON m.rid = g.rid
    JOIN ehost e ON e.authorid = m.authorid AND e.institutionid = g.host
    WHERE EXISTS (SELECT 1 FROM ehost e2
                  WHERE e2.authorid = g.gold_authorid AND e2.institutionid = g.host)
      AND m.rid IN (SELECT rid FROM (SELECT DISTINCT rid FROM gold ORDER BY rid) USING SAMPLE %d ROWS (reservoir, 42))""" % S)
n_rid = con.sql("SELECT count(DISTINCT rid) FROM cah").fetchone()[0]
n_cand = con.sql("SELECT count(*) FROM cah").fetchone()[0]
print("%s sample: %d rids, %d candidates" % (el(), n_rid, n_cand), flush=True)

# n_papers (tiebreak key) + oracle subfields
con.sql("""CREATE TABLE gpc AS SELECT authorid, count(DISTINCT paperid) AS n_papers
    FROM %s WHERE authorid IN (SELECT DISTINCT authorid FROM cah) GROUP BY 1""" % PAPID)
con.sql("""CREATE TABLE top3 AS
    WITH ids AS (SELECT DISTINCT authorid FROM cah),
    papers AS (SELECT authorid,paperid FROM %s WHERE authorid IN (SELECT authorid FROM ids)),
    lvl1 AS (SELECT fieldid FROM %s WHERE level=1),
    fc AS (SELECT p.authorid,pf.fieldid,count(*) n FROM papers p
           JOIN %s pf ON p.paperid=pf.paperid WHERE pf.fieldid IN (SELECT fieldid FROM lvl1) GROUP BY 1,2),
    r AS (SELECT authorid,fieldid,row_number() OVER(PARTITION BY authorid ORDER BY n DESC,fieldid) rk FROM fc)
    SELECT authorid, list(fieldid) fields FROM r WHERE rk<=3 GROUP BY authorid""" % (PAPID, FLD, PF))

# up to K titles per candidate, concatenated
con.sql("""CREATE TABLE cand_titles AS
    WITH cp AS (
        SELECT authorid, paperid,
               row_number() OVER (PARTITION BY authorid ORDER BY paperid) rk
        FROM %s WHERE authorid IN (SELECT DISTINCT authorid FROM cah))
    SELECT c.authorid, string_agg(t.title, ' . ') AS doc, count(*) AS n_titles
    FROM cp c JOIN %s t ON c.paperid = t.paperid
    WHERE c.rk <= %d AND t.title IS NOT NULL AND t.title <> ''
    GROUP BY c.authorid""" % (PAPID, TITLE, K))
print("%s titles fetched" % el(), flush=True)

feat = con.sql("""
    SELECT c.rid, c.authorid, c.gold_authorid, COALESCE(p.n_papers,0) AS n_papers,
           (c.authorid=c.gold_authorid) AS is_gold, ct.doc, t.fields
    FROM cah c LEFT JOIN gpc p ON p.authorid=c.authorid
    LEFT JOIN cand_titles ct ON ct.authorid=c.authorid
    LEFT JOIN top3 t ON t.authorid=c.authorid""").df()

# ---- embed: bge-m3, longer context for author docs ----
import torch
from transformers import AutoTokenizer, AutoModel
dev = "cuda" if torch.cuda.is_available() else "cpu"
tok = AutoTokenizer.from_pretrained("BAAI/bge-m3")
mdl = AutoModel.from_pretrained("BAAI/bge-m3").to(dev).eval()
@torch.no_grad()
def embed(texts, maxlen):
    out = []
    for i in range(0, len(texts), 64):
        enc = tok(texts[i:i+64], padding=True, truncation=True, max_length=maxlen, return_tensors="pt").to(dev)
        v = mdl(**enc).last_hidden_state[:, 0]
        out.append(torch.nn.functional.normalize(v, p=2, dim=1).cpu().numpy())
    return np.vstack(out)
print("%s bge-m3 loaded (device=%s)" % (el(), dev), flush=True)

ftext = dict(zip(gold["rid"].astype(int), gold["field_text"]))
docs = feat["doc"].fillna("").tolist()
has_doc = [bool(d.strip()) for d in docs]
DV = np.zeros((len(docs), 1024), dtype=np.float32)
idx = [i for i, h in enumerate(has_doc) if h]
if idx:
    DV[idx] = embed([docs[i] for i in idx], 256)
rids_uni = sorted(set(feat["rid"]))
ftxts = [str(ftext.get(r, "")) for r in rids_uni]
FV = embed(ftxts, 64)
fvec = {r: FV[i] for i, r in enumerate(rids_uni)}
feat["sim"] = [float(DV[i] @ fvec[feat["rid"].iloc[i]]) if has_doc[i] else np.nan
               for i in range(len(feat))]
print("%s scored" % el(), flush=True)

feat["fset"] = feat["fields"].apply(lambda x: set(x.tolist()) if isinstance(x, np.ndarray) else (set(x) if isinstance(x, list) else set()))
groups = {rid: g for rid, g in feat.groupby("rid")}
N = len(groups)

def report(name, pick_fn):
    hit = sum(bool(pick_fn(g)["is_gold"]) for g in groups.values())
    print("  %-38s %4d/%d = %.1f%%" % (name, hit, N, 100*hit/N))

def by_papers(g): return g.sort_values(["n_papers","authorid"],ascending=[False,True]).iloc[0]
def by_sim(g):
    gg = g[g["sim"].notna()]
    return by_papers(g) if gg.empty else gg.sort_values(["sim","authorid"],ascending=[False,True]).iloc[0]
def filt(floor):
    def f(g):
        s = g[g["sim"] >= floor]
        return by_papers(s if not s.empty else g)
    return f
def oracle(g):
    gf = set().union(*g.loc[g["is_gold"],"fset"]) if g["is_gold"].any() else set()
    if gf:
        k = g[g["fset"].apply(lambda s: len(s & gf) > 0)]
        return by_papers(k if not k.empty else g)
    return by_papers(g)

print("=" * 62)
report("baseline: argmax papers", by_papers)
report("argmax title-sim (pure content)", by_sim)
for fl in [0.40, 0.45, 0.50, 0.55, 0.60]:
    report("filter title-sim>=%.2f + argmax papers" % fl, filt(fl))
report("ORACLE subfield-overlap (reference)", oracle)
print("=" * 62)
# discrimination: gold vs non-gold sim
gs = feat.loc[feat["is_gold"] & feat["sim"].notna(), "sim"]
ws = feat.loc[~feat["is_gold"] & feat["sim"].notna(), "sim"]
print("title-sim  gold: mean=%.3f p25=%.3f | nongold: mean=%.3f p75=%.3f"
      % (gs.mean(), gs.quantile(.25), ws.mean(), ws.quantile(.75)))
print("%s done" % el(), flush=True)
