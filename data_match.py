# -*- coding: utf-8 -*-
import duckdb
import pandas as pd

con = duckdb.connect()

# EU.xlsx 注册成视图,并加一列行号 rid(从 0 开始),用来和 val_host_ids.csv 按行对齐
con.sql("""
    CREATE VIEW eu AS
    SELECT (row_number() OVER ()) - 1 AS rid, *
    FROM read_xlsx('EU.xlsx', all_varchar = true)
""")

# 名字归一化宏:去重音(ö->o、é->e)-> 小写 -> 去首尾/压缩空格。第一轮用它做匹配键。
con.sql(r"""
    CREATE MACRO norm(x) AS
    regexp_replace(trim(lower(strip_accents(x))), '\s+', ' ', 'g')
""")

# 首名+末名(姓)key:丢掉中间名/缩写,用于 match_1b 模糊补配(如 "Jonathan Lawrence Marchini" -> "jonathan|marchini")
con.sql(r"""
    CREATE MACRO fl(x) AS
    regexp_extract(norm(x), '^(\S+)', 1) || '|' || regexp_extract(norm(x), '(\S+)$', 1)
""")

# 排序词集 key:把名字按词拆开、排序、再拼回,解决姓名顺序颠倒(如 "Madanbabu Mohan" == "Mohan Madanbabu")
con.sql(r"""
    CREATE MACRO wset(x) AS
    array_to_string(list_sort(string_split(norm(x), ' ')), ' ')
""")

# 每个 EU 行的 host 机构对应的 OpenAlex 机构 id(之前已经匹配好的桥,按 row_id 对齐 rid)
con.sql("CREATE VIEW host AS SELECT * FROM 'val_host_ids.csv'")

# EU 的 Domain 抽成大类代码(PE / LS / SH),供第三轮领域消歧用
con.sql("""
    CREATE VIEW eu_dom AS
    SELECT rid, regexp_extract("Domain", '\\(([A-Z]{2})\\)', 1) AS dom
    FROM eu
""")

# OpenAlex 19 个顶层领域映射为EU 三大类
con.sql("""
    CREATE VIEW fielddomain AS
    SELECT * FROM (VALUES
        ('C86803240','LS'), ('C71924100','LS'),                        -- Biology, Medicine
        ('C185592680','PE'),('C121332964','PE'),('C41008148','PE'),    -- Chemistry, Physics, Computer science
        ('C127413603','PE'),('C192562407','PE'),('C33923547','PE'),    -- Engineering, Materials science, Mathematics
        ('C127313418','PE'),('C39432304','PE'),                        -- Geology, Environmental science
        ('C162324750','SH'),('C144133560','SH'),('C144024400','SH'),   -- Economics, Business, Sociology
        ('C17744445','SH'), ('C15744967','SH'), ('C95457728','SH'),    -- Political science, Psychology, History
        ('C138885662','SH'),('C142362112','SH'),('C205649164','SH')    -- Philosophy, Art, Geography
    ) AS t(fieldid, dom)
""")

# EU 的 Panel 抽成 ERC 面板码(PE8 / LS7 / SH6 …),比 Domain(只有 PE/LS/SH 三类)细得多,
# 供第三轮"细领域"消歧:同一大类里再区分是数学/物理/化学/…
con.sql("""
    CREATE VIEW eu_panel AS
    SELECT rid, regexp_extract("Panel", '^([A-Z]{2}[0-9]+)', 1) AS panel
    FROM eu
""")

