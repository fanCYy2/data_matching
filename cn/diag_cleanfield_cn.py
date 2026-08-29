# -*- coding: utf-8 -*-
"""诊断:洗干净候选作者的子学科标签,能不能把"真人"在领域信号上抬到"冒名者"之上?

背景(见 memory cn-disambig-negatives / cn-field-matching-research):
  层3 领域信号目前用【候选作者全部论文 count(*)】聚出 top-3 level-1 子学科,再和 NSFC 中文
  领域文本做 bge-m3 cosine。DeepSeek 猜:top-3 被中游跨学科论文污染(黄海→Transport eng.)。
  三个洗法里 level-2 数据没有(fields 只到 level 1),只剩两个可做:
    · first/last:只数该作者【一作/末作(通讯)】的论文  —— author_position 100% 填充
    · weighted :按 paperfields.score_openalex 置信度给每个 field 标签加权(而非平权 count)

这是【定向探针,不是重跑管线】。只在 573 个 disambig_wrong(真人被同名冒名者顶掉)上,量:
  对每个 rid,真人(gold)与被选中的冒名者(pred)各自 top-3 子学科对该行中文领域的 max cosine,
  margin = gold_sim - pred_sim。margin>0 = 领域信号本可把真人挑出来。
比较四种聚合下 margin>0 的占比:all(现状) / firstlast / weighted / firstlast+weighted。

判决准则:若洗完 margin>0 占比、以及 margin>=SEM_MARGIN 的占比,较 all 明显上升 → 值得升级为
全候选池 + eval_cn.py 全量重跑;若基本不动 → 领域内容路彻底证伪,停。
"""
import sys, time
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import duckdb, numpy as np, pandas as pd
import cn_match

T0 = time.time()
def el(): return "[%5.0fs]" % (time.time() - T0)

SEM_MARGIN = cn_match.SEM_MARGIN   # 0.03,与管线同口径

# ---- 1. 取 disambig_wrong 的 (rid, gold, pred) + 中文领域文本 ----
err = pd.read_csv("eval_cn_errors.csv", dtype=str)
dw = err[err["reason"] == "disambig_wrong"][["rid", "cn_name", "gold_authorid", "pred", "src"]].copy()
dw["rid"] = dw["rid"].astype(int)

cn_df, _ = cn_match.load_cn()
ftext = dict(zip(cn_df["rid"].astype(int), cn_df["field_text"]))
dw["field_text"] = dw["rid"].map(ftext)
dw = dw[dw["field_text"].notna() & dw["gold_authorid"].notna() & dw["pred"].notna()].reset_index(drop=True)
print("%s disambig_wrong 有中文领域文本+gold+pred:%d 行" % (el(), len(dw)), flush=True)

cand_ids = pd.unique(pd.concat([dw["gold_authorid"], dw["pred"]], ignore_index=True))
print("%s 涉及候选作者(gold∪pred)去重:%d" % (el(), len(cand_ids)), flush=True)

con = duckdb.connect()
con.sql("SET memory_limit='10GB'"); con.sql("SET temp_directory='.duckdb_tmp'")
con.sql("SET preserve_insertion_order=false")
con.register("cand", pd.DataFrame({"authorid": cand_ids}))

# ---- 2. 四种聚合各算 top-3 level-1 子学科(只在候选作者上,便宜)----
# 返回 {authorid: [(field_name, weight), ...]}  取 top-3
def topk_map(sql, tag):
    t = time.time()
    df = con.sql(sql).df()
    m = {}
    for aid, g in df.groupby("authorid"):
        m[aid] = list(zip(g["field_name"], g["w"]))
    print("%s   [%s] 有子学科的候选作者 %d(%.0fs)" % (el(), tag, len(m), time.time()-t), flush=True)
    return m

LVL1 = "(SELECT fieldid, display_name FROM '../sciscinet_fields.parquet' WHERE level=1)"

