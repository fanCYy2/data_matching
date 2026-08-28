# -*- coding: utf-8 -*-
"""中国学者名单(cn.xlsx)→ OpenAlex authorid 匹配 —— 复用 data_match.py 的四层漏斗,
但把【姓名匹配层(层1)】特殊化为中文处理:中文名经 chinese_names.romanize() 展开成数据集里
实际出现的各种【罗马化拼音面貌】+ 中文原名,再用既有的 norm()/fl()/wset() 宏去 join。

与 EU 版(data_match.py)的差异,均按用户口径:
  · 层1 名字:EU 直接拿 Latin 名比;CN 先中文→拼音变体,覆盖 中文↔拼音 与 中文↔中文。
  · 层2 机构:CN 依托单位是中文,尚未建「中文机构→OpenAlex id」桥 —— 本轮【暂缓】,所有行
    直通层3(host_id 置空)。代码路径保留,后续接桥即可。
  · 层3 领域:【只用 v1】(子学科名 × 领域文本 的 bge cosine),不加载 v2(ERC 原型是英文摘要
    面板,与中文领域文本不对口)。层3 语义模型改用【多语种 BAAI/bge-m3】:候选作者子学科名是
    英文(来自 sciscinet_fields,如 Chromatography),CN 领域文本是中文(如「色谱分析」),
    靠 bge-m3 的跨语种能力对齐(替代早期只支持英文的 bge-small-en)。bge-m3 稠密向量同为 CLS
    池化 + L2 归一,故打分代码不变;仅 SEM_THRESH/SEM_MARGIN 因 cosine 尺度变化需重标。
  · data_match.py(EU)完全不改;本文件自带所需的宏/嵌入器/v1 打分(少量重复,换 EU 不被动)。

真值:cn.xlsx 第 6 列(无表头,read_xlsx 会漏)含 1949 个 authorid(1997–2009),仅作评测
(eval_cn.py),不参与匹配。
"""

import os
import re
import time
import duckdb
import numpy as np
import pandas as pd

_T0 = time.time()
def _el():
    return "[%5.0fs]" % (time.time() - _T0)

from chinese_names import romanize, chinese_form
from fuzzyname_vendored import names_match

con = duckdb.connect()
con.sql("SET temp_directory='.duckdb_tmp'")
con.sql("SET memory_limit='12GB'")
con.sql("SET preserve_insertion_order=false")

EMB_MODEL = "BAAI/bge-m3"   # 多语种:英文子学科名 ↔ 中文领域文本 跨语种 cosine(替代英文 bge-small-en)
SEM_THRESH = 0.62       # v1 绝对下限(注意:换 bge-m3 后 cosine 尺度变化,需用 eval_cn.py 重标)
SEM_MARGIN = 0.03       # v1 同 rid 第一名对第二名最小领先
PAPER_FLOOR = int(os.environ.get("CN_PAPER_FLOOR", "0"))   # CN 默认不设论文数下限闸(评测看全量)
HETERONYM = os.environ.get("CN_HETERONYM", "1") == "1"      # 名的多音字是否展开(默认开,提召回)
USE_FUZZY = os.environ.get("CN_FUZZY", "1") == "1"          # 层1 是否跑首末名模糊兜底
USE_ALIAS = os.environ.get("CN_ALIAS", "1") == "1"          # 层1 是否跑别名(慢:100M unnest,迭代时可关)

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
    CREATE MACRO fl(x) AS
    regexp_extract(norm(x), '^(\S+)', 1) || '|' || regexp_extract(norm(x), '(\S+)$', 1)
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
    rid = 行序(0 起,与 序号-1 对齐)。依托单位/研究领域按关键词判并纠正对调。"""
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
        # 无机构且无领域(1994–1996 早期 212 行,均无真值):层2/层3 无任何消歧锚点,
        # 只能滑到"论文数最高"瞎猜 → 直接丢弃,不参与匹配/评测/输出。rid 仍按原行号
        # (i-1),故 host 桥对齐与其余行的 rid 均不受影响。
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
# 层1:姓名匹配(exact / alias / fuzzy),对象换成 cn_variants
# ---------------------------------------------------------------------------
def match_1a():
    """精确:norm(display_name) = norm(variant)。variant 含罗马化各面貌 + 中文原名,
    故【中文↔拼音】和【中文↔中文】都由这一 join 覆盖。"""
    return con.sql("""
        SELECT DISTINCT
            v.rid, c.cn_name AS eu_name, sci.authorid, sci.display_name AS sci_name
        FROM cn_variants v
        JOIN cn c            ON c.rid = v.rid
        JOIN '../sciscinet_authors.parquet' sci
          ON norm(sci.display_name) = norm(v.variant)
    """).df()


def match_1b():
    """别名:variant 命中 display_name_alternatives(整名 norm 或 词集 wset)。对所有 rid 生效。"""
    return con.sql("""
        WITH keys AS (
            SELECT DISTINCT norm(variant) AS nn, wset(variant) AS ws FROM cn_variants
        ),
        alias AS (
            SELECT authorid, display_name, norm(alt) AS nalias, wset(alt) AS wsalias
            FROM (
                SELECT authorid, display_name,
                       unnest(from_json(display_name_alternatives, '["VARCHAR"]')) AS alt
                FROM '../sciscinet_author_details.parquet'
            )
            WHERE norm(alt) IN (SELECT nn FROM keys)
               OR wset(alt) IN (SELECT ws FROM keys)
        )
        SELECT DISTINCT
            v.rid, c.cn_name AS eu_name, a.authorid, a.display_name AS sci_name
        FROM cn_variants v
        JOIN cn c     ON c.rid = v.rid
        JOIN alias a  ON norm(v.variant) = a.nalias
                      OR wset(v.variant) = a.wsalias
    """).df()


def match_1c():
    """首末名模糊兜底(最松):只对精确+别名都没配上的 rid,且只用【完整罗马化】变体
    (排除首字母缩写式与中文形,fl 对它们过松/无意义)。fl(display_name)=fl(variant)。"""
    return con.sql("""
        SELECT DISTINCT
            v.rid, c.cn_name AS eu_name, sci.authorid, sci.display_name AS sci_name
        FROM cn_variants v
        JOIN cn c ON c.rid = v.rid
        JOIN '../sciscinet_authors.parquet' sci
          ON fl(sci.display_name) = fl(v.variant)
        WHERE v.kind = 'roman'
          AND v.rid NOT IN (SELECT DISTINCT rid FROM r1_ea)
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


