# -*- coding: utf-8 -*-
"""离线判别度验证:CN 版 v2(领域聚类原型 x 作者摘要质心)——在接线进 cn_match.py 之前先给硬数字。

动机
----
EU/US 的层3 v2 成功靠「两侧同语言富文本」:作者论文 title+abstract 质心 x ERC 面板原型 /
NSF award Abstract。CN 名单侧只有一条中文短语,按文本逐建原型不可行(3093 个去重领域文本里
2636 个只出现一次)。本脚本验证的替代设计:

  1. 把中文领域文本嵌入后聚类成 K 个「领域簇」(类比 ERC 的 28 个面板);
  2. 每个簇的内容原型 = 簇内【gold/可信作者】英文摘要质心的均值(英文内容空间);
  3. 打分 = cosine(候选作者摘要质心, 簇内容原型)——两侧同语言,中文领域文本只负责分簇。

人群
----
与 coauthor_probe.py / validate_title_cn.py 完全同口径的「可解决区」:gold 存活 host 过滤、
候选 = 同一 host 机构下的同名候选(probe4 oracle 94.1%、baseline 85.9% 就是这套)。

协议(防泄漏)
------------
对每个被测行 r(属于簇 c):簇内容原型只用【簇 c 内、非 r、gold_authorid != gold(r)】的
gold 作者的摘要质心(留一,且排除同一真人重复行)。原型里的样本与 r 的答案完全无关。

指标
----
  - baseline argmax 论文数(可解决区,参考 85.9%)
  - argmax v2-sim(纯内容排序)
  - filter v2-sim>=floor + argmax 论文数(floor 扫描)
  - acc@1 loose/strict/ties(候选>=2 且有分)
  - 真人 vs 冒名者 sim 分布(判别度;对标 US v2 0.789 vs 0.626)

运行
----
依赖:numpy pandas duckdb torch transformers(首次会从 HF 下载 BAAI/bge-m3)。
建议在 GPU 机器上跑;产物有缓存(author_vectors_cn_*.npz / field_vectors_cn.npz),重跑不用重嵌。

  cd d:/data_matching/cn
  # 全量(约 1664 个可解决区行,最慢在 92GB 摘要扫描 + 嵌入):
  python validate_v2_cn.py
  # 快速冒烟(S=80 行, K=8 篇/作者, KS=50,100):
  S=80 K=8 KS=50,100 python validate_v2_cn.py

环境变量:
  S       采样的可解决区行数,0=全部(默认 0)
  K       每作者最多取多少篇论文建摘要质心(默认 20)
  KS      簇数列表,逗号分隔(默认 "50,100,200,400")
  MAXLEN  bge-m3 输入上限(默认 256)
  DEVICE  cuda/cpu,默认自动
  REBUILD=1 忽略缓存重新嵌入
"""
import os
import re
import sys
import time
import zipfile
import json
import xml.etree.ElementTree as ET

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import numpy as np
import pandas as pd

T0 = time.time()
def _el():
    return "[%5.0fs]" % (time.time() - T0)

# ---------------- 配置 ----------------
S = int(os.environ.get("S", "0"))
K = int(os.environ.get("K", "20"))
KS = [int(x) for x in os.environ.get("KS", "50,100,200,400").split(",")]
MAXLEN = int(os.environ.get("MAXLEN", "256"))
DEVICE = os.environ.get("DEVICE", "")
REBUILD = os.environ.get("REBUILD", "") == "1"
EMB_MODEL = "BAAI/bge-m3"
SEED = 42

ABS_FILE = "../sciscinet_papertitleabstract.parquet"
PAPID_FILE = "../sciscinet_authors_paperid.parquet"
EDGES_FILE = "../sciscinet_paper_author_affiliation.parquet"
AUTHVEC_CACHE = "author_vectors_cn_K%d_S%d.npz" % (K, S)
FIELDVEC_CACHE = "field_vectors_cn.npz"


