# -*- coding: utf-8 -*-
import os
import duckdb
import numpy as np
import pandas as pd

from fuzzyname_vendored import names_match   # L1c 模糊候选的成对精度复核(vendored,见文件头)

con = duckdb.connect()

# ===== L1c 名字精度过滤(fuzzyname)=====
# match_1c 用 fl(首名|末名)召回,positional 且丢中间名,会把"首末名相同但中间名冲突"的
# 同名者也召进来(如 EU 'Jonathan Paul Marchini' 误配 sci 'Jonathan Lawrence Marchini')。
# 打开后用 fuzzyname 成对复核每个模糊候选,只保留 名字规则下算同一人 的(首字母/中间名一致/
# 子集/昵称)。只作用在 match_1c 的候选上,不动 shell_reopen(那层已有机构确认闸)。
# 默认【关】,不改变原有流水线行为;A/B 时用 L1C_FUZZYNAME=1 打开。
L1C_FUZZYNAME = os.environ.get("L1C_FUZZYNAME", "0") == "1"

# 允许把大表扫描/连接的中间结果溢写到磁盘:内存版 DuckDB(connect() 不带文件)默认
# 无 temp_directory、完全不溢写,match_2 扫 6.8GB 的 affiliation parquet 会 OOM。
# 纯执行层配置,不影响任何匹配结果。
con.sql("SET temp_directory='.duckdb_tmp'")     # 溢写目录(关键)
con.sql("SET memory_limit='12GB'")              # 卡在可用内存以内
con.sql("SET preserve_insertion_order=false")   # 大扫描/聚合省内存

# ===== 第三轮语义相似度参数 =====
# bge cosine 有 ~0.5 的高地板:正确匹配 ~0.73-0.81,跨领域 ~0.50-0.70。
# 判定以"同一 rid 下取最高分候选"为主(相对),THRESH 只当"连最像的都明显不相关就不收"的安全闸。
EMB_MODEL = "BAAI/bge-small-en-v1.5"
SEM_THRESH = 0.62   # 绝对下限:候选前3领域名与 EU 面板文本的最大 cosine 需 >= 此值
SEM_MARGIN = 0.03   # 同 rid 多个候选都过阈值时,第一名要比第二名高出的最小差,否则判为模糊不定

# ===== 空壳重开(shell_reopen)参数 =====
# name_unique 里"锁定作者是低产空壳、但存在一个首末名相同、在 EU host 机构发过文的更高产
# 候选"的 rid:不在 L1 锁死,放开首末名候选交给 L2 机构裁决。A/B(verify_orcid 独立复核)实测
# 机构确认子集 9 修正/0 打破;唯一翻车案例发生在"机构确认不了、只按论文数最高"时——所以闸是
# 【机构确认】而非【论文数最高】,机构确认不了的空壳一律保持锁定(不回归)。详见 [[name-unique-alias-hole]]。
SHELL_MAXP = 2       # 锁定作者论文数 <= 此值 → 视为空壳嫌疑,触发重开检查
HOMONYM_MINP = 10    # 首末名同名者论文数下限(高产)
HOMONYM_RATIO = 5    # 同名者论文数需 >= RATIO × 锁定者(相对更高产)

# EU.xlsx 注册成视图,并加一列行号 rid(从 0 开始),用来和 val_host_ids.csv 按行对齐
con.sql("""
    CREATE VIEW eu AS
    SELECT (row_number() OVER ()) - 1 AS rid, *
    FROM read_xlsx('EU.xlsx', all_varchar = true)
""")

# 名字归一化宏:先把连字符/破折号家族(普通连字符 U+002D、软连字符 U+00AD、
# Unicode 连字符/破折号 U+2010–U+2015、减号 U+2212)统一成空格 -> 去重音(é->e)
# -> 小写 -> 去首尾 / 压缩空格。第一轮用它做匹配键。
# 只收窄到"连字符类"、不动句点等其它标点:OpenAlex 名字会混入 U+2010 等奇怪连字符
con.sql(r"""
    CREATE MACRO norm(x) AS
    regexp_replace(
        trim(lower(strip_accents(
            regexp_replace(x, '[\x{002D}\x{00AD}\x{2010}-\x{2015}\x{2212}]', ' ', 'g')
        ))),
        '\s+', ' ', 'g')
""")