# ---------------------------------------------------------------------------
# 层3:领域取数(v1)——候选作者前3 level-1 子学科名 + 该行中文领域文本
# ---------------------------------------------------------------------------
def match_3():
    return con.sql("""
        WITH cand AS (SELECT DISTINCT authorid FROM r3in),
        papers AS (
            SELECT authorid, paperid FROM '../sciscinet_authors_paperid.parquet'
            WHERE authorid IN (SELECT authorid FROM cand)
        ),
        lvl1 AS (
            SELECT fieldid, display_name FROM '../sciscinet_fields.parquet' WHERE level = 1
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
            FROM ranked r JOIN lvl1 l ON r.fieldid = l.fieldid
            WHERE r.rnk <= 3
        )
        SELECT c.rid, c.eu_name, c.authorid, c.match_type, c.host_id,
               c.eu_text, top3.field_name, top3.share, top3.rnk
        FROM r3in c
        LEFT JOIN top3 ON c.authorid = top3.authorid
    """).df()


# ===== v1 语义打分(自带副本,不 import data_match)=====
_EMBED = {}


def _get_embedder():
    if "fn" in _EMBED:
        return _EMBED["fn"]
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    import torch
    from transformers import AutoTokenizer, AutoModel
    device = "cuda" if torch.cuda.is_available() else "cpu"
    tok = AutoTokenizer.from_pretrained(EMB_MODEL)
    model = AutoModel.from_pretrained(EMB_MODEL).to(device)
    model.eval()
    print("%s 嵌入模型 %s 载入(device=%s)" % (_el(), EMB_MODEL, device), flush=True)

    @torch.no_grad()
    def embed(texts):
        out = []
        for i in range(0, len(texts), 64):
            enc = tok(texts[i:i + 64], padding=True, truncation=True,
                      max_length=64, return_tensors="pt").to(device)
            v = model(**enc).last_hidden_state[:, 0]   # bge-m3 稠密向量 = CLS token
            v = torch.nn.functional.normalize(v, p=2, dim=1)
            out.append(v.cpu().numpy())
        return np.vstack(out)

    _EMBED["fn"] = embed
    return embed


def semantic_score_v1(long_df):
    """每 (rid, authorid) 一行:sim = 其前3 子学科名与该行领域文本的最大 cosine。"""
    keys = ["rid", "eu_name", "authorid", "match_type", "host_id"]
    df = long_df.copy()
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
    return (df.groupby(keys, dropna=False)["cos"].max()
              .reset_index().rename(columns={"cos": "sim"}))


