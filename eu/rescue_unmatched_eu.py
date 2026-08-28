# -*- coding: utf-8 -*-
r"""
rescue_unmatched_eu.py  ——  抢救 EU.xlsx 里 data_match.py 四层跑完仍未匹配的 140 行

============================ 背景 ============================
data_match.py 产出 matched_final.csv(3763 行)+ held_lowpaper.csv(339 行),
EU.xlsx 共 4242 行,还剩 140 行完全没匹配上(其中 6 行姓名为空、126 个去重姓名)。
未配上的根因(见 analyze_unmatched.py / analyze_pool2.py):名字格式脏(逗号分隔多名、
Dr.、Geb./Ép 婚后婚前标记、括号昵称、双姓 Spanish/Catalan、camelCase 无空格、姓名倒序)
和源侧拼写变体/昵称(Evanthia→Evi、Susannne→Susan、Joseph→Joost),让四层的 norm/fl/wset
key 全都对不上。少数是快照(sciscinet parquet)里真缺失的人。

============================ 思路(精度优先) ============================
本脚本【只新增文件、绝不改动】matched_final.csv / data_match.py 的既有结果。核心是把
data_match 的裁决闸复用到一个【更宽的召回】上,用 host 机构做硬准入,把假阳性挡在门外:

  L0 名字清洗   去称谓/逗号/括号昵称/婚后婚前标记/后缀;库侧 camelCase 拆分 + 连字符族归一,
                姓名倒序用 fuzzyname 的正反变体兜住。
  L1 召回       只按【末名 = 姓氏候选(末/倒数第二/首 token)】做等值召回(不卡首名,避免把
                Evi/Susan 这类昵称/拼写变体在召回阶段就漏掉),把 given 名的相容性交给 fuzzyname。
  L2 机构准入   host_ids_bridge 的 host_openalex_id 做【唯一硬闸】:候选作者必须在该行 host
                机构发过文(join sciscinet_paper_author_affiliation),与 data_match.match_2 同口径。
  L2.5 fuzzyname 对每个 (EU名, 候选名) 用 names_match() 跑正反变体复核,砍掉同机构同姓的其他人;
                判 False 但机构强命中的进 low_confidence,不一票否决。
  L3 语义       host+fuzzy 后仍多候选的,用 data_match 的 v1 语义(候选前3 level-1 子学科名 ×
                EU 面板文本 的最大 cosine)选一个;无 torch/无面板文本则跳过。
  L4 论文数     仍平局的按去重 paperid 论文数取最高;最终答案论文数 < PAPER_FLOOR(30)的
                移出 rescue_matched、写进 low_confidence(held 风格,reason=lowpaper)。
  L5 API 兜底   本地 host+fuzzy 没定下来的【有名字】行,调 OpenAlex /authors?search=,用 host
                机构核对 affiliations;命中 -> 回 sciscinet 查论文数定档;0 命中 -> 快照真缺失,人工。

产物(全部新增):
  rescue_matched.csv   同 matched_final 列:rid,eu_name,authorid,match_type,host_id,source,n_papers
  rescue_lowconf.csv   低置信待人工:上面各列 + reason,cname,n_cand,fuzzy_pass
  rescue_summary.md     每层召回/确定/低置信/剩余明细
运行:cd D:\data_matching\eu && python rescue_unmatched_eu.py
"""
import os
import re
import sys
import json
import time
import unicodedata
from pathlib import Path

import numpy as np
import pandas as pd
import duckdb

# Windows 控制台默认 gbk,打印机构名/带重音的人名会 UnicodeEncodeError;强制 utf-8
try:
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
except Exception:
    pass

from fuzzyname_vendored import names_match, _NICK   # 成对姓名同一性复核(与 data_match 同一 vendored)

# ============================ 参数 ============================
PAPER_FLOOR = 30            # 论文数下限闸,与 data_match.py 一致(精度优先)
SEM_THRESH = 0.62           # 语义 v1 绝对下限(与 data_match._semantic_score_v1 同口径)
SEM_MARGIN = 0.03           # 语义 v1 同 rid 第一名对第二名最小领先
EMB_MODEL = "BAAI/bge-small-en-v1.5"
USE_SEMANTIC = os.environ.get("RESCUE_SEM", "1") == "1"   # 关掉 = 跳过 L3,直接 L4 论文数
USE_API = os.environ.get("RESCUE_API", "1") == "1"        # 关掉 = 跳过 L5 API 兜底

# ============================ _NICK 追加等价组 ============================
# fuzzyname 的昵称表默认没有这几组(观测自 140 名单):Patricio/Patrick、Evi/Evanthia、
# Joseph/Joost、Lampros/Lambros、Mikolaj/Mikolai。补进去后 names_match 才会把它们判同一人。
_EXTRA_NICK = [
    ["lampros", "lambros"],
    ["patricio", "patrick", "patrizio", "patrice"],
    ["evi", "evanthia"],
    ["joseph", "joost", "jozef", "joop"],
    ["mikolaj", "mikolai", "nikolaj", "nicolai"],
    ["dzmitry", "dmitry", "dmitri", "dzmitri"],
]
for _grp in _EXTRA_NICK:
    for _t in _grp:
        _NICK.setdefault(_t, set()).update(_grp)