# ---------------- 纯标准库:读 cn.xlsx 拿 gold ----------------
INST_KW = ["大学", "学院", "研究所", "研究院", "中心", "医院", "实验室", "学校",
           "研究中心", "科学院", "大學", "學院", "研究員", "所", "局", "系", "站",
           "厂", "公司", "集团"]


def _looks_inst(s):
    return isinstance(s, str) and any(k in s for k in INST_KW)


def load_gold_xlsx(path="cn.xlsx"):
    """rid -> (gold_authorid, field_text)。rid = 行序(0 起,与 cn_match 一致)。
    依托单位/研究领域按关键词判并纠正对调(与 cn_match.load_cn 同口径)。"""
    NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    z = zipfile.ZipFile(path)
    root = ET.parse(z.open("xl/sharedStrings.xml")).getroot()
    shared = ["".join(t.text or "" for t in si.iter(NS + "t"))
              for si in root.findall(NS + "si")]
    rows = []
    sheet = ET.parse(z.open("xl/worksheets/sheet1.xml")).getroot()
    for row in sheet.iter(NS + "row"):
        cells = {}
        for c in row.findall(NS + "c"):
            col = re.match(r"[A-Z]+", c.get("r")).group()
            v = c.find(NS + "v")
            if v is None:
                val = None
            elif c.get("t") == "s":
                val = shared[int(v.text)]
            else:
                val = v.text
            cells[col] = val
        rows.append(cells)
    out = {}
    for i, r in enumerate(rows):
        if i == 0:
            continue
        name = r.get("B")
        if name is None:
            continue
        c2, c3, aid = r.get("C"), r.get("D"), r.get("F")
        if _looks_inst(c3) and not _looks_inst(c2):
            _, field = c3, c2
        else:
            _, field = c2, c3
        field = (field.strip() if isinstance(field, str) and field.strip() else None)
        if aid is None:
            continue
        aid = str(aid).strip()
        if not aid:
            continue
        out[i - 1] = (aid, field)
    return out


# ---------------- DuckDB:可解决区(与 probe4 / title 验证同口径) ----------------
def build_addressable():
    import duckdb
    import pandas as pd

    con = duckdb.connect()
    con.sql("SET temp_directory='.duckdb_tmp'")
    con.sql("SET memory_limit='12GB'")
    con.sql("SET preserve_insertion_order=false")

    gold_raw = pd.DataFrame(
        [{"rid": int(r), "gold_authorid": str(a)} for r, (a, _) in gold.items()])
    con.register("gold_raw", gold_raw)
    con.sql("""
        CREATE TABLE gold AS
        SELECT g.rid, g.gold_authorid, h.host_openalex_id AS host
        FROM gold_raw g JOIN 'cn_host_ids_bridge.csv' h ON g.rid = h.row_id
        WHERE h.host_openalex_id IS NOT NULL AND h.host_openalex_id <> ''""")
    con.sql("""
        CREATE TABLE ehost AS
        SELECT DISTINCT authorid, institutionid FROM '%s'
        WHERE institutionid IN (SELECT DISTINCT host FROM gold)""" % EDGES_FILE)
    sample_sql = ("AND m.rid IN (SELECT rid FROM "
                  "(SELECT DISTINCT rid FROM gold ORDER BY rid) "
                  "USING SAMPLE %d ROWS (reservoir, %d))" % (S, SEED)) if S > 0 else ""
    con.sql("""
        CREATE TABLE cah AS
        SELECT DISTINCT m.rid, m.authorid, g.host, g.gold_authorid
        FROM 'match_1_cn.csv' m JOIN gold g ON m.rid = g.rid
        JOIN ehost e ON e.authorid = m.authorid AND e.institutionid = g.host
        WHERE EXISTS (SELECT 1 FROM ehost e2
                      WHERE e2.authorid = g.gold_authorid AND e2.institutionid = g.host)
          %s""" % sample_sql)
    n_rid = con.sql("SELECT count(DISTINCT rid) FROM cah").fetchone()[0]
    n_cand = con.sql("SELECT count(*) FROM cah").fetchone()[0]
    print("%s 可解决区: %d 行, %d 候选行(S=%s)" % (_el(), n_rid, n_cand, S), flush=True)

    # 每候选论文数(层4 决胜键)
    con.sql("""
        CREATE TABLE gpc AS
        SELECT authorid, count(DISTINCT paperid) AS n_papers
        FROM '%s' WHERE authorid IN (SELECT DISTINCT authorid FROM cah) GROUP BY 1""" % PAPID_FILE)

    feat = con.sql("""
        SELECT c.rid, c.authorid, c.gold_authorid, COALESCE(p.n_papers, 0) AS n_papers,
               (c.authorid = c.gold_authorid) AS is_gold
        FROM cah c LEFT JOIN gpc p ON p.authorid = c.authorid""").df()
    return con, feat


