# -*- coding: utf-8 -*-
"""
候选召回 POC —— 两段式匹配的【第一段(召回/blocking)】重做。

现状盲区:data_match.py 第一段 blocking 用单一精确键 —— 精确 norm() 全名相等 + fl()「首词|末词」。
fl 假设「姓 = 最后一个 token」,对西语双姓/姓名颠倒/拼写变体有【系统性盲区】:
  EU `Julián Valero Moreno`(fl=julian|moreno) 与真人 `Julián Valero`(fl=julian|valero)
  key 不等 → 真候选进不了候选池,后面的 fuzzyname 根本没被调用(而它其实判 True)。

本 POC(第一段):对每个 EU 名字,从 sciscinet_authors.parquet(display_name)召回候选,用
【盲区互补的并集】:
  (a) char n-gram(3,4)TF-IDF 余弦 top-K —— 无类别盲区地治双姓/变体/颠倒/重音;
  (b) ∪「首名首字母 | 其它整词」精确键(rarity-capped)—— 补 (a) 对【纯单字母缩写】的盲区。
第二段仍用 fuzzyname_vendored.names_match 做精度过滤(缩写/中间名/昵称强于通用 JW/Lev)。

规模现实:sciscinet_authors.parquet 实为 ~1.0e8 行(非任务书所说 ~4e6)。100M 名字整表 TF-IDF
矩阵(~18GB nnz)放不进 12GB,故 (a) 采用【EU 名字词表 + 流式扫库 + 每 EU 名 top-K】:
词表只取 EU 名的 char (3,4)-gram(~2万维),丢弃语料 DF 过高(>DF_CAP)的高频 gram(近乎
零 IDF、只会把乘积搞稠),IDF 用语料样本估;然后分块扫全表,块内算 (Q·Cᵀ) 只留每 EU 名 top-K。
归一化沿用 data_match.norm() 口径(连字符家族→空格、去重音、小写、压空格)。全程不看 ORCID。
"""
import os
import re
import sys
import time
import unicodedata

import duckdb
import numpy as np
import pandas as pd
from scipy.sparse import csr_matrix
from sklearn.feature_extraction.text import TfidfVectorizer, CountVectorizer

from fuzzyname_vendored import names_match  # 第二段:成对姓名同一性(vendored,勿替换)

# ----------------------------- 配置 -----------------------------
AUTHORS = "../sciscinet_authors.parquet"
NGRAM = (3, 4)          # char_wb n-gram;3-4 gram 判别力足、乘积稀疏(2-gram 近乎全连、爆内存)
DF_CAP = 0.004          # 丢弃语料 DF 超过此比例的 gram(高频、近零 IDF、只会把乘积搞稠)
IDF_SAMPLE = 3_000_000  # 估 IDF / 语料 DF 的样本行数(reservoir)
CHUNK = 2_000_000       # 流式扫库每块行数
MAX_CHUNKS = int(os.environ.get("RECALL_MAX_CHUNKS", "0"))  # >0 时只扫前 N 块(冒烟测试用)
TOPK = 50               # 每个 EU 名保留的 n-gram 候选上限(控制扇出)
SIM_MIN = 0.40          # n-gram 余弦下限:标定自 matched_final 自配对余弦分布——0.40 覆盖 99.5%
                        # 旧命中,再低几乎无增益(残差是近零余弦的非拉丁别名/昵称,非阈值相邻)
BKEY_CAP = int(os.environ.get("BKEY_CAP", "50"))  # branch-b 每个「首字母|词」键的语料作者数上限:超过即判为常见姓(branch-a
                        # 的 trigram 已覆盖)并丢弃 → 只保留稀有姓,杀掉扇出、保留纯缩写盲区
OUT_RAW = "recall_ngram_raw.parquet"   # 第一段(a)原始召回(未过 fuzzyname),供评估召回率
OUT_CAND = "candidates_poc.csv"        # 最终候选表(过 fuzzyname)

# 连字符/破折号家族(U+002D/00AD/2010–2015/2212)→ 与 data_match.norm() 同口径统一成空格
_HYPHENS = re.compile("[" + "".join(map(chr, [0x2d, 0xad] + list(range(0x2010, 0x2016)) + [0x2212])) + "]")


def norm(s):
    """与 data_match.py 的 DuckDB 宏 norm() 对齐:连字符家族→空格 → 去重音 → 小写 → 压空格。"""
    if s is None:
        return ""
    s = _HYPHENS.sub(" ", str(s))
    s = "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", s.strip().lower())