# 首名+末名(姓)key:丢掉中间名/缩写,用于 match_1c 模糊补配(如 "Jonathan Lawrence Marchini" -> "jonathan|marchini")
con.sql(r"""
    CREATE MACRO fl(x) AS
    regexp_extract(norm(x), '^(\S+)', 1) || '|' || regexp_extract(norm(x), '(\S+)$', 1)
""")

# 排序词集 key:把名字按词拆开、排序、再拼回,解决姓名顺序颠倒(如 "Madanbabu Mohan" == "Mohan Madanbabu")
con.sql(r"""
    CREATE MACRO wset(x) AS
    array_to_string(list_sort(string_split(norm(x), ' ')), ' ')
""")

# 每个 EU 行的 host 机构对应的 OpenAlex 机构 id(按 row_id 对齐 rid)。
# 桥 = host_ids_bridge.csv:resolve_host_ids_api.py 的产物(exact/fuzzy/weak)为主,
#      旧 val_host_ids.csv 只补新方法为空的 106 个空档 -> 覆盖 3921/4242。
# 只取 row_id + host_openalex_id 两列即可(matched_name/method 等复核信息不参与匹配)。
con.sql("CREATE VIEW host AS SELECT row_id, host_openalex_id FROM 'host_ids_bridge.csv'")

# 第三轮语义匹配用:每个 EU 行的"领域文本"。优先用 Panel 全名(去掉 "PE9 - " 前缀,
# 如 "Universe Sciences"),Panel 缺失("-"/空)时回退用 Domain(去掉 "(PE)" 后缀)。
# 两者都缺则为 NULL(该行无法做语义比较)。
con.sql(r"""
    CREATE VIEW eu_field_text AS
    SELECT rid,
        CASE
            WHEN "Panel" IS NOT NULL AND "Panel" <> '-'
                THEN trim(regexp_replace("Panel", '^[A-Z]{2}[0-9]+\s*-\s*', ''))
            WHEN "Domain" IS NOT NULL AND "Domain" <> '-'
                THEN trim(regexp_replace("Domain", '\s*\([A-Z]{2}\)\s*$', ''))
            ELSE NULL
        END AS eu_text
    FROM eu
""")


def match_1a():
    # 第一轮粗匹配:authors 表的 display_name 和 EU 表的 Researcher(s) 比较。
    # 用 norm() 归一化后再比, 防止一些奇怪的欧洲字母不一样导致匹配不上
    # 两张表可能都存在重名
    result = con.sql("""
        SELECT
            eu.rid             AS rid,
            eu."Researcher(s)" AS eu_name,
            sci.authorid       AS authorid,
            sci.display_name   AS sci_name
        FROM eu
        JOIN 'sciscinet_authors.parquet' sci
          ON norm(eu."Researcher(s)") = norm(sci.display_name)
    """).df()
    return result


def match_1b():
    # 别名召回:用 author_details 的 display_name_alternatives(曾用名/别名/拼写变体)。
    # 【对所有 rid 生效,别名与 display_name 平级】——哪怕某 rid 已被精确命中,别名也可能
    # 指向另一个 authorid(真人常把 EU 里的写法登记成别名,而其 display_name 是缩写/带中间名/
    # 带重音的形式,精确层只会命中一个论文数极少的空壳)。这些别名候选并进候选池,
    # name_unique 的唯一性判定改用 精确∪别名(见 main);消歧仍交给机构/语义/层4。
    # 两种 key:整名别名(拼写变体)、排序词集(姓名顺序颠倒)。整串/整词集相等,精度高。
    result = con.sql("""
        WITH nm AS (
            -- 所有有名字的 EU 行(不再排除已精确命中的 rid)
            SELECT rid,
                   "Researcher(s)"       AS eu_name,
                   norm("Researcher(s)") AS nn,
                   wset("Researcher(s)") AS ws
            FROM eu
            WHERE "Researcher(s)" IS NOT NULL
        ),
        alias AS (
            -- 展开别名数组,只保留 key 命中上面这些没配上名字的
            SELECT authorid, display_name, norm(alt) AS nalias, wset(alt) AS wsalias
            FROM (
                SELECT authorid, display_name,
                       unnest(from_json(display_name_alternatives, '["VARCHAR"]')) AS alt
                FROM 'sciscinet_author_details.parquet'
            )
            WHERE norm(alt) IN (SELECT nn FROM nm)
               OR wset(alt) IN (SELECT ws FROM nm)
        )
        SELECT DISTINCT
            nm.rid, nm.eu_name, a.authorid,
            a.display_name AS sci_name
        FROM nm
        JOIN alias a
          ON nm.nn = a.nalias
          OR nm.ws = a.wsalias
    """).df()
    return result