# ---------------- 抽作者论文 title+abstract ----------------
def fetch_docs(con, feat):
    """每作者取 <= K 篇英文论文,返回 authorid -> doc(title. abstract)。
    92GB 表只做一次半连接扫描 + 倒排索引还原(与 extract_abstracts.py 同法)。"""
    con.sql("""
        CREATE TEMP TABLE need AS
        WITH cp AS (
            SELECT authorid, paperid,
                   row_number() OVER (PARTITION BY authorid ORDER BY paperid) rk
            FROM '%s' WHERE authorid IN (SELECT DISTINCT authorid FROM cah))
        SELECT authorid, paperid FROM cp WHERE rk <= %d""" % (PAPID_FILE, K))
    n_map = con.sql("SELECT count(*) FROM need").fetchone()[0]
    print("%s 待抽论文映射 %d 行(K=%d),开始扫 92GB ..." % (_el(), n_map, K), flush=True)
    raw = con.sql("""
        SELECT a.paperid, a.title, a.abstract_inverted_index
        FROM '%s' a
        SEMI JOIN (SELECT DISTINCT paperid FROM need) k ON a.paperid = k.paperid
        WHERE a.language = 'en'""" % ABS_FILE).df()
    print("%s 扫描完成:英文论文 %d 篇" % (_el(), len(raw)), flush=True)

    def deinvert(s):
        if not isinstance(s, str) or not s:
            return None
        try:
            d = json.loads(s)
        except Exception:
            return None
        pairs = [(p, tok) for tok, ps in d.items() for p in ps]
        if not pairs:
            return None
        pairs.sort()
        return " ".join(t for _, t in pairs)

    raw["doc"] = [str(t or "").strip() + ". " + str(a or "").strip()
                  for t, a in zip(raw["title"], raw["abstract_inverted_index"].map(deinvert))]
    raw = raw[raw["doc"].str.len() > 5].copy()
    pid_doc = raw[["paperid", "doc"]].drop_duplicates("paperid")
    need = con.sql("SELECT authorid, paperid FROM need").df()
    merged = need.merge(pid_doc, on="paperid", how="inner")
    docs_by_author = merged.groupby("authorid")["doc"].apply(list).to_dict()
    print("%s 有摘要文本的作者 %d / 候选作者(有匹配文本)" % (_el(), len(docs_by_author)), flush=True)
    return docs_by_author


# ---------------- bge-m3 嵌入 ----------------
_EMB = {}


