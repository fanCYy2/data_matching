# -*- coding: utf-8 -*-
"""
resolve_host_ids_us.py  ——  US NSF 名单(US.csv)承担机构 -> OpenAlex 机构 id

复用 resolve_host_ids_api.py(EU 版)的在线 API 反查 + 分四档思路, 针对 NSF 名单做适配:

  1) 输入换成 US.csv。机构名在 "Organization" 列, 全部为美国机构(country_code=US),
     不像 EU 那样每格带 "[PIC,国家码]", 也没有 CN 的列错位问题。
     产物与 host_ids_bridge.csv 同构: row_id, host_openalex_id(row_id = 0 起的行号,
     与 us_match.py 的 rid 对齐)。us_match.py 默认就读 us_host_ids_bridge.csv。

  2) 【NSF 法律主体"外壳"清洗】NSF 报的是承担资助的法律/行政实体, 往往不是 OpenAlex
     认得的校名, 需要去壳:
       - 治理机构前缀: "Regents of the University of Michigan - Ann Arbor",
         "Board of Trustees of the ...", "President and Fellows of Harvard College" 等;
       - 基金会/公司后缀: "Georgia Tech Research Corporation", "... Research Foundation",
         "..., Inc." 等;
       - 缩写展开: "Pennsylvania State Univ University Park" 里的 "Univ" -> "University"。
     做法与 CN 版"中科院去前缀"同构:
       - 查询回退链: 原名+US -> 原名去US -> 去壳/展开/校区规整 各写法+US;
       - 复核(classify)时给【查询名】同时生成「原名 key」和「去壳后 key」, 让去壳命中
         的机构也能定为 exact(而非被误降成 weak)。

其余(限速/重试/预算用尽优雅停/缓存自愈/不裸信 top-1 分四档)与 EU/CN 版一致。
产物: us_host_ids_bridge.csv (row_id,host_openalex_id) + us_host_ids_resolved.csv(审计)
"""
import os
import sys
import re
import json
import time
import unicodedata
import urllib.request
import urllib.error
import urllib.parse
from pathlib import Path

import pandas as pd

try:
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
except Exception:
    pass

# ============================ 配置 ============================
US_CSV = "US.csv"
INST_COL = "Organization"          # 承担机构名所在列

OUT_BRIDGE = "us_host_ids_bridge.csv"       # 与 host_ids_bridge.csv 同构(us_match.py 直接读)
OUT_RESOLVED = "us_host_ids_resolved.csv"   # 带 PI/州/单位/分档的审计表
CACHE_PATH = "api_cache_institutions_us.json"

# key 优先读环境变量, 再读同目录 .openalex_api_key 文件
API_KEY = (os.environ.get("OPENALEX_API_KEY", "").strip()
           or (Path(".openalex_api_key").read_text(encoding="utf-8").strip()
               if Path(".openalex_api_key").exists() else ""))
MAILTO = "emiyafancy@gmail.com"
CC = "US"                          # 名单全为美国机构
PER_PAGE = 25

MIN_INTERVAL = 0.15
RETRY = 5
BACKOFF_BASE = 1.5
BUDGET_STOP_THRESHOLD = 120

FUZZY_JACCARD = 0.60
FUZZY_COVER = 0.80

# 人工核定的机构 override(优先级最高): 机构名 -> (OpenAlex id, 规范名)。
# 跑完一轮后, 从 weak 档里挑 API 搜歪的高频机构, 人工查准后钉在这里。初始为空。
OVERRIDES = {
    # "Georgia Tech Research Corporation": ("I130701444", "Georgia Institute of Technology"),
}


def _ov_key(s):
    """override 匹配用的归一 key: 压掉空白/大小写差异, 避免写法差异漏配。"""
    return re.sub(r"\s+", " ", str(s)).strip().lower()


OVERRIDES_NORM = {_ov_key(k): v for k, v in OVERRIDES.items()}