def match_1c():
    # 第一轮兜底(最松、放在最后):归一化精确、别名都没配上的 EU 行,丢掉中间名和缩写。
    # 首名末名较宽松、fan-out 大,所以这些候选不单独可信,一律要靠第二轮机构过滤才会被接受。
    # 只处理不在 r1_ea(精确+别名)里的 rid,避免用松匹配盖掉已被更高精度层配上的行。
    result = con.sql("""
        SELECT
            eu.rid             AS rid,
            eu."Researcher(s)" AS eu_name,
            sci.authorid         AS authorid,
            sci.display_name     AS sci_name
        FROM eu
        JOIN 'sciscinet_authors.parquet' sci
          ON fl(sci.display_name) = fl(eu."Researcher(s)")
        WHERE eu."Researcher(s)" IS NOT NULL
          AND eu.rid NOT IN (SELECT DISTINCT rid FROM r1_ea)
    """).df()
    return result


def match_2():
    result = con.sql("""
        WITH cand AS (
            -- 拿待定候选 pend;只补上该 EU 行 host 机构的 openalex_id
            SELECT
                pend.rid,
                pend.eu_name,
                pend.authorid,
                pend.match_type,
                h.host_openalex_id AS host_id
            FROM pend
            JOIN host h ON pend.rid = h.row_id
            WHERE h.host_openalex_id IS NOT NULL
        ),
        aff AS (
            -- 只查候选作者、且机构正好是某个 EU host 机构的记录,去重到 (作者, 机构)
            SELECT DISTINCT authorid, institutionid
            FROM 'sciscinet_paper_author_affiliation.parquet'
            WHERE authorid      IN (SELECT authorid FROM cand)
              AND institutionid IN (SELECT host_id  FROM cand)
        )
        -- 名字 + 机构 都对上的才留下
        SELECT c.rid, c.eu_name, c.authorid, c.match_type, c.host_id
        FROM cand c
        JOIN aff a
          ON c.authorid = a.authorid
         AND c.host_id  = a.institutionid
    """).df()
    return result


def match_3():
    """层3 语义消歧的取数部分(纯 DuckDB,不含 embedding)。

    对每个待消歧候选 author,取他发文占比【前 3】的 level-1 子学科名(如 Astronomy /
    Optics / Astrophysics),连同该 EU 行的领域文本(eu_text)一起返回。
    返回长表:每 (rid, authorid) 最多 3 行,列 = rid, eu_name, authorid, match_type,
    host_id, eu_text, field_name, share, rnk。没有 level-1 论文的 author -> field_name 为 NULL。
    语义打分(embedding + cosine)在 semantic_score() 里做。
    """
    result = con.sql("""
        WITH cand AS (
            SELECT DISTINCT authorid FROM r3in
        ),
        papers AS (
            -- 只取候选 author 的论文
            SELECT authorid, paperid
            FROM 'sciscinet_authors_paperid.parquet'
            WHERE authorid IN (SELECT authorid FROM cand)
        ),
        lvl1 AS (
            -- OpenAlex level-1 子学科(284 个),细领域信号就用这一级
            SELECT fieldid, display_name
            FROM 'sciscinet_fields.parquet'
            WHERE level = 1
        ),
        field_cnt AS (
            -- 每个 author 在各 level-1 子学科的发文数
            SELECT p.authorid, pf.fieldid, count(*) AS n
            FROM papers p
            JOIN 'sciscinet_paperfields.parquet' pf ON p.paperid = pf.paperid
            WHERE pf.fieldid IN (SELECT fieldid FROM lvl1)
            GROUP BY 1, 2
        ),
        ranked AS (
            -- 各 level-1 子学科的发文占比,按发文数排名
            SELECT authorid, fieldid, n,
                   n * 1.0 / sum(n) OVER (PARTITION BY authorid) AS share,
                   row_number() OVER (PARTITION BY authorid ORDER BY n DESC, fieldid) AS rnk
            FROM field_cnt
        ),
        top3 AS (
            -- 每个 author 占比前 3 的 level-1 子学科(带学科名)
            SELECT r.authorid, l.display_name AS field_name, r.share, r.rnk
            FROM ranked r
            JOIN lvl1 l ON r.fieldid = l.fieldid
            WHERE r.rnk <= 3
        )
        SELECT c.rid, c.eu_name, c.authorid, c.match_type, c.host_id,
               t.eu_text, top3.field_name, top3.share, top3.rnk
        FROM r3in c
        JOIN eu_field_text t ON c.rid = t.rid
        LEFT JOIN top3        ON c.authorid = top3.authorid
    """).df()
    return result