# ERC 面板码 -> OpenAlex 19 个 level-0 领域(手工对应)。每个面板给 1~2 个最贴近的 level-0 领域,
# 略放宽以免把真人过滤掉;映射严格落在该面板对应的 PE/LS/SH 大类内,是对 fielddomain 的细化。
con.sql("""
    CREATE VIEW panelfield AS
    SELECT * FROM (VALUES
        -- PE 理工
        ('PE1','C33923547'),                                  -- Mathematics -> Mathematics
        ('PE2','C121332964'),                                 -- Fundamental Constituents of Matter -> Physics
        ('PE3','C121332964'),                                 -- Condensed Matter Physics -> Physics
        ('PE4','C185592680'),('PE4','C121332964'),            -- Physical & Analytical Chemistry -> Chemistry, Physics
        ('PE5','C185592680'),('PE5','C192562407'),            -- Synthetic Chemistry & Materials -> Chemistry, Materials
        ('PE6','C41008148'),                                  -- Computer Science & Informatics -> Computer science
        ('PE7','C127413603'),('PE7','C41008148'),             -- Systems & Communication Eng -> Engineering, CS
        ('PE8','C127413603'),('PE8','C192562407'),            -- Products & Processes Eng -> Engineering, Materials
        ('PE9','C121332964'),                                 -- Universe Sciences -> Physics (Astronomy 在 Physics 下)
        ('PE10','C127313418'),('PE10','C39432304'),           -- Earth System Science -> Geology, Environmental sci
        ('PE11','C192562407'),('PE11','C127413603'),          -- Materials Engineering -> Materials, Engineering
        -- LS 生命
        ('LS1','C86803240'),('LS1','C185592680'),             -- Molecules of Life -> Biology, Chemistry
        ('LS2','C86803240'),                                  -- Integrative Biology (genes/genomes) -> Biology
        ('LS3','C86803240'),                                  -- Cellular/Developmental Biology -> Biology
        ('LS4','C71924100'),('LS4','C86803240'),              -- Physiology in Health/Disease -> Medicine, Biology
        ('LS5','C71924100'),('LS5','C86803240'),              -- Neuroscience -> Medicine, Biology
        ('LS6','C71924100'),('LS6','C86803240'),              -- Immunity/Infection -> Medicine, Biology
        ('LS7','C71924100'),                                  -- Diagnosis & Treatment of Diseases -> Medicine
        ('LS8','C86803240'),('LS8','C39432304'),              -- Environmental Biology/Ecology -> Biology, Env sci
        ('LS9','C86803240'),('LS9','C127413603'),             -- Biotechnology & Biosystems Eng -> Biology, Engineering
        -- SH 人文社科
        ('SH1','C162324750'),('SH1','C144133560'),            -- Markets & Organisations -> Economics, Business
        ('SH2','C17744445'),                                  -- Institutions/Governance/Legal -> Political science
        ('SH3','C144024400'),('SH3','C17744445'),             -- The Social World -> Sociology, Political science
        ('SH4','C15744967'),                                  -- The Human Mind -> Psychology
        ('SH5','C142362112'),('SH5','C95457728'),             -- Cultures & Cultural Production -> Art, History
        ('SH6','C95457728'),                                  -- The Study of the Human Past -> History
        ('SH7','C205649164'),('SH7','C144024400'),            -- Human Mobility/Environment/Space -> Geography, Sociology
        ('SH8','C142362112'),('SH8','C95457728')              -- Studies of Cultures and Arts -> Art, History
    ) AS t(panel, fieldid)
""")


