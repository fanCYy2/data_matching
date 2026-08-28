# -*- coding: utf-8 -*-
"""
verify_orcid.py —— 用 ORCID 独立复核 author_id 匹配(刻意不产生循环验证)

复刻人工验证法:谷歌找到真人的代表作 → 回 OpenAlex 看这些论文的作者里有没有他 →
核对 author_id 是否一致。这里把"谷歌找代表作"换成 ORCID 官方公共 API(结构化、稳定),
验证半段照旧回 OpenAlex。

================= 为什么这样不构成循环验证 =================
被测系统 = 匹配流水线 data_match.py,它只用 姓名/机构/领域/论文数 定 author_id,
【从未用过 ORCID】。所以:
  - 标准答案(真人代表作 DOI)来自 ORCID,与流水线正交 → 独立;
  - 找人(定位 ORCID)只用 EU 表给的【姓名 + 机构】,【绝不】回看 OpenAlex 或候选
    author_id 去反挑 ORCID —— 一旦用被测对象挑标准答案,就成了蛇吃尾巴的循环。
  - 只对能【独立且唯一】定位到 ORCID 的行下判定;定位不到的记 no_orcid,老实留人工。
附加信息(不参与定档):顺带比对"候选 author 在 OpenAlex 上登记的 orcid"是否 == 独立
找到的 orcid。两条独立路径撞到同一 ORCID 是很强的佐证,但只当参考列,不用它定案。

================= 流程 =================
  A. 逐行(ORCID):名字检索 → 用机构消歧到唯一 ORCID → 取其自报论文的 DOI(上限 MAX_WORKS)
  B. 批量(OpenAlex):所有唯一 DOI 按 50 个一批查 /works,得到每篇的作者(id + 名字)
  C. 逐行判定:在"作者名字能对上本人"的论文里,看候选 id 命中多少篇,并算"这些论文里
     最常出现的同名 author_id"(= 本人应得的 id):
        match         候选就是应得 id,且命中 >= 2 篇
        mismatch      候选一篇没命中,且另有一个同名 id 才是应得 id(顺带把正确 id 报出来)
        review        候选命中一部分但不是主导(可能 OpenAlex 拆分/同名混入),人工看一眼
        inconclusive  能在 OpenAlex 查到、且作者名对得上的代表作太少,证据不足
        no_orcid      没能独立唯一定位到 ORCID(0 命中 / 同名多个都消歧不掉)→ 留人工

用法:
  python verify_orcid.py [csv1 csv2 ...]      # 默认三份测试集样本
  可选环境变量:
    OPENALEX_API_KEY  强烈建议设置(免费额度 1000 次/天,足够一次跑完)
    VERIFY_LIMIT=N    每份文件只跑前 N 行(冒烟测试用)
产物:<输入名>_orcid_verified.csv
"""
import os
import re
import sys
import json
import time
import unicodedata
import urllib.request
import urllib.parse
from pathlib import Path

import pandas as pd


# ============================ 机构解析 / 归一 / 分词 ============================
# (原从 resolve_host_ids_api 复用;该脚本已删,这里内联同一套逻辑,保持一致、无外部依赖)
STOP = {"of", "the", "and", "for", "de", "la", "le",
        "des", "du", "di", "der", "und", "el"}


def strip_accents(s):
    """去掉变音符号: 'München' -> 'Munchen'。用于跨语言/拼写差异的兜底比较。"""
    return "".join(c for c in unicodedata.normalize("NFKD", str(s))
                   if not unicodedata.combining(c))


def norm(s):
    """基础归一: 小写 + '&'->' and ' + 非字母数字变空格 + 压缩空白。非字符串/空串 -> None。"""
    if not isinstance(s, str):
        return None
    s = re.sub(r"&", " and ", s.lower())
    s = re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE)
    return re.sub(r"\s+", " ", s).strip() or None


