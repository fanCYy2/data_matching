# -*- coding: utf-8 -*-
"""
US NSF award principal investigators -> SciSciNet/OpenAlex authorid.

复用 EU 版 data_match.py 的四层漏斗:
  层1 名字(exact / alias / fuzzy)
  层2 机构(需要 us_host_ids_bridge.csv)
  层3 语义领域(见下)
  层4 兜底(passed -> 论文数 -> authorid)

US 数据与 EU 数据的栏目映射:
  EU Researcher(s)          -> US PrincipalInvestigator
  EU Host Institution(s)    -> US Organization(经 us_host_ids_bridge.csv 映射到 OpenAlex 机构 id)

层3 语义领域(US 原生,与 EU 不同):
  EU 只有 Panel/Domain 短标签,得先套 ERC 面板原型(欧洲 ERC 分类体系)才有富向量;US 的
  NSF 分支/项目结构与 ERC 面板对不上,那套体系不适用。但 US 每条 award 自带整段 Abstract,
  两侧都有富文本,于是直接做 abstract-vs-abstract:
    v2(默认,需 author_vectors_us.npz):
       作者论文 title+abstract 质心  ×  该行 NSF award Abstract 向量  的 cosine。
       作者无英文摘要 -> 回退用其 top-3 level-1 子学科名质心(同一嵌入空间,保证同 rid 可比)。
       离线产物:extract_abstracts_us.py -> build_author_vectors_us.py -> author_vectors_us.npz。
    v1(回退,缺 author_vectors_us.npz 或 US_SEM_V2=0 时):
       作者 top-3 level-1 子学科名  ×  本行 Title + Program(s) 短文本 的最大 cosine。
  首跑(还没有 author_vectors_us.npz)自动走 v1 生成候选池 match_3_us.csv;据此建作者向量后,
  再跑即自动切 v2。阈值量纲两版不同,分别标定(见下方 SEM_* 常量)。

"""
import os
import sys