# ---------------------------------------------------------------------------
# 层4:兜底(passed → 论文数 → authorid)
# ---------------------------------------------------------------------------
def match_4():
    return con.sql("""
        WITH cand AS (
            SELECT DISTINCT rid, eu_name, authorid, match_type, host_id, passed FROM r4in
        ),
        pcnt AS (
            SELECT authorid, count(DISTINCT paperid) AS n_papers
            FROM '../sciscinet_authors_paperid.parquet'
            WHERE authorid IN (SELECT DISTINCT authorid FROM cand)
            GROUP BY authorid
        ),
        ranked AS (
            SELECT c.rid, c.eu_name, c.authorid, c.match_type, c.host_id, c.passed,
                   COALESCE(p.n_papers, 0) AS n_papers,
                   row_number() OVER (
                       PARTITION BY c.rid
                       ORDER BY c.passed DESC, COALESCE(p.n_papers, 0) DESC, c.authorid
                   ) AS rnk
            FROM cand c LEFT JOIN pcnt p ON c.authorid = p.authorid
        )
        SELECT rid, eu_name, authorid, match_type, host_id, passed, n_papers
        FROM ranked WHERE rnk = 1
    """).df()


def main():
    cols = ['rid', 'eu_name', 'authorid', 'match_type', 'host_id', 'source']
    resolved = []

    cn_df, var_df = load_cn()
    con.register('cn', cn_df[['rid', 'cn_name', 'field_text']])
    con.register('cn_variants', var_df)
    total = len(cn_df)
    print('CN 名单:%d 行 | 罗马化变体 %d 条(heteronym=%s)| 有真值 %d'
          % (total, len(var_df), HETERONYM, cn_df['gold_authorid'].notna().sum()), flush=True)

    # 层1
    r1_exact = match_1a(); r1_exact['match_type'] = 'exact'
    con.register('r1_exact', r1_exact)
    print('%s 层1a 精确完成:%d 候选行' % (_el(), len(r1_exact)), flush=True)
    r1_alias = match_1b() if USE_ALIAS else r1_exact.iloc[:0].copy()
    r1_alias['match_type'] = 'alias'
    print('%s 层1b 别名完成:%d 候选行(alias=%s)' % (_el(), len(r1_alias), USE_ALIAS), flush=True)
    r1_ea = pd.concat([r1_exact, r1_alias], ignore_index=True)
    con.register('r1_ea', r1_ea)
    r1_fuzzy = match_1c() if USE_FUZZY else r1_exact.iloc[:0].copy()
    r1_fuzzy['match_type'] = 'fuzzy'
    print('%s 层1c 模糊完成:%d 候选行' % (_el(), len(r1_fuzzy)), flush=True)

    r1 = pd.concat([r1_exact, r1_alias, r1_fuzzy], ignore_index=True)
    pending = set(r1['rid'])
    print('层1 候选:精确 %d | 别名 %d | 模糊 %d | 有候选的行 %d'
          % (r1_exact['rid'].nunique(), r1_alias['rid'].nunique(),
             r1_fuzzy['rid'].nunique(), len(pending)), flush=True)

    # name_unique:精确∪别名 合起来仍唯一 authorid,且该 rid 有精确命中
    ea_ids = r1_ea[['rid', 'authorid']].drop_duplicates()
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
    # 仍多个→平局进层3,机构缺失/候选都没在该机构→带全部候选进层3(host_id 空)。
    pend = r1[r1['rid'].isin(pending)]
    con.register('pend', pend)
    r2 = match_2()
    print('%s 层2 机构过滤完成:%d 候选行' % (_el(), len(r2)), flush=True)
    r2cnt = r2.groupby('rid')['authorid'].nunique()
    take2 = set(r2cnt[r2cnt == 1].index)               # 机构后仅剩 1 人 → 定
    tie_rids = set(r2cnt[r2cnt > 1].index)             # 机构后仍多人 → 层3
    t = r2[r2['rid'].isin(take2)].copy()
    t['source'] = 'round2_inst'
    resolved.append(t[cols])
    pending -= take2
    noinst_rids = pending - tie_rids                   # 无 host / 候选都没在该机构
    print('层2 机构 → 确定 %d,平局 %d,缺失 %d,剩下 %d'
          % (len(take2), len(tie_rids), len(noinst_rids), len(pending)), flush=True)

    # 层3 输入:平局(机构过滤后候选) + 缺失(层1 全部候选,host_id 空)
    l3_ties = r2[r2['rid'].isin(tie_rids)][cols[:-1]]
    l3_noinst = pend[pend['rid'].isin(noinst_rids)][['rid', 'eu_name', 'authorid', 'match_type']].copy()
    l3_noinst['host_id'] = pd.NA
    l3_in = (pd.concat([l3_ties, l3_noinst[cols[:-1]]], ignore_index=True).drop_duplicates())
    con.register('r3in_pre', l3_in)
    l3_in = con.sql("""
        SELECT p.rid, p.eu_name, p.authorid, p.match_type, p.host_id, c.field_text AS eu_text
        FROM r3in_pre p JOIN cn c ON p.rid = c.rid
    """).df()
    con.register('r3in', l3_in)

    # 层3:领域 v1
    r3_long = match_3()
    print('%s 层3 取数完成:%d 行长表' % (_el(), len(r3_long)), flush=True)
    scored = semantic_score_v1(r3_long)
    scored = scored.sort_values(['rid', 'sim'], ascending=[True, False]).reset_index(drop=True)
    print('层3 语义打分:v1 子学科名×中文领域文本(thresh=%.2f, margin=%.2f)'
          % (SEM_THRESH, SEM_MARGIN))

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
    print('%s 层3 语义 → 确定 %d,剩下 %d' % (_el(), len(take3), len(pending)), flush=True)

    r3 = r3_long.merge(scored[['rid', 'authorid', 'sim']], on=['rid', 'authorid'], how='left')
    r3 = r3.sort_values(['rid', 'sim', 'authorid', 'rnk'],
                        ascending=[True, False, True, True]).reset_index(drop=True)

    # 层4:兜底
    l4_in = (l3_in[l3_in['rid'].isin(pending)]
             .merge(scored[['rid', 'authorid', 'sim']], on=['rid', 'authorid'], how='left'))
    l4_in['passed'] = (l4_in['sim'] >= SEM_THRESH).fillna(False)
    con.register('r4in', l4_in)
    r4res = match_4()
    r4res['source'] = np.where(r4res['passed'], 'round4_semtie', 'round4_maxpapers')
    take4 = set(r4res['rid'])
    resolved.append(r4res[cols])
    pending -= take4
    print('%s 层4 兜底 → 确定 %d(语义平局 %d,论文数 %d),剩下 %d'
          % (_el(), len(take4), int((r4res['source'] == 'round4_semtie').sum()),
             int((r4res['source'] == 'round4_maxpapers').sum()), len(pending)), flush=True)

    r4 = r4res.sort_values('rid').reset_index(drop=True)

    # 收口
    final = (pd.concat(resolved, ignore_index=True)
               .drop_duplicates(subset='rid').sort_values('rid').reset_index(drop=True))
    # 补论文数 + 论文数下限闸(CN 默认 floor=0 → 不扣留)
    con.register('final_ids', final[['authorid']].drop_duplicates())
    npdf = con.sql("""
        SELECT authorid, count(DISTINCT paperid) AS n_papers
        FROM '../sciscinet_authors_paperid.parquet'
        WHERE authorid IN (SELECT authorid FROM final_ids)
        GROUP BY authorid
    """).df()
    final = final.merge(npdf, on='authorid', how='left')
    final['n_papers'] = final['n_papers'].fillna(0).astype(int)
    held = final[final['n_papers'] < PAPER_FLOOR].copy()
    final = final[final['n_papers'] >= PAPER_FLOOR].reset_index(drop=True)
    # 附回中文名 / 领域 / 真值,便于人工核对与评测
    final = final.merge(cn_df[['rid', 'cn_name', 'field_text', 'inst_text',
                               'year', 'gold_authorid']], on='rid', how='left')

    r1.to_csv("match_1_cn.csv", index=False, encoding='utf-8-sig')
    r3.to_csv("match_3_cn.csv", index=False, encoding='utf-8-sig')
    r4.to_csv("match_4_cn.csv", index=False, encoding='utf-8-sig')
    final.to_csv("matched_final_cn.csv", index=False, encoding='utf-8-sig')
    if PAPER_FLOOR > 0:
        held.to_csv("held_lowpaper_cn.csv", index=False, encoding='utf-8-sig')

    print()
    print('汇总:匹配成功 %d / %d = %.1f%%(未定 %d)'
          % (len(final), total, 100.0 * len(final) / total, len(pending)))
    print('  来源:名字唯一 %d | 机构 %d | 语义领域 %d | 语义平局 %d | 论文数兜底 %d'
          % (int((final['source'] == 'name_unique').sum()),
             int((final['source'] == 'round2_inst').sum()),
             int((final['source'] == 'round3_semantic').sum()),
             int((final['source'] == 'round4_semtie').sum()),
             int((final['source'] == 'round4_maxpapers').sum())))
    print('结果:matched_final_cn.csv, match_1_cn.csv, match_3_cn.csv, match_4_cn.csv')


if __name__ == "__main__":
    main()