# ============================ L0 名字清洗 ============================
_HYPHENS = "[-­‐-―−]"   # 连字符/破折号家族,与 data_match.norm 同口径


def _strip_accents(s):
    s = unicodedata.normalize("NFKD", str(s))
    return "".join(c for c in s if not unicodedata.combining(c))


def clean_tokens(name):
    """EU 'Researcher(s)' -> 清洗后的有序 token 列表(given... 姓)。
    去括号昵称/称谓/婚后婚前标记/后缀/逗号/句点,连字符族->空格,去重音小写。"""
    s = str(name)
    s = re.sub(r"\([^)]*\)", " ", s)                     # 括号昵称 (Charissa)/(Elise)
    s = _strip_accents(s).lower()                        # 去重音要在标记正则之前(Ép->ep)
    s = re.sub(_HYPHENS, " ", s)                         # 连字符族 -> 空格(Yvan-Charvet -> yvan charvet)
    s = re.sub(r"\b(dr|prof|professor|mr|mrs|ms|ing)\b\.?", " ", s)               # 称谓
    s = re.sub(r"\b(geb|geborne|geborene|ep|epouse|nee|verw)\b\.?", " ", s)       # 婚后/婚前标记
    s = re.sub(r"\b(jr|sr|ii|iii|iv)\b", " ", s)         # 后缀
    s = s.replace(",", " ").replace(".", " ")
    return [t for t in re.sub(r"\s+", " ", s).strip().split() if t]


def rev(s):
    """姓名倒序:'liv hornekaer' -> 'hornekaer liv'。给 fuzzyname 补一个反向变体。"""
    return " ".join(str(s).split()[::-1])


def fuzzy_ok(eu_clean, cname):
    """(EU清洗名, 候选库名) 是否 fuzzy 同一人:原样 / EU 倒序 / 候选倒序,任一 True 即通过。"""
    for a, b in ((eu_clean, cname), (rev(eu_clean), cname), (eu_clean, rev(cname))):
        try:
            if names_match(a, b):
                return True
        except Exception:
            continue
    return False


def name_anchor_ok(eu_toks, cname):
    """名字锚定护栏,专治 names_match 子集规则的【子串巧合误配】:
    如 'E Lejeune' 因 'e lejeune' 恰是 'jeanin(e lejeune) lorthois' 的子串而被判同一人,
    可缩写首字母 E 从没和 Sylvie 核对过。规则(与词序无关,对候选整名 token 集判):
      (1) 候选里每个【单字母缩写】token,其首字母必须命中某个 EU token 的首字母;
      (2) 候选里至少有一个【非缩写】token 与某 EU token 强对应(相等/前缀/昵称)。
    'Mira'⊂'tihoMira'(截断昵称、非缩写)不受影响;真缩写匹配(H. A. Helfgott 等)照过。"""
    ct = clean_tokens(cname)
    if not ct:
        return True
    eu_inits = {t[0] for t in eu_toks if t}
    for t in ct:
        if len(t) == 1 and t not in eu_inits:          # 缩写首字母对不上 EU 任一名 -> 否
            return False
    for f in ct:
        if len(f) == 1:
            continue
        for e in eu_toks:
            if (f == e or f.startswith(e) or e.startswith(f)
                    or f in _NICK.get(e, ()) or e in _NICK.get(f, ())):
                return True                              # 有一个实名 token 强对应即通过
    return False


def eu_field_text(row):
    """复用 data_match.eu_field_text 逻辑:Panel 全名(去 'PE9 - ' 前缀)优先,回退 Domain(去 '(PE)')。"""
    panel = row.get("Panel")
    if isinstance(panel, str) and panel.strip() and panel.strip() != "-":
        return re.sub(r"^[A-Z]{2}[0-9]+\s*-\s*", "", panel).strip()
    dom = row.get("Domain")
    if isinstance(dom, str) and dom.strip() and dom.strip() != "-":
        return re.sub(r"\s*\([A-Z]{2}\)\s*$", "", dom).strip()
    return None


# ============================ 数据准备 ============================
def load_unmatched():
    """重建未匹配集(与 analyze_unmatched.py 一致):EU 全表 - matched_final.rid - held_lowpaper.rid。
    返回 (eu 全表, unmatched DataFrame[含 rid/eu_name/host_id/eu_text/toks/eu_clean])。"""
    eu = pd.read_excel("EU.xlsx")
    mf = pd.read_csv("matched_final.csv")
    held = pd.read_csv("held_lowpaper.csv")
    done = set(mf["rid"]) | set(held["rid"])
    rids = [r for r in eu.index if r not in done]
    bridge = pd.read_csv("host_ids_bridge.csv")
    host_map = dict(zip(bridge["row_id"], bridge["host_openalex_id"]))

    recs = []
    for r in rids:
        row = eu.loc[r]
        nm = row["Researcher(s)"]
        host = host_map.get(r)
        host = host if isinstance(host, str) and host.strip() else None
        toks = clean_tokens(nm) if isinstance(nm, str) and nm.strip() else []
        recs.append({
            "rid": r,
            "eu_name": nm if isinstance(nm, str) else "",
            "host_id": host,
            "eu_text": eu_field_text(row),
            "toks": toks,
            "eu_clean": " ".join(toks),
        })
    return eu, pd.DataFrame(recs)