def tokens(s):
    """把名字拆成【去停用词后的词集合】(frozenset),供模糊匹配用(词序无关)。"""
    n = norm(s)
    return frozenset(t for t in n.split() if t not in STOP) if n else frozenset()


def parse_host(raw):
    """把 Host 单元格拆成 (主承担机构名, 国家码)。找第一个 '[数字,两位国家码]',
    其前部分为主机构名,方括号里的国家码为主机构国家;无合法方括号时国家码为 None。"""
    if not isinstance(raw, str):
        return None, None
    m = re.search(r"\[\d+,([A-Z]{2})\]", raw)
    if m:
        cc = m.group(1)
        name = raw[:m.start()].strip().rstrip(",").strip()
    else:
        cc, name = None, raw.strip()
    return name or None, cc

try:
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
except Exception:
    pass

# ============================ 配置 ============================
DEFAULT_INPUTS = ["round2_sample_50.csv", "round3_sample_50.csv", "round4_sample_100.csv"]
MAILTO = "emiyafancy@gmail.com"
OPENALEX_KEY = os.environ.get("OPENALEX_API_KEY", "TOITSqA1Pb9diO6gaSUxV0").strip()
LIMIT = int(os.environ.get("VERIFY_LIMIT", "0") or 0)   # >0 时每文件只跑前 N 行

# ORCID 公共 API 凭据(可选但强烈建议):带 token 后限流大大放松,不再动辄退避重试。
# 三选一:① 直接给已换好的 ORCID_TOKEN;② 给 ORCID_CLIENT_ID + ORCID_CLIENT_SECRET
# (脚本自动用 client_credentials 换只读 token);③ 都不给 -> 匿名访问(慢,易 429)。
ORCID_CLIENT_ID = os.environ.get("ORCID_CLIENT_ID", "APP-C42ER4RX4ECOJCJO").strip()
ORCID_CLIENT_SECRET = os.environ.get("ORCID_CLIENT_SECRET", "f70bb6b5-cc7e-4108-b7c4-77f88a191fa9").strip()
ORCID_TOKEN = os.environ.get("ORCID_TOKEN", "").strip()
ORCID_TOKEN_URL = "https://orcid.org/oauth/token"

MAX_WORKS = 25          # 每个真人最多取多少篇自报论文回 OpenAlex 核对
MIN_EVID = 3            # 作者名对得上、且能在 OpenAlex 查到的论文 < 此数 -> 证据不足
DOI_BATCH = 50          # OpenAlex /works 的 doi OR 过滤单批上限
ORCID_SLEEP = 0.2       # ORCID 两次请求间隔
OA_SLEEP = 0.2          # OpenAlex 两次请求间隔
CACHE_PATH = "verify_orcid_cache.json"

ORCID_SEARCH = "https://pub.orcid.org/v3.0/expanded-search/"
ORCID_WORKS = "https://pub.orcid.org/v3.0/{}/works"
OA_WORKS = "https://api.openalex.org/works"
OA_AUTHORS = "https://api.openalex.org/authors"
UA = {"User-Agent": f"eu-verify-orcid/1.0 ({MAILTO})"}


# ============================ 通用 HTTP(带轻量重试) ============================
def _http(url, headers=None, tries=4):
    """GET 一个 JSON;瞬断/5xx/429 短退避重试;彻底失败返回 None(调用方勿缓存成结果)。"""
    h = dict(UA)
    h.update(headers or {})
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504):
                time.sleep(1.5 ** i + 0.5)
                continue
            if e.code in (401, 403):
                print(f"    [ORCID/OA {e.code}] 可能需要 token 或被限,放弃该请求")
            return None
        except Exception:
            time.sleep(1.5 ** i + 0.5)
    return None


