# -*- coding: utf-8 -*-
"""
层3 v2 语义打分的验证 + 阈值标定。真值 = ORCID 独立复核 verdict='match' 的 rid
(选中 authorid = 真人)。复用 erc_embed.score_semantic_v2(与 data_match.py 同一实现),
在 match_3.csv 的候选长表上打 v2 分,评估:
  1) acc@1:真人能否在候选里排第 1(对比 match_3.csv 里已有的老分 sim_old);
  2) 正确/错误候选的分数分布(区分作者有摘要文本 vs 回退子学科名);
  3) SEM_THRESH_V2 / SEM_MARGIN_V2 网格扫描:层3 判定的 覆盖 vs 正确 权衡。
"""
import sys
import numpy as np
import pandas as pd

from erc_embed import score_semantic_v2, load_author_vectors
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# ---- 真值 ----
truth = []
for f in ["round3_sample_50_orcid_verified.csv", "round4_sample_100_orcid_verified.csv"]:
    d = pd.read_csv(f)
    truth.append(d[d["verdict"] == "match"][["rid", "authorid"]]
                 .rename(columns={"authorid": "true_authorid"}))
truth = pd.concat(truth).drop_duplicates("rid").reset_index(drop=True)
truth_rids = set(truth["rid"])
print(f"真值 rid(verdict=match): {len(truth)}")

# ---- 候选长表(match_3.csv),取真值 rid ----
m3 = pd.read_csv("match_3.csv").rename(columns={"sim": "sim_old"})
long_df = m3[m3["rid"].isin(truth_rids)].copy()

# ---- v2 打分 ----
v2 = score_semantic_v2(long_df).rename(columns={"sim": "sim_v2"})

# 每 (rid,authorid) 的老分
old = (long_df.groupby(["rid", "authorid"])["sim_old"].max().reset_index())
cand = v2.merge(old, on=["rid", "authorid"], how="left").merge(truth, on="rid", how="left")
cand["is_true"] = cand["authorid"] == cand["true_authorid"]

aid2i, _ = load_author_vectors()
cand["has_text"] = cand["authorid"].map(lambda a: a in aid2i)

ncand = cand.groupby("rid")["authorid"].nunique()
multi = set(ncand[ncand >= 2].index)
print(f"候选>=2 的 rid: {len(multi)}  (候选=1 平凡命中 {int((ncand==1).sum())})")


def acc_at1(col):
    """loose:真人分数 >= 所有候选(含并列即算赢);strict:真人分数 严格 > 所有错误候选;
    ties:真人与某错误候选并列最高(loose 算赢但其实分不开)。"""
    loose = strict = ties = cov = 0
    for rid, g in cand.groupby("rid"):
        if rid not in multi:
            continue
        gt = g[g["is_true"]]
        if gt.empty or gt[col].isna().all():
            continue
        cov += 1
        ts = gt[col].max()
        ws = g[~g["is_true"]][col].dropna()
        wmax = ws.max() if len(ws) else -np.inf
        if ts >= wmax:
            loose += 1
            if ts > wmax:
                strict += 1
            else:
                ties += 1
    return loose, strict, ties, cov


print("\n===== acc@1(真人第1 / 可评),候选>=2 =====")
print("  方法      loose  strict  ties(并列分不开)")
for col in ["sim_old", "sim_v2"]:
    lo, st, ti, cov = acc_at1(col)
    print(f"  {col:7s}: {lo:2d}/{cov} ({lo/cov*100:.0f}%)  {st:2d}/{cov} ({st/cov*100:.0f}%)  {ti}")

print("\n===== v2 分数分布(候选>=2) =====")
sub = cand[cand["rid"].isin(multi)]
for tag, m in [("正确", sub["is_true"]), ("错误", ~sub["is_true"])]:
    s = sub[m]["sim_v2"].dropna()
    print(f"  {tag}: n={len(s)} mean={s.mean():.3f} p10={s.quantile(.1):.3f} "
          f"p50={s.median():.3f} p90={s.quantile(.9):.3f}")
print("  --- 按作者是否有摘要文本拆分(正确候选)---")
for tag, m in [("有文本", sub["is_true"] & sub["has_text"]),
               ("回退名", sub["is_true"] & ~sub["has_text"])]:
    s = sub[m]["sim_v2"].dropna()
    if len(s):
        print(f"  {tag}: n={len(s)} mean={s.mean():.3f} p10={s.quantile(.1):.3f}")

# ---- 阈值网格:模拟层3 判定 ----
print("\n===== 阈值扫描(仅真值 match、候选>=2 的 rid;理想:correct 高、wrong=0)=====")
print("  THRESH MARGIN | resolved correct wrong  defer")
rids_multi = [r for r in truth_rids if r in multi]
for th in [0.70, 0.74, 0.76, 0.78, 0.80]:
    for mg in [0.00, 0.01, 0.02, 0.03]:
        res = corr = wrong = 0
        for rid in rids_multi:
            g = cand[cand["rid"] == rid]
            p = g[g["sim_v2"] >= th].sort_values("sim_v2", ascending=False)
            if p.empty:
                continue
            if len(p) == 1 or (p.iloc[0]["sim_v2"] - p.iloc[1]["sim_v2"] >= mg):
                res += 1
                if p.iloc[0]["is_true"]:
                    corr += 1
                else:
                    wrong += 1
        print(f"   {th:.2f}  {mg:.2f}  |   {res:3d}    {corr:3d}   {wrong:3d}   {len(rids_multi)-res:3d}")