# ============================ L1 召回 + L2 机构准入 ============================
def recall_and_gate(con, um):
    """末名=姓氏候选 的等值召回(不卡首名)+ host 机构准入。
    返回 host-confirmed 候选长表 DataFrame[rid, authorid, cname, host_id]。"""
    named = um[um["toks"].apply(len) >= 1].copy()

    # 姓氏候选 = 末 token + 倒数第二 token + 首 token(兼顾双姓与姓名倒序);去重成 (rid, surtok)
    surn = []
    names_rows = []
    for _, r in named.iterrows():
        t = r["toks"]
        surs = {t[-1]}
        if len(t) >= 2:
            surs.add(t[-2]); surs.add(t[0])
        for s in surs:
            surn.append((r["rid"], s))
        names_rows.append((r["rid"], r["eu_clean"], r["host_id"]))
    eu_surn = pd.DataFrame(surn, columns=["rid", "surtok"]).drop_duplicates()
    eu_names = pd.DataFrame(names_rows, columns=["rid", "eu_clean", "host_id"])
    con.register("eu_surn", eu_surn)
    con.register("eu_names", eu_names)

    # base:作者表末 token(camelCase 拆分 + 连字符族归一 + 去重音,和 EU 清洗同口径)
    t0 = time.time()
    con.sql(r"""
        CREATE OR REPLACE TEMP TABLE base AS
        SELECT authorid, display_name AS cname,
               regexp_extract(cn, '(\S+)$', 1) AS l
        FROM (
            SELECT authorid, display_name,
                regexp_replace(regexp_replace(trim(lower(strip_accents(
                    regexp_replace(display_name, '([\p{Ll}])([\p{Lu}])', '\1 \2', 'g')  -- camelCase 拆分
                ))), '[\x{002D}\x{00AD}\x{2010}-\x{2015}\x{2212}]', ' ', 'g'),           -- 连字符族->空格
                '\s+', ' ', 'g') AS cn
            FROM '../sciscinet_authors.parquet'
            WHERE display_name IS NOT NULL
        )
    """)
    print("  base(作者末名索引)建好 %.1fs" % (time.time() - t0))

    t0 = time.time()
    con.sql(r"""
        CREATE OR REPLACE TEMP TABLE cand AS
        SELECT DISTINCT n.rid, b.authorid, b.cname, n.host_id
        FROM eu_surn s
        JOIN base b     ON b.l = s.surtok
        JOIN eu_names n ON n.rid = s.rid
    """)
    pool = con.sql("SELECT rid, count(DISTINCT authorid) p FROM cand GROUP BY rid").df()
    print("  L1 召回(末名=姓氏) %.1fs:候选作者 %d,覆盖 %d 个 rid,pool 中位 %.0f/最大 %d"
          % (time.time() - t0, con.sql("SELECT count(DISTINCT authorid) FROM cand").fetchone()[0],
             len(pool), pool["p"].median(), pool["p"].max()))

    # L2 机构准入:候选作者必须在该行 host 机构发过文(与 match_2 同口径)
    t0 = time.time()
    hc = con.sql(r"""
        WITH aff AS (
            SELECT DISTINCT authorid, institutionid
            FROM '../sciscinet_paper_author_affiliation.parquet'
            WHERE authorid      IN (SELECT DISTINCT authorid FROM cand)
              AND institutionid IN (SELECT DISTINCT host_id  FROM cand WHERE host_id IS NOT NULL)
        )
        SELECT DISTINCT c.rid, c.authorid, c.cname, c.host_id
        FROM cand c
        JOIN aff a ON c.authorid = a.authorid AND c.host_id = a.institutionid
    """).df()
    gate_pool = hc.groupby("rid")["authorid"].nunique()
    print("  L2 机构准入 %.1fs:host 确认候选 %d 对,覆盖 %d 个 rid(pool1=%d, 2-5=%d, >5=%d)"
          % (time.time() - t0, len(hc), gate_pool.nunique() if False else gate_pool.index.nunique(),
             int((gate_pool == 1).sum()), int(((gate_pool >= 2) & (gate_pool <= 5)).sum()),
             int((gate_pool > 5).sum())))
    return hc, pool


# ============================ L3 语义(v1,复用 data_match 口径) ============================
_EMBED = {}


def _get_embedder():
    if "fn" in _EMBED:
        return _EMBED["fn"]
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    import torch
    from transformers import AutoTokenizer, AutoModel
    tok = AutoTokenizer.from_pretrained(EMB_MODEL)
    model = AutoModel.from_pretrained(EMB_MODEL); model.eval()

    @torch.no_grad()
    def embed(texts):
        out = []
        for i in range(0, len(texts), 64):
            enc = tok(texts[i:i + 64], padding=True, truncation=True,
                      max_length=64, return_tensors="pt")
            v = model(**enc).last_hidden_state[:, 0]
            v = torch.nn.functional.normalize(v, p=2, dim=1)
            out.append(v.cpu().numpy())
        return np.vstack(out)

    _EMBED["fn"] = embed
    return embed