# ============================ ORCID 鉴权(可选,提速用) ============================
def get_orcid_token():
    """确定本次要用的 ORCID token:优先 ORCID_TOKEN;否则用 client_id/secret 换一个只读
    token(client_credentials + scope=/read-public,不需要用户登录)。拿不到就匿名访问。"""
    global ORCID_TOKEN
    if ORCID_TOKEN:
        print("[ORCID] 使用已提供的 ORCID_TOKEN"); return
    if ORCID_CLIENT_ID and ORCID_CLIENT_SECRET:
        body = urllib.parse.urlencode({
            "client_id": ORCID_CLIENT_ID, "client_secret": ORCID_CLIENT_SECRET,
            "grant_type": "client_credentials", "scope": "/read-public"}).encode()
        req = urllib.request.Request(
            ORCID_TOKEN_URL, data=body,
            headers={"Accept": "application/json",
                     "Content-Type": "application/x-www-form-urlencoded"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                ORCID_TOKEN = json.loads(r.read().decode("utf-8")).get("access_token", "")
            print("[ORCID] 已用 client_id/secret 换到 read-public token" if ORCID_TOKEN
                  else "[ORCID] 换 token 失败,退回匿名访问(会慢)")
        except Exception as e:
            print(f"[ORCID] 换 token 出错({type(e).__name__}: {e}),退回匿名访问(会慢)")
    else:
        print("[ORCID] 未提供凭据,匿名访问(慢、易 429)。设 ORCID_CLIENT_ID/SECRET 可提速。")


def orcid_headers():
    """ORCID 请求头:JSON + 有 token 时带 Bearer(限流更松)。"""
    h = {"Accept": "application/json"}
    if ORCID_TOKEN:
        h["Authorization"] = f"Bearer {ORCID_TOKEN}"
    return h


# ============================ 缓存(只存成功结果) ============================
def load_cache():
    if Path(CACHE_PATH).exists():
        try:
            return json.loads(Path(CACHE_PATH).read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_cache(c):
    Path(CACHE_PATH).write_text(json.dumps(c, ensure_ascii=False), encoding="utf-8")


# ============================ 人名归一 ============================
def _pn(s):
    """人名归一:去重音 + 小写 + 仅留字母和空格 + 压空白。"""
    s = strip_accents(str(s)).lower()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z\s]", " ", s)).strip()


def name_key(full):
    """(姓=最后一个词, 名首字母)。用于把一篇论文里的作者对上"就是本人"。
    对 'Martin Jinek' / 'Martin Jínek' / 'M. Jinek' 都能对上。"""
    p = _pn(full).split()
    if not p:
        return (None, None)
    return (p[-1], p[0][0])


def split_researcher(eu_name):
    """EU 'Researcher(s)' 可能含多位;取第一位。返回 (given_first_token, family_last_token, 全名)。"""
    first = re.split(r"[;/]| and ", str(eu_name))[0].strip()
    toks = _pn(first).split()
    if len(toks) < 2:
        return None
    return toks[0], toks[-1], first


# ============================ 机构消歧(不看 OpenAlex) ============================
def inst_match(eu_inst, orcid_insts):
    """EU 机构名 与 ORCID 自报机构列表 是否算同一机构(词集合子集/高重叠)。"""
    et = tokens(eu_inst)
    if not et:
        return False
    for oi in orcid_insts or []:
        ot = tokens(oi)
        if not ot:
            continue
        inter = len(et & ot)
        if et <= ot or ot <= et:
            return True
        if inter and (inter / len(et | ot) >= 0.5 or inter / len(et) >= 0.6):
            return True
    return False


# ============================ ORCID 侧 ============================
def orcid_find(given, family, eu_inst, cache):
    """名字检索 → 机构消歧到唯一 ORCID。返回 (orcid, conf) 或 (None, 原因)。

    conf: strong=机构对上且唯一 / medium=全球同名仅一人(机构未佐证)。
    None 的原因: none=0命中 / ambiguous=多个同名消歧不掉。
    """
    key = f"search|{given}|{family}"
    if key in cache:
        cands = cache[key]
    else:
        q = urllib.parse.quote(f"given-names:{given} AND family-name:{family}")
        data = _http(f"{ORCID_SEARCH}?q={q}&rows=25", headers=orcid_headers())
        if data is None:
            return None, "netfail"
        cands = [{
            "orcid": r.get("orcid-id"),
            "insts": r.get("institution-name") or [],
        } for r in (data.get("expanded-result") or []) if r.get("orcid-id")]
        cache[key] = cands
        time.sleep(ORCID_SLEEP)

    if not cands:
        return None, "none"
    matched = [c for c in cands if inst_match(eu_inst, c["insts"])]
    if len(matched) == 1:
        return matched[0]["orcid"], "strong"
    if len(matched) > 1:
        return None, "ambiguous"          # 同名同机构多个,真歧义,留人工
    if len(cands) == 1:
        return cands[0]["orcid"], "medium"  # 全球同名仅一人,机构没佐证但基本是他
    return None, "ambiguous"              # 多个同名、机构都对不上 → 消歧不掉


def orcid_dois(orcid, cache):
    """取某 ORCID 自报论文的 (doi, title) 列表(每个 work group 取一条,带 DOI 的)。"""
    key = f"works|{orcid}"
    if key in cache:
        return cache[key]
    data = _http(ORCID_WORKS.format(orcid), headers=orcid_headers())
    if data is None:
        return None
    out = []
    for grp in (data.get("group") or []):
        ws = (grp.get("work-summary") or [{}])[0]
        title = (((ws.get("title") or {}).get("title") or {}) or {}).get("value")
        for eid in ((ws.get("external-ids") or {}).get("external-id") or []):
            if eid.get("external-id-type") == "doi":
                doi = (eid.get("external-id-value") or "").strip().lower()
                if doi:
                    out.append([doi, title])
                break
    cache[key] = out
    time.sleep(ORCID_SLEEP)
    return out


# ============================ OpenAlex 侧 ============================
def _norm_doi(d):
    return re.sub(r"^https?://doi\.org/", "", str(d or "").strip().lower())


def openalex_fetch_dois(dois, cache):
    """批量拉 DOI 的作者名单,写进 cache['oa|<doi>'] = [[authorid, name], ...](空表=未找到)。"""
    need = [d for d in dois if f"oa|{d}" not in cache and "|" not in d]
    for i in range(0, len(need), DOI_BATCH):
        batch = need[i:i + DOI_BATCH]
        filt = "doi:" + "|".join(urllib.parse.quote(d, safe="/.-_:") for d in batch)
        params = {"filter": filt, "select": "id,doi,authorships",
                  "per-page": DOI_BATCH, "mailto": MAILTO}
        if OPENALEX_KEY:
            params["api_key"] = OPENALEX_KEY
        url = OA_WORKS + "?" + urllib.parse.urlencode(params, safe=":|/.-_")
        data = _http(url)
        found = set()
        if data:
            for w in data.get("results", []):
                d = _norm_doi(w.get("doi"))
                pairs = [[((a.get("author") or {}).get("id") or "").rsplit("/", 1)[-1],
                          (a.get("author") or {}).get("display_name") or ""]
                         for a in (w.get("authorships") or [])]
                cache[f"oa|{d}"] = pairs
                found.add(d)
        for d in batch:                    # 本批里没返回的 = OpenAlex 里查无此 DOI
            if d not in found:
                cache[f"oa|{d}"] = []
        time.sleep(OA_SLEEP)


def openalex_author_orcids(ids, cache):
    """批量取候选 authorid 在 OpenAlex 上登记的 orcid(仅作附加确认列,不参与定档)。"""
    need = [a for a in ids if f"oaorcid|{a}" not in cache and a]
    for i in range(0, len(need), 50):
        batch = need[i:i + 50]
        params = {"filter": "ids.openalex:" + "|".join(batch),
                  "select": "id,orcid", "per-page": 50, "mailto": MAILTO}
        if OPENALEX_KEY:
            params["api_key"] = OPENALEX_KEY
        url = OA_AUTHORS + "?" + urllib.parse.urlencode(params, safe=":|")
        data = _http(url)
        got = set()
        if data:
            for a in data.get("results", []):
                aid = (a.get("id") or "").rsplit("/", 1)[-1]
                cache[f"oaorcid|{aid}"] = a.get("orcid") or ""
                got.add(aid)
        for a in batch:
            if a not in got:
                cache[f"oaorcid|{a}"] = ""
        time.sleep(OA_SLEEP)


# ============================ 判定 ============================
def _short_orcid(u):
    return re.sub(r"^https?://orcid\.org/", "", str(u or "").strip())


def verdict_for_row(cand, eu_name, dois, cache):
    """在本人的代表作里核对候选 id。返回一堆证据 + 结论。"""
    fam, ini = name_key(eu_name)
    n_name = 0            # 作者名对得上本人、且在 OpenAlex 查到的论文数
    cand_hits = 0        # 其中作者名单含候选 id 的
    from collections import Counter
    dom = Counter()      # 本人各论文对应的同名 author_id 频次 → 找"应得 id"
    for doi, _t in dois[:MAX_WORKS]:
        pairs = cache.get(f"oa|{doi}")
        if not pairs:                     # 未找到 / 空作者
            continue
        # 这篇论文里,名字对得上本人的 author_id(通常恰好 1 个)
        person_ids = [aid for aid, nm in pairs
                      if aid and name_key(nm) == (fam, ini)]
        if not person_ids:
            continue
        n_name += 1
        for aid in set(person_ids):
            dom[aid] += 1
        if cand in person_ids:
            cand_hits += 1
    dom_id, dom_ct = (dom.most_common(1)[0] if dom else (None, 0))
    share = round(cand_hits / n_name, 2) if n_name else 0.0

    if n_name < MIN_EVID:
        verdict, note = "inconclusive", f"可核对代表作仅 {n_name} 篇,证据不足"
    elif cand == dom_id:
        verdict, note = "match", f"命中 {cand_hits}/{n_name} 篇,且是本人主导 id"
    elif cand_hits == 0:
        verdict = "mismatch"
        note = f"候选 0/{n_name} 命中;本人应为 {dom_id}(命中 {dom_ct}/{n_name})"
    else:
        verdict = "review"
        note = f"候选命中 {cand_hits}/{n_name},但主导 id 是 {dom_id}({dom_ct}/{n_name}),疑拆分/同名"
    return dict(n_name_match=n_name, cand_hits=cand_hits, dominant_id=dom_id,
                dominant_share=share, verdict=verdict, note=note)


# ============================ 主流程(逐文件) ============================
def process(path, cache):
    df = pd.read_csv(path, encoding="utf-8-sig", dtype=str)
    if LIMIT:
        df = df.head(LIMIT)
    if not {"eu_name", "authorid"} <= set(df.columns):
        print(f"[跳过] {path} 缺 eu_name/authorid 列")
        return

    # ---- A. ORCID 定位 + 取本人代表作 DOI ----
    rows = []
    all_dois = set()
    for _, r in df.iterrows():
        rec = dict(r)
        eu_name = r["eu_name"]
        cand = r["authorid"]
        eu_inst = None
        if isinstance(r.get("host"), str) and r["host"].strip():
            h = r["host"].strip().lstrip("*").strip()
            eu_inst = parse_host(h)[0] or h        # 去掉 '* ' 和 '[id,CC]' 尾巴
        sp = split_researcher(eu_name)
        if not sp:
            rec.update(orcid="", orcid_conf="badname", verdict="no_orcid",
                       note="姓名无法拆分", n_name_match="", cand_hits="",
                       dominant_id="", dominant_share="", orcid_vs_openalex="")
            rows.append(rec)
            continue
        given, family, _ = sp
        orcid, conf = orcid_find(given, family, eu_inst or "", cache)
        rec["orcid"] = orcid or ""
        rec["orcid_conf"] = conf
        if not orcid:
            rec.update(verdict="no_orcid", note=f"未唯一定位 ORCID({conf})",
                       n_name_match="", cand_hits="", dominant_id="",
                       dominant_share="", orcid_vs_openalex="")
            rows.append(rec)
            continue
        dois = orcid_dois(orcid, cache)
        if dois is None:
            rec.update(verdict="no_orcid", note="ORCID 论文拉取失败", n_name_match="",
                       cand_hits="", dominant_id="", dominant_share="", orcid_vs_openalex="")
            rows.append(rec)
            continue
        rec["_dois"] = dois
        for d, _ in dois[:MAX_WORKS]:
            all_dois.add(d)
        rows.append(rec)
    save_cache(cache)

    # ---- B. 批量拉 OpenAlex 作者名单 + 候选的登记 orcid ----
    print(f"    [OpenAlex] 批量核对 {len(all_dois)} 个唯一 DOI ...")
    openalex_fetch_dois(sorted(all_dois), cache)
    openalex_author_orcids([r["authorid"] for r in rows], cache)
    save_cache(cache)

    # ---- C. 逐行判定 ----
    out = []
    for rec in rows:
        if "_dois" not in rec:            # A 阶段已定(no_orcid 等)
            out.append(rec)
            continue
        cand = rec["authorid"]
        res = verdict_for_row(cand, rec["eu_name"], rec.pop("_dois"), cache)
        rec.update(res)
        # 附加确认:候选在 OpenAlex 登记的 orcid 是否 == 独立找到的
        oa_orcid = _short_orcid(cache.get(f"oaorcid|{cand}", ""))
        mine = _short_orcid(rec["orcid"])
        rec["orcid_vs_openalex"] = ("same" if oa_orcid and oa_orcid == mine
                                    else "diff" if oa_orcid else "oa_none")
        out.append(rec)

    res_df = pd.DataFrame(out)
    lead = [c for c in ["rid", "eu_name", "authorid", "source", "host"] if c in res_df.columns]
    tail = ["orcid", "orcid_conf", "n_name_match", "cand_hits", "dominant_id",
            "dominant_share", "orcid_vs_openalex", "verdict", "note"]
    res_df = res_df[lead + [c for c in tail if c in res_df.columns]
                    + [c for c in res_df.columns if c not in lead + tail]]
    outp = str(Path(path).with_name(Path(path).stem + "_orcid_verified.csv"))
    res_df.to_csv(outp, index=False, encoding="utf-8-sig")

    vc = res_df["verdict"].value_counts().to_dict()
    judged = sum(v for k, v in vc.items() if k in ("match", "mismatch", "review"))
    print(f"[{Path(path).name}] {len(res_df)} 行 -> {Path(outp).name}")
    print(f"    分档: {vc}")
    print(f"    可自动判定(match/mismatch/review) {judged} 行, "
          f"no_orcid/inconclusive 留人工 {len(res_df) - judged} 行")
    return res_df


def main():
    inputs = sys.argv[1:] or DEFAULT_INPUTS
    inputs = [p for p in inputs if Path(p).exists()]
    if not inputs:
        print("没有可处理的输入 CSV(需含列 eu_name, authorid;host 可选)")
        return
    if not OPENALEX_KEY:
        print("[提示] 未设 OPENALEX_API_KEY:免费额度约 100 次/天,行数多会撞限;建议设 key(1000/天)。")
    get_orcid_token()
    cache = load_cache()
    for p in inputs:
        print("=" * 72)
        process(p, cache)
    save_cache(cache)
    print("\n完成。人工只需看 mismatch / review,以及 no_orcid / inconclusive 那几档。")


if __name__ == "__main__":
    main()