STOP = {"of", "the", "and", "for", "de", "la", "le",
        "des", "du", "di", "der", "und", "el", "at"}

API_BASE = "https://api.openalex.org/institutions"
SELECT = ("id,display_name,display_name_alternatives,"
          "display_name_acronyms,country_code,ror,relevance_score")


class BudgetExhausted(Exception):
    def __init__(self, retry_after):
        self.retry_after = retry_after
        super().__init__(f"daily budget exhausted, resets in ~{retry_after}s")


# ======================= 归一化 helper =======================
def strip_accents(s):
    return "".join(c for c in unicodedata.normalize("NFKD", str(s))
                   if not unicodedata.combining(c))


def norm(s):
    """小写 + '&'->' and ' + 非字母数字变空格 + 压空白 + 去变音。非字符串/空串 -> None。"""
    if not isinstance(s, str):
        return None
    s = strip_accents(s)
    s = re.sub(r"&", " and ", s.lower())
    s = re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE)
    return re.sub(r"\s+", " ", s).strip() or None


# NSF 法律主体外壳: 治理机构前缀(去壳得核心校名)。大小写不敏感, 只削最外层一次。
_PREFIX_RE = re.compile(
    r"^(?:the\s+)?"
    r"(?:board\s+of\s+(?:regents|trustees|governors)\s+of\s+(?:the\s+)?|"
    r"regents\s+of\s+(?:the\s+)?|"
    r"trustees\s+of\s+(?:the\s+)?|"
    r"president\s+and\s+fellows\s+of\s+|"
    r"rector\s+and\s+visitors\s+of\s+(?:the\s+)?|"
    r"curators\s+of\s+(?:the\s+)?)",
    re.IGNORECASE)

# 独立的 leading "The"(非治理前缀那种): 'The Scripps Research Institute' -> 'Scripps ...'。
_LEAD_THE_RE = re.compile(r"^the\s+", re.IGNORECASE)

# 基金会/公司/治理后缀(可叠加, 反复削): "... Research Corporation", "... Board of Trustees",
# "..., Trustees", "... Auxiliary Services Corporation", "... Enterprises", "..., Inc." 等。
_SUFFIX_RE = re.compile(
    r"[\s,]*(?:board\s+of\s+(?:trustees|regents|governors)|trustees|"
    r"research\s+and\s+service\s+foundation|research\s+foundation|"
    r"research\s+corporation|foundation|"
    r"auxiliary\s+services(?:\s+corporation)?|enterprises|inc\.?)\s*$",
    re.IGNORECASE)

# 校区后缀标记(只削 'Campus'/'Main Campus'/'University Park' 标记, 保留城市词, 深一层由截尾兜底)。
_CAMPUS_RE = re.compile(r"[\s,]*(?:main\s+campus|university\s+park|campus)\s*$", re.IGNORECASE)

# " at " 连接词 -> 空格: OpenAlex 规范名多不含 "at", NSF 却常写 "University of X at Y"。
_AT_RE = re.compile(r"\s+at\s+", re.IGNORECASE)

# 缩写整词展开(不误伤 'Universal'/'Restore' 等)。
_ABBR = [(re.compile(r"\bUniv\.?\b", re.IGNORECASE), "University"),
         (re.compile(r"\bTech\b", re.IGNORECASE), "Technology"),
         (re.compile(r"\bInst\b", re.IGNORECASE), "Institute"),
         (re.compile(r"\bRes\b", re.IGNORECASE), "Research")]

_TRAIL_STOP = {"of", "the", "at", "for", "and"}


