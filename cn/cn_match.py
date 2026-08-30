# -*- coding: utf-8 -*-
"""中国学者名单(cn.xlsx)→ OpenAlex authorid 匹配 —— 两层漏斗:姓名 + 机构。

设计口径(用户 2026-08-30 拍板,简化自早期四层版):
  · 层1 名字:中文名经 chinese_names.romanize() 展开成数据集里实际出现的各种【罗马化拼音
    面貌】+ 中文原名,再用 norm()/wset() 宏去 join。【只做 exact 与 alias,不做 fuzzy】——
    首末名模糊兜底(fl)过松,在几百上千的同名候选池里只会徒增误配。
      · exact:norm(display_name) = norm(variant),覆盖 中文↔拼音 与 中文↔中文。
      · alias:variant 命中 display_name_alternatives(整名 norm 或词集 wset)。
  · 层2 机构:候选 authorid 必须在该行 host 机构(cn_host_ids_bridge.csv)发过文。
    机构过滤后候选【唯一】→ 定档(round2_inst)。
  · 【领域层已彻底移除】。多轮实证(field-tag / title-content / v2 摘要聚类,见 HANDOFF.md /
    METHODS_TRIED.md)一致表明:同机构同名者内部,领域/内容信号判别度≈0,只起副作用。
  · 【论文数兜底已移除】。名字+机构无法唯一确定的行(机构内仍多个同名 / 无 host 桥 / 候选都
    不在该机构)一律【留空不匹配】—— 目标是 auto 子集精度,宁可牺牲覆盖也不做低置信瞎猜。

产物:matched_final_cn.csv(只含 name_unique + round2_inst 两类确定匹配)、
     match_1_cn.csv(层1 候选池,供 eval_cn.py 算候选召回)。

真值:cn.xlsx 第 6 列(无表头,read_xlsx 会漏)含 1949 个 authorid(1997–2009),仅作评测
(eval_cn.py),不参与匹配。
"""

import os
import time
import duckdb
import pandas as pd

_T0 = time.time()
def _el():
    return "[%5.0fs]" % (time.time() - _T0)

from chinese_names import romanize, chinese_form

con = duckdb.connect()
con.sql("SET temp_directory='.duckdb_tmp'")
con.sql("SET memory_limit='12GB'")
con.sql("SET preserve_insertion_order=false")

HETERONYM = os.environ.get("CN_HETERONYM", "1") == "1"      # 名的多音字是否展开(默认开,提召回)
USE_ALIAS = os.environ.get("CN_ALIAS", "1") == "1"          # 层1 是否跑别名(慢:100M unnest,迭代时可关)
# 预归一化名字索引(build_name_index_cn.py 产出)。存在即走索引路径:把每轮 exact 全表 norm-join
# (~247s)+ alias unnest+归一化(~371s)降到秒级。CN_INDEX=0 可强制回退现扫全表。
USE_INDEX = os.environ.get("CN_INDEX", "1") == "1" and os.path.exists("name_index_cn.parquet")

# 机构关键词:cn.xlsx 的「依托单位/研究领域」两列在不同年份【会整列对调】,按关键词判哪列是机构。
INST_KW = ["大学", "学院", "研究所", "研究院", "中心", "医院", "实验室", "学校", "研究中心",
           "科学院", "大學", "學院", "研究員", "所", "局", "系", "站", "厂", "公司", "集团"]


# ===== 名字归一化宏(CN 版:在 EU 口径上【额外去句点】)=====
# CN 侧变体是干净生成的,不存在 EU 那种「Cynthia. Sharma 顶掉真人」的风险;而数据集把缩写写成
# "J. Meng" / "Yongchuan. Chen",去句点后才能和变体 "j meng" / "yongchuan chen" 对上。
con.sql(r"""
    CREATE MACRO norm(x) AS
    regexp_replace(
        trim(lower(strip_accents(
            regexp_replace(x, '[\x{002D}\x{00AD}\x{2010}-\x{2015}\x{2212}.]', ' ', 'g')
        ))),
        '\s+', ' ', 'g')
""")
con.sql(r"""
    CREATE MACRO wset(x) AS
    array_to_string(list_sort(string_split(norm(x), ' ')), ' ')
""")

