# -*- coding: utf-8 -*-
"""US 层3 v2(abstract-vs-abstract)验证 + 阈值标定。

真值 = 层2 机构确认(round2_inst)的 rid:host 机构过滤在同名候选里【唯一】选出一个
authorid,该 authorid 视作银真值。机构信号与语义(领域)正交,故可当独立真值用来标语义阈值
——类比 EU 用 ORCID(见 eu/eval_sem_v2.py)。

在这些 rid 的【全部层1 同名候选】上打 v2 分,评估:
  1) acc@1:机构确认的真人能否在 v2 语义分里排第1(loose / strict / ties 并列分不开);
  2) 正确 / 错误候选的分数分布(再按作者有摘要文本 vs 回退子学科名拆分);
  3) THRESH / MARGIN 网格 → 覆盖 vs 正确 权衡,据此定 SEM_THRESH_V2 / SEM_MARGIN_V2。

前置:author_vectors_us.npz 需覆盖这些 rid 的候选(build 候选并集里已含 round2_inst 候选)。
运行:python eval_sem_us.py
"""
import sys

import numpy as np
import pandas as pd

import us_match as m   # 导入即建 con / us 视图 / host 桥;_semantic_score_v2 直接可用
from us_embed import load_author_vectors

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

if not m._use_v2():
    raise SystemExit("[error] author_vectors_us.npz 不存在或 US_SEM_V2=0;先建作者向量再标定。")

# ---- 真值:round2_inst(match_2_us.csv 里该 rid 唯一 authorid)----
m2 = pd.read_csv("match_2_us.csv")
cnt = m2.groupby("rid")["authorid"].nunique()
truth_rids = set(cnt[cnt == 1].index)
truth = (m2[m2["rid"].isin(truth_rids)][["rid", "authorid"]]
         .drop_duplicates().rename(columns={"authorid": "true_authorid"}))
print(f"真值 rid(round2_inst 机构唯一确认): {len(truth)}")

# ---- 候选:这些 rid 的全部层1 同名候选(真人 + 同名错误候选)----
m1 = pd.read_csv("match_1_us.csv")
cin = (m1[m1["rid"].isin(truth_rids)][["rid", "us_name", "authorid", "match_type"]]
       .drop_duplicates().copy())
cin["host_id"] = pd.NA
m.con.register("r3in", cin)
long_df = m.match_3()          # 补 us_text / field_name / share / rnk

# ---- v2 打分(与 us_match.py 同一实现)----
v2 = m._semantic_score_v2(long_df).rename(columns={"sim": "sim_v2"})
cand = v2.merge(truth, on="rid", how="left")
cand["is_true"] = cand["authorid"] == cand["true_authorid"]

aid2i, _ = load_author_vectors()
cand["has_text"] = cand["authorid"].map(lambda a: a in aid2i)

ncand = cand.groupby("rid")["authorid"].nunique()
multi = set(ncand[ncand >= 2].index)
print(f"候选>=2 的 rid: {len(multi)}  (候选=1 平凡命中 {int((ncand == 1).sum())})")


def acc_at1(col):
    """loose:真人分>=所有候选(含并列即算赢);strict:真人分严格>所有错误候选;
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
print("  方法      loose        strict       ties(并列分不开)")
lo, st, ti, cov = acc_at1("sim_v2")
if cov:
    print(f"  sim_v2 : {lo:3d}/{cov} ({lo/cov*100:.0f}%)  {st:3d}/{cov} ({st/cov*100:.0f}%)  {ti}")
else:
    print("  (无可评 rid)")

print("\n===== v2 分数分布(候选>=2) =====")
sub = cand[cand["rid"].isin(multi)]
for tag, mask in [("正确", sub["is_true"]), ("错误", ~sub["is_true"])]:
    s = sub[mask]["sim_v2"].dropna()
    if len(s):
        print(f"  {tag}: n={len(s)} mean={s.mean():.3f} p10={s.quantile(.1):.3f} "
              f"p50={s.median():.3f} p90={s.quantile(.9):.3f}")
print("  --- 正确候选按作者是否有摘要文本拆分 ---")
for tag, mask in [("有文本", sub["is_true"] & sub["has_text"]),
                  ("回退名", sub["is_true"] & ~sub["has_text"])]:
    s = sub[mask]["sim_v2"].dropna()
    if len(s):
        print(f"  {tag}: n={len(s)} mean={s.mean():.3f} p10={s.quantile(.1):.3f} p50={s.median():.3f}")

# ---- 阈值网格:模拟层3 判定(理想:correct 高、wrong=0、defer 少)----
print("\n===== 阈值扫描(真值 round2_inst、候选>=2;correct=选中真人,wrong=选错,defer=不定交层4)=====")
print("  THRESH MARGIN | resolved correct wrong  defer")
rids_multi = sorted(multi)
best = None
for th in [0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70]:
    for mg in [0.00, 0.01, 0.02, 0.03, 0.05]:
        res = corr = wrong = 0
        for rid in rids_multi:
            g = cand[cand["rid"] == rid]
            p = g[g["sim_v2"] >= th].sort_values("sim_v2", ascending=False)
            if p.empty:
                continue
            if len(p) == 1 or (p.iloc[0]["sim_v2"] - p.iloc[1]["sim_v2"] >= mg):
                res += 1
                corr += int(bool(p.iloc[0]["is_true"]))
                wrong += int(not bool(p.iloc[0]["is_true"]))
        defer = len(rids_multi) - res
        print(f"   {th:.2f}  {mg:.2f}  |   {res:4d}   {corr:4d}  {wrong:4d}  {defer:4d}")