def semantic_pick(con, ties, eu_text_map):
    """对 host+fuzzy 后仍多候选的 rid 做 v1 语义消歧。
    ties: DataFrame[rid, authorid, cname, host_id](同一 rid 有 >=2 authorid)。
    返回 {rid: authorid} 语义能唯一定下来的;失败/无信号的 rid 不在返回里(交 L4)。"""
    if ties.empty:
        return {}
    con.register("r3in", ties[["rid", "authorid"]].drop_duplicates())
    long = con.sql(r"""
        WITH cand AS (SELECT DISTINCT authorid FROM r3in),
        papers AS (
            SELECT authorid, paperid FROM '../sciscinet_authors_paperid.parquet'
            WHERE authorid IN (SELECT authorid FROM cand)
        ),
        lvl1 AS (SELECT fieldid, display_name FROM '../sciscinet_fields.parquet' WHERE level = 1),
        field_cnt AS (
            SELECT p.authorid, pf.fieldid, count(*) AS n
            FROM papers p JOIN '../sciscinet_paperfields.parquet' pf ON p.paperid = pf.paperid
            WHERE pf.fieldid IN (SELECT fieldid FROM lvl1)
            GROUP BY 1, 2
        ),
        ranked AS (
            SELECT authorid, fieldid, n,
                   row_number() OVER (PARTITION BY authorid ORDER BY n DESC, fieldid) AS rnk
            FROM field_cnt
        )
        SELECT r.rid, r.authorid, l.display_name AS field_name
        FROM r3in r
        JOIN ranked rk ON r.authorid = rk.authorid AND rk.rnk <= 3
        JOIN lvl1 l    ON rk.fieldid = l.fieldid
    """).df()
    long["eu_text"] = long["rid"].map(eu_text_map)
    long = long.dropna(subset=["eu_text", "field_name"])
    if long.empty:
        return {}

    embed = _get_embedder()
    texts = pd.unique(pd.concat([long["eu_text"], long["field_name"]], ignore_index=True))
    vecs = embed(list(texts)); idx = {t: i for i, t in enumerate(texts)}
    long["cos"] = [float(vecs[idx[f]] @ vecs[idx[e]]) for f, e in zip(long["field_name"], long["eu_text"])]
    sim = long.groupby(["rid", "authorid"])["cos"].max().reset_index(name="sim")

    picks = {}
    for rid, g in sim.groupby("rid"):
        passed = g[g["sim"] >= SEM_THRESH].sort_values("sim", ascending=False)
        if passed.empty:
            continue
        if len(passed) == 1 or (passed.iloc[0]["sim"] - passed.iloc[1]["sim"] >= SEM_MARGIN):
            picks[rid] = passed.iloc[0]["authorid"]
    return picks


# ============================ 论文数 ============================
def paper_counts(con, authorids):
    if len(authorids) == 0:
        return {}
    con.register("pc_ids", pd.DataFrame({"authorid": list(authorids)}))
    df = con.sql(r"""
        SELECT authorid, count(DISTINCT paperid) AS n
        FROM '../sciscinet_authors_paperid.parquet'
        WHERE authorid IN (SELECT authorid FROM pc_ids)
        GROUP BY authorid
    """).df()
    return dict(zip(df["authorid"], df["n"]))


# ============================ L5 OpenAlex API 兜底 ============================
API_CACHE = "api_cache_authors.json"


def _load_key():
    p = Path("../.openalex_api_key")
    return p.read_text(encoding="utf-8").strip() if p.exists() else \
        os.environ.get("OPENALEX_API_KEY", "").strip()