def _embedder():
    if "fn" in _EMB:
        return _EMB["fn"]
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    import torch
    from transformers import AutoTokenizer, AutoModel
    dev = DEVICE or ("cuda" if torch.cuda.is_available() else "cpu")
    tok = AutoTokenizer.from_pretrained(EMB_MODEL)
    mdl = AutoModel.from_pretrained(EMB_MODEL).to(dev)
    mdl.eval()

    @torch.no_grad()
    def embed(texts, maxlen):
        out = []
        for i in range(0, len(texts), 64):
            enc = tok(texts[i:i + 64], padding=True, truncation=True,
                      max_length=maxlen, return_tensors="pt").to(dev)
            v = mdl(**enc).last_hidden_state[:, 0]
            out.append(torch.nn.functional.normalize(v, p=2, dim=1).cpu().numpy())
        return np.vstack(out) if out else np.zeros((0, 1024), np.float32)

    print("%s 嵌入模型 %s 载入(device=%s)" % (_el(), EMB_MODEL, dev), flush=True)
    _EMB["fn"] = embed
    return embed


def centroid(vecs):
    m = np.asarray(vecs, np.float32).mean(0)
    n = np.linalg.norm(m)
    return (m / n).astype(np.float32) if n > 0 else m.astype(np.float32)


def build_author_vectors(docs_by_author):
    if not REBUILD and os.path.exists(AUTHVEC_CACHE):
        d = np.load(AUTHVEC_CACHE, allow_pickle=True)
        ids = [str(x) for x in d["ids"]]
        print("%s 复用作者向量缓存 %s(%d 作者)" % (_el(), AUTHVEC_CACHE, len(ids)), flush=True)
        return {a: d["mat"][i] for i, a in enumerate(ids)}, ids
    embed = _embedder()
    all_docs = sorted({d for lst in docs_by_author.values() for d in lst})
    print("%s 待嵌入唯一文档 %d 篇(maxlen=%d)..." % (_el(), len(all_docs), MAXLEN), flush=True)
    V = embed(all_docs, MAXLEN)
    di = {d: i for i, d in enumerate(all_docs)}
    vecs = {}
    for a, lst in docs_by_author.items():
        vs = [V[di[d]] for d in lst if d in di]
        if vs:
            vecs[a] = centroid(vs)
    np.savez(AUTHVEC_CACHE,
             ids=np.array(list(vecs), dtype=object),
             mat=np.vstack(list(vecs.values())).astype(np.float32))
    print("%s 已写 %s(%d 作者)" % (_el(), AUTHVEC_CACHE, len(vecs)), flush=True)
    return vecs, list(vecs)


def build_field_vectors(ftexts):
    if not REBUILD and os.path.exists(FIELDVEC_CACHE):
        d = np.load(FIELDVEC_CACHE, allow_pickle=True)
        old = [str(x) for x in d["texts"]]
        if set(old) == set(ftexts):
            return {t: d["mat"][i] for i, t in enumerate(old)}
    embed = _embedder()
    V = embed(list(ftexts), 64)
    np.savez(FIELDVEC_CACHE,
             texts=np.array(list(ftexts), dtype=object),
             mat=V.astype(np.float32))
    return {t: V[i] for i, t in enumerate(ftexts)}


# ---------------- 纯 numpy KMeans(kmeans++ 初始化) ----------------
def kmeans(X, k, seed=SEED, restarts=3, iters=60):
    """X 已 L2 归一(单位向量)。返回 (labels, centers)。"""
    rng = np.random.default_rng(seed)
    best = (None, None, np.inf)
    for _ in range(restarts):
        centers = [X[rng.integers(len(X))]]
        for _ in range(1, k):
            d = np.min([((X - c) ** 2).sum(1) for c in centers], axis=0)
            p = d / d.sum()
            centers.append(X[rng.choice(len(X), p=p)])
        centers = np.vstack(centers)
        for _ in range(iters):
            dists = np.column_stack([((X - c) ** 2).sum(1) for c in centers])
            labels = dists.argmin(1)
            newc = np.vstack([X[labels == j].mean(0) if (labels == j).any()
                              else centers[j] for j in range(k)])
            newc /= np.linalg.norm(newc, axis=1, keepdims=True) + 1e-12
            if np.abs(newc - centers).max() < 1e-6:
                centers = newc
                break
            centers = newc
        dists = np.column_stack([((X - c) ** 2).sum(1) for c in centers])
        labels = dists.argmin(1)
        inertia = dists[np.arange(len(X)), labels].sum()
        if inertia < best[2]:
            best = (labels, centers, inertia)
    return best[0], best[1]