def log(msg):
    print("[%7.1fs] %s" % (time.time() - T0, msg), flush=True)


T0 = time.time()


def load_eu(con):
    """EU.xlsx → (rid, eu_name)。rid = 行号(从 0),与 data_match.py 同口径。"""
    eu = con.sql("""
        SELECT (row_number() OVER ()) - 1 AS rid, "Researcher(s)" AS eu_name
        FROM read_xlsx('EU.xlsx', all_varchar=true)
    """).df()
    eu = eu[eu["eu_name"].notna()].copy()
    eu["nn"] = eu["eu_name"].map(norm)
    eu = eu[eu["nn"].str.len() > 0].reset_index(drop=True)
    return eu


def build_vectorizer(con, eu_norm):
    """词表 = EU 名的 char (3,4)-gram;丢弃语料 DF>DF_CAP 的高频 gram;IDF 用语料样本估。
    返回一个 fit 好的 TfidfVectorizer(vocabulary 固定、norm='l2'),transform 即 L2 归一。"""
    base_vocab = TfidfVectorizer(analyzer="char_wb", ngram_range=NGRAM, min_df=1).fit(eu_norm).vocabulary_
    log("EU 词表(全): %d 个 %s-gram" % (len(base_vocab), NGRAM))

    sample = con.sql(f"""
        SELECT display_name FROM '{AUTHORS}'
        WHERE display_name IS NOT NULL
        USING SAMPLE {IDF_SAMPLE} ROWS (reservoir, 42)
    """).df()["display_name"].map(norm).tolist()
    log("IDF 样本: %d 行" % len(sample))

    cnt = CountVectorizer(analyzer="char_wb", ngram_range=NGRAM, vocabulary=base_vocab, binary=True)
    dfreq = np.asarray(cnt.transform(sample).sum(axis=0)).ravel() / max(1, len(sample))
    keep = sorted(t for t, i in base_vocab.items() if dfreq[i] <= DF_CAP)
    log("DF_CAP=%.3f 保留 %d/%d gram(丢弃 %d 个高频近零-IDF gram)"
        % (DF_CAP, len(keep), len(base_vocab), len(base_vocab) - len(keep)))

    tfidf = TfidfVectorizer(analyzer="char_wb", ngram_range=NGRAM, vocabulary=keep)
    tfidf.fit(sample)   # 在样本上估 IDF(vocabulary 固定为保留集)
    return tfidf


def stage1a_ngram(con, eu, tfidf):
    """流式 n-gram 召回:分块扫全表,块内算 (Q · Cᵀ),每 EU 名维护跨块 top-K(score>=SIM_MIN)。
    返回 DataFrame[rid, authorid, sci_name, score]。"""
    Q = tfidf.transform(eu["nn"].tolist())          # (nQ, V) CSR, L2 归一
    nQ = Q.shape[0]
    rid_arr = eu["rid"].to_numpy()
    # 每 EU 名的运行 top-K:score / authorid / name 三条并行列表
    best_score = [np.empty(0, np.float32) for _ in range(nQ)]
    best_aid = [[] for _ in range(nQ)]
    best_name = [[] for _ in range(nQ)]

    con.execute(f"SELECT authorid, display_name FROM '{AUTHORS}' WHERE display_name IS NOT NULL")
    reader = con.fetch_record_batch(CHUNK)
    seen = 0
    t_tf = t_mm = t_topk = 0.0
    for bi, batch in enumerate(reader):
        ids = batch.column("authorid").to_pylist()
        names = batch.column("display_name").to_pylist()
        seen += len(ids)
        t = time.time(); C = tfidf.transform([norm(x) for x in names]); t_tf += time.time() - t
        t = time.time(); P = (Q @ C.T).tocsr(); t_mm += time.time() - t   # (nQ, chunk)
        t = time.time()
        indptr, indices, data = P.indptr, P.indices, P.data
        for i in range(nQ):
            a, b = indptr[i], indptr[i + 1]
            if a == b:
                continue
            d = data[a:b]
            mask = d >= SIM_MIN
            if not mask.any():
                continue
            cols = indices[a:b][mask]
            sc = d[mask]
            # 与已有 top-K 合并,取前 TOPK
            msc = np.concatenate([best_score[i], sc])
            maid = best_aid[i] + [ids[c] for c in cols]
            mnm = best_name[i] + [names[c] for c in cols]
            if msc.size > TOPK:
                keep = np.argpartition(-msc, TOPK)[:TOPK]
                best_score[i] = msc[keep]
                best_aid[i] = [maid[k] for k in keep]
                best_name[i] = [mnm[k] for k in keep]
            else:
                best_score[i] = msc
                best_aid[i] = maid
                best_name[i] = mnm
        t_topk += time.time() - t
        if MAX_CHUNKS and bi + 1 >= MAX_CHUNKS:
            log("  (RECALL_MAX_CHUNKS=%d 提前停,仅冒烟测试)" % MAX_CHUNKS)
            break
        if (bi + 1) % 5 == 0:
            log("  ...块 %d, 已扫 %d 行 (tf %.0fs / mm %.0fs / topk %.0fs)"
                % (bi + 1, seen, t_tf, t_mm, t_topk))
    log("n-gram 扫库完成:%d 行,tf %.0fs / matmul %.0fs / topk %.0fs" % (seen, t_tf, t_mm, t_topk))

    rows = []
    for i in range(nQ):
        for sc, aid, nm in zip(best_score[i], best_aid[i], best_name[i]):
            rows.append((int(rid_arr[i]), aid, nm, float(sc)))
    return pd.DataFrame(rows, columns=["rid", "authorid", "sci_name", "score"])