# 每个 CN 行 host 机构 → OpenAlex 机构 id 的桥(cn_host_ids_bridge.csv,row_id 对齐 rid)。
# 结构与 EU 版 host_ids_bridge.csv 完全一致;由用户侧解析程序产出(exact/weak/override),
# 已正确处理「依托单位/研究领域」按年份对调的情况。覆盖 4301/4603(有真值行 1902/1949=97.6%)。
con.sql("CREATE VIEW host AS SELECT row_id, host_openalex_id FROM 'cn_host_ids_bridge.csv'")


# ---------------------------------------------------------------------------
# 读入 cn.xlsx 并生成罗马化变体表
# ---------------------------------------------------------------------------
def _looks_inst(s):
    return isinstance(s, str) and any(k in s for k in INST_KW)


def load_cn():
    """读 cn.xlsx(含无表头的第 6 列 authorid),返回:
       cn_df: rid, cn_name, field_text, inst_text, year, gold_authorid
       var_df: rid, variant, kind('roman'/'initial'/'chinese')  —— 罗马化变体长表
    rid = 行序(0 起,与 序号-1 对齐)。依托单位/研究领域按关键词判并纠正对调。
    field_text 仅作输出/评测的元信息保留,不再参与任何匹配(领域层已移除)。"""
    import openpyxl
    wb = openpyxl.load_workbook("cn.xlsx", read_only=True, data_only=True)
    ws = wb["Sheet1"]
    recs = []
    n_dropped = 0
    for i, r in enumerate(ws.iter_rows(values_only=True)):
        if i == 0:
            continue
        seq, name, c2, c3, year, aid = (list(r) + [None] * 6)[:6]
        if name is None:
            continue
        # 纠正 依托单位/研究领域 对调:机构关键词命中的那列当机构,另一列当领域
        if _looks_inst(c3) and not _looks_inst(c2):
            inst, field = c3, c2
        else:
            inst, field = c2, c3
        inst = inst.strip() if isinstance(inst, str) and inst.strip() else None
        field = field.strip() if isinstance(field, str) and field.strip() else None
        # 无机构且无领域(1994–1996 早期 212 行,均无真值):既无机构锚点又无任何元信息,
        # 名字唯一以外无从核验 → 直接丢弃,不参与匹配/评测/输出。rid 仍按原行号(i-1),
        # 故 host 桥对齐与其余行的 rid 均不受影响。
        if inst is None and field is None:
            n_dropped += 1
            continue
        recs.append((i - 1, str(name).strip(), field, inst,
                     year, (str(aid).strip() if aid else None)))
    wb.close()
    if n_dropped:
        print("load_cn:丢弃无机构无领域行 %d(1994–1996 早期,无消歧锚点)" % n_dropped, flush=True)
    cn_df = pd.DataFrame(recs, columns=["rid", "cn_name", "field_text",
                                        "inst_text", "year", "gold_authorid"])

    # 罗马化变体长表:每个中文名 → romanize() 变体 ∪ 中文原名
    rows = []
    for rid, name in zip(cn_df["rid"], cn_df["cn_name"]):
        for v in romanize(name, heteronym=HETERONYM):
            kind = "initial" if any(len(tok) == 1 for tok in v.split()) else "roman"
            rows.append((rid, v, kind))
        cf = chinese_form(name)
        if cf:
            rows.append((rid, cf, "chinese"))
    var_df = pd.DataFrame(rows, columns=["rid", "variant", "kind"]).drop_duplicates()
    return cn_df, var_df


# ---------------------------------------------------------------------------
# 层1:姓名匹配(只 exact / alias,不做 fuzzy),对象换成 cn_variants
# ---------------------------------------------------------------------------
def match_1a():
    """精确:norm(display_name) = norm(variant)(nn 相等)。variant 含罗马化各面貌 + 中文原名,
    故【中文↔拼音】和【中文↔中文】都由这一 join 覆盖。cn_keys 已把变体侧 norm/wset 预算好。
    有 name_index_cn.parquet 时走预归一化索引(kind='e'),否则现扫 authors 全表逐行 norm()。"""
    if USE_INDEX:
        return con.sql("""
            SELECT DISTINCT k.rid, k.eu_name, idx.authorid
            FROM cn_keys k
            JOIN 'name_index_cn.parquet' idx ON idx.kind = 'e' AND idx.nn = k.nn
        """).df()
    return con.sql("""
        SELECT DISTINCT k.rid, k.eu_name, sci.authorid
        FROM cn_keys k
        JOIN '../sciscinet_authors.parquet' sci ON norm(sci.display_name) = k.nn
    """).df()


