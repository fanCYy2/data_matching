# -*- coding: utf-8 -*-
"""
resolve_host_ids_cn.py  ——  中国学者名单(cn.xlsx)依托单位 -> OpenAlex 机构 id

复用 resolve_host_ids_api.py 的在线 API 反查 + 分档思路, 针对中文名单做三处适配:

  1) 输入换成 cn.xlsx。列: 序号 / 姓名 / 依托单位 / 研究领域 / 年度 / (第6列)author id。
     产物和 host_ids_bridge.csv 同构: row_id, host_openalex_id(row_id = 0 起的行号)。

  2) 【列错位修正】2019、2020 两个年度的「依托单位」与「研究领域」整列互换了
     (依托单位里装的是研究方向, 研究领域里才是单位)。已核实: 这两年 研究领域列
     100% 像机构、依托单位列 0% 像机构; 其余年份反之。故按年度取真正的机构列。
     1994-1996 早期行两列皆空 -> 无机构可解析, host_openalex_id 留空。

  3) 【中文查询回退/复核】OpenAlex 的 /institutions?search 已索引中文规范名+别名,
     "北京大学" 这类直接命中。但中科院所往往需去掉「中国科学院」前缀才搜得到
     (如 中国科学院大连化学物理研究所 -> 大连化学物理研究所 才有结果), 校区括号
     "中国地质大学(武汉)" 也需把括号规整成空格再搜。因此:
       - 查询回退链: 原名+CN -> 原名去CN -> 中科院去前缀 -> 括号规整/去括号;
       - 复核(classify)时给【查询名】同时生成「原名 key」和「去中科院前缀 key」,
         这样去前缀命中的所也能定为 exact(而非被误降成 weak)。

其余(限速/重试/预算用尽优雅停/缓存自愈/不裸信 top-1 分四档)与 EU 版一致。
产物: cn_host_ids_bridge.csv (row_id,host_openalex_id) + cn_host_ids_resolved.csv(审计)
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
CN_XLSX = "cn.xlsx"
INST_COL = "依托单位"          # 正常年份的机构列
FIELD_COL = "研究领域"         # 正常年份的研究方向列(2019/2020 与上列互换)
YEAR_COL = "年度"
SWAP_YEARS = {2019, 2020}     # 这两年 依托单位/研究领域 整列互换

OUT_BRIDGE = "cn_host_ids_bridge.csv"       # 与 host_ids_bridge.csv 同构
OUT_RESOLVED = "cn_host_ids_resolved.csv"   # 带姓名/单位/分档的审计表
CACHE_PATH = "api_cache_institutions_cn.json"

# key 优先读环境变量, 再读同目录 .openalex_api_key 文件
API_KEY = (os.environ.get("OPENALEX_API_KEY", "").strip()
           or (Path(".openalex_api_key").read_text(encoding="utf-8").strip()
               if Path(".openalex_api_key").exists() else ""))
MAILTO = "emiyafancy@gmail.com"
CC = "CN"                      # 名单全为中国机构
PER_PAGE = 25

MIN_INTERVAL = 0.15
RETRY = 5
BACKOFF_BASE = 1.5
BUDGET_STOP_THRESHOLD = 120

FUZZY_JACCARD = 0.60
FUZZY_COVER = 0.80

# 人工核定的机构 override(优先级最高): 机构名 -> (OpenAlex id, 规范名)。
# 用于 API 把同名/近名所搜歪的少数高频机构(见 weak 档), 人工查准后钉死。
OVERRIDES = {
    "中国科学院金属研究所":   ("I4210126304", "Institute of Metal Research"),
    "中国科学院自动化研究所": ("I4210112150", "Institute of Automation"),
    "中国地质大学(武汉)":     ("I3124059619", "China University of Geosciences"),
    "中国科学院地球化学研究所": ("I4210135735", "Institute of Geochemistry"),
}


def _ov_key(s):
    """override 匹配用的归一 key: 统一全/半角括号, 去空白。避免括号写法差异漏配。"""
    return re.sub(r"\s+", "", str(s)).replace("（", "(").replace("）", ")")


OVERRIDES_NORM = {_ov_key(k): v for k, v in OVERRIDES.items()}

STOP = {"of", "the", "and", "for", "de", "la", "le",
        "des", "du", "di", "der", "und", "el"}

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
    """小写 + '&'->' and ' + 非字母数字(含 CJK 之外)变空格 + 压空白。
    re.UNICODE 下 \\w 含中日韩汉字, 所以中文字符会保留, 括号/标点变空格。"""
    if not isinstance(s, str):
        return None
    s = re.sub(r"&", " and ", s.lower())
    s = re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE)
    return re.sub(r"\s+", " ", s).strip() or None


def _strip_cas_prefix(name):
    """去掉「中国科学院」及紧随的「、X部/委/局」等前缀, 得到所名核心。
    '中国科学院大连化学物理研究所' -> '大连化学物理研究所'
    '中国科学院、水利部成都山地灾害与环境研究所' -> '成都山地灾害与环境研究所'
    非中科院开头的名字原样返回(不误伤)。"""
    if not name.startswith("中国科学院"):
        return name
    core = name[len("中国科学院"):]
    core = re.sub(r"^[、，]\s*", "", core)
    core = re.sub(r"^[一-龥]{2,4}[部委局]\s*", "", core)  # 联合承担的部委
    return core.strip() or name


def _depar(name):
    """括号(全/半角)内容前后加空格并去括号: '中国地质大学(武汉)' -> '中国地质大学 武汉'。"""
    return re.sub(r"\s+", " ",
                  re.sub(r"[\(（]([^)）]*)[\)）]", r" \1 ", name)).strip()


def norm_keys_query(s):
    """【查询名】的归一 key 组: 原名 + 去中科院前缀 + 括号规整 + 去括号。
    任一与候选的某写法归一相等即判 exact —— 让去前缀/校区命中也算精确。"""
    keys = set()
    for v in (s, _strip_cas_prefix(s), _depar(s),
              re.sub(r"[\(（][^)）]*[\)）]", "", s)):
        n = norm(v)
        if n:
            keys.add(n)
    return keys


def norm_keys(s):
    """【候选名】的归一 key(单个写法)。候选中英别名各自算一个 key。"""
    n = norm(s)
    return {n} if n else set()


def tokens(s):
    n = norm(s)
    return frozenset(t for t in n.split() if t not in STOP) if n else frozenset()


# ======================= OpenAlex API 访问层(与 EU 版一致) =======================
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
            req = urllib.request.Request(url, headers={"User-Agent": f"cn-match/1.0 ({MAILTO})"})
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
    if Path(CACHE_PATH).exists():
        try:
            raw = json.loads(Path(CACHE_PATH).read_text(encoding="utf-8"))
            return {k: v for k, v in raw.items() if v}
        except Exception:
            return {}
    return {}


def save_cache(cache):
    Path(CACHE_PATH).write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")


def _query_variants(name):
    """0 命中时的备用查询写法(有序去重, 仅在真的改变字符串时产生)。
    中科院去前缀 / 括号规整 / 去括号 —— 命中后仍由 classify 用原名护栏复核。"""
    variants = []

    def add(s):
        s = re.sub(r"\s+", " ", s).strip().rstrip("、，,").strip()
        if s and s != name and s not in variants:
            variants.append(s)

    add(_strip_cas_prefix(name))
    add(_depar(name))
    add(re.sub(r"[\(（][^)）]*[\)）]", "", name))   # 直接去掉校区括号
    return variants


def search_institutions(name, cache):
    """搜一个机构名, 带缓存 + 多级 0 命中回退。
    回退链: 原名+CN -> 原名去CN -> 各清洗写法+CN。
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

    for q in _query_variants(name):     # 清洗写法(带 CN 保精度)
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
    method: exact / fuzzy / weak。查询名用 norm_keys_query(含去中科院前缀)。"""
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


# ============================ 机构列(修正错位) ============================
def pick_institution(row):
    """按年度取真正的机构名。2019/2020 两列互换 -> 取 研究领域 列; 否则取 依托单位 列。
    返回 (机构名 or None, 来源列名)。"""
    try:
        yr = int(row[YEAR_COL])
    except (ValueError, TypeError):
        yr = None
    src = FIELD_COL if yr in SWAP_YEARS else INST_COL
    val = row[src]
    if isinstance(val, str) and val.strip():
        return val.strip(), src
    return None, src


# ============================ 主流程 ============================
def main():
    if not API_KEY:
        print("[警告] 未设置 API_KEY(环境变量 OPENALEX_API_KEY 或 .openalex_api_key 文件),")
        print("       免费额度仅 $0.10/天(约100次 search), 会很快撞 429。")

    df = pd.read_excel(CN_XLSX)
    df["row_id"] = df.index
    print(f"[CN]   {len(df)} 行 <- {CN_XLSX}")

    picked = df.apply(pick_institution, axis=1)
    df["inst_name"] = [p[0] for p in picked]
    df["inst_src"] = [p[1] for p in picked]
    n_swap = int((df["inst_src"] == FIELD_COL).sum())
    n_null = int(df["inst_name"].isna().sum())
    print(f"[列修正] 2019/2020 取『{FIELD_COL}』列作机构名: {n_swap} 行; 其余取『{INST_COL}』列")
    print(f"[空机构] {n_null} 行无依托单位(早期年份), host_openalex_id 将留空")

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

    # 产物1: 与 host_ids_bridge.csv 同构
    df[["row_id", "host_openalex_id"]].to_csv(OUT_BRIDGE, index=False, encoding="utf-8-sig")

    # 产物2: 审计表(带姓名/单位/来源列/分档)
    audit_cols = ["row_id", "序号", "姓名", "年度", "inst_name", "inst_src",
                  "host_openalex_id", "matched_name", "method", "relevance", "ror"]
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