def _strip_wrapper(name):
    """去掉 leading 'The' + 治理机构前缀 + 基金会/公司/治理后缀, 得到核心校名。
    'Regents of the University of Michigan - Ann Arbor' -> 'University of Michigan - Ann Arbor'
    'Georgia Tech Research Corporation'                 -> 'Georgia Tech'
    'The University of Central Florida Board of Trustees'-> 'University of Central Florida'
    非外壳名原样返回(不误伤)。"""
    s = _LEAD_THE_RE.sub("", _PREFIX_RE.sub("", name).strip()).strip()
    prev = None
    while s and s != prev:                 # 后缀可能叠加(如 'Foundation, Inc.'), 反复削
        prev = s
        s = _SUFFIX_RE.sub("", s).strip().rstrip(",").strip()
    return s or name


def _expand_abbr(name):
    """机构名常见缩写整词展开: Univ->University, Tech->Technology, Inst->Institute, Res->Research。
    'Rochester Institute of Tech' -> 'Rochester Institute of Technology'"""
    for rx, rep in _ABBR:
        name = rx.sub(rep, name)
    return name


def _drop_at(name):
    """' at ' 连接词换空格: 'University of Colorado at Boulder' -> 'University of Colorado Boulder'。"""
    return re.sub(r"\s+", " ", _AT_RE.sub(" ", name)).strip()


def _strip_campus(name):
    """削掉校区后缀标记(保留城市词): 'University of Virginia Main Campus' -> 'University of Virginia',
    'University of Alaska Fairbanks Campus' -> 'University of Alaska Fairbanks'。"""
    return _CAMPUS_RE.sub("", name).strip().rstrip(",").strip()


def _decampus(name):
    """校区分隔符规整成空格: 连字符/' - '/逗号 -> 空格, 让 'X-Campus'/'X, Campus' 词化。
    'University of California-Berkeley' -> 'University of California Berkeley'"""
    return re.sub(r"\s+", " ", re.sub(r"[\-,]", " ", name)).strip()


def _canon(name):
    """一条确定性清洗链(去壳 -> 去at -> 去校区标记 -> 缩写展开)。高置信, 并入 exact-key。
    'Regents of the University of Michigan - Ann Arbor' -> 'University of Michigan - Ann Arbor'
    'Pennsylvania State Univ University Park'           -> 'Pennsylvania State University'"""
    s = _expand_abbr(_strip_campus(_drop_at(_strip_wrapper(name))))
    return re.sub(r"\s+", " ", s).strip()


def _truncations(name):
    """兜底: 从 canon 形去掉末尾 1~2 个 token(裸城市校区), 末词落停用词则继续削。
    'University of Oregon Eugene' -> 'University of Oregon';
    'Rutgers University New Brunswick' -> 'Rutgers University'。仅扩召回, 命中判 fuzzy/weak。"""
    toks = _canon(name).split()
    out = []
    for drop in (1, 2):
        cand = toks[:len(toks) - drop] if len(toks) > drop else []
        while cand and cand[-1].lower() in _TRAIL_STOP:
            cand = cand[:-1]
        if len(cand) >= 2:
            out.append(" ".join(cand))
    return out


def _suny_cuny(name):
    """SUNY/CUNY 校区展开(OpenAlex 命名不统一, 逐个写法试): 'SUNY at Stony Brook' ->
    ['Stony Brook University','SUNY Stony Brook','Stony Brook College','Stony Brook'];
    'CUNY Hunter College' -> ['Hunter College','CUNY Hunter College']。仅扩召回。"""
    out = []
    m = re.match(r"^SUNY(?:\s+College)?\s+at\s+(.+)$", name, re.IGNORECASE)
    if m:
        x = m.group(1).strip()
        out += [f"{x} University", f"SUNY {x}", f"{x} College", x]
    m = re.match(r"^CUNY\s+(.+)$", name, re.IGNORECASE)
    if m:
        x = m.group(1).strip()
        out += [x, f"CUNY {x}"]
    return out