def api_search_author(name, host_id, key, cache):
    """OpenAlex /authors?search=name;返回 (authorid, matched_name, 'host'|'nohost') 或 None。
    优先返回 affiliations/last_known_institutions 含该 host 机构的作者(host 核对);
    没有 host 命中但有结果时返回相关性最高者标 'nohost'(低置信)。429 退避、失败不缓存空。"""
    import requests
    key_str = f"{name}|||{host_id}"
    if key_str in cache:
        c = cache[key_str]
        return tuple(c) if c else None

    params = {
        "search": name,
        "per-page": 25,
        "select": "id,display_name,display_name_alternatives,works_count,"
                  "last_known_institutions,affiliations",
        "mailto": "emiyafancy@gmail.com",
    }
    if key:
        params["api_key"] = key
    url = "https://api.openalex.org/authors"
    results = None
    for attempt in range(5):
        try:
            time.sleep(0.2)   # 限速,压在礼貌池额度内
            resp = requests.get(url, params=params, timeout=30,
                                headers={"User-Agent": "eu-rescue/1.0 (emiyafancy@gmail.com)"})
            if resp.status_code == 429:
                ra = int(resp.headers.get("Retry-After", "0") or 0)
                if ra > 120:
                    print("    [API] 当日额度用尽(Retry-After=%ds),停 API,已完成的照常写出" % ra)
                    raise KeyboardInterrupt   # 让上层优雅停 API
                wait = ra or 1.5 ** attempt
                print("    [API] 429 短时限流,%.1fs 后重试(%d/5)" % (wait, attempt + 1))
                time.sleep(wait); continue
            if resp.status_code >= 500:
                time.sleep(1.5 ** attempt); continue
            resp.raise_for_status()
            results = resp.json().get("results", [])
            break
        except KeyboardInterrupt:
            raise
        except Exception as e:
            print("    [API] %s;%.1fs 后重试(%d/5)" % (type(e).__name__, 1.5 ** attempt, attempt + 1))
            time.sleep(1.5 ** attempt)
    if results is None:
        return None   # 本轮网络失败:不缓存,留 pending

    def inst_ids(a):
        ids = set()
        for it in (a.get("last_known_institutions") or []):
            if it.get("id"):
                ids.add(it["id"].rsplit("/", 1)[-1])
        for af in (a.get("affiliations") or []):
            it = af.get("institution") or {}
            if it.get("id"):
                ids.add(it["id"].rsplit("/", 1)[-1])
        return ids

    hit = None
    if host_id:
        host_matches = [a for a in results if host_id in inst_ids(a)]
        if host_matches:
            a = max(host_matches, key=lambda x: x.get("works_count") or 0)
            hit = (a["id"].rsplit("/", 1)[-1], a.get("display_name"), "host")
    if hit is None and results:
        a = results[0]
        hit = (a["id"].rsplit("/", 1)[-1], a.get("display_name"), "nohost")
    cache[key_str] = list(hit) if hit else []
    return hit


