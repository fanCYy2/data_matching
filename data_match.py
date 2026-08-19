# -*- coding: utf-8 -*-
import os
import duckdb
import numpy as np
import pandas as pd

from fuzzyname_vendored import names_match   # 同名者补召 homonym_reopen 的成对姓名同一性复核(vendored)
from erc_embed import score_semantic_v2, PROTO_FILE, AUTHORVEC_FILE   # 层3 v2 语义打分(C2)

con = duckdb.connect()

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

# ===== 层3 v2:语义领域打分(C2:作者论文摘要文本 vs ERC 面板原型)=====
# 默认【开】。把作者侧从"前3子学科名"换成"论文标题+摘要质心",面板侧从"面板名字符串"换成
# ERC 分类数据集(SIRIS-Lab/erc-classification-dataset)建的面板原型;ORCID 真值集上
# acc@1 73%→89%(见 eval_sem_v2.py)。实现见 erc_embed.score_semantic_v2。
#   依赖离线产物:erc_panel_prototypes.npz(build_panel_prototypes.py)、
#                author_vectors.npz(build_author_vectors.py);缺任一 -> 自动回退 v1。
#   作者无英文摘要 -> 回退用其 top-3 子学科名质心(仍对齐到面板原型,保证同 rid 内可比)。
#   v2 分数量纲与 v1 不同,阈值单独标定(见 eval_sem_v2.py);SEM_V2=0 可回退做 A/B。
SEM_V2 = os.environ.get("SEM_V2", "1") == "1"
# 标定(eval_sem_v2.py,ORCID 真值 56 个候选>=2 的 rid):v2 正确候选 mean=0.888/p10=0.851,
# 错误候选 mean=0.765/p50=0.771 → 阈值 0.80 落在两者之间(passed 即"在该面板上")。
# v2 严格 acc@1 = 51/56(91%)、零并列;v1 严格仅 35/56(62%)、16 个真人与错误候选并列分不开。
SEM_THRESH_V2 = 0.80   # v2 绝对下限:最像的候选也要 >= 此值才算"在该面板上"(否则 passed=False → 层4)
SEM_MARGIN_V2 = 0.01   # v2 同 rid 第一名对第二名最小领先;不足则判模糊,交层4(passed + 论文数)

# ===== 论文数下限闸(精度优先,全层通用)=====
# 最终选中的作者若论文数 < PAPER_FLOOR,不判为匹配成功:直接移出 matched_final、写入
# held_lowpaper.csv 待人工(不尝试"换更高产同名候选",直接扔)。空壳(shell)按定义低产,
# 这道闸直接挡掉"名字/机构对上了、但其实是个低产空壳"的假阳性。依据 ORCID 独立真值:
# <30 篇错误率 ~25% vs >=30 篇 ~1%。闸作用在收口处的最终答案上,不分哪一层给的。
PAPER_FLOOR = 30

# ===== 同名者补召(homonym_reopen)=====
# fl 召回门造成的漏配:真人以中间名/缩写另存、只有 fl 能召回却被 match_1c 的 `rid NOT IN r1_ea`
# 门掉,精确/别名位被空壳占了,真人进不了候选池。host_ids_bridge 现已【100% 覆盖】(4241/4242,
# 每行唯一机构 id)→ 直接以「在该行 host 机构发过文」这一事实作为 fl 召回的【唯一准入闸】,不再用
# 空壳阈值 / 全局论文数比 / 词集超集这些替代闸(它们只是 host 信号缺失时的近似)。判定与裁决全部
# 交回既有的 L2 机构 / L3 语义 / L4 论文数三层,homonym_reopen 只负责【把真人放进候选池】。
# 均不看 ORCID(与 verify_orcid.py 正交)。详见 homonym_reopen、[[fl-recall-mid-initial-hole]]、
# [[name-unique-alias-hole]]。

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


def _use_v2():
    """v2 可用 = 开关打开 且 两个离线产物都在;否则回退 v1。"""
    return SEM_V2 and os.path.exists(PROTO_FILE) and os.path.exists(AUTHORVEC_FILE)