# (a) all:候选作者全部论文,平权 count —— 复刻现状 match_3 的 field_cnt
SQL_ALL = f"""
WITH papers AS (
    SELECT authorid, paperid FROM '../sciscinet_authors_paperid.parquet'
    WHERE authorid IN (SELECT authorid FROM cand)),
fc AS (
    SELECT p.authorid, pf.fieldid, count(*) AS w
    FROM papers p JOIN '../sciscinet_paperfields.parquet' pf ON p.paperid=pf.paperid
    WHERE pf.fieldid IN (SELECT fieldid FROM {LVL1}) GROUP BY 1,2),
r AS (SELECT authorid, fieldid, w,
             row_number() OVER (PARTITION BY authorid ORDER BY w DESC, fieldid) rk FROM fc)
SELECT r.authorid, l.display_name AS field_name, r.w
FROM r JOIN {LVL1} l ON r.fieldid=l.fieldid WHERE r.rk<=3
"""

# (b) firstlast:只数该作者一作/末作的论文(author_position 来自 affiliation 表)
SQL_FL = f"""
WITH papers AS (
    SELECT DISTINCT authorid, paperid
    FROM '../sciscinet_paper_author_affiliation.parquet'
    WHERE authorid IN (SELECT authorid FROM cand)
      AND author_position IN ('first','last')),
fc AS (
    SELECT p.authorid, pf.fieldid, count(*) AS w
    FROM papers p JOIN '../sciscinet_paperfields.parquet' pf ON p.paperid=pf.paperid
    WHERE pf.fieldid IN (SELECT fieldid FROM {LVL1}) GROUP BY 1,2),
r AS (SELECT authorid, fieldid, w,
             row_number() OVER (PARTITION BY authorid ORDER BY w DESC, fieldid) rk FROM fc)
SELECT r.authorid, l.display_name AS field_name, r.w
FROM r JOIN {LVL1} l ON r.fieldid=l.fieldid WHERE r.rk<=3
"""

# (c) weighted:全部论文,但每个 field 标签按 score_openalex 置信度求和(而非平权)
SQL_W = f"""
WITH papers AS (
    SELECT authorid, paperid FROM '../sciscinet_authors_paperid.parquet'
    WHERE authorid IN (SELECT authorid FROM cand)),
fc AS (
    SELECT p.authorid, pf.fieldid, sum(pf.score_openalex) AS w
    FROM papers p JOIN '../sciscinet_paperfields.parquet' pf ON p.paperid=pf.paperid
    WHERE pf.fieldid IN (SELECT fieldid FROM {LVL1}) GROUP BY 1,2),
r AS (SELECT authorid, fieldid, w,
             row_number() OVER (PARTITION BY authorid ORDER BY w DESC, fieldid) rk FROM fc)
SELECT r.authorid, l.display_name AS field_name, r.w
FROM r JOIN {LVL1} l ON r.fieldid=l.fieldid WHERE r.rk<=3
"""

# (d) firstlast + weighted:一作/末作论文 × score 加权
SQL_FLW = f"""
WITH papers AS (
    SELECT DISTINCT authorid, paperid
    FROM '../sciscinet_paper_author_affiliation.parquet'
    WHERE authorid IN (SELECT authorid FROM cand)
      AND author_position IN ('first','last')),
fc AS (
    SELECT p.authorid, pf.fieldid, sum(pf.score_openalex) AS w
    FROM papers p JOIN '../sciscinet_paperfields.parquet' pf ON p.paperid=pf.paperid
    WHERE pf.fieldid IN (SELECT fieldid FROM {LVL1}) GROUP BY 1,2),
r AS (SELECT authorid, fieldid, w,
             row_number() OVER (PARTITION BY authorid ORDER BY w DESC, fieldid) rk FROM fc)
SELECT r.authorid, l.display_name AS field_name, r.w
FROM r JOIN {LVL1} l ON r.fieldid=l.fieldid WHERE r.rk<=3
"""

print("%s 取子学科(4 种聚合)..." % el(), flush=True)
M = {
    "all":       topk_map(SQL_ALL, "all"),
    "firstlast": topk_map(SQL_FL,  "firstlast"),
    "weighted":  topk_map(SQL_W,   "weighted"),
    "fl+weight": topk_map(SQL_FLW, "fl+weight"),
}

