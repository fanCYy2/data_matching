# -*- coding: utf-8 -*-
"""cn_match.py 的评测:用 cn.xlsx 里已有的 1949 个 authorid 真值,估两个指标。

  指标 A · 层1 候选召回:真值 authorid 是否落在该行【层1 候选池】(match_1_cn.csv)里。
           —— 衡量「中文→拼音」罗马化覆盖度,是本轮的主指标。
  指标 B · 端到端准确:最终选中(matched_final_cn.csv)是否等于真值。按 source / 年份拆分。
           —— 衡量整条漏斗;层2 暂缺 + 跨语种领域信号弱,常见名(多个 Wei Wang)会偏低,预期如此。

真值里有少数 authorid 已被 OpenAlex 合并/重定向,不在 authors.parquet 里(记为 unresolvable),
指标 A 的召回率同时按【全部真值】与【可解析真值】两个分母报,避免被这几个拖低。
"""

import duckdb
import pandas as pd

import cn_match   # 复用 load_cn(含真值列)


def load_gold():
    cn_df, _ = cn_match.load_cn()
    gold = cn_df[cn_df["gold_authorid"].notna()][
        ["rid", "cn_name", "year", "gold_authorid"]].copy()
    return gold


def main():
    gold = load_gold()
    con = duckdb.connect()
    con.sql("SET memory_limit='8GB'"); con.sql("SET temp_directory='.duckdb_tmp'")

    # 哪些真值 id 在 authors.parquet 里(可解析)
    con.register("gold", gold[["gold_authorid"]].drop_duplicates())
    resolvable = set(con.sql("""
        SELECT g.gold_authorid FROM gold g
        JOIN '../sciscinet_authors.parquet' a ON g.gold_authorid = a.authorid
    """).df()["gold_authorid"])
    gold["resolvable"] = gold["gold_authorid"].isin(resolvable)

    n_all = len(gold)
    n_res = int(gold["resolvable"].sum())
    print("真值:%d 行 | 可解析(在 authors.parquet)%d | 不可解析(已合并)%d"
          % (n_all, n_res, n_all - n_res))
    print("=" * 70)

    # ---------- 指标 A:层1 候选召回 ----------
    cand = pd.read_csv("match_1_cn.csv", dtype=str)
    cand_by_rid = cand.groupby("rid")["authorid"].agg(set).to_dict()
    gold["rid_s"] = gold["rid"].astype(str)
    gold["recalled"] = [g in cand_by_rid.get(r, set())
                        for r, g in zip(gold["rid_s"], gold["gold_authorid"])]
    gold["n_cand"] = [len(cand_by_rid.get(r, set())) for r in gold["rid_s"]]

    recA_all = gold["recalled"].mean()
    recA_res = gold.loc[gold["resolvable"], "recalled"].mean()
    print("指标 A · 层1 候选召回:")
    print("  全部真值 : %d/%d = %.1f%%" % (gold["recalled"].sum(), n_all, 100 * recA_all))
    print("  可解析   : %d/%d = %.1f%%"
          % (gold.loc[gold["resolvable"], "recalled"].sum(), n_res, 100 * recA_res))
    print("  候选池大小(有召回的行):中位 %d,均值 %.0f,90分位 %d"
          % (gold.loc[gold["recalled"], "n_cand"].median(),
             gold.loc[gold["recalled"], "n_cand"].mean(),
             gold.loc[gold["recalled"], "n_cand"].quantile(0.9)))
    print("=" * 70)

    # ---------- 指标 B:端到端准确 ----------
    fin = pd.read_csv("matched_final_cn.csv", dtype=str)
    fin_pick = dict(zip(fin["rid"].astype(str), fin["authorid"]))
    fin_src = dict(zip(fin["rid"].astype(str), fin["source"]))
    gold["pred"] = [fin_pick.get(r) for r in gold["rid_s"]]
    gold["src"] = [fin_src.get(r) for r in gold["rid_s"]]
    gold["correct"] = gold["pred"] == gold["gold_authorid"]
    gold["has_pred"] = gold["pred"].notna()

    accB_all = gold["correct"].mean()
    accB_res = gold.loc[gold["resolvable"], "correct"].mean()
    print("指标 B · 端到端准确(最终选中 == 真值):")
    print("  全部真值 : %d/%d = %.1f%%" % (gold["correct"].sum(), n_all, 100 * accB_all))
    print("  可解析   : %d/%d = %.1f%%"
          % (gold.loc[gold["resolvable"], "correct"].sum(), n_res, 100 * accB_res))
    print("  (给出了预测的行 %d,其中正确 %d)"
          % (gold["has_pred"].sum(), gold["correct"].sum()))

    print("\n  按 source 拆(该来源命中的真值行 / 该来源覆盖的真值行 = 精度):")
    for s, g in gold[gold["has_pred"]].groupby("src"):
        print("    %-18s 命中 %d/%d = %.1f%%" % (s, g["correct"].sum(), len(g),
                                                100 * g["correct"].mean()))

    print("\n  分解:")
    recalled_wrong = gold[gold["recalled"] & ~gold["correct"]]
    not_recalled = gold[~gold["recalled"] & gold["resolvable"]]
    print("    名字层召回但最终选错(消歧失败):%d" % len(recalled_wrong))
    print("    名字层根本没召回(可解析真值)  :%d" % len(not_recalled))
    print("=" * 70)

    # ---------- 落盘:错误明细,便于人工核 ----------
    con.register("g", gold[["gold_authorid", "pred"]].dropna().drop_duplicates())
    names = con.sql("""
        SELECT a.authorid, a.display_name FROM '../sciscinet_authors.parquet' a
        WHERE a.authorid IN (SELECT gold_authorid FROM g)
           OR a.authorid IN (SELECT pred FROM g WHERE pred IS NOT NULL)
    """).df()
    nm = dict(zip(names["authorid"], names["display_name"]))
    err = gold[~gold["correct"]].copy()
    err["gold_name"] = err["gold_authorid"].map(nm)
    err["pred_name"] = err["pred"].map(nm)
    err["reason"] = err.apply(
        lambda r: "not_recalled" if (r["resolvable"] and not r["recalled"])
        else ("unresolvable_gold" if not r["resolvable"] else "disambig_wrong"), axis=1)
    err = err.sort_values(["reason", "year"])[
        ["rid", "cn_name", "year", "gold_authorid", "gold_name",
         "pred", "pred_name", "src", "recalled", "n_cand", "reason"]]
    err.to_csv("eval_cn_errors.csv", index=False, encoding="utf-8-sig")
    print("错误明细已写 eval_cn_errors.csv(%d 行);reason 分布:%s"
          % (len(err), err["reason"].value_counts().to_dict()))


if __name__ == "__main__":
    main()