# ---------------- 评测 ----------------
def evaluate(feat, fvecs, ftext_by_rid, avecs):
    """对每个簇数 K 做留一簇原型验证。"""
    rids = sorted(feat["rid"].unique())
    cand = {r: g for r, g in feat.groupby("rid")}
    # 只对【已嵌入、且属于评测集】的领域文本建簇(与 __main__ 传入的 fvecs 对齐)
    ftexts = sorted(t for t in ftext_by_rid.values() if t and t in fvecs)
    F = np.vstack([fvecs[t] for t in ftexts])
    fi = {t: i for i, t in enumerate(ftexts)}
    has_vec = np.array([str(a) in avecs for a in feat["authorid"]])
    n_cand_vec = feat.loc[has_vec, "authorid"].nunique()
    print("%s 候选作者 %d, 有摘要质心 %d;gold 有质心的行 %d"
          % (_el(), feat["authorid"].nunique(), n_cand_vec,
             sum(cand[r].loc[cand[r]["is_gold"], "authorid"].isin(avecs).any()
                 for r in rids)), flush=True)

    for k in KS:
        t0 = time.time()
        labels, centers = kmeans(F, k, seed=SEED)
        clabel = {t: int(labels[fi[t]]) for t in ftexts}
        # 簇 -> 该簇全部行(被测池)
        cluster_rows = {}
        for r in rids:
            t = ftext_by_rid.get(r)
            if t and t in clabel:
                cluster_rows.setdefault(clabel[t], []).append(r)

        sim_rows = []      # (rid, authorid, n_papers, is_gold, sim)
        n_proto = 0
        for r in rids:
            g = cand[r]
            t = ftext_by_rid.get(r)
            if not t or t not in clabel:
                continue
            c = clabel[t]
            ga = g.loc[g["is_gold"], "authorid"]
            gold_a = str(ga.iloc[0]) if len(ga) == 1 else None
            # 留一:排除本行、排除同一真人,取簇内其余 gold 作者的摘要质心
            pool = []
            seen = set()
            for r2 in cluster_rows.get(c, []):
                if r2 == r:
                    continue
                g2 = cand[r2]
                ga2 = g2.loc[g2["is_gold"], "authorid"]
                if len(ga2) != 1:
                    continue
                a2 = str(ga2.iloc[0])
                if a2 != gold_a and a2 in avecs and a2 not in seen:
                    seen.add(a2)
                    pool.append(avecs[a2])
            if not pool:
                continue
            proto = centroid(pool)
            n_proto += 1
            for _, row in g.iterrows():
                a = str(row["authorid"])
                if a in avecs:
                    sim_rows.append((r, a, int(row["n_papers"]),
                                     bool(row["is_gold"]),
                                     float(avecs[a] @ proto)))

        sim_df = pd.DataFrame(sim_rows, columns=["rid", "authorid", "n_papers",
                                                 "is_gold", "sim"]) if sim_rows else \
            pd.DataFrame(columns=["rid", "authorid", "n_papers", "is_gold", "sim"])
        # acc@1 分母 = 候选>=2、且真人也拿到分(有摘要质心)的行
        n_multi = 0
        loose = strict = ties = 0
        for r, g in sim_df.groupby("rid"):
            a_ids = g["authorid"].nunique()
            gt = g.loc[g["is_gold"]]
            if a_ids < 2 or gt.empty:
                continue
            n_multi += 1
            ts = gt["sim"].max()
            ws = g.loc[~g["is_gold"], "sim"]
            wmax = ws.max() if len(ws) else -np.inf
            if ts >= wmax:
                loose += 1
                if ts > wmax:
                    strict += 1
                else:
                    ties += 1
        cov_multi = n_multi

        # 决策规则:argmax papers / argmax sim / filter floor + papers。
        # 关键:所有规则的兜底都用【完整候选池】(有 sim 的合并进来、没 sim 的为 NaN),
        # 与 title 验证同口径;baseline 只按论文数在完整池里决胜。
        def by_papers(g):
            return g.sort_values(["n_papers", "authorid"], ascending=[False, True]).iloc[0]

        def by_sim(g):
            s = g[g["sim"].notna()]
            if s.empty:
                return by_papers(g)
            return s.sort_values(["sim", "authorid"], ascending=[False, True]).iloc[0]

        def filt(floor):
            def f(g):
                s = g[g["sim"] >= floor]
                return by_papers(s if not s.empty else g)
            return f

        full = {r: g for r, g in feat.groupby("rid")}
        scored = {r: g for r, g in sim_df.groupby("rid")}
        merged = {}
        for r, g in full.items():
            if r in scored:
                m = g.merge(scored[r][["rid", "authorid", "sim"]],
                            on=["rid", "authorid"], how="left")
            else:
                m = g.copy()
                m["sim"] = np.nan
            merged[r] = m
        N = len(full)

        def count(pick):
            hit = 0
            for r, g in merged.items():
                hit += bool(pick(g)["is_gold"])
            return hit

        b0 = count(by_papers)
        b1 = count(by_sim)
        bs = {fl: count(filt(fl)) for fl in [0.55, 0.60, 0.65, 0.70, 0.75]}

        gs = sim_df.loc[sim_df["is_gold"], "sim"]
        ws2 = sim_df.loc[~sim_df["is_gold"], "sim"]
        print("=" * 64)
        print("簇数 K=%d | %d 行有留一原型 / %d 行 | 多候选有分行 %d"
              % (k, n_proto, N, cov_multi), flush=True)
        print("  baseline argmax papers : %4d/%d = %.1f%%" % (b0, N, 100.0 * b0 / N))
        print("  argmax v2-sim          : %4d/%d = %.1f%%" % (b1, N, 100.0 * b1 / N))
        for fl, h in bs.items():
            print("  filter sim>=%.2f+papers  : %4d/%d = %.1f%%" % (fl, h, N, 100.0 * h / N))
        if cov_multi:
            print("  acc@1(候选>=2) loose %d/%d = %.1f%% | strict %d | ties %d"
                  % (loose, cov_multi, 100.0 * loose / cov_multi, strict, ties))
        if len(gs) and len(ws2):
            print("  v2-sim gold   : mean=%.3f p10=%.3f p25=%.3f p50=%.3f"
                  % (gs.mean(), gs.quantile(.1), gs.quantile(.25), gs.median()))
            print("  v2-sim nongold: mean=%.3f p50=%.3f p75=%.3f p90=%.3f"
                  % (ws2.mean(), ws2.median(), ws2.quantile(.75), ws2.quantile(.9)))
        print("  (该 K 用时 %.0fs)" % (time.time() - t0), flush=True)


if __name__ == "__main__":
    gold = load_gold_xlsx()
    print("%s gold 行 %d" % (_el(), len(gold)), flush=True)
    con, feat = build_addressable()
    docs_by_author = fetch_docs(con, feat)
    avecs, _ = build_author_vectors(docs_by_author)
    ftext_by_rid = {int(r): t for r, (_, t) in gold.items() if t}
    ftexts = sorted({ftext_by_rid[r] for r in feat["rid"].unique() if r in ftext_by_rid})
    print("%s 去重领域文本 %d 个" % (_el(), len(ftexts)), flush=True)
    fvecs = build_field_vectors(ftexts)
    evaluate(feat, fvecs, ftext_by_rid, avecs)
    print("%s done" % _el(), flush=True)
