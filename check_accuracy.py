# -*- coding: utf-8 -*-
# 验证 matched_final.csv 的可信度(没有标准答案,用独立信号交叉验证 + 抽样)
import duckdb

con = duckdb.connect()

con.sql("CREATE VIEW eu AS SELECT (row_number() OVER ())-1 AS rid, * FROM read_xlsx('EU.xlsx', all_varchar=true)")
con.sql("CREATE VIEW final AS SELECT * FROM 'matched_final.csv'")

# EU Domain 大类
con.sql("""CREATE VIEW eu_dom AS
    SELECT rid, regexp_extract("Domain", '\\(([A-Z]{2})\\)', 1) AS eu_domain FROM eu""")

# 顶层领域 -> PE/LS/SH
con.sql("""CREATE VIEW fielddomain AS
    SELECT * FROM (VALUES
        ('C86803240','LS'),('C71924100','LS'),
        ('C185592680','PE'),('C121332964','PE'),('C41008148','PE'),
        ('C127413603','PE'),('C192562407','PE'),('C33923547','PE'),
        ('C127313418','PE'),('C39432304','PE'),
        ('C162324750','SH'),('C144133560','SH'),('C144024400','SH'),
        ('C17744445','SH'),('C15744967','SH'),('C95457728','SH'),
        ('C138885662','SH'),('C142362112','SH'),('C205649164','SH')
    ) AS t(fieldid, dom)""")

# --- 信号1:每个匹配作者的发文主领域(过巨表,只查这 1814 个 authorid) ---
con.sql("""CREATE VIEW author_dom AS
    WITH papers AS (
        SELECT authorid, paperid FROM 'sciscinet_authors_paperid.parquet'
        WHERE authorid IN (SELECT DISTINCT authorid FROM final)
    ),
    fc AS (
        SELECT p.authorid, pf.fieldid, count(*) n
        FROM papers p JOIN 'sciscinet_paperfields.parquet' pf ON p.paperid = pf.paperid
        WHERE pf.fieldid IN (SELECT fieldid FROM fielddomain)
        GROUP BY 1,2
    )
    SELECT c.authorid, fd.dom AS author_domain
    FROM (SELECT authorid, arg_max(fieldid, n) top_field FROM fc GROUP BY authorid) c
    JOIN fielddomain fd ON c.top_field = fd.fieldid""")

# --- 汇总表:每条匹配 + 各种证据 ---
ev = con.sql("""
    SELECT f.rid, f.eu_name, f.authorid, f.source,
           d.eu_domain, a.author_domain,
           sci.h_index, sci.productivity,
           det.orcid, det.works_count, det.cited_by_count
    FROM final f
    LEFT JOIN eu_dom d       ON f.rid = d.rid
    LEFT JOIN author_dom a   ON f.authorid = a.authorid
    LEFT JOIN 'sciscinet_authors.parquet' sci ON f.authorid = sci.authorid
    LEFT JOIN 'ai''s_output/val_matched_details.csv' det ON f.authorid = det.authorid
""").df()
ev.to_csv("accuracy_evidence.csv", index=False, encoding='utf-8-sig')

print('总匹配数:', len(ev))
print()

# 信号1:领域一致性(只看第二轮机构选出的,领域是独立信号)
r2 = ev[ev['source'] == 'round2_inst']
both = r2.dropna(subset=['eu_domain', 'author_domain'])
both = both[both['eu_domain'] != '']
agree = (both['eu_domain'] == both['author_domain']).sum()
print('=== 信号1:领域交叉验证(仅 round2_inst,共', len(r2), '条)===')
print('  能算出作者领域的:', len(both))
print('  领域与 EU 一致:', agree, f'({agree/len(both)*100:.1f}%)')
print('  领域不一致(可疑):', len(both) - agree)
print()

# 信号2:h-index / 发文量分布
print('=== 信号2:匹配作者的 h_index 分布 ===')
print(ev['h_index'].describe().to_string())
print('  h_index = 0 的(很可疑):', int((ev['h_index'] == 0).sum()))
print('  h_index < 5 的:', int((ev['h_index'] < 5).sum()))
print()

# 信号3:ORCID 覆盖
print('=== 信号3:有 ORCID 的匹配作者 ===')
print('  有 orcid:', int(ev['orcid'].notna().sum()), f"/ {len(ev)}")
print()

# 抽样:领域不一致的可疑样本 + 随机样本
print('=== 可疑样本:领域不一致的 15 条 ===')
susp = both[both['eu_domain'] != both['author_domain']]
print(susp[['rid','eu_name','eu_domain','author_domain','h_index','works_count']].head(15).to_string())
print()
print('=== 随机抽 15 条看证据 ===')
print(ev.sample(min(15, len(ev)), random_state=1)[
    ['rid','eu_name','eu_domain','author_domain','h_index','works_count','orcid']].to_string())