import duckdb
import numpy as np
import pandas as pd


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
os.chdir(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from fuzzyname_vendored import names_match   # 同名者补召 homonym_reopen 的成对姓名复核
from us_embed import embed, centroid, load_author_vectors, AUTHORVEC_FILE   # 层3 v2 共享嵌入


con = duckdb.connect()

# 大表扫描/连接的中间结果允许溢写, 避免扫 6.8GB affiliation parquet 时 OOM。
# 纯执行层配置, 不影响匹配结果。
con.sql("SET temp_directory='.duckdb_tmp'")
con.sql("SET memory_limit='12GB'")
con.sql("SET preserve_insertion_order=false")


# ===== 语义相似度参数 =====
# v1(回退):作者子学科名 × Title+Program(s) 短文本。沿用 EU v1 尺度。
SEM_THRESH = float(os.environ.get("US_SEM_THRESH", "0.62"))
SEM_MARGIN = float(os.environ.get("US_SEM_MARGIN", "0.03"))

# v2(默认,abstract-vs-abstract):作者论文摘要质心 × NSF award Abstract。
# 已标定(2026-08-28,eval_sem_us.py,871 个 round2_inst 机构确认 rid 作银真值):
#   正确候选 sim mean=0.789/p10=0.686/p50=0.805,错误 mean=0.626/p50=0.628;
#   纯 v2 语义严格 acc@1 = 778/871 = 89% 且零并列(同口径层4 max-papers 仅 79.2%)。
#   THRESH=0.68 ≈ 正确候选 p10:拦掉离题的 top pick(尤其保护"真人不在候选池"的无机构行,
#   避免硬选一个次相似的错人);MARGIN=0.00:实测任何正 margin 都把语义近并列推给更弱的层4
#   论文数兜底,反而降低总正确率(bge 余弦被压高、近并列极多,raw margin 是钝器)。
SEM_THRESH_V2 = float(os.environ.get("US_SEM_THRESH_V2", "0.68"))
SEM_MARGIN_V2 = float(os.environ.get("US_SEM_MARGIN_V2", "0.00"))

# v2 总开关:有 author_vectors_us.npz 且未显式关闭时启用;否则回退 v1。
SEM_V2 = os.environ.get("US_SEM_V2", "1") == "1"


def _use_v2():
    return SEM_V2 and os.path.exists(AUTHORVEC_FILE)

# 论文数下限闸。EU 版用 30; 若 US 只想看全量, 可 US_PAPER_FLOOR=0。
PAPER_FLOOR = int(os.environ.get("US_PAPER_FLOOR", "30"))

USE_ALIAS = os.environ.get("US_ALIAS", "1") == "1"
USE_FUZZY = os.environ.get("US_FUZZY", "1") == "1"


# US.csv 注册成视图, 加行号 rid(从 0 开始)。全部按 VARCHAR 读, 避免日期/金额列被 DuckDB 猜测。
SOURCE_CSV = os.environ.get("US_SOURCE_CSV", "US.csv")
_source_sql = SOURCE_CSV.replace("\\", "/").replace("'", "''")
con.sql(f"""
    CREATE VIEW us AS
    SELECT (row_number() OVER ()) - 1 AS rid, *
    FROM read_csv_auto('{_source_sql}', header = true, all_varchar = true)
""")


# 名字归一化宏(与 EU 版一致):连字符/破折号家族统一成空格 -> 去重音 -> 小写 -> 压缩空白。
# 不动句点, 避免 "Cynthia. Sharma" 这类残缺记录顶掉真人。
con.sql(r"""
    CREATE MACRO norm(x) AS
    regexp_replace(
        trim(lower(strip_accents(
            regexp_replace(x, '[\x{002D}\x{00AD}\x{2010}-\x{2015}\x{2212}]', ' ', 'g')
        ))),
        '\s+', ' ', 'g')
""")

# 首名+末名(姓)key, 用于 match_1c 模糊补配。
con.sql(r"""
    CREATE MACRO fl(x) AS
    regexp_extract(norm(x), '^(\S+)', 1) || '|' || regexp_extract(norm(x), '(\S+)$', 1)
""")

# 排序词集 key, 解决姓名顺序颠倒。
con.sql(r"""
    CREATE MACRO wset(x) AS
    array_to_string(list_sort(string_split(norm(x), ' ')), ' ')
""")


# 每个 US 行的 host 机构 -> OpenAlex 机构 id 桥表(row_id 与 rid 对齐)。
# 结构和 EU 版 host_ids_bridge.csv 一致。若尚未生成, 本脚本仍可跑, 只是层2 没有机构闸,
# 所有待定行会直通层3/层4。
BRIDGE_CSV = os.environ.get("US_HOST_BRIDGE", "us_host_ids_bridge.csv")
if os.path.exists(BRIDGE_CSV):
    _bridge_sql = BRIDGE_CSV.replace("\\", "/").replace("'", "''")
    con.sql(f"CREATE VIEW host AS SELECT row_id, host_openalex_id FROM '{_bridge_sql}'")
else:
    con.sql("CREATE TABLE host(row_id BIGINT, host_openalex_id VARCHAR)")
    print("[warning] 未找到 us_host_ids_bridge.csv: 层2 机构消歧将被跳过, "
          "所有名字未唯一命中的行直通层3/层4。")


# 第三轮语义匹配用:每个 US 行的领域文本。
# US 没有 Panel/Domain, 用 Title + Program(s) 作为最接近"项目研究领域"的短文本;
# Program(s) 缺失时回退 NSFDirectorate, 再缺则为 NULL(该行无法做语义比较)。
con.sql(r"""
    CREATE VIEW us_field_text AS
    SELECT rid,
        CASE
            WHEN "Title" IS NOT NULL AND "Title" <> ''
                THEN trim("Title")
                     || COALESCE(' | ' || NULLIF(trim("Program(s)"), ''), '')
            WHEN "Program(s)" IS NOT NULL AND "Program(s)" <> ''
                THEN trim("Program(s)")
            WHEN "NSFDirectorate" IS NOT NULL AND "NSFDirectorate" <> ''
                THEN trim("NSFDirectorate")
            ELSE NULL
        END AS us_text
    FROM us
""")


def match_1a():
    """层1a 精确:norm(PrincipalInvestigator) = norm(sciscinet display_name)。"""
    return con.sql("""
        SELECT
            us.rid                         AS rid,
            us."PrincipalInvestigator"     AS us_name,
            sci.authorid                   AS authorid,
            sci.display_name               AS sci_name
        FROM us
        JOIN '../sciscinet_authors.parquet' sci
          ON norm(us."PrincipalInvestigator") = norm(sci.display_name)
    """).df()


def match_1b():
    """层1b 别名:展开 display_name_alternatives, 用 norm 或 wset 命中。"""
    return con.sql("""
        WITH nm AS (
            SELECT rid,
                   "PrincipalInvestigator"     AS us_name,
                   norm("PrincipalInvestigator") AS nn,
                   wset("PrincipalInvestigator") AS ws
            FROM us
            WHERE "PrincipalInvestigator" IS NOT NULL
        ),
        alias AS (
            SELECT authorid, display_name, norm(alt) AS nalias, wset(alt) AS wsalias
            FROM (
                SELECT authorid, display_name,
                       unnest(from_json(display_name_alternatives, '["VARCHAR"]')) AS alt
                FROM '../sciscinet_author_details.parquet'
            )
            WHERE norm(alt) IN (SELECT nn FROM nm)
               OR wset(alt) IN (SELECT ws FROM nm)
        )
        SELECT DISTINCT
            nm.rid, nm.us_name, a.authorid, a.display_name AS sci_name
        FROM nm
        JOIN alias a
          ON nm.nn = a.nalias
          OR nm.ws = a.wsalias
    """).df()


def match_1c():
    """层1c 首末名模糊兜底, 只处理精确+别名都没配上的 rid。"""
    return con.sql("""
        SELECT
            us.rid                         AS rid,
            us."PrincipalInvestigator"     AS us_name,
            sci.authorid                   AS authorid,
            sci.display_name               AS sci_name
        FROM us
        JOIN '../sciscinet_authors.parquet' sci
          ON fl(sci.display_name) = fl(us."PrincipalInvestigator")
        WHERE us."PrincipalInvestigator" IS NOT NULL
          AND us.rid NOT IN (SELECT DISTINCT rid FROM r1_ea)
    """).df()


def match_2():
    """层2 机构消歧:候选 author 必须在该行 US host 机构发过文。"""
    return con.sql("""
        WITH cand AS (
            SELECT
                pend.rid,
                pend.us_name,
                pend.authorid,
                pend.match_type,
                h.host_openalex_id AS host_id
            FROM pend
            JOIN host h ON pend.rid = h.row_id
            WHERE h.host_openalex_id IS NOT NULL
              AND h.host_openalex_id <> ''
        ),
        aff AS (
            SELECT DISTINCT authorid, institutionid
            FROM '../sciscinet_paper_author_affiliation.parquet'
            WHERE authorid      IN (SELECT authorid FROM cand)
              AND institutionid IN (SELECT host_id  FROM cand)
        )
        SELECT c.rid, c.us_name, c.authorid, c.match_type, c.host_id
        FROM cand c
        JOIN aff a
          ON c.authorid = a.authorid
         AND c.host_id  = a.institutionid
    """).df()


def match_3():
    """层3 语义消歧的取数部分:候选作者前3 level-1 子学科名 + 该行 us_text。"""
    return con.sql("""
        WITH cand AS (
            SELECT DISTINCT authorid FROM r3in
        ),
        papers AS (
            SELECT authorid, paperid
            FROM '../sciscinet_authors_paperid.parquet'
            WHERE authorid IN (SELECT authorid FROM cand)
        ),
        lvl1 AS (
            SELECT fieldid, display_name
            FROM '../sciscinet_fields.parquet'
            WHERE level = 1
        ),
        field_cnt AS (
            SELECT p.authorid, pf.fieldid, count(*) AS n
            FROM papers p
            JOIN '../sciscinet_paperfields.parquet' pf ON p.paperid = pf.paperid
            WHERE pf.fieldid IN (SELECT fieldid FROM lvl1)
            GROUP BY 1, 2
        ),
        ranked AS (
            SELECT authorid, fieldid, n,
                   n * 1.0 / sum(n) OVER (PARTITION BY authorid) AS share,
                   row_number() OVER (PARTITION BY authorid ORDER BY n DESC, fieldid) AS rnk
            FROM field_cnt
        ),
        top3 AS (
            SELECT r.authorid, l.display_name AS field_name, r.share, r.rnk
            FROM ranked r
            JOIN lvl1 l ON r.fieldid = l.fieldid
            WHERE r.rnk <= 3
        )
        SELECT c.rid, c.us_name, c.authorid, c.match_type, c.host_id,
               t.us_text, top3.field_name, top3.share, top3.rnk
        FROM r3in c
        JOIN us_field_text t ON c.rid = t.rid
        LEFT JOIN top3 ON c.authorid = top3.authorid
    """).df()


_SCORE_KEYS = ["rid", "us_name", "authorid", "match_type", "host_id"]


def _row_abstracts(rids):
    """取这些 rid 的"领域查询文本":优先整段 Abstract,缺则 Title+Program(s),
    再缺 Program(s)/NSFDirectorate,全缺 -> None。返回 {rid: text}。"""
    con.register("_score_rids", pd.DataFrame({"rid": list(rids)}))
    d = con.sql("""
        SELECT u.rid,
            CASE
                WHEN "Abstract" IS NOT NULL AND trim("Abstract") <> ''
                    THEN trim("Abstract")
                WHEN "Title" IS NOT NULL AND trim("Title") <> ''
                    THEN trim("Title")
                         || COALESCE(' | ' || NULLIF(trim("Program(s)"), ''), '')
                WHEN "Program(s)" IS NOT NULL AND trim("Program(s)") <> ''
                    THEN trim("Program(s)")
                WHEN "NSFDirectorate" IS NOT NULL AND trim("NSFDirectorate") <> ''
                    THEN trim("NSFDirectorate")
                ELSE NULL
            END AS atext
        FROM us u JOIN _score_rids s ON u.rid = s.rid
    """).df()
    return dict(zip(d["rid"], d["atext"]))


def semantic_score(long_df):
    """层3 打分调度:有 author_vectors_us.npz 走 v2(abstract-vs-abstract),否则回退 v1。"""
    if _use_v2():
        return _semantic_score_v2(long_df)
    return _semantic_score_v1(long_df)


def _semantic_score_v1(long_df):
    """v1:候选作者 top-3 子学科名 × 该行 us_text(Title+Program)的【最大】cosine。
    无领域/无 us_text -> sim = NaN。"""
    keys = _SCORE_KEYS
    df = long_df.copy()
    texts = pd.unique(pd.concat(
        [df["us_text"].dropna(), df["field_name"].dropna()], ignore_index=True))
    if len(texts) == 0:
        base = df[keys].drop_duplicates().reset_index(drop=True)
        base["sim"] = np.nan
        return base

    vecs = embed(list(texts), maxlen=64)
    idx = {t: i for i, t in enumerate(texts)}

    def cos(f, e):
        if not isinstance(f, str) or not isinstance(e, str):
            return np.nan
        return float(vecs[idx[f]] @ vecs[idx[e]])

    df["cos"] = [cos(f, e) for f, e in zip(df["field_name"], df["us_text"])]
    return (df.groupby(keys, dropna=False)["cos"].max()
              .reset_index().rename(columns={"cos": "sim"}))


def _semantic_score_v2(long_df):
    """v2:作者论文摘要质心 × 该行 NSF award Abstract 向量 的 cosine。

    - 作者表征:在 author_vectors_us.npz 里 -> 论文文本质心(富信号);
                否则 -> 其 top-3 子学科名质心(回退,同一嵌入空间,保证同 rid 可比)。
    - 查询表征:该 rid 的 Abstract(缺则 Title+Program 等,见 _row_abstracts)整段直接嵌入。
    无查询文本 / 作者无任何表征 -> sim = NaN(交层4 按论文数兜底)。
    """
    keys = _SCORE_KEYS
    df = long_df.copy()

    # 每 rid 一个查询文本(整段 Abstract),嵌入成目标文档向量
    rid_text = _row_abstracts(pd.unique(df["rid"]))
    texts = list({t for t in rid_text.values() if isinstance(t, str) and t})
    tvecs = dict(zip(texts, embed(texts, maxlen=256))) if texts else {}

    # 作者表征:优先论文摘要质心;缺 -> top-3 子学科名质心
    aid2i, amat = load_author_vectors()
    per = (df.dropna(subset=["field_name"])
             .groupby("authorid")["field_name"].apply(lambda s: list(pd.unique(s))))
    fb_authors = [a for a in pd.unique(df["authorid"]) if a not in aid2i]
    fb_names = sorted({n for a in fb_authors if a in per.index for n in per[a]})
    fnv = dict(zip(fb_names, embed(fb_names, maxlen=64))) if fb_names else {}
    fb_vec = {}
    for a in fb_authors:
        if a in per.index:
            fb_vec[a] = centroid([fnv[n] for n in per[a]])

    def rep(a):
        return amat[aid2i[a]] if a in aid2i else fb_vec.get(a)

    base = df[keys].drop_duplicates().reset_index(drop=True)
    sims = []
    for r, a in zip(base["rid"], base["authorid"]):
        tv = tvecs.get(rid_text.get(r))
        v = rep(a)
        sims.append(float(v @ tv) if (tv is not None and v is not None) else np.nan)
    base["sim"] = sims
    return base


def match_4():
    """层4 兜底:passed DESC -> 论文数 DESC -> authorid ASC, 每 rid 取一个。"""
    return con.sql("""
        WITH cand AS (
            SELECT DISTINCT rid, us_name, authorid, match_type, host_id, passed FROM r4in
        ),
        pcnt AS (
            SELECT authorid, count(DISTINCT paperid) AS n_papers
            FROM '../sciscinet_authors_paperid.parquet'
            WHERE authorid IN (SELECT DISTINCT authorid FROM cand)
            GROUP BY authorid
        ),
        ranked AS (
            SELECT c.rid, c.us_name, c.authorid, c.match_type, c.host_id, c.passed,
                   COALESCE(p.n_papers, 0) AS n_papers,
                   row_number() OVER (
                       PARTITION BY c.rid
                       ORDER BY c.passed DESC, COALESCE(p.n_papers, 0) DESC, c.authorid
                   ) AS rnk
            FROM cand c
            LEFT JOIN pcnt p ON c.authorid = p.authorid
        )
        SELECT rid, us_name, authorid, match_type, host_id, passed, n_papers
        FROM ranked
        WHERE rnk = 1
    """).df()


def homonym_reopen(uniq_rids, r1):
    """同名者补召(L1->L2 交接):host 准入的 fl 召回, 把真人放进候选池。

    与 EU 版同口径, 只负责补候选; 消歧仍交给层2/层3/层4。
    """
    cols = ['rid', 'us_name', 'authorid', 'match_type']
    empty = r1.iloc[:0][['rid', 'us_name', 'authorid']].assign(match_type='fuzzy')[cols]

    ea = r1[r1['match_type'].isin(['exact', 'alias'])][['rid', 'us_name']].drop_duplicates()
    if ea.empty:
        return set(), empty

    exist = r1[['rid', 'authorid']].drop_duplicates()
    con.register('reopen_rids', ea)
    con.register('reopen_exist', exist)

    cand = con.sql("""
        WITH rids AS (
            SELECT DISTINCT r.rid, r.us_name, h.host_openalex_id AS host_id
            FROM reopen_rids r JOIN host h ON r.rid = h.row_id
            WHERE h.host_openalex_id IS NOT NULL AND h.host_openalex_id <> ''
        ),
        flh AS (
            SELECT DISTINCT r.rid, r.us_name, sci.authorid, sci.display_name AS cname
            FROM rids r
            JOIN '../sciscinet_authors.parquet' sci
              ON fl(sci.display_name) = fl(r.us_name)
            JOIN '../sciscinet_paper_author_affiliation.parquet' aff
              ON aff.authorid = sci.authorid AND aff.institutionid = r.host_id
            LEFT JOIN reopen_exist e ON e.rid = r.rid AND e.authorid = sci.authorid
            WHERE e.authorid IS NULL
        )
        SELECT rid, us_name, authorid, cname FROM flh
    """).df()
    if cand.empty:
        return set(), empty

    cand = cand[cand.apply(lambda r: names_match(r['us_name'], r['cname']), axis=1)]
    if cand.empty:
        return set(), empty

    inject = cand[['rid', 'us_name', 'authorid']].copy()
    inject['match_type'] = 'fuzzy'
    withdraw = set(inject['rid']) & set(uniq_rids)
    return withdraw, inject[cols]


def main():
    cols = ['rid', 'us_name', 'authorid', 'match_type', 'host_id', 'source']
    resolved = []

    # 层1:精确 -> 别名 -> 首末名模糊
    r1_exact = match_1a()
    r1_exact['match_type'] = 'exact'
    con.register('r1_exact', r1_exact)

    r1_alias = match_1b() if USE_ALIAS else r1_exact.iloc[:0].copy()
    r1_alias['match_type'] = 'alias'
    r1_ea = pd.concat([r1_exact, r1_alias], ignore_index=True)
    con.register('r1_ea', r1_ea)

    r1_fuzzy = match_1c() if USE_FUZZY else r1_exact.iloc[:0].copy()
    r1_fuzzy['match_type'] = 'fuzzy'

    r1 = pd.concat([r1_exact, r1_alias, r1_fuzzy], ignore_index=True)
    pending = set(r1['rid'])

    # name_unique:精确∪别名 合起来仍唯一 authorid, 且该 rid 有精确命中。
    ea_ids = pd.concat([r1_exact[['rid', 'authorid']], r1_alias[['rid', 'authorid']]],
                       ignore_index=True).drop_duplicates()
    ea_nunique = ea_ids.groupby('rid')['authorid'].nunique()
    uniq_rids = set(ea_nunique[ea_nunique == 1].index) & set(r1_exact['rid'])

    withdraw, inject = homonym_reopen(uniq_rids, r1)
    uniq_rids -= withdraw
    if not inject.empty:
        r1 = pd.concat([r1, inject], ignore_index=True)

    take = (r1_exact[r1_exact['rid'].isin(uniq_rids)]
            .drop_duplicates(subset='rid').copy())
    take['host_id'] = pd.NA
    take['source'] = 'name_unique'
    resolved.append(take[cols])
    pending -= uniq_rids

    print('层1→L2 交接:名字唯一 %d(host 确认 fl 重开退出 %d),剩下 %d'
          % (len(uniq_rids), len(withdraw), len(pending)))

    # 层2:机构消歧
    pend = r1[r1['rid'].isin(pending)]
    con.register('pend', pend)
    r2 = match_2()
    r2cnt = r2.groupby('rid')['authorid'].nunique()
    take2 = set(r2cnt[r2cnt == 1].index)
    tie_rids = set(r2cnt[r2cnt > 1].index)
    t = r2[r2['rid'].isin(take2)].copy()
    t['source'] = 'round2_inst'
    resolved.append(t[cols])
    pending -= take2

    noinst_rids = pending - tie_rids
    print('层2 机构 → 确定 %d,机构平局 %d,机构缺失 %d,剩下 %d'
          % (len(take2), len(tie_rids), len(noinst_rids), len(pending)))

    # 层3:机构平局 + 机构缺失一起进语义消歧
    l3_ties = r2[r2['rid'].isin(tie_rids)][cols[:-1]]
    l3_noinst = pend[pend['rid'].isin(noinst_rids)][['rid', 'us_name', 'authorid', 'match_type']].copy()
    l3_noinst['host_id'] = pd.NA
    l3_in = (pd.concat([l3_ties, l3_noinst[cols[:-1]]], ignore_index=True)
               .drop_duplicates())
    con.register('r3in', l3_in)

    r3_long = match_3()
    scored = semantic_score(r3_long)
    scored = scored.sort_values(['rid', 'sim'], ascending=[True, False]).reset_index(drop=True)
    sem_thresh, sem_margin = (SEM_THRESH_V2, SEM_MARGIN_V2) if _use_v2() else (SEM_THRESH, SEM_MARGIN)
    print('层3 语义打分:%s (thresh=%.2f, margin=%.2f)'
          % ('v2 作者摘要质心 × award Abstract' if _use_v2()
             else 'v1 子学科名 × Title+Program(s)', sem_thresh, sem_margin))

    picks = []
    for rid, g in scored.groupby('rid'):
        passed = g[g['sim'] >= sem_thresh].sort_values('sim', ascending=False)
        if passed.empty:
            continue
        if len(passed) == 1 or (passed.iloc[0]['sim'] - passed.iloc[1]['sim'] >= sem_margin):
            picks.append(passed.iloc[[0]].assign(source='round3_semantic'))

    r3res = pd.concat(picks, ignore_index=True) if picks else scored.iloc[:0].assign(source=pd.NA)
    take3 = set(r3res['rid'])
    resolved.append(r3res[cols])
    pending -= take3
    print('层3 语义 → 确定 %d(sim>=%.2f,margin>=%.2f),剩下 %d'
          % (len(take3), sem_thresh, sem_margin, len(pending)))

    r3 = r3_long.merge(scored[['rid', 'authorid', 'sim']], on=['rid', 'authorid'], how='left')
    r3 = r3.sort_values(['rid', 'sim', 'authorid', 'rnk'],
                        ascending=[True, False, True, True]).reset_index(drop=True)

    # 层4:兜底
    l4_in = (l3_in[l3_in['rid'].isin(pending)]
             .merge(scored[['rid', 'authorid', 'sim']], on=['rid', 'authorid'], how='left'))
    l4_in['passed'] = (l4_in['sim'] >= sem_thresh).fillna(False)
    con.register('r4in', l4_in)
    r4res = match_4()
    r4res['source'] = np.where(r4res['passed'], 'round4_semtie', 'round4_maxpapers')
    take4 = set(r4res['rid'])
    resolved.append(r4res[cols])
    pending -= take4
    print('层4 兜底 → 确定 %d(语义平局 %d,论文数兜底 %d),剩下 %d'
          % (len(take4), int((r4res['source'] == 'round4_semtie').sum()),
             int((r4res['source'] == 'round4_maxpapers').sum()), len(pending)))

    r4 = r4res.sort_values('rid').reset_index(drop=True)

    # 收口
    final = (pd.concat(resolved, ignore_index=True)
               .drop_duplicates(subset='rid').sort_values('rid').reset_index(drop=True))

    con.register('final_ids', final[['authorid']].drop_duplicates())
    npdf = con.sql("""
        SELECT authorid, count(DISTINCT paperid) AS n_papers
        FROM '../sciscinet_authors_paperid.parquet'
        WHERE authorid IN (SELECT authorid FROM final_ids)
        GROUP BY authorid
    """).df()
    final = final.merge(npdf, on='authorid', how='left')
    final['n_papers'] = final['n_papers'].fillna(0).astype(int)
    held = (final[final['n_papers'] < PAPER_FLOOR]
            .sort_values(['source', 'n_papers', 'rid']).reset_index(drop=True))
    final = final[final['n_papers'] >= PAPER_FLOOR].reset_index(drop=True)

    r1.to_csv("match_1_us.csv", index=False, encoding='utf-8-sig')
    r2.to_csv("match_2_us.csv", index=False, encoding='utf-8-sig')
    r3.to_csv("match_3_us.csv", index=False, encoding='utf-8-sig')
    r4.to_csv("match_4_us.csv", index=False, encoding='utf-8-sig')
    final.to_csv("matched_final_us.csv", index=False, encoding='utf-8-sig')
    held.to_csv("held_lowpaper_us.csv", index=False, encoding='utf-8-sig')

    total = con.sql("SELECT count(*) FROM us").fetchone()[0]
    n_matched, n_held = len(final), len(held)
    print()
    print('汇总:已唯一确定的 US 记录(论文数下限闸 = %d)' % PAPER_FLOOR)
    print('匹配成功:', n_matched, '| 筛选率: %d / %d = %.1f%%'
          % (n_matched, total, 100.0 * n_matched / total))
    print('低产扣留(< %d 篇,待人工): %d,按 source: %s'
          % (PAPER_FLOOR, n_held, held['source'].value_counts().to_dict()))
    print('  来源:名字唯一', int((final['source'] == 'name_unique').sum()),
          '| 机构', int((final['source'] == 'round2_inst').sum()),
          '| 语义领域', int((final['source'] == 'round3_semantic').sum()),
          '| 语义平局兜底', int((final['source'] == 'round4_semtie').sum()),
          '| 论文数兜底', int((final['source'] == 'round4_maxpapers').sum()))
    print('  名字类型:精确', int((final['match_type'] == 'exact').sum()),
          '| 首末名模糊', int((final['match_type'] == 'fuzzy').sum()),
          '| 别名', int((final['match_type'] == 'alias').sum()))
    print('结果已保存:match_1_us.csv, match_2_us.csv, match_3_us.csv, match_4_us.csv, '
          'matched_final_us.csv, held_lowpaper_us.csv')


if __name__ == "__main__":
    main()