def match_1b():
    """别名:variant 命中 display_name_alternatives(整名 norm 或词集 wset)。对所有 rid 生效。
    【关键改写】末尾原来是 `norm(variant)=nalias OR wset(variant)=wsalias` 的 OR-join —— DuckDB
    无法对 `a=b OR c=d` 建哈希连接,退化成块嵌套循环(56k 变体 × ~186万别名存活行),实测比等值
    连接慢 125×,是此前进程卡死 60 分钟的元凶。现拆成【两个等值 join 的 UNION】,秒级完成。
    有索引走 kind='a';否则现扫 author_details(unnest+归一化)。注意 WHERE 里的 `IN(..) OR IN(..)`
    是【过滤半连接】(对每行算一次、线性),不是那个病态 join,保留无妨。"""
    if USE_INDEX:
        return con.sql("""
            SELECT DISTINCT rid, eu_name, authorid FROM (
                SELECT k.rid, k.eu_name, idx.authorid
                  FROM cn_keys k JOIN 'name_index_cn.parquet' idx
                    ON idx.kind = 'a' AND idx.nn = k.nn
                UNION
                SELECT k.rid, k.eu_name, idx.authorid
                  FROM cn_keys k JOIN 'name_index_cn.parquet' idx
                    ON idx.kind = 'a' AND idx.ws = k.ws
            )
        """).df()
    return con.sql("""
        WITH alias AS (
            SELECT authorid, norm(alt) AS nalias, wset(alt) AS wsalias
            FROM (
                SELECT authorid,
                       unnest(from_json(display_name_alternatives, '["VARCHAR"]')) AS alt
                FROM '../sciscinet_author_details.parquet'
            )
            WHERE norm(alt) IN (SELECT nn FROM cn_keys)
               OR wset(alt) IN (SELECT ws FROM cn_keys)
        )
        SELECT DISTINCT rid, eu_name, authorid FROM (
            SELECT k.rid, k.eu_name, a.authorid
              FROM cn_keys k JOIN alias a ON k.nn = a.nalias
            UNION
            SELECT k.rid, k.eu_name, a.authorid
              FROM cn_keys k JOIN alias a ON k.ws = a.wsalias
        )
    """).df()


# ---------------------------------------------------------------------------
# 层2:机构消歧 —— 候选 authorid 必须在该行 host 机构发过文(与 EU 版 match_2 同口径)
# ---------------------------------------------------------------------------
def match_2():
    return con.sql("""
        WITH cand AS (
            SELECT pend.rid, pend.eu_name, pend.authorid, pend.match_type,
                   h.host_openalex_id AS host_id
            FROM pend JOIN host h ON pend.rid = h.row_id
            WHERE h.host_openalex_id IS NOT NULL AND h.host_openalex_id <> ''
        ),
        aff AS (
            SELECT DISTINCT authorid, institutionid
            FROM '../sciscinet_paper_author_affiliation.parquet'
            WHERE authorid      IN (SELECT authorid FROM cand)
              AND institutionid IN (SELECT host_id  FROM cand)
        )
        SELECT c.rid, c.eu_name, c.authorid, c.match_type, c.host_id
        FROM cand c
        JOIN aff a ON c.authorid = a.authorid AND c.host_id = a.institutionid
    """).df()