def norm_keys_query(s):
    """【查询名】的归一 key 组: 原名 + 各条确定性清洗(去壳/去at/去校区标记/缩写展开/连字符规整)。
    任一与候选某写法归一相等即判 exact —— 让这些清洗命中也算精确。截尾/SUNY 属启发式,
    不进 exact-key(只扩召回, 命中后由 top-1 相似度定 fuzzy/weak, 供人工复核)。"""
    keys = set()
    for v in (s, _strip_wrapper(s), _canon(s), _expand_abbr(s),
              _drop_at(s), _strip_campus(s), _decampus(s)):
        n = norm(v)
        if n:
            keys.add(n)
    return keys


def norm_keys(s):
    """【候选名】的归一 key(单个写法)。候选各别名/缩写各自算一个 key。"""
    n = norm(s)
    return {n} if n else set()


def tokens(s):
    n = norm(s)
    return frozenset(t for t in n.split() if t not in STOP) if n else frozenset()


# ======================= OpenAlex API 访问层(与 EU/CN 版一致) =======================
_last_call = [0.0]


def _throttle():
    wait = MIN_INTERVAL - (time.time() - _last_call[0])
    if wait > 0:
        time.sleep(wait)
    _last_call[0] = time.time()


def _retry_after(err):
    ra = err.headers.get("Retry-After") if err.headers else None
    if ra and str(ra).isdigit():
        return int(ra)
    try:
        return int(json.loads(err.read().decode()).get("retryAfter", 0))
    except Exception:
        return 0


def api_get(params):
    """一次 GET /institutions。返回 results 列表 | None(本轮失败, 勿缓存);
    额度用尽抛 BudgetExhausted。"""
    p = {**params, "select": SELECT, "per-page": PER_PAGE, "mailto": MAILTO}
    if API_KEY:
        p["api_key"] = API_KEY
    url = API_BASE + "?" + urllib.parse.urlencode(p, safe=":")
    for attempt in range(RETRY):
        _throttle()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": f"us-match/1.0 ({MAILTO})"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read().decode("utf-8")
            if not body.strip():
                raise ValueError("empty body")
            return json.loads(body).get("results", [])
        except urllib.error.HTTPError as e:
            if e.code == 429:
                ra = _retry_after(e)
                if ra > BUDGET_STOP_THRESHOLD:
                    raise BudgetExhausted(ra)
                wait = ra or BACKOFF_BASE ** attempt
                print(f"    [API] 429 短时限流, {wait:.1f}s 后重试({attempt + 1}/{RETRY})")
                time.sleep(wait)
            elif e.code in (500, 502, 503, 504):
                wait = BACKOFF_BASE ** attempt
                print(f"    [API] HTTP {e.code}, {wait:.1f}s 后重试({attempt + 1}/{RETRY})")
                time.sleep(wait)
            else:
                print(f"    [API] HTTP {e.code} 放弃: {url}")
                return None
        except Exception as e:
            wait = BACKOFF_BASE ** attempt
            print(f"    [API] {type(e).__name__}: {e}; {wait:.1f}s 后重试({attempt + 1}/{RETRY})")
            time.sleep(wait)
    return None