def semantic_score(long_df):
    """层3 打分调度:v2(作者论文摘要 vs ERC 面板原型)优先,缺产物/关开关则回退 v1。"""
    if _use_v2():
        return score_semantic_v2(long_df)
    return _semantic_score_v1(long_df)


def _semantic_score_v1(long_df):
    """v1(旧版):每 (rid, authorid) 返回一行,sim = 其前3 level-1 领域名
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


def homonym_reopen(uniq_rids, r1):
    """同名者补召(L1→L2 交接):修补 fl 召回门造成的漏配——真人常以【中间名/缩写】形式登记
    (如 "Susana Q. Lima"),norm 保留句点 → 精确/别名/词集都配不上,只有 fl(首|末名)能召回;
    但 match_1c 的 `rid NOT IN r1_ea` 门控让 fl 只在"精确+别名全空"时才跑。于是只要有别的同名者
    (常是挂过 host 一两篇的空壳)占了精确/别名位,真人就进不了候选池。

    host_ids_bridge 现已【100% 覆盖】→ 以「在该行 host 机构发过文」这一事实作为 fl 召回的【唯一准入
    闸】:对每个【已有精确/别名候选】的 rid(name_unique 与多候选都算),把「首末名相同 + 在该 host
    发过文 + 不在现有候选」的作者(names_match 成对复核,丢掉中间名冲突的非同一人)并入候选池。
    这样真人就进了池;name_unique 行若因此多出候选 → 退出 name_unique(withdraw),后续裁决全交回
    既有的 L2 机构 / L3 语义 / L4 论文数三层。空壳阈值 / 全局论文数比 / 词集超集这些替代闸(host 缺失
    时的近似)不再需要,也不再有"直接定档"分支。均不看 ORCID,与 verify_orcid.py 正交。
    见 [[fl-recall-mid-initial-hole]]、[[name-unique-alias-hole]]。

    返回 (withdraw:set 因多出 host 确认候选而退出 name_unique 的 rid,
          inject:DataFrame[rid,eu_name,authorid,match_type='fuzzy'] 并入候选池的 fl 候选)。
    """
    cols = ['rid', 'eu_name', 'authorid', 'match_type']
    empty = r1.iloc[:0][['rid', 'eu_name', 'authorid']].assign(match_type='fuzzy')[cols]

    # 准入闸只对【已有名字命中(精确/别名)】的 rid 补 fl;纯 fl 兜底行由 match_1c 单独处理,不重复。
    ea = r1[r1['match_type'].isin(['exact', 'alias'])][['rid', 'eu_name']].drop_duplicates()
    if ea.empty:
        return set(), empty
    exist = r1[['rid', 'authorid']].drop_duplicates()   # 该 rid 已有的全部候选,注入时去重
    con.register('reopen_rids', ea)
    con.register('reopen_exist', exist)

    # 首末名同名 + 在该行 host 机构发过文 + 不在现有候选 —— host 发文事实即准入(与 match_2 同口径)
    cand = con.sql("""
        WITH rids AS (
            SELECT DISTINCT r.rid, r.eu_name, h.host_openalex_id AS host_id
            FROM reopen_rids r JOIN host h ON r.rid = h.row_id
            WHERE h.host_openalex_id IS NOT NULL AND h.host_openalex_id <> ''
        ),
        flh AS (
            SELECT DISTINCT r.rid, r.eu_name, sci.authorid, sci.display_name AS cname
            FROM rids r
            JOIN 'sciscinet_authors.parquet' sci
              ON fl(sci.display_name) = fl(r.eu_name)
            JOIN 'sciscinet_paper_author_affiliation.parquet' aff
              ON aff.authorid = sci.authorid AND aff.institutionid = r.host_id
            LEFT JOIN reopen_exist e ON e.rid = r.rid AND e.authorid = sci.authorid
            WHERE e.authorid IS NULL
        )
        SELECT rid, eu_name, authorid, cname FROM flh
    """).df()
    if cand.empty:
        return set(), empty

    # names_match 成对复核:fl 只看首|末名,可能召进中间名冲突的另一个真人,丢掉非同一人
    cand = cand[cand.apply(lambda r: names_match(r['eu_name'], r['cname']), axis=1)]
    if cand.empty:
        return set(), empty

    inject = cand[['rid', 'eu_name', 'authorid']].copy()
    inject['match_type'] = 'fuzzy'
    withdraw = set(inject['rid']) & set(uniq_rids)   # 只有 name_unique 行需"退出";多候选行本就进 L2
    return withdraw, inject[cols]


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

    # 同名者补召(L1→L2 交接):host 准入的 fl 召回,把真人放进候选池,见 homonym_reopen 文档。
    #   withdraw = 因多出 host 确认的 fl 候选而退出 name_unique 的行;inject = 注入的 fl 候选。
    withdraw, inject = homonym_reopen(uniq_rids, r1)
    uniq_rids -= withdraw
    if not inject.empty:
        r1 = pd.concat([r1, inject], ignore_index=True)        # 注入候选并入池,供后面 pend/L2 使用

    take = (r1_exact[r1_exact['rid'].isin(uniq_rids)]
            .drop_duplicates(subset='rid').copy())
    take['host_id'] = pd.NA
    take['source'] = 'name_unique'
    resolved.append(take[cols])
    pending -= uniq_rids

    print('层1→L2 交接:名字唯一 %d(host 确认 fl 重开退出 %d),剩下 %d'
          % (len(uniq_rids), len(withdraw), len(pending)))

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
    # v2/v1 分数量纲不同,阈值随之切换(passed 标志、层4 也用同一组)
    sem_thresh, sem_margin = (SEM_THRESH_V2, SEM_MARGIN_V2) if _use_v2() else (SEM_THRESH, SEM_MARGIN)
    print('层3 语义打分:%s(thresh=%.2f, margin=%.2f)'
          % ('v2 摘要×原型' if _use_v2() else 'v1 子学科名×面板名', sem_thresh, sem_margin))

    # 判定(相对为主 + 绝对下限):同一 rid 下,先取过阈值(sim >= SEM_THRESH)的候选,
    #   - 只有 1 个过阈值            -> 收(round3_semantic)
    #   - 多个过阈值、且第一名比第二名高出 SEM_MARGIN -> 收最高分的那个
    #   - 多个过阈值但差距 < margin  -> 判为模糊,不定(留人工)
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
    l4_in['passed'] = (l4_in['sim'] >= sem_thresh).fillna(False)
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

    # 论文数下限闸(全层通用,精度优先):给每个最终选中的作者补论文数(去重 paperid,没记录记 0),
    # 低于 PAPER_FLOOR 的不算匹配成功——移出 matched_final、单独写 held_lowpaper.csv 待人工。
    con.register('final_ids', final[['authorid']].drop_duplicates())
    npdf = con.sql("""
        SELECT authorid, count(DISTINCT paperid) AS n_papers
        FROM 'sciscinet_authors_paperid.parquet'
        WHERE authorid IN (SELECT authorid FROM final_ids)
        GROUP BY authorid
    """).df()
    final = final.merge(npdf, on='authorid', how='left')
    final['n_papers'] = final['n_papers'].fillna(0).astype(int)
    held = (final[final['n_papers'] < PAPER_FLOOR]
            .sort_values(['source', 'n_papers', 'rid']).reset_index(drop=True))
    final = final[final['n_papers'] >= PAPER_FLOOR].reset_index(drop=True)

    r1.to_csv("match_1.csv", index=False, encoding='utf-8-sig')
    r2.to_csv("match_2.csv", index=False, encoding='utf-8-sig')
    r3.to_csv("match_3.csv", index=False, encoding='utf-8-sig')
    r4.to_csv("match_4.csv", index=False, encoding='utf-8-sig')
    final.to_csv("matched_final.csv", index=False, encoding='utf-8-sig')
    held.to_csv("held_lowpaper.csv", index=False, encoding='utf-8-sig')

    total = con.sql("SELECT count(*) FROM eu").fetchone()[0]
    n_matched, n_held = len(final), len(held)
    print('汇总:已唯一确定的 EU 记录(论文数下限闸 = %d)' % PAPER_FLOOR)
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
    print("结果已保存:match_1.csv, match_2.csv, match_3.csv, match_4.csv, "
          "matched_final.csv, held_lowpaper.csv")


if __name__ == "__main__":
    main()