# ============================ 主流程 ============================
def main():
    con = duckdb.connect()
    con.sql("SET memory_limit='8GB'")
    con.sql("SET temp_directory='.duckdb_tmp'")
    con.sql("SET preserve_insertion_order=false")

    eu, um = load_unmatched()
    n_total = len(eu)
    n_empty = int((um["eu_name"].str.strip() == "").sum())
    named = um[um["eu_name"].str.strip() != ""]
    eu_text_map = dict(zip(um["rid"], um["eu_text"]))
    eu_name_map = dict(zip(um["rid"], um["eu_name"]))
    host_map = dict(zip(um["rid"], um["host_id"]))
    clean_map = dict(zip(um["rid"], um["eu_clean"]))
    toks_map = dict(zip(um["rid"], um["toks"]))
    print("未匹配 %d 行(空姓名 %d,去重姓名 %d),全部有 host_id:%s"
          % (len(um), n_empty, named["eu_name"].nunique(),
             bool(um["host_id"].notna().all())))

    # ---- L1 召回 + L2 机构准入 ----
    print("\n[L1+L2] 召回 + host 机构准入")
    hc, name_pool = recall_and_gate(con, um)

    # ---- L2.5 fuzzyname 复核 ----
    print("\n[L2.5] fuzzyname 正反变体复核")
    hc = hc.copy()
    hc["fuzzy_pass"] = [fuzzy_ok(clean_map[r], c) and name_anchor_ok(toks_map[r], c)
                        for r, c in zip(hc["rid"], hc["cname"])]
    print("  host 确认候选 %d 对,fuzzyname+锚定 通过 %d 对(涉及 %d 个 rid)"
          % (len(hc), int(hc["fuzzy_pass"].sum()),
             hc.loc[hc["fuzzy_pass"], "rid"].nunique()))

    strong = hc[hc["fuzzy_pass"]].copy()          # host 确认 + 名字相容(高置信来源)
    matched = []       # 定案行:dict(rid,eu_name,authorid,match_type,host_id,source)
    lowconf = []       # 低置信行:上面 + reason,cname,n_cand,fuzzy_pass
    resolved_rids = set()

    # ---- 直接定 / 收集平局 ----
    ties = []          # 需要 L3/L4 消歧的 (rid) -> strong 子集
    for rid, g in strong.groupby("rid"):
        ids = g["authorid"].unique()
        if len(ids) == 1:
            matched.append(dict(rid=rid, eu_name=eu_name_map[rid], authorid=ids[0],
                                match_type="fuzzy", host_id=host_map[rid],
                                source="rescue_host_unique"))
            resolved_rids.add(rid)
        else:
            ties.append(rid)
    print("  host+fuzzy 唯一直接定 %d;多候选待 L3/L4 消歧 %d" % (len(resolved_rids), len(ties)))

    tie_df = strong[strong["rid"].isin(ties)].copy()

    # ---- L3 语义消歧(可选,复用 data_match v1 口径) ----
    sem_picks = {}
    if USE_SEMANTIC and not tie_df.empty:
        print("\n[L3] 语义消歧(v1:候选前3子学科名 × EU 面板文本)")
        try:
            sem_picks = semantic_pick(con, tie_df, eu_text_map)
            print("  语义唯一定下 %d 个 rid" % len(sem_picks))
        except Exception as e:
            print("  语义层跳过(%s: %s)" % (type(e).__name__, e))
            sem_picks = {}
    for rid, aid in sem_picks.items():
        matched.append(dict(rid=rid, eu_name=eu_name_map[rid], authorid=aid,
                            match_type="fuzzy", host_id=host_map[rid], source="rescue_semantic"))
        resolved_rids.add(rid)

    # ---- L4 论文数兜底(strong 平局里语义没定的) ----
    l4_rids = [r for r in ties if r not in sem_picks]
    if l4_rids:
        print("\n[L4] 论文数兜底(strong 平局)")
        l4 = tie_df[tie_df["rid"].isin(l4_rids)].copy()
        pc = paper_counts(con, set(l4["authorid"]))
        l4["n"] = l4["authorid"].map(pc).fillna(0).astype(int)
        for rid, g in l4.sort_values(["rid", "n", "authorid"], ascending=[True, False, True]).groupby("rid"):
            top = g.iloc[0]
            matched.append(dict(rid=rid, eu_name=eu_name_map[rid], authorid=top["authorid"],
                                match_type="fuzzy", host_id=host_map[rid], source="rescue_maxpapers"))
            resolved_rids.add(rid)
        print("  论文数定下 %d 个 rid" % len(l4_rids))

    # ---- host 命中但 fuzzyname 全 False 的:低置信(机构强、名字判否,不一票否决)----
    hc_rids = set(hc["rid"])
    fuzzy_fail_only = sorted(hc_rids - set(strong["rid"]) - resolved_rids)
    if fuzzy_fail_only:
        ff = hc[hc["rid"].isin(fuzzy_fail_only)].copy()
        pc = paper_counts(con, set(ff["authorid"]))          # 一次批量查论文数(避免逐 rid 扫大表)
        ff["n"] = ff["authorid"].map(pc).fillna(0).astype(int)
        for rid, g in ff.groupby("rid"):
            top = g.sort_values("n", ascending=False).iloc[0]  # 挑论文数最高者作为人工首选
            lowconf.append(dict(rid=rid, eu_name=eu_name_map[rid], authorid=top["authorid"],
                                match_type="fuzzy", host_id=host_map[rid], source="rescue_host_fuzzyfail",
                                reason="host_ok_fuzzy_fail", cname=top["cname"],
                                n_cand=int(g["authorid"].nunique()), fuzzy_pass=False))
            resolved_rids.add(rid)
        print("\n[低置信] host 命中但 fuzzyname 判否 %d 个 rid -> 待人工" % len(fuzzy_fail_only))

    # ---- L5 API 兜底:仍未定的【有名字】行 ----
    named_rids = set(named["rid"])
    unresolved_named = sorted(named_rids - resolved_rids)
    api_matched_rids = set()
    if USE_API and unresolved_named:
        print("\n[L5] OpenAlex API 兜底(%d 个未定 rid)" % len(unresolved_named))
        key = _load_key()
        cache = json.loads(Path(API_CACHE).read_text(encoding="utf-8")) if Path(API_CACHE).exists() else {}
        cache = {k: v for k, v in cache.items() if v != []}   # 丢弃空值条目(重查,自愈)
        api_hits = []   # (rid, authorid, matched_name, kind)
        try:
            for i, rid in enumerate(unresolved_named, 1):
                nm = eu_name_map[rid]
                q = " ".join(clean_tokens(nm)) or str(nm)
                hit = api_search_author(q, host_map[rid], key, cache)
                if hit:
                    api_hits.append((rid, hit[0], hit[1], hit[2]))
                if i % 20 == 0:
                    Path(API_CACHE).write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
        except KeyboardInterrupt:
            print("  API 提前停止(额度/中断),已查到的照常处理")
        Path(API_CACHE).write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")

        # API 命中回 sciscinet 查论文数;host 命中且论文数够 -> 定案,否则低置信
        if api_hits:
            pc = paper_counts(con, {h[1] for h in api_hits})
            for rid, aid, mname, kind in api_hits:
                n = int(pc.get(aid, 0))
                if kind == "host" and n >= PAPER_FLOOR:
                    matched.append(dict(rid=rid, eu_name=eu_name_map[rid], authorid=aid,
                                        match_type="api", host_id=host_map[rid], source="rescue_api"))
                    resolved_rids.add(rid); api_matched_rids.add(rid)
                else:
                    reason = ("api_host_lowpaper" if kind == "host" else "api_nohost")
                    lowconf.append(dict(rid=rid, eu_name=eu_name_map[rid], authorid=aid,
                                        match_type="api", host_id=host_map[rid], source="rescue_api",
                                        reason=reason, cname=mname, n_cand=1,
                                        fuzzy_pass=fuzzy_ok(clean_map[rid], mname or "")))
                    resolved_rids.add(rid)
        print("  API host 命中且论文数>=%d 定案 %d;其余(低产/无host)入低置信 %d"
              % (PAPER_FLOOR, len(api_matched_rids), len(api_hits) - len(api_matched_rids)))

    # ---- 论文数 + PAPER_FLOOR 闸(对所有定案行)----
    md = pd.DataFrame(matched)
    if not md.empty:
        pc = paper_counts(con, set(md["authorid"]))
        md["n_papers"] = md["authorid"].map(pc).fillna(0).astype(int)
        low = md[md["n_papers"] < PAPER_FLOOR].copy()
        md = md[md["n_papers"] >= PAPER_FLOOR].copy()
        for _, r in low.iterrows():        # 定案但论文数 < 30 -> held 风格低置信
            lowconf.append(dict(rid=r["rid"], eu_name=r["eu_name"], authorid=r["authorid"],
                                match_type=r["match_type"], host_id=r["host_id"], source=r["source"],
                                reason="lowpaper", cname="", n_cand=1, fuzzy_pass=True,
                                n_papers=r["n_papers"]))
    else:
        md = pd.DataFrame(columns=["rid", "eu_name", "authorid", "match_type", "host_id", "source", "n_papers"])

    # ---- 重复 grant 行传播:同 (清洗名, host 机构) 的多条行 = 同一人;其一已确定 -> 复制给其余 ----
    # EU 一人多项目时会出现完全同名同机构的重复行,四层/本脚本对重复行可能给出不一致答案
    # (一条命中真人、另一条落到 1 篇空壳)。这里以已定案的高置信行为准,统一覆盖它的重复行。
    all_rids = set(um["rid"])
    if not md.empty:
        key2aid = {}
        for _, r in md.iterrows():
            key2aid[(clean_map.get(r["rid"]), r["host_id"])] = \
                (r["authorid"], r["match_type"], int(r["n_papers"]))
        dup_rows = []
        for rid in sorted(all_rids - set(md["rid"])):
            k = (clean_map.get(rid), host_map.get(rid))
            if k[0] and k in key2aid:
                aid, mt, npp = key2aid[k]
                dup_rows.append(dict(rid=rid, eu_name=eu_name_map[rid], authorid=aid,
                                     match_type=mt, host_id=host_map[rid],
                                     source="rescue_duplicate", n_papers=npp))
        if dup_rows:
            dd = pd.DataFrame(dup_rows)
            md = pd.concat([md, dd], ignore_index=True)
            dup_set = set(dd["rid"])
            lowconf = [lc for lc in lowconf if lc["rid"] not in dup_set]
            print("\n[重复传播] 同名同机构的重复 grant 行复制已定案 %d 行" % len(dup_set))

    # ---- 剩余(未定 + 空姓名)----
    done_rids = set(md["rid"]) | {lc["rid"] for lc in lowconf}
    leftover = sorted(all_rids - done_rids)

    # ================= 写出 =================
    cols_m = ["rid", "eu_name", "authorid", "match_type", "host_id", "source", "n_papers"]
    md = md.sort_values("rid")[cols_m]
    md.to_csv("rescue_matched.csv", index=False, encoding="utf-8-sig")

    lc = pd.DataFrame(lowconf)
    if not lc.empty:
        if "n_papers" not in lc.columns:
            lc["n_papers"] = np.nan
        # 补低置信行的论文数(API/fuzzyfail 行)
        need = lc[lc["n_papers"].isna()]
        if not need.empty:
            pc = paper_counts(con, set(need["authorid"]))
            lc.loc[lc["n_papers"].isna(), "n_papers"] = \
                lc.loc[lc["n_papers"].isna(), "authorid"].map(pc)
        lc["n_papers"] = lc["n_papers"].fillna(0).astype(int)
        cols_l = ["rid", "eu_name", "authorid", "match_type", "host_id", "source",
                  "n_papers", "reason", "cname", "n_cand", "fuzzy_pass"]
        for c in cols_l:
            if c not in lc.columns:
                lc[c] = ""
        lc = lc.sort_values(["reason", "rid"])[cols_l]
    else:
        lc = pd.DataFrame(columns=["rid", "eu_name", "authorid", "match_type", "host_id",
                                   "source", "n_papers", "reason", "cname", "n_cand", "fuzzy_pass"])
    lc.to_csv("rescue_lowconf.csv", index=False, encoding="utf-8-sig")

    # ================= summary =================
    hcpool = hc.groupby("rid")["authorid"].nunique()   # host 准入后每 rid 的候选数(决策相关)
    write_summary(um, md, lc, leftover, name_pool, hcpool, n_total, len(eu),
                  api_matched_rids, eu_name_map, host_map)

    # ================= 控制台汇总 =================
    print("\n================= 抢救汇总 =================")
    print("未匹配总数           : %d(空姓名 %d)" % (len(um), n_empty))
    print("rescue_matched(>=30) : %d" % len(md))
    if not md.empty:
        print("  按 source:", md["source"].value_counts().to_dict())
    print("rescue_lowconf       : %d" % len(lc))
    if not lc.empty:
        print("  按 reason:", lc["reason"].value_counts().to_dict())
    print("剩余未定(含空姓名)  : %d -> %s%s"
          % (len(leftover), leftover[:20], " ..." if len(leftover) > 20 else ""))
    print("\n新增确定后 EU 匹配率  : (3763 + %d) / %d = %.1f%%"
          % (len(md), n_total, 100.0 * (3763 + len(md)) / n_total))
    print("产物:rescue_matched.csv, rescue_lowconf.csv, rescue_summary.md")