def main():
    cols = ['rid', 'eu_name', 'authorid', 'match_type', 'host_id', 'source']
    resolved = []

    cn_df, var_df = load_cn()
    con.register('cn', cn_df[['rid', 'cn_name']])
    con.register('cn_variants', var_df)
    # 变体侧键预归一化一次:exact/alias 两层都复用,且让末尾 UNION 走纯等值连接。
    con.sql("""CREATE OR REPLACE TEMP TABLE cn_keys AS
               SELECT DISTINCT v.rid, c.cn_name AS eu_name,
                      norm(v.variant) AS nn, wset(v.variant) AS ws
               FROM cn_variants v JOIN cn c ON c.rid = v.rid""")
    total = len(cn_df)
    print('CN 名单:%d 行 | 罗马化变体 %d 条(heteronym=%s)| 有真值 %d | 索引=%s'
          % (total, len(var_df), HETERONYM, cn_df['gold_authorid'].notna().sum(),
             '开(name_index_cn.parquet)' if USE_INDEX else '关(现扫全表)'), flush=True)

    # 层1:姓名(exact + alias,不做 fuzzy)
    r1_exact = match_1a(); r1_exact['match_type'] = 'exact'
    print('%s 层1a 精确完成:%d 候选行' % (_el(), len(r1_exact)), flush=True)
    r1_alias = match_1b() if USE_ALIAS else r1_exact.iloc[:0].copy()
    r1_alias['match_type'] = 'alias'
    print('%s 层1b 别名完成:%d 候选行(alias=%s)' % (_el(), len(r1_alias), USE_ALIAS), flush=True)

    r1 = pd.concat([r1_exact, r1_alias], ignore_index=True)   # 层1 候选池 = exact ∪ alias
    pending = set(r1['rid'])
    print('层1 候选:精确 %d | 别名 %d | 有候选的行 %d'
          % (r1_exact['rid'].nunique(), r1_alias['rid'].nunique(), len(pending)), flush=True)

    # name_unique:精确∪别名 合起来仍唯一 authorid,且该 rid 有精确命中 → 直接定档
    ea_ids = r1[['rid', 'authorid']].drop_duplicates()
    ea_nunique = ea_ids.groupby('rid')['authorid'].nunique()
    uniq_rids = set(ea_nunique[ea_nunique == 1].index) & set(r1_exact['rid'])
    take = (r1_exact[r1_exact['rid'].isin(uniq_rids)]
            .drop_duplicates(subset='rid').copy())
    take['host_id'] = pd.NA
    take['source'] = 'name_unique'
    resolved.append(take[cols])
    pending -= uniq_rids
    print('层1→ 名字唯一 %d,剩下 %d' % (len(uniq_rids), len(pending)), flush=True)

    # 层2:机构消歧(host 桥已就位)。候选须在该行 host 机构发过文;过滤后唯一→定档,
    # 其余(机构内仍多个同名 / 无 host / 候选都不在该机构)→ 留空不匹配。
    pend = r1[r1['rid'].isin(pending)]
    con.register('pend', pend)
    r2 = match_2()
    print('%s 层2 机构过滤完成:%d 候选行' % (_el(), len(r2)), flush=True)
    r2cnt = r2.groupby('rid')['authorid'].nunique()
    take2 = set(r2cnt[r2cnt == 1].index)               # 机构后仅剩 1 人 → 定
    tie_rids = set(r2cnt[r2cnt > 1].index)             # 机构后仍多人 → 留空
    t = r2[r2['rid'].isin(take2)].copy()
    t['source'] = 'round2_inst'
    resolved.append(t[cols])
    pending -= take2
    noinst_rids = pending - tie_rids                   # 无 host / 候选都没在该机构
    print('层2 机构 → 确定 %d,机构内平局 %d,无机构信号 %d,均留空不匹配;剩余未匹配 %d'
          % (len(take2), len(tie_rids), len(noinst_rids), len(pending)), flush=True)

    # 收口:只输出 name_unique + round2_inst 两类确定匹配;其余行不给预测(留空)。
    final = (pd.concat(resolved, ignore_index=True)
               .drop_duplicates(subset='rid').sort_values('rid').reset_index(drop=True))
    # 补论文数(纯信息列,便于人工核对;不再作任何过滤/兜底)
    con.register('final_ids', final[['authorid']].drop_duplicates())
    npdf = con.sql("""
        SELECT authorid, count(DISTINCT paperid) AS n_papers
        FROM '../sciscinet_authors_paperid.parquet'
        WHERE authorid IN (SELECT authorid FROM final_ids)
        GROUP BY authorid
    """).df()
    final = final.merge(npdf, on='authorid', how='left')
    final['n_papers'] = final['n_papers'].fillna(0).astype(int)
    # 附回中文名 / 领域 / 机构 / 年份 / 真值,便于人工核对与评测
    final = final.merge(cn_df[['rid', 'cn_name', 'field_text', 'inst_text',
                               'year', 'gold_authorid']], on='rid', how='left')

    r1.to_csv("match_1_cn.csv", index=False, encoding='utf-8-sig')
    final.to_csv("matched_final_cn.csv", index=False, encoding='utf-8-sig')

    n_final = len(final)
    print()
    print('汇总:匹配成功 %d / %d = %.1f%%(留空未匹配 %d)'
          % (n_final, total, 100.0 * n_final / total, total - n_final))
    print('  来源:名字唯一 %d | 机构唯一 %d'
          % (int((final['source'] == 'name_unique').sum()),
             int((final['source'] == 'round2_inst').sum())))
    print('结果:matched_final_cn.csv, match_1_cn.csv')


if __name__ == "__main__":
    main()