_EMBED = {}


def _get_embedder():
    """惰性加载本地 bge 句向量模型;返回 embed(list[str]) -> np.ndarray(已 L2 归一)。"""
    if "fn" in _EMBED:
        return _EMBED["fn"]
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    import torch
    from transformers import AutoTokenizer, AutoModel
    tok = AutoTokenizer.from_pretrained(EMB_MODEL)
    model = AutoModel.from_pretrained(EMB_MODEL)
    model.eval()

    @torch.no_grad()
    def embed(texts):
        out = []
        for i in range(0, len(texts), 64):
            enc = tok(texts[i:i + 64], padding=True, truncation=True,
                      max_length=64, return_tensors="pt")
            v = model(**enc).last_hidden_state[:, 0]            # bge: CLS pooling
            v = torch.nn.functional.normalize(v, p=2, dim=1)
            out.append(v.cpu().numpy())
        return np.vstack(out)

    _EMBED["fn"] = embed
    return embed


def semantic_score(long_df):
    """把 match_3() 的长表打分:每 (rid, authorid) 返回一行,sim = 其前3 level-1 领域名
    与该行 eu_text 的【最大】cosine(取最像的一个领域)。无领域/无 eu_text -> sim = NaN。"""
    keys = ["rid", "eu_name", "authorid", "match_type", "host_id"]
    df = long_df.copy()

    # 唯一短文本一次性嵌入(领域名 + EU 面板文本,合计约几百条)
    texts = pd.unique(pd.concat(
        [df["eu_text"].dropna(), df["field_name"].dropna()], ignore_index=True))
    if len(texts) == 0:
        base = df[keys].drop_duplicates().reset_index(drop=True)
        base["sim"] = np.nan
        return base
    vecs = _get_embedder()(list(texts))
    idx = {t: i for i, t in enumerate(texts)}

    def cos(f, e):
        if not isinstance(f, str) or not isinstance(e, str):
            return np.nan
        return float(vecs[idx[f]] @ vecs[idx[e]])

    df["cos"] = [cos(f, e) for f, e in zip(df["field_name"], df["eu_text"])]
    sim = (df.groupby(keys, dropna=False)["cos"].max()
             .reset_index().rename(columns={"cos": "sim"}))
    return sim


def match_4():
    """层4 兜底:层3 之后仍有歧义(重名候选无法收敛)的 rid,在候选里定案。候选来自 r4in
    (层3 未定的 rid 及其同名候选),每个候选带一个 passed 标志 = 该候选在层3 是否过了语义阈值。

    排序键(同一 rid 内):passed DESC → 论文数 DESC → authorid ASC。含义:
      - 若该 rid 有过阈值的候选(A 类:语义近似平局),只在【过阈值的候选】里挑论文数最高的,
        把语义不相关的高产同名者挡在门外;
      - 若一个都没过阈值(B 类:无语义信号),passed 全 False,自动退回【纯论文数】兜底。
    论文数用 sciscinet_authors_paperid.parquet 按 authorid 数【去重 paperid】,没记录记 0 篇。
    并列时按 authorid 升序取一个,保证确定。返回每 rid 一行,带 passed / n_papers。
    """
    result = con.sql("""
        WITH cand AS (
            SELECT DISTINCT rid, eu_name, authorid, match_type, host_id, passed FROM r4in
        ),
        pcnt AS (
            -- 每个候选 author 的论文数(去重 paperid)
            SELECT authorid, count(DISTINCT paperid) AS n_papers
            FROM 'sciscinet_authors_paperid.parquet'
            WHERE authorid IN (SELECT DISTINCT authorid FROM cand)
            GROUP BY authorid
        ),
        ranked AS (
            -- 同一 rid 内:先按是否过语义阈值,再按论文数降序,没有论文记录的 author 记 0 篇
            SELECT c.rid, c.eu_name, c.authorid, c.match_type, c.host_id, c.passed,
                   COALESCE(p.n_papers, 0) AS n_papers,
                   row_number() OVER (
                       PARTITION BY c.rid
                       ORDER BY c.passed DESC, COALESCE(p.n_papers, 0) DESC, c.authorid
                   ) AS rnk
            FROM cand c
            LEFT JOIN pcnt p ON c.authorid = p.authorid
        )
        SELECT rid, eu_name, authorid, match_type, host_id, passed, n_papers
        FROM ranked
        WHERE rnk = 1
    """).df()
    return result