def write_summary(um, md, lc, leftover, name_pool, hcpool, n_total, n_eu,
                  api_matched_rids, eu_name_map, host_map):
    L = []
    L.append("# EU 未匹配 140 行抢救汇总\n")
    L.append("data_match.py 四层跑完后剩的 %d 行(matched_final 3763 + held 339 之外),"
             "本脚本用 host 机构硬准入 + fuzzyname + 语义/论文数补救。\n" % len(um))

    n_empty = int((um["eu_name"].str.strip() == "").sum())
    L.append("## 一、总量")
    L.append("| 项 | 数 |")
    L.append("| --- | ---: |")
    L.append("| 未匹配行 | %d |" % len(um))
    L.append("| 其中空姓名 | %d |" % n_empty)
    L.append("| 去重姓名 | %d |" % um[um["eu_name"].str.strip() != ""]["eu_name"].nunique())
    L.append("| **rescue_matched(论文数≥%d)** | **%d** |" % (PAPER_FLOOR, len(md)))
    L.append("| rescue_lowconf(待人工) | %d |" % len(lc))
    L.append("| 剩余未定 | %d |" % len(leftover))
    L.append("")

    L.append("## 二、rescue_matched 按来源(source)")
    L.append("| source | 数 | 含义 |")
    L.append("| --- | ---: | --- |")
    meaning = {
        "rescue_host_unique": "host 机构确认 + fuzzyname 唯一,直接定(最可信)",
        "rescue_semantic": "host+fuzzy 多候选,v1 语义唯一选出",
        "rescue_maxpapers": "host+fuzzy 多候选,语义未定,取论文数最高",
        "rescue_api": "本地未定,OpenAlex API 按 host 机构核对命中",
        "rescue_duplicate": "与某已定案行同名同机构(同一人的另一条 grant 行),复制答案",
    }
    if not md.empty:
        for s, c in md["source"].value_counts().items():
            L.append("| %s | %d | %s |" % (s, c, meaning.get(s, "")))
    L.append("")

    L.append("## 三、rescue_lowconf 按原因(reason)")
    L.append("| reason | 数 | 含义 |")
    L.append("| --- | ---: | --- |")
    rmean = {
        "host_ok_fuzzy_fail": "机构强命中但 fuzzyname 判否(可能同机构同姓他人,需人工看名字)",
        "lowpaper": "机构+名字都对上但论文数 < %d(held 风格,快照太薄)" % PAPER_FLOOR,
        "api_host_lowpaper": "API 按 host 命中但论文数 < %d" % PAPER_FLOOR,
        "api_nohost": "API 有结果但没在 host 机构核对上(相关性 top-1,弱)",
    }
    if not lc.empty:
        for s, c in lc["reason"].value_counts().items():
            L.append("| %s | %d | %s |" % (s, c, rmean.get(s, "")))
    L.append("")

    L.append("## 四、候选 pool 分布")
    L.append("- 名字召回(末名=姓氏,host 准入【前】):候选作者共 %d,覆盖 %d 个 rid,pool 中位 %.0f/最大 %d "
             "(按姓氏召回本就宽,靠 host+fuzzy 收口)"
             % (0 if name_pool is None or name_pool.empty else int(name_pool["p"].sum()),
                0 if name_pool is None or name_pool.empty else len(name_pool),
                0 if name_pool is None or name_pool.empty else name_pool["p"].median(),
                0 if name_pool is None or name_pool.empty else int(name_pool["p"].max())))
    L.append("- **host 机构准入【后】每 rid 候选数(决策相关)**:")
    if hcpool is not None and len(hcpool):
        for lo, hi, lab in [(1, 1, "1(直接可定)"), (2, 5, "2-5"), (6, 20, "6-20"), (21, 10**12, ">20")]:
            L.append("  - pool %s: %d 个 rid" % (lab, int(((hcpool >= lo) & (hcpool <= hi)).sum())))
    L.append("")

    L.append("## 五、rescue_matched 明细(抽验用)")
    L.append("| rid | eu_name | authorid | source | n_papers |")
    L.append("| ---: | --- | --- | --- | ---: |")
    for _, r in md.iterrows():
        L.append("| %d | %s | %s | %s | %d |"
                 % (r["rid"], str(r["eu_name"]).replace("|", "/"), r["authorid"],
                    r["source"], r["n_papers"]))
    L.append("")

    L.append("## 六、rescue_lowconf 明细(待人工)")
    L.append("| rid | eu_name | authorid | cname | reason | n_papers |")
    L.append("| ---: | --- | --- | --- | --- | ---: |")
    if not lc.empty:
        for _, r in lc.iterrows():
            L.append("| %d | %s | %s | %s | %s | %d |"
                     % (r["rid"], str(r["eu_name"]).replace("|", "/"), r["authorid"],
                        str(r.get("cname", "")).replace("|", "/"), r["reason"], r["n_papers"]))
    L.append("")

    L.append("## 七、剩余未定名单(快照真缺失 / 空姓名 -> 人工)")
    L.append("| rid | eu_name | host_id |")
    L.append("| ---: | --- | --- |")
    for rid in leftover:
        L.append("| %d | %s | %s |"
                 % (rid, str(eu_name_map.get(rid, "")).replace("|", "/") or "(空姓名)",
                    host_map.get(rid, "")))
    L.append("")

    Path("rescue_summary.md").write_text("\n".join(L), encoding="utf-8")


if __name__ == "__main__":
    main()
