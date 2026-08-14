# -*- coding: utf-8 -*-
"""
make_test_samples.py —— 从 matched_final.csv 抽取人工验证测试集。

对指定来源层(source)各随机抽 N 行,补上可读的 host 机构名和一个空的 yes_or_no 列
(供人工标注该匹配对不对),写成 round{2,3}_sample_50.csv / round4_sample_100.csv。
round4 兜底层额外并入 n_papers(选中候选的论文数),便于核对兜底决策。

host 机构名规则(和 matched_final 的 host_id 单一对齐,可复现):
  - host_id 有值 -> openalex_institutions_eu.parquet 里该 id 的 canonical_name(去重取一);
  - host_id 为空 -> '* ' + EU.xlsx 原始 "Host Institution(s)" 文本。
    '* ' 前缀是刻意的标记:该行没有机构过滤(机构缺失),匹配只靠名字+语义,复核时更审慎。

随机种子固定(SEED),保证每次抽到同一批行、结果可复现;想换一批改 SEED 即可。
"""
import duckdb
import pandas as pd

SEED = 42
SAMPLES = [                          # (输出文件, matched_final 里的 source 列表, 抽样行数)
    ('round2_sample_50.csv',  ['round2_inst'],                        50),
    ('round3_sample_50.csv',  ['round3_semantic'],                    50),
    ('round4_sample_100.csv', ['round4_semtie', 'round4_maxpapers'], 100),
]
COLS = ['rid', 'eu_name', 'authorid', 'match_type', 'host_id', 'source', 'host', 'yes_or_no']

con = duckdb.connect()
con.sql("CREATE VIEW eu AS SELECT (row_number() OVER ())-1 AS rid, * "
        "FROM read_xlsx('EU.xlsx', all_varchar=true)")

# host_id -> OpenAlex 机构规范名(一个 id 可能有多条名变体,去重后取字典序第一个,确定)
inst = con.sql("""
    SELECT openalex_id AS host_id, min(canonical_name) AS inst_name
    FROM 'openalex_institutions_eu.parquet'
    WHERE openalex_id IS NOT NULL
    GROUP BY openalex_id
""").df()

# EU 原始 host 文本(host_id 缺失时回退用)
eu_host = con.sql('SELECT rid, "Host Institution(s)" AS eu_host FROM eu').df()

final = pd.read_csv('matched_final.csv', encoding='utf-8-sig')
final['host_id'] = final['host_id'].astype('object')  # 空单元 -> NaN,保证按 str 关联


def add_host(df):
    df = df.merge(inst, on='host_id', how='left').merge(eu_host, on='rid', how='left')
    star = '* ' + df['eu_host'].fillna('')             # 未解析机构的回退文本(带 * 标记)
    df['host'] = df['inst_name'].where(df['host_id'].notna(), other=star)
    df['host'] = df['host'].fillna(star)               # host_id 有值但机构表查不到名的极少数
    return df


# 层4 各候选的论文数(round4 抽样并入,便于核对兜底决策)
m4 = pd.read_csv('match_4.csv', encoding='utf-8-sig')[['rid', 'n_papers']]

for fname, sources, n in SAMPLES:
    sub = final[final['source'].isin(sources)]
    samp = (add_host(sub)
            .sample(n=min(n, len(sub)), random_state=SEED)
            .sort_values('rid'))
    cols = COLS
    if any(s.startswith('round4') for s in sources):   # round4 额外带 n_papers
        samp = samp.merge(m4, on='rid', how='left')
        cols = COLS[:6] + ['n_papers'] + COLS[6:]
    samp['yes_or_no'] = ''                              # 空列,人工填 y/n
    samp[cols].to_csv(fname, index=False, encoding='utf-8-sig')
    print('%s: 从 %d 条 [%s] 抽 %d 行' % (fname, len(sub), '/'.join(sources), len(samp)))
