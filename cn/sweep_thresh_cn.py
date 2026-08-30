# -*- coding: utf-8 -*-
"""层3 阈值标定:用 match_3_cn.csv 里已算好的 bge-m3 `sim`(候选前3子学科名 × 中文领域文本
的最大 cosine)对齐真值,做两件事,不用重跑全流程/重嵌:

  ① 区分力诊断:layer-3 候选里【真值命中 vs 未命中】的 sim 分布 —— 看 bge-m3 到底分不分得开,
     以及「天花板」(真值候选存在率 / 真值是否是 sim 最高的那个)。
  ② 网格扫描:复刻 cn_match 的 round3 决策逻辑(sim≥THRESH 且 top1-top2≥MARGIN 才定),
     在 SEM_THRESH × SEM_MARGIN 网格上报 round3 的【定档数 / 正确数 / 精度】,选最优阈值。

只读 cn/ 下的 match_3_cn.csv + cn.xlsx(经 cn_match.load_cn 拿真值),从 cn/ 目录跑。
"""
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # 避开 Windows GBK 控制台

import numpy as np
import pandas as pd

import cn_match  # 复用 load_cn(含真值列)

MAXPAPERS_BASELINE = 0.753  # eval_cn 里 round4_maxpapers 的精度:round3 至少要超过它才有意义