def stage1b_initial(con, eu):
    """branch-b:「首名首字母 | 其它整词」精确键并集(rarity-capped)。补 branch-a 对【纯单字母缩写】
    的盲区(J. vs John —— 单字母与整词无共享 trigram)。双姓安全:对每个非首词都出键(不假设「姓=末词」)。
    rarity cap:丢弃匹配语料作者数 > BKEY_CAP 的键(常见姓,branch-a 的 trigram 已覆盖,且是扇出炸弹)。
    返回 DataFrame[rid, authorid, sci_name, key]。"""
    con.register("eu_b", eu[["rid", "eu_name", "nn"]])
    con.execute("""
        CREATE OR REPLACE TEMP TABLE eu_keys AS
        WITH t AS (SELECT rid, eu_name, string_split(nn, ' ') AS toks FROM eu_b)
        SELECT DISTINCT rid, eu_name, substr(toks[1], 1, 1) || '|' || tok AS k
        FROM t, unnest(t.toks[2:]) AS u(tok)
        WHERE length(toks) >= 2 AND length(tok) >= 3
    """)
    log("branch-b: EU 键 %d 个(distinct %d)"
        % (con.sql("SELECT count(*) FROM eu_keys").fetchone()[0],
           con.sql("SELECT count(DISTINCT k) FROM eu_keys").fetchone()[0]))
    # 语料侧展开「首字母|词」键,semi-join EU 键;物化后按键的作者数做 rarity cap
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE bpairs AS
        WITH euk AS (SELECT DISTINCT k FROM eu_keys),
        c AS (SELECT authorid, display_name, string_split(norm(display_name), ' ') AS toks
              FROM '{AUTHORS}' WHERE display_name IS NOT NULL),
        ck AS (SELECT authorid, display_name AS cname, substr(toks[1], 1, 1) || '|' || tok AS k
               FROM c, unnest(c.toks[2:]) AS u(tok)
               WHERE length(toks) >= 2 AND length(tok) >= 3)
        SELECT ck.authorid, ck.cname, ck.k FROM ck JOIN euk USING(k)
    """)
    npairs = con.sql("SELECT count(*) FROM bpairs").fetchone()[0]
    # 展开回 (rid, authorid) 并带上该键的语料作者数 key_n;整表(未 cap)落盘,便于离线重调 cap 而不重扫
    full = con.sql("""
        WITH kc AS (SELECT k, count(DISTINCT authorid) AS n FROM bpairs GROUP BY k)
        SELECT e.rid, e.eu_name, b.authorid, b.cname AS sci_name, b.k AS key, kc.n AS key_n
        FROM bpairs b JOIN kc USING(k) JOIN eu_keys e ON e.k = b.k
    """).df().drop_duplicates(["rid", "authorid", "key"])
    full.to_parquet("recall_initial_raw.parquet", index=False)
    res = full[full["key_n"] <= BKEY_CAP].drop_duplicates(["rid", "authorid"])
    log("branch-b: 原始键对 %d(落盘 recall_initial_raw.parquet)→ rarity-cap(<=%d 作者/键)后 %d 对,%d EU 名"
        % (npairs, BKEY_CAP, len(res), res["rid"].nunique()))
    return res


def stage2_fuzzyname(cand, eu):
    """第二段精度过滤:对每个候选对 names_match(eu_name, sci_name);按 (eu_name, sci_name) 缓存去重。"""
    name_of = dict(zip(eu["rid"], eu["eu_name"]))
    cand = cand.copy()
    cand["eu_name"] = cand["rid"].map(name_of)
    pairs = cand[["eu_name", "sci_name"]].drop_duplicates()
    log("fuzzyname 去重后待判对: %d(总候选 %d)" % (len(pairs), len(cand)))
    verdict = {}
    for en, sn in pairs.itertuples(index=False):
        verdict[(en, sn)] = names_match(en, sn)
    cand["fuzzy_ok"] = [verdict[(en, sn)] for en, sn in zip(cand["eu_name"], cand["sci_name"])]
    return cand


def main():
    con = duckdb.connect()
    con.sql("SET temp_directory='.duckdb_tmp'")
    con.sql("SET memory_limit='11GB'")
    con.sql("SET preserve_insertion_order=false")
    # DuckDB 侧 norm() 宏(branch-b 用),与 data_match.py / 本文件 Python norm() 同口径
    con.sql(r"""CREATE MACRO norm(x) AS regexp_replace(trim(lower(strip_accents(
        regexp_replace(x, '[\x{002D}\x{00AD}\x{2010}-\x{2015}\x{2212}]', ' ', 'g')))), '\s+', ' ', 'g')""")

    eu = load_eu(con)
    log("EU 名字: %d 行(去空/去重前 rid 保留)" % len(eu))
    tfidf = build_vectorizer(con, eu["nn"].tolist())

    # --- 第一段(a):n-gram TF-IDF 流式召回 ---
    raw_a = stage1a_ngram(con, eu, tfidf)
    raw_a.to_parquet(OUT_RAW, index=False)
    fa = raw_a.groupby("rid").size()
    log("branch-a(n-gram)原始召回: %d 对,%d EU 名,已存 %s" % (len(raw_a), raw_a["rid"].nunique(), OUT_RAW))
    log("  扇出: mean %.1f / p50 %.0f / p95 %.0f / max %d"
        % (fa.mean(), fa.median(), fa.quantile(.95), fa.max()))

    # --- 第一段(b):首字母|姓 精确键(rarity-capped)---
    raw_b = stage1b_initial(con, eu)

    # --- 并集(盲区互补):按 (rid, authorid) 合并,标注来源 ---
    a = raw_a[["rid", "authorid", "sci_name", "score"]].copy()
    b = raw_b[["rid", "authorid", "sci_name"]].copy()
    union = a.merge(b, on=["rid", "authorid"], how="outer", suffixes=("_a", "_b"))
    union["sci_name"] = union["sci_name_a"].fillna(union["sci_name_b"])
    in_a = union["sci_name_a"].notna()
    in_b = union["sci_name_b"].notna()
    union["source"] = np.where(in_a & in_b, "ngram+initial", np.where(in_a, "ngram", "initial"))
    union = union[["rid", "authorid", "sci_name", "score", "source"]]
    union.to_parquet("recall_raw_union.parquet", index=False)
    log("并集(a∪b)原始召回: %d 对(仅a %d / 仅b %d / 两者 %d)"
        % (len(union), int((union.source == "ngram").sum()),
           int((union.source == "initial").sum()), int((union.source == "ngram+initial").sum())))

    # --- 第二段:fuzzyname 精度过滤 ---
    cand = stage2_fuzzyname(union, eu)
    out = cand[cand["fuzzy_ok"]][["rid", "eu_name", "authorid", "sci_name", "source", "score"]].sort_values(
        ["rid", "score"], ascending=[True, False], na_position="last").reset_index(drop=True)
    out.to_csv(OUT_CAND, index=False, encoding="utf-8-sig")
    fo = out.groupby("rid").size()
    log("第二段过滤后候选: %d 对(过 fuzzyname),%d EU 名,已存 %s" % (len(out), out["rid"].nunique(), OUT_CAND))
    log("  扇出(过滤后): mean %.1f / p50 %.0f / p95 %.0f / max %d"
        % (fo.mean(), fo.median(), fo.quantile(.95), fo.max()))
    log("总耗时 墙钟 %.1fs / CPU %.1fs(墙钟含系统休眠时不可信,以 CPU 与各阶段计时为准)"
        % (time.time() - T0, time.process_time()))


if __name__ == "__main__":
    main()