# ======================= 缓存 =======================
def load_cache():
    """载入缓存。保留所有条目, 含确认 0 命中的 [](空列表)。
    区别于 EU/CN 老版(丢弃 [] 以自愈"429 被错存成空"的旧 bug): 本版 429/预算用尽已由
    BudgetExhausted 处理、网络失败 net_fail 返 None 不缓存, 故 [] 一定是【真 0 命中】,
    应持久化, 避免每次重跑都把 none 机构的整条回退变体链(~8 次调用)重烧一遍(计费漏)。
    缺席的 key = 从未查过 = pending。"""
    if Path(CACHE_PATH).exists():
        try:
            return json.loads(Path(CACHE_PATH).read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_cache(cache):
    Path(CACHE_PATH).write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")


def _query_variants(name):
    """0 命中时的备用查询写法(有序去重, 仅在真的改变字符串时产生)。
    高置信清洗在前(canon/去壳/去at/去校区/缩写/连字符), 启发式兜底在后(SUNY·CUNY·截尾);
    search 取第一个非空即止。命中后仍由 classify 用原名+清洗形护栏复核。"""
    variants = []

    def add(s):
        s = re.sub(r"\s+", " ", s).strip().rstrip(",").strip()
        if s and s != name and s not in variants:
            variants.append(s)

    add(_canon(name))                    # 1) 确定性清洗链(去壳+去at+去校区标记+缩写展开)
    add(_strip_wrapper(name))            # 2) 仅去壳
    add(_drop_at(name))                  # 3) 仅去 at
    add(_strip_campus(name))             # 4) 仅去校区标记
    add(_expand_abbr(name))              # 5) 仅缩写展开
    add(_decampus(name))                 # 6) 连字符/逗号 -> 空格
    if " - " in name:                    # 7) 取 ' - ' 之前主段: 'X - Campus' -> 'X'
        head = name.split(" - ", 1)[0]
        add(_canon(head))
        add(head)
    for x in _suny_cuny(name):           # 8) SUNY/CUNY 校区展开
        add(x)
    for t in _truncations(name):         # 9) 裸城市校区: 截尾兜底(判 fuzzy/weak)
        add(t)
    return variants


def search_institutions(name, cache):
    """搜一个机构名, 带缓存 + 多级 0 命中回退。
    回退链: 原名+US -> 原名去US -> 各清洗写法+US。
    返回 results(可能空=真0命中) | None(本轮网络失败, 未缓存)。"""
    key = f"{name}|||{CC}"
    if key in cache:
        return cache[key]

    net_fail = [False]

    def attempt(q, use_cc):
        params = {"search": q}
        if use_cc:
            params["filter"] = f"country_code:{CC}"
        r = api_get(params)
        if r is None:
            net_fail[0] = True
        return r

    res = api_get({"search": name, "filter": f"country_code:{CC}"})
    if res:
        cache[key] = res
        return res
    if res is None:
        return None

    r2 = attempt(name, False)           # 去国家过滤
    if r2:
        cache[key] = r2
        return r2

    for q in _query_variants(name):     # 清洗写法(带 US 保精度)
        rv = attempt(q, True)
        if rv:
            cache[key] = rv
            return rv

    if net_fail[0]:
        return None
    cache[key] = []
    return []


# ======================= 分档(不裸信 top-1) =======================
def cand_keys(r):
    names = [r.get("display_name")] + \
            (r.get("display_name_alternatives") or []) + \
            (r.get("display_name_acronyms") or [])
    ks = set()
    for nm in names:
        ks |= norm_keys(nm)
    return ks


def cand_token_sets(r):
    names = [r.get("display_name")] + \
            (r.get("display_name_alternatives") or []) + \
            (r.get("display_name_acronyms") or [])
    return [tokens(nm) for nm in names if nm]


def _id(r):
    return (r.get("id") or "").rsplit("/", 1)[-1] or None


def classify(name, results):
    """返回 (openalex_id, matched_name, method, relevance, ror) 或 None(真无结果)。
    method: exact / fuzzy / weak。查询名用 norm_keys_query(含去壳/展开)。"""
    if not results:
        return None
    qkeys = norm_keys_query(name)
    qtok = tokens(name)

    for r in results:                       # 档1 exact
        if qkeys & cand_keys(r):
            return (_id(r), r.get("display_name"), "exact",
                    r.get("relevance_score"), r.get("ror"))

    top = results[0]                        # 档2/3 看 top-1
    best_j, best_c = 0.0, 0.0
    for tv in cand_token_sets(top):
        if not tv or not qtok:
            continue
        inter = len(qtok & tv)
        j = inter / len(qtok | tv)
        c = inter / len(qtok)
        if j > best_j:
            best_j, best_c = j, c
    method = "fuzzy" if (best_j >= FUZZY_JACCARD and best_c >= FUZZY_COVER) else "weak"
    return (_id(top), top.get("display_name"), method,
            top.get("relevance_score"), top.get("ror"))


# ============================ 主流程 ============================
def main():
    if not API_KEY:
        print("[警告] 未设置 API_KEY(环境变量 OPENALEX_API_KEY 或 .openalex_api_key 文件),")
        print("       免费额度仅 $0.10/天(约100次 search), 619 个机构会很快撞 429。")

    df = pd.read_csv(US_CSV, dtype=str)
    df["row_id"] = df.index
    print(f"[US]   {len(df)} 行 <- {US_CSV}")

    df["inst_name"] = df[INST_COL].map(
        lambda v: v.strip() if isinstance(v, str) and v.strip() else None)
    n_null = int(df["inst_name"].isna().sum())
    if n_null:
        print(f"[空机构] {n_null} 行无 {INST_COL}, host_openalex_id 将留空")

    uniq = sorted(df["inst_name"].dropna().unique())
    cache = load_cache()
    todo = sum(1 for nm in uniq if f"{nm}|||{CC}" not in cache)
    print(f"[去重] {len(uniq)} 个唯一机构; 缓存命中 {len(uniq) - todo}, 需调 API {todo} 个")

    resolved = {}   # name -> 分档结果 | None(真无结果); 缺席 = pending
    stopped = False
    try:
        for i, nm in enumerate(uniq, 1):
            res = search_institutions(nm, cache)
            if res is None:
                continue
            resolved[nm] = classify(nm, res)
            if i % 50 == 0:
                save_cache(cache)
                print(f"    ...已处理 {i}/{len(uniq)}")
    except BudgetExhausted as e:
        stopped = True
        print(f"[停止] 当日额度用尽, 约 {e.retry_after / 3600:.1f} 小时后(UTC零点)重置。"
              f"已解析照常写出, 剩余带 key 重跑续点。")
    save_cache(cache)

    def lookup(row):
        nm = row["inst_name"]
        if pd.isna(nm) or nm is None:
            return pd.Series([None, None, "none", None, None])
        ov = OVERRIDES_NORM.get(_ov_key(nm))       # 人工 override 优先
        if ov:
            return pd.Series([ov[0], ov[1], "override", None, None])
        if nm not in resolved:
            return pd.Series([None, None, "pending", None, None])
        hit = resolved[nm]
        if hit:
            return pd.Series([hit[0], hit[1], hit[2], hit[3], hit[4]])
        return pd.Series([None, None, "none", None, None])

    df[["host_openalex_id", "matched_name", "method", "relevance", "ror"]] = \
        df.apply(lookup, axis=1)

    # 产物1: 与 host_ids_bridge.csv 同构(us_match.py 直接读)
    df[["row_id", "host_openalex_id"]].to_csv(OUT_BRIDGE, index=False, encoding="utf-8-sig")

    # 产物2: 审计表(带 PI/州/单位/分档)
    audit_cols = ["row_id", "AwardNumber", "PrincipalInvestigator", "OrganizationState",
                  "inst_name", "host_openalex_id", "matched_name", "method", "relevance", "ror"]
    df[[c for c in audit_cols if c in df.columns]].to_csv(
        OUT_RESOLVED, index=False, encoding="utf-8-sig")

    ok = int(df["host_openalex_id"].notna().sum())
    vc = df["method"].value_counts()
    print(f"[完成] 解析成功 {ok}/{len(df)} ({ok / len(df) * 100:.1f}%)")
    print("       分档:", {k: int(v) for k, v in vc.items()})
    if stopped or (df["method"] == "pending").any():
        print("       注意: 有 pending 行(本轮没查成), 补额度/明天重跑续上。")
    print(f"       -> {OUT_BRIDGE} (row_id,host_openalex_id)")
    print(f"       -> {OUT_RESOLVED} (weak/none/pending 建议人工复核)")


if __name__ == "__main__":
    main()