def shell_reopen(uniq_rids, r1_exact):
    """空壳重开:在 name_unique 直接定档的 rid 里,挑出"锁定作者是低产空壳(<=SHELL_MAXP 篇)、
    且存在一个首末名相同、论文数高得多(>=HOMONYM_MINP 且 >=HOMONYM_RATIO 倍)、并且在该 EU 行
    host 机构发过文的候选"的 rid —— 这些 rid 的精确命中往往是个空壳,真人以中间名/缩写形式另存。
    对它们:不在 L1 锁死,放开首末名候选交给 L2 机构裁决。

    机构确认是关键闸(而非论文数最高):A/B 实测机构确认子集 9 修正/0 打破,唯一翻车发生在机构
    确认不了、仅按论文数最高时。机构确认不了的空壳一律保持锁定(不动、不回归)。

    返回 (reopen_rids:set, extra_fuzzy:DataFrame[rid,eu_name,authorid,match_type='fuzzy'])。
    仍不依赖 ORCID:论文数只作为 matcher 内部触发器,与 verify_orcid.py 的独立复核正交。
    """
    empty = r1_exact.iloc[:0][['rid', 'eu_name', 'authorid']].assign(match_type='fuzzy')
    nu_all = (r1_exact[r1_exact['rid'].isin(uniq_rids)]
              .drop_duplicates(subset='rid')[['rid', 'eu_name', 'authorid']]
              .rename(columns={'authorid': 'locked_authorid'}))
    if nu_all.empty:
        return set(), empty
    con.register('nu_all', nu_all)

    # 1) 先只算锁定作者的论文数,过滤出"空壳"rid —— 把后面昂贵的首末名 fan-out 限制在这一小撮
    shell = con.sql(f"""
        WITH pc AS (
            SELECT authorid, count(DISTINCT paperid) AS n
            FROM 'sciscinet_authors_paperid.parquet'
            WHERE authorid IN (SELECT locked_authorid FROM nu_all)
            GROUP BY authorid
        )
        SELECT nu_all.rid, nu_all.eu_name, nu_all.locked_authorid,
               COALESCE(pc.n, 0) AS locked_papers
        FROM nu_all LEFT JOIN pc ON nu_all.locked_authorid = pc.authorid
        WHERE COALESCE(pc.n, 0) <= {SHELL_MAXP}
    """).df()
    if shell.empty:
        return set(), empty
    con.register('shell', shell)

    # 2) 只对空壳 rid 做首末名召回,带上每个候选的论文数 + 是否在该 rid 的 host 机构发过文
    flc = con.sql(f"""
        WITH flcand AS (
            SELECT s.rid, s.eu_name, s.locked_authorid, s.locked_papers,
                   sci.authorid AS cand_authorid
            FROM shell s
            JOIN 'sciscinet_authors.parquet' sci
              ON fl(sci.display_name) = fl(s.eu_name)
        ),
        pc AS (
            SELECT authorid, count(DISTINCT paperid) AS n
            FROM 'sciscinet_authors_paperid.parquet'
            WHERE authorid IN (SELECT cand_authorid FROM flcand)
            GROUP BY authorid
        ),
        athost AS (
            -- 候选作者在"其所属 rid 的 host 机构"发过文(与 match_2 同一口径的机构确认)
            SELECT DISTINCT f.rid, f.cand_authorid
            FROM flcand f
            JOIN host h ON f.rid = h.row_id
            JOIN 'sciscinet_paper_author_affiliation.parquet' aff
              ON aff.authorid = f.cand_authorid
             AND aff.institutionid = h.host_openalex_id
        )
        SELECT f.rid, f.eu_name, f.locked_authorid, f.locked_papers, f.cand_authorid,
               COALESCE(pc.n, 0) AS n_papers,
               (f.cand_authorid = f.locked_authorid) AS is_locked,
               (ah.cand_authorid IS NOT NULL) AS at_host
        FROM flcand f
        LEFT JOIN pc     ON f.cand_authorid = pc.authorid
        LEFT JOIN athost ah ON f.rid = ah.rid AND f.cand_authorid = ah.cand_authorid
    """).df()

    # 3) 判定 reopen:存在一个 非锁定 + 高产 + 机构确认 的首末名同名者
    good = flc[(~flc['is_locked']) & (flc['at_host'])
               & (flc['n_papers'] >= HOMONYM_MINP)
               & (flc['n_papers'] >= HOMONYM_RATIO * flc['locked_papers'].clip(lower=1))]
    reopen_rids = set(good['rid'])

    # 4) reopen rid 的全部(非锁定)首末名候选一起放进候选池,由 L2 机构过滤裁决(可能有多个候选
    #    都在该机构 → L2 平局再交 L3 语义,符合既有分层)
    extra = (flc[flc['rid'].isin(reopen_rids) & (~flc['is_locked'])]
             [['rid', 'eu_name', 'cand_authorid']]
             .rename(columns={'cand_authorid': 'authorid'})
             .drop_duplicates())
    extra['match_type'] = 'fuzzy'
    return reopen_rids, extra