# ---- 3. bge-m3 嵌入:284 个子学科名 + 全部涉及的中文领域文本 ----
vocab = con.sql(f"SELECT DISTINCT display_name FROM {LVL1} WHERE display_name IS NOT NULL").df()["display_name"].tolist()
texts = sorted({t for t in dw["field_text"] if isinstance(t, str) and t.strip()})
embed = cn_match._get_embedder()
print("%s 嵌入 %d 子学科名 + %d 领域文本..." % (el(), len(vocab), len(texts)), flush=True)
Vv = embed(vocab); Vt = embed(texts)
vix = {n: i for i, n in enumerate(vocab)}
tix = {t: i for i, t in enumerate(texts)}

def sim_author(field_text, subfields):
    """max cosine(领域文本, 该作者 top-3 子学科名) —— 与 semantic_score_v1 同口径。"""
    if not subfields or field_text not in tix:
        return np.nan
    tvec = Vt[tix[field_text]]
    best = -1.0
    for name, _w in subfields:
        if name in vix:
            best = max(best, float(Vv[vix[name]] @ tvec))
    return best if best > -1.0 else np.nan

# ---- 4. 逐 rid 逐方案算 gold_sim / pred_sim / margin ----
rows = []
for _, r in dw.iterrows():
    ft, gid, pid = r["field_text"], r["gold_authorid"], r["pred"]
    rec = {"rid": r["rid"], "src": r["src"]}
    for scheme, m in M.items():
        gs = sim_author(ft, m.get(gid)); ps = sim_author(ft, m.get(pid))
        rec[f"{scheme}_gold"] = gs
        rec[f"{scheme}_pred"] = ps
        rec[f"{scheme}_margin"] = (gs - ps) if (gs==gs and ps==ps) else np.nan
    # 子学科集合是否随聚合改变(证明 lever 真的动了)
    def nameset(m, aid):
        return frozenset(n for n, _ in m.get(aid, []))
    rec["gold_fl_changed"] = int(nameset(M["all"], gid) != nameset(M["firstlast"], gid))
    rec["gold_w_changed"]  = int(nameset(M["all"], gid) != nameset(M["weighted"], gid))
    rows.append(rec)
res = pd.DataFrame(rows)
res.to_csv("diag_cleanfield_cn.csv", index=False, encoding="utf-8-sig")

# ---- 5. 汇总 ----
print("\n" + "=" * 74)
print("每种聚合:gold vs pred 头对头(N=%d disambig_wrong)" % len(res))
print("  cover = gold&pred 都有子学科档案的 rid 数(firstlast 会因无一作/末作而掉档)")
print("-" * 74)
print("  %-11s %6s %8s %9s %11s %13s" %
      ("scheme", "cover", "mrg>0", "mrg>=%.2f" % SEM_MARGIN, "mean_mrg", "median_mrg"))
for scheme in M:
    mcol = res[f"{scheme}_margin"].dropna()
    n = len(mcol)
    if n == 0:
        print("  %-11s %6d  (无可比)" % (scheme, 0)); continue
    gt0 = (mcol > 0).mean()
    gtm = (mcol >= SEM_MARGIN).mean()
    print("  %-11s %6d %7.1f%% %8.1f%% %11.4f %13.4f" %
          (scheme, n, 100*gt0, 100*gtm, mcol.mean(), mcol.median()))
print("-" * 74)
# lever 是否真的改变了 gold 的子学科集合
print("gold 子学科集合被改变的比例:firstlast %.0f%% | weighted %.0f%%(=0 则该 lever 对 gold 无效)"
      % (100*res["gold_fl_changed"].mean(), 100*res["gold_w_changed"].mean()))
# 关键增量:相对 all,margin 由负转正(本来选错、洗后能选对)的 rid 数
base = res["all_margin"]
for scheme in ["firstlast", "weighted", "fl+weight"]:
    s = res[f"{scheme}_margin"]
    both = base.notna() & s.notna()
    flip_win  = int(((base[both] <= 0) & (s[both] > 0)).sum())   # 本来 gold 输 → 洗后赢
    flip_lose = int(((base[both] > 0) & (s[both] <= 0)).sum())   # 本来 gold 赢 → 洗后输(副作用)
    print("  vs all:%s 使 gold 由负转正 %d,由正转负 %d,净 %+d(N可比=%d)"
          % (scheme, flip_win, flip_lose, flip_win - flip_lose, int(both.sum())))
print("=" * 74)
print("%s 明细已写 diag_cleanfield_cn.csv" % el(), flush=True)