def main():
    # ---- 真值:rid -> gold_authorid ----
    cn_df, _ = cn_match.load_cn()
    gold = cn_df[cn_df["gold_authorid"].notna()][["rid", "gold_authorid"]]
    gold_map = dict(zip(gold["rid"].astype(int), gold["gold_authorid"].astype(str)))

    # ---- layer-3 候选:每个 (rid, authorid) 一条 sim ----
    # 用 pandas 读(领域文本含逗号且带引号,duckdb 的 quote 自动探测会误判)
    cand = pd.read_csv("match_3_cn.csv", usecols=["rid", "authorid", "sim"],
                       dtype={"authorid": str}, encoding="utf-8-sig")
    cand = cand.dropna(subset=["sim"]).copy()
    cand["rid"] = cand["rid"].astype(int)
    cand = cand.drop_duplicates(subset=["rid", "authorid"])
    print("layer-3 候选:%d 个 (rid,authorid) | 覆盖 rid %d"
          % (len(cand), cand["rid"].nunique()))

    # 只在【有真值 且 进了 layer3】的 rid 上评估
    cand_g = cand[cand["rid"].isin(gold_map)].copy()
    cand_g["is_gold"] = [gold_map.get(r) == a
                         for r, a in zip(cand_g["rid"], cand_g["authorid"])]
    gold_rids_l3 = sorted(cand_g["rid"].unique())
    print("有真值且进 layer3 的 rid:%d" % len(gold_rids_l3))

    # ---- ① 区分力诊断 ----
    gs = cand_g.loc[cand_g["is_gold"], "sim"]
    ws = cand_g.loc[~cand_g["is_gold"], "sim"]
    qs = [0.10, 0.25, 0.50, 0.75, 0.90]
    print("\n=== ① sim 分布(layer-3 候选)===")
    print("  真值候选  n=%-5d  min/中位/均值/max = %.3f / %.3f / %.3f / %.3f"
          % (len(gs), gs.min(), gs.median(), gs.mean(), gs.max()))
    print("            分位 " + "  ".join("%d%%=%.3f" % (int(q*100), gs.quantile(q)) for q in qs))
    print("  非真值候选 n=%-5d  min/中位/均值/max = %.3f / %.3f / %.3f / %.3f"
          % (len(ws), ws.min(), ws.median(), ws.mean(), ws.max()))
    print("            分位 " + "  ".join("%d%%=%.3f" % (int(q*100), ws.quantile(q)) for q in qs))

    # 天花板:真值候选是否存在 / 是否是该 rid 的 sim 最高者
    n_gold_present = int(cand_g.groupby("rid")["is_gold"].any().sum())
    top_is_gold = 0
    for rid, g in cand_g.groupby("rid"):
        gg = g.sort_values("sim", ascending=False)
        if bool(gg.iloc[0]["is_gold"]):
            top_is_gold += 1
    print("\n=== 天花板(共 %d 个有真值 layer3 rid)===" % len(gold_rids_l3))
    print("  真值候选存在(sim 非空)   : %d (%.1f%%)"
          % (n_gold_present, 100 * n_gold_present / len(gold_rids_l3)))
    print("  真值恰是 sim 最高的候选    : %d (%.1f%%)  ← round3 用 argmax-sim 的绝对上限"
          % (top_is_gold, 100 * top_is_gold / len(gold_rids_l3)))

    # ---- 预算每个 rid 的 (sim降序, is_gold降序) 数组,供网格快速评估 ----
    per_rid = {}
    for rid, g in cand_g.groupby("rid"):
        gg = g.sort_values("sim", ascending=False)
        per_rid[rid] = (gg["sim"].to_numpy(), gg["is_gold"].to_numpy())

    def eval_cfg(thresh, margin):
        picked = correct = 0
        for sims, isg in per_rid.values():
            keep = sims >= thresh
            if not keep.any():
                continue
            ps = sims[keep]; pg = isg[keep]
            if len(ps) == 1 or (ps[0] - ps[1] >= margin):
                picked += 1
                correct += int(pg[0])
        prec = correct / picked if picked else float("nan")
        return picked, correct, prec

    # 先复现当前 0.62/0.03,核对是否≈eval 的 53/177=29.9%(验证模拟与真流水一致)
    p0, c0, pr0 = eval_cfg(0.62, 0.03)
    print("\n=== 复现当前配置 THRESH=0.62 MARGIN=0.03 ===")
    print("  round3 定档 %d,正确 %d,精度 %.1f%%（eval 实测 53/177=29.9%%,应吻合）"
          % (p0, c0, 100 * pr0))

    # ---- ② 网格扫描 ----
    threshs = [round(x, 2) for x in np.arange(0.50, 0.905, 0.02)]
    margins = [0.00, 0.01, 0.02, 0.03, 0.05, 0.08]
    print("\n=== ② 网格扫描(round3 精度;基线 maxpapers=%.1f%%,须超过才划算)==="
          % (100 * MAXPAPERS_BASELINE))
    print("  行=THRESH 列=MARGIN,单元= 定档数/正确数(精度%%)")
    header = "THRESH\\MARGIN " + "".join("%14s" % ("%.2f" % m) for m in margins)
    print(header)
    rows = []
    for t in threshs:
        cells = []
        for m in margins:
            pk, co, pr = eval_cfg(t, m)
            cells.append("%d/%d(%s)" % (pk, co, "--" if pk == 0 else "%.0f%%" % (100*pr)))
            rows.append((t, m, pk, co, pr))
        print("%-13s" % ("%.2f" % t) + "".join("%14s" % c for c in cells))

    # ---- 推荐:在"精度显著高于基线"里挑定档数最多的 ----
    df = pd.DataFrame(rows, columns=["thresh", "margin", "picked", "correct", "prec"])
    good = df[(df["picked"] >= 20) & (df["prec"] >= MAXPAPERS_BASELINE + 0.05)]
    print("\n=== 推荐候选(定档≥20 且 精度≥基线+5pt=%.0f%%),按定档数降序 ==="
          % (100 * (MAXPAPERS_BASELINE + 0.05)))
    if good.empty:
        print("  ⚠️ 没有一组能同时满足『精度显著超基线』和『有量』——")
        print("     说明 bge-m3 的 sim 在中英跨语种上区分力不足,单靠调阈值救不回 round3;")
        print("     结论应是:layer3 语义在 CN 上收益有限,考虑关掉 round3 直接进 layer4,")
        print("     或改『翻译领域词→英文再嵌』/ 换打分口径。看上面①的分布与天花板判定。")
    else:
        for _, r in good.sort_values(["picked", "prec"], ascending=False).head(8).iterrows():
            print("  THRESH=%.2f MARGIN=%.2f → 定档 %d,正确 %d,精度 %.1f%%"
                  % (r["thresh"], r["margin"], int(r["picked"]), int(r["correct"]), 100*r["prec"]))


if __name__ == "__main__":
    main()