def main():
    cols = ['rid', 'eu_name', 'authorid', 'match_type', 'host_id', 'source']
    resolved = []   # 每层剔除定下来的行(带 source)

    # 第一轮:名字匹配。精度从高到低排层,前层先占坑、后层排除已占的 rid:
    # 精确 → 别名(整名变体/词集,高精度) → 首末名模糊(丢中间名,最松、兜底)。
    r1_exact = match_1a()
    r1_exact['match_type'] = 'exact'
    con.register('r1_exact', r1_exact)                 # 供 match_1b 排除已精确配上的 rid

    r1_alias = match_1b()
    r1_alias['match_type'] = 'alias'
    r1_ea = pd.concat([r1_exact, r1_alias], ignore_index=True)
    con.register('r1_ea', r1_ea)                       # 供 match_1c 排除已精确/别名配上的 rid

    r1_fuzzy = match_1c()
    r1_fuzzy['match_type'] = 'fuzzy'

    # L1c 精度过滤:fl 只按首|末名配对,漏掉中间名一致性;用 fuzzyname 成对复核,丢掉
    # 中间名冲突/非同一人的模糊候选。fl 已保证首末名相同 → 过滤只会移除中间名冲突子集,
    # 不会误伤"两边都没中间名"的干净命中。见文件头 L1C_FUZZYNAME 说明。
    if L1C_FUZZYNAME and len(r1_fuzzy):
        keep = r1_fuzzy.apply(lambda r: names_match(r['eu_name'], r['sci_name']), axis=1)
        n_rid_before = r1_fuzzy['rid'].nunique()
        n_lost_all = n_rid_before - r1_fuzzy[keep]['rid'].nunique()
        print('L1c fuzzyname 过滤:模糊候选 %d → 保留 %d(丢 %d);受影响后整 rid 全丢 %d'
              % (len(r1_fuzzy), int(keep.sum()), int((~keep).sum()), n_lost_all))
        r1_fuzzy = r1_fuzzy[keep].reset_index(drop=True)

    r1 = pd.concat([r1_exact, r1_alias, r1_fuzzy], ignore_index=True)
    pending = set(r1['rid'])                            # 还没唯一确定的 EU 行

    # print('候选行数: 精确', len(r1_exact), '| 模糊', len(r1_fuzzy), '| 别名', len(r1_alias))
    # print('有候选的 EU 记录:', len(pending))
    # print()

    # name_unique(层1 直接定档):只针对"有精确命中"的 rid,且当 精确∪别名 合起来仍是
    # 唯一 authorid 时才收。别名现已对所有 rid 生效(match_1b),所以"精确唯一、但别名带出了
    # 另一个 authorid"的 rid 不再算唯一——把两边候选一起降级,交给 机构/语义/层4 消歧。
    # 真正干净的唯一命中(精确唯一、且无冲突别名)照旧在这里定,保护面不变。
    ea_ids = pd.concat([r1_exact[['rid', 'authorid']], r1_alias[['rid', 'authorid']]],
                       ignore_index=True).drop_duplicates()
    ea_nunique = ea_ids.groupby('rid')['authorid'].nunique()
    uniq_rids = set(ea_nunique[ea_nunique == 1].index) & set(r1_exact['rid'])

    # 空壳重开:name_unique 里"锁定低产空壳、但有机构确认的高产首末名同名者"的 rid,退出 L1
    # 直接定档,把首末名候选放进候选池交给 L2 机构裁决(A/B 实测 9 修/0 破,见 [[name-unique-alias-hole]])。
    reopen_rids, extra_fuzzy = shell_reopen(uniq_rids, r1_exact)
    uniq_rids -= reopen_rids
    if not extra_fuzzy.empty:
        r1 = pd.concat([r1, extra_fuzzy], ignore_index=True)   # 新候选并入池,供后面 pend/L2 使用

    take = (r1_exact[r1_exact['rid'].isin(uniq_rids)]
            .drop_duplicates(subset='rid').copy())
    take['host_id'] = pd.NA
    take['source'] = 'name_unique'
    resolved.append(take[cols])
    pending -= uniq_rids
    print('层1 名字唯一 → 确定 %d(空壳重开退出 %d,转 L2),剩下 %d'
          % (len(uniq_rids), len(reopen_rids), len(pending)))

    #层2:机构消歧(只处理剩下的重名/模糊/别名候选) 
    pend = r1[r1['rid'].isin(pending)]
    con.register('pend', pend)
    r2 = match_2()
    r2cnt = r2.groupby('rid')['authorid'].nunique()
    take2 = set(r2cnt[r2cnt == 1].index)               # 机构后只剩 1 个 认为确定
    tie_rids = set(r2cnt[r2cnt > 1].index)             # 机构后仍多个 ，进入match3 由领域确定
    t = r2[r2['rid'].isin(take2)].copy()
    t['source'] = 'round2_inst'
    resolved.append(t[cols])
    pending -= take2

    # 层2 用不上机构的行(host 机构 id 缺失,或候选 author 都没在该机构待过):r2 里根本没有它们,
    # 之前会被直接丢掉。现在把它们也送进层3,靠领域消歧(带层1的全部同名候选,host_id 置空)。
    noinst_rids = pending - tie_rids
    print('层2 机构 → 确定 %d,机构平局 %d,机构缺失 %d,剩下 %d'
          % (len(take2), len(tie_rids), len(noinst_rids), len(pending)))


    # 层3:领域消歧(机构平局 + 机构缺失 一起进)
    l3_ties = r2[r2['rid'].isin(tie_rids)][cols[:-1]]                       # 机构平局:用机构过滤后的候选
    l3_noinst = pend[pend['rid'].isin(noinst_rids)][['rid', 'eu_name', 'authorid', 'match_type']].copy()
    l3_noinst['host_id'] = pd.NA                                            # 机构缺失:没有机构佐证
    l3_in = (pd.concat([l3_ties, l3_noinst[cols[:-1]]], ignore_index=True)
               .drop_duplicates())
    con.register('r3in', l3_in)

    # 层3 语义消歧:候选前3 level-1 领域名 与 EU 面板文本 的最大 cosine
    r3_long = match_3()                                 # 长表:每候选最多 3 行领域名
    scored = semantic_score(r3_long)                    # 每 (rid, authorid) 一行,带 sim
    scored = scored.sort_values(['rid', 'sim'], ascending=[True, False]).reset_index(drop=True)

    # 判定(相对为主 + 绝对下限):同一 rid 下,先取过阈值(sim >= SEM_THRESH)的候选,
    #   - 只有 1 个过阈值            -> 收(round3_semantic)
    #   - 多个过阈值、且第一名比第二名高出 SEM_MARGIN -> 收最高分的那个
    #   - 多个过阈值但差距 < margin  -> 判为模糊,不定(留人工)
    picks = []
    for rid, g in scored.groupby('rid'):
        passed = g[g['sim'] >= SEM_THRESH].sort_values('sim', ascending=False)
        if passed.empty:
            continue
        if len(passed) == 1 or (passed.iloc[0]['sim'] - passed.iloc[1]['sim'] >= SEM_MARGIN):
            picks.append(passed.iloc[[0]].assign(source='round3_semantic'))
    r3res = pd.concat(picks, ignore_index=True) if picks else scored.iloc[:0].assign(source=pd.NA)
    take3 = set(r3res['rid'])
    resolved.append(r3res[cols])
    pending -= take3
    print('层3 语义 → 确定 %d(sim>=%.2f,margin>=%.2f),剩下 %d'
          % (len(take3), SEM_THRESH, SEM_MARGIN, len(pending)))
    print()

    # match_3.csv:长表 + 每候选的 sim,便于人工核对语义打分
    r3 = r3_long.merge(scored[['rid', 'authorid', 'sim']], on=['rid', 'authorid'], how='left')
    r3 = r3.sort_values(['rid', 'sim', 'authorid', 'rnk'],
                        ascending=[True, False, True, True]).reset_index(drop=True)

    # 层4:兜底定案。层1-3 都没能唯一确定的 rid(重名候选仍收不敛),在候选里选一个定给它。
    # 候选沿用进层3 的同名候选(l3_in),按剩下未定的 rid 过滤,并 merge 回层3 已算好的语义分:
    # passed = 该候选是否过了 SEM_THRESH。层4 先按 passed 再按论文数排(见 match_4),这样:
    #   - 有候选过阈值的 rid(语义近似平局)→ 只在过阈值候选里挑最高产的,挡掉无关高产同名者;
    #   - 一个都没过阈值的 rid(无语义信号)→ 退回纯论文数兜底。
    # 这一层把所有还有候选的 rid 都定下来(不再留人工),代价是牺牲一点精度换全覆盖。
    l4_in = (l3_in[l3_in['rid'].isin(pending)]
             .merge(scored[['rid', 'authorid', 'sim']], on=['rid', 'authorid'], how='left'))
    l4_in['passed'] = (l4_in['sim'] >= SEM_THRESH).fillna(False)
    con.register('r4in', l4_in)
    r4res = match_4()
    # source 拆两类:semtie = 有语义背书的平局裁决(可信度高);maxpapers = 纯论文数硬兜底(低置信,建议抽查)
    r4res['source'] = np.where(r4res['passed'], 'round4_semtie', 'round4_maxpapers')
    take4 = set(r4res['rid'])
    resolved.append(r4res[cols])
    pending -= take4
    print('层4 兜底 → 确定 %d(语义平局 %d,论文数兜底 %d),剩下 %d'
          % (len(take4), int((r4res['source'] == 'round4_semtie').sum()),
             int((r4res['source'] == 'round4_maxpapers').sum()), len(pending)))
    print()

    # match_4.csv:层4 各 rid 选中的 author 及其论文数,便于人工核对兜底决策
    r4 = r4res.sort_values('rid').reset_index(drop=True)

    # 最后整理数据
    final = (pd.concat(resolved, ignore_index=True)
               .drop_duplicates(subset='rid').sort_values('rid').reset_index(drop=True))

    r1.to_csv("match_1.csv", index=False, encoding='utf-8-sig')
    r2.to_csv("match_2.csv", index=False, encoding='utf-8-sig')
    r3.to_csv("match_3.csv", index=False, encoding='utf-8-sig')
    r4.to_csv("match_4.csv", index=False, encoding='utf-8-sig')
    final.to_csv("matched_final.csv", index=False, encoding='utf-8-sig')

    total = con.sql("SELECT count(*) FROM eu").fetchone()[0]
    print('汇总:已唯一确定的 EU 记录')
    print('总数:', len(final), '| 筛选率: %d / %d = %.1f%%' % (len(final), total, 100.0 * len(final) / total))
    print('  来源:名字唯一', int((final['source'] == 'name_unique').sum()),
          '| 机构', int((final['source'] == 'round2_inst').sum()),
          '| 语义领域', int((final['source'] == 'round3_semantic').sum()),
          '| 语义平局兜底', int((final['source'] == 'round4_semtie').sum()),
          '| 论文数兜底', int((final['source'] == 'round4_maxpapers').sum()))
    print('  名字类型:精确', int((final['match_type'] == 'exact').sum()),
          '| 首末名模糊', int((final['match_type'] == 'fuzzy').sum()),
          '| 别名', int((final['match_type'] == 'alias').sum()))
    print("结果已保存:match_1.csv, match_2.csv, match_3.csv, match_4.csv, matched_final.csv")


if __name__ == "__main__":
    main()