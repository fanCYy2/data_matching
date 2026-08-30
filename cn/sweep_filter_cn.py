# -*- coding: utf-8 -*-
"""Test DISCRETIZED field matching (approach that mirrors the 94% oracle).
Instead of thresholding bge-m3 cosine(subfield_name, field_text), we:
  1. map each Chinese field_text -> its top-K nearest level-1 subfield NAMES (argmax, not threshold)
  2. keep candidates whose own top-3 subfields overlap that set
  3. argmax n_papers among survivors (fallback to all if none)
Sweep K and report gold accuracy vs the no-filter baseline (74.0%).
Reads match_3_cn.csv (candidate subfields + field_text) + parquet (n_papers) + gold.
"""
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import duckdb
import numpy as np
import pandas as pd

import cn_match

def main():
    cn_df, _ = cn_match.load_cn()
    gold = cn_df[cn_df["gold_authorid"].notna()][["rid", "gold_authorid"]]
    gold_map = dict(zip(gold["rid"].astype(int), gold["gold_authorid"].astype(str)))

    # candidate rows: rid, authorid, field_name(candidate subfield), eu_text(Chinese field)
    df = pd.read_csv("match_3_cn.csv",
                     usecols=["rid", "authorid", "field_name", "eu_text"],
                     dtype={"authorid": str}, encoding="utf-8-sig", low_memory=False)
    df["rid"] = df["rid"].astype(int)
    df = df[df["rid"].isin(gold_map)].copy()

    # per (rid, authorid): set of candidate subfields ; per rid: field_text
    cand_fields = (df.dropna(subset=["field_name"])
                     .groupby(["rid", "authorid"])["field_name"].agg(set))
    field_text = df.dropna(subset=["eu_text"]).groupby("rid")["eu_text"].first().to_dict()

    # n_papers per candidate
    ids = df[["authorid"]].drop_duplicates()
    con = duckdb.connect(); con.sql("SET memory_limit='8GB'"); con.sql("SET temp_directory='.duckdb_tmp'")
    con.register("ids", ids)
    npdf = con.sql("""SELECT authorid, count(DISTINCT paperid) AS n_papers
        FROM '../sciscinet_authors_paperid.parquet'
        WHERE authorid IN (SELECT authorid FROM ids) GROUP BY 1""").df()
    npm = dict(zip(npdf["authorid"], npdf["n_papers"]))

    # level-1 subfield vocabulary (the space candidates live in)
    vocab = con.sql("""SELECT DISTINCT display_name FROM '../sciscinet_fields.parquet'
        WHERE level = 1 AND display_name IS NOT NULL""").df()["display_name"].tolist()

    # embed vocab + unique field texts with the same bge-m3 used in the pipeline
    embed = cn_match._get_embedder()
    ftexts = sorted(set(field_text.values()))
    V = embed(vocab)                      # (nvocab, d), L2-normalized
    F = embed(ftexts)                     # (nftext, d)
    sims = F @ V.T                        # cosine (both normalized)
    order = np.argsort(-sims, axis=1)     # nearest subfields per field text
    ft_idx = {t: i for i, t in enumerate(ftexts)}
    vocab_arr = np.array(vocab)

    # assemble per-candidate table for evaluation
    rows = []
    for (rid, aid), fset in cand_fields.items():
        rows.append((rid, aid, fset, npm.get(aid, 0), gold_map.get(rid) == aid))
    cg = pd.DataFrame(rows, columns=["rid", "authorid", "fset", "n_papers", "is_gold"])
    # also include candidates with NO subfields (fset empty) so pool matches reality
    all_cand = df[["rid", "authorid"]].drop_duplicates()
    have = set(zip(cg["rid"], cg["authorid"]))
    extra = [(r, a, set(), npm.get(a, 0), gold_map.get(r) == a)
             for r, a in zip(all_cand["rid"], all_cand["authorid"]) if (r, a) not in have]
    cg = pd.concat([cg, pd.DataFrame(extra, columns=cg.columns)], ignore_index=True)
    groups = {rid: g for rid, g in cg.groupby("rid")}
    rids = sorted(groups)
    ceiling = sum(g["is_gold"].any() for g in groups.values())
    print("population gold rids=%d | ceiling(gold in cands)=%d (%.1f%%)"
          % (len(rids), ceiling, 100*ceiling/len(rids)))

    def true_set(rid, K):
        t = field_text.get(rid)
        if t is None:
            return None
        return set(vocab_arr[order[ft_idx[t]][:K]].tolist())

    def accuracy(K):
        hit = 0
        for rid, g in groups.items():
            ts = true_set(rid, K)
            if ts:
                keep = g["fset"].apply(lambda s: len(s & ts) > 0)
                surv = g[keep] if keep.any() else g
            else:
                surv = g
            pick = surv.sort_values(["n_papers", "authorid"], ascending=[False, True]).iloc[0]
            hit += bool(pick["is_gold"])
        return hit, len(rids), 100*hit/len(rids)

    print("=" * 60)
    # baseline
    hit = 0
    for rid, g in groups.items():
        pick = g.sort_values(["n_papers", "authorid"], ascending=[False, True]).iloc[0]
        hit += bool(pick["is_gold"])
    print("baseline (no filter):                 %4d/%d = %.1f%%" % (hit, len(rids), 100*hit/len(rids)))
    print("-" * 60)
    for K in [1, 2, 3, 5, 8, 12]:
        h, n, a = accuracy(K)
        print("  top-%-2d nearest-subfield filter:     %4d/%d = %.1f%%" % (K, h, n, a))

if __name__ == "__main__":
    main()