def match_1():
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
    # 第一轮补充:归一化精确没配上的 EU 行,丢掉中间名和缩写
    # 首名末名较宽松、fan-out 大,所以这些候选不单独可信,一律要靠第二轮机构过滤才会被接受。
    # 只处理不在 r1_exact 里的 rid, 避免弄乱已经精确配上的行。
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
          AND eu.rid NOT IN (SELECT DISTINCT rid FROM r1_exact)
    """).df()
    return result


def match_1c():
    # 精确(match_1)和首末名(match_1b)都没配上的行,用 author_details 的
    # display_name_alternatives(曾用名/别名/拼写变体)再召回。
    # 两种 key:整名别名(拼写变体)、排序词集(姓名顺序颠倒)。松散匹配,靠下游机构/领域佐证。
    result = con.sql("""
        WITH nm AS (
            -- 还没有任何名字候选的 EU 行(不在精确+模糊结果 r1_ef 里)
            SELECT rid,
                   "Researcher(s)"       AS eu_name,
                   norm("Researcher(s)") AS nn,
                   wset("Researcher(s)") AS ws
            FROM eu
            WHERE "Researcher(s)" IS NOT NULL
              AND rid NOT IN (SELECT DISTINCT rid FROM r1_ef)
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
    result = con.sql("""
        WITH cand AS (
            -- 层3全部待消歧候选,带上 EU 的 Domain 大类(粗)和 Panel 面板码(细)
            SELECT c.rid, c.eu_name, c.authorid, c.match_type, c.host_id,
                   d.dom AS eu_domain, p.panel AS eu_panel
            FROM r3in c
            JOIN eu_dom d ON c.rid = d.rid
            LEFT JOIN eu_panel p ON c.rid = p.rid
        ),
        papers AS (
            -- 只取这些候选 author 的论文
            SELECT authorid, paperid
            FROM 'sciscinet_authors_paperid.parquet'
            WHERE authorid IN (SELECT DISTINCT authorid FROM cand)
        ),
        field_cnt AS (
            -- 每个 author 在各 level-0 顶层领域的发文数
            SELECT p.authorid, pf.fieldid, count(*) AS n
            FROM papers p
            JOIN 'sciscinet_paperfields.parquet' pf ON p.paperid = pf.paperid
            WHERE pf.fieldid IN (SELECT fieldid FROM fielddomain)
            GROUP BY 1, 2
        ),
        field_share AS (
            -- 每个 author 各顶层领域的论文占比,以及他最高的那个领域的论文数
            SELECT authorid, fieldid, n,
                   n * 1.0 / sum(n) OVER (PARTITION BY authorid) AS share,
                   max(n)          OVER (PARTITION BY authorid) AS max_n
            FROM field_cnt
        ),
        main_fields AS (
            -- author 的主 level-0 领域(占比 >= 15% 或就是最高的那个),细领域消歧用这个
            SELECT DISTINCT authorid, fieldid
            FROM field_share
            WHERE share >= 0.15 OR n = max_n
        ),
        main_doms AS (
            -- 把主 level-0 领域再归到 PE/LS/SH 大类并去重(粗领域消歧,保持原行为)
            SELECT DISTINCT mf.authorid, fd.dom
            FROM main_fields mf
            JOIN fielddomain fd ON mf.fieldid = fd.fieldid
        ),
        author_domset AS (
            -- 每个 author 的主大类集合(拼成字符串便于核对)
            SELECT authorid, string_agg(dom, ',' ORDER BY dom) AS author_domains
            FROM main_doms GROUP BY authorid
        ),
        author_fieldset AS (
            -- 每个 author 的主 level-0 领域名集合(便于核对细领域)
            SELECT mf.authorid, string_agg(f.display_name, ',' ORDER BY f.display_name) AS author_fields
            FROM main_fields mf
            JOIN 'sciscinet_fields.parquet' f ON mf.fieldid = f.fieldid
            GROUP BY mf.authorid
        )
        -- 先按粗大类佐证(EU 的 domain 落在 author 主大类集合里),再给一个"细领域是否也对上"的标记:
        -- panel_ok = author 的主 level-0 领域 与 该 EU 面板映射的 level-0 领域 有交集
        SELECT c.rid, c.eu_name, c.authorid, c.match_type, c.host_id,
               c.eu_domain, c.eu_panel, ds.author_domains, fs.author_fields,
               EXISTS (
                   SELECT 1 FROM main_fields mf
                   JOIN panelfield pf2 ON mf.fieldid = pf2.fieldid
                   WHERE mf.authorid = c.authorid AND pf2.panel = c.eu_panel
               ) AS panel_ok
        FROM cand c
        JOIN main_doms a           ON c.authorid = a.authorid AND a.dom = c.eu_domain
        JOIN author_domset ds      ON c.authorid = ds.authorid
        LEFT JOIN author_fieldset fs ON c.authorid = fs.authorid
    """).df()
    return result


def main():
    cols = ['rid', 'eu_name', 'authorid', 'match_type', 'host_id', 'source']
    resolved = []   # 每层剔除定下来的行(带 source)

    # 第一轮:名字匹配
    # 归一化精确，首末名 别名,
    r1_exact = match_1()
    r1_exact['match_type'] = 'exact'
    con.register('r1_exact', r1_exact)                 # 供 match_1b 排除已精确配上的 rid

    r1_fuzzy = match_1b()
    r1_fuzzy['match_type'] = 'fuzzy'
    r1_ef = pd.concat([r1_exact, r1_fuzzy], ignore_index=True)
    con.register('r1_ef', r1_ef)                       # 供 match_1c 排除已精确/模糊配上的 rid

    r1_alias = match_1c()
    r1_alias['match_type'] = 'alias'

    r1 = pd.concat([r1_exact, r1_fuzzy, r1_alias], ignore_index=True)
    pending = set(r1['rid'])                            # 还没唯一确定的 EU 行

    # print('候选行数: 精确', len(r1_exact), '| 模糊', len(r1_fuzzy), '| 别名', len(r1_alias))
    # print('有候选的 EU 记录:', len(pending))
    # print()

    
    exact_counts = r1_exact.groupby('rid').size()
    uniq_rids = set(exact_counts[exact_counts == 1].index)
    take = r1_exact[r1_exact['rid'].isin(uniq_rids)].copy()
    take['host_id'] = pd.NA
    take['source'] = 'name_unique'
    resolved.append(take[cols])
    pending -= uniq_rids
    print('层1 名字唯一 → 确定 %d,剩下 %d' % (len(uniq_rids), len(pending)))

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

    r3 = match_3().sort_values(['rid', 'authorid']).reset_index(drop=True)

    # 两段式消歧:先用粗大类(PE/LS/SH),粗大类还剩多个同名的,再用细领域(ERC 面板 -> level-0 领域)打破。
    #   - 粗后唯一          -> round3_field(和之前一样)
    #   - 粗后仍多个、细后唯一 -> round3_panel(靠细领域打破平局,置信度略低,单独标记便于核对)
    picks = []
    for rid, g in r3.groupby('rid'):
        if g['authorid'].nunique() == 1:               # 粗大类后就只剩 1 个
            picks.append(g.iloc[[0]].assign(source='round3_field'))
        else:
            gf = g[g['panel_ok']]                      # 细领域也对上的子集
            if gf['authorid'].nunique() == 1:          # 细领域把平局打破到唯一
                picks.append(gf.iloc[[0]].assign(source='round3_panel'))
    r3res = pd.concat(picks, ignore_index=True) if picks else r3.iloc[:0].assign(source=pd.NA)
    take3 = set(r3res['rid'])
    take3_field = int((r3res['source'] == 'round3_field').sum())
    take3_panel = int((r3res['source'] == 'round3_panel').sum())
    resolved.append(r3res[cols])
    pending -= take3
    print('层3 领域 → 确定 %d(粗领域 %d + 细领域打破平局 %d),剩下 %d'
          % (len(take3), take3_field, take3_panel, len(pending)))
    print()

    # 最后整理数据
    final = (pd.concat(resolved, ignore_index=True)
               .drop_duplicates(subset='rid').sort_values('rid').reset_index(drop=True))

    r1.to_csv("match_1.csv", index=False, encoding='utf-8-sig')
    r2.to_csv("match_2.csv", index=False, encoding='utf-8-sig')
    r3.to_csv("match_3.csv", index=False, encoding='utf-8-sig')
    final.to_csv("matched_final.csv", index=False, encoding='utf-8-sig')

    total = con.sql("SELECT count(*) FROM eu").fetchone()[0]
    print('汇总:已唯一确定的 EU 记录')
    print('总数:', len(final), '| 筛选率: %d / %d = %.1f%%' % (len(final), total, 100.0 * len(final) / total))
    print('  来源:名字唯一', int((final['source'] == 'name_unique').sum()),
          '| 机构', int((final['source'] == 'round2_inst').sum()),
          '| 粗领域', int((final['source'] == 'round3_field').sum()),
          '| 细领域', int((final['source'] == 'round3_panel').sum()))
    print('  名字类型:精确', int((final['match_type'] == 'exact').sum()),
          '| 首末名模糊', int((final['match_type'] == 'fuzzy').sum()),
          '| 别名', int((final['match_type'] == 'alias').sum()))
    print("结果已保存:match_1.csv, match_2.csv, match_3.csv, matched_final.csv")


if __name__ == "__main__":
    main()