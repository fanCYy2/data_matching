# -*- coding: utf-8 -*-
"""
resolve_host_ids_api.py  ——  直接调 OpenAlex 在线 API 反查机构 id(替代 S3 快照方案)

======================== 总体思路 ========================
输入: EU.xlsx 里每一行的 "Host Institution(s)" 列, 形如
        "Delft University of Technology [999977366,NL]"
输出: 每行对应的 OpenAlex 机构 id(如 I98358874), 写成 CSV。

和旧的 resolve_host_ids.py 的区别:
  旧: 先用 build_institution_dict.py 从 S3 快照自建一本别名字典, 再本地
      手写归一化 + 模糊匹配去查这本字典。
  新(本脚本): 不建字典, 把机构名直接丢给 OpenAlex 的
      /institutions?search=... , 让官方相关性排序帮我们选。

为什么 API 方案通常更靠谱、也更省心:
  - search 内部已经索引了 规范名 + 别名(多语言) + 缩写, 等于官方替我们
    做掉了最难的那部分归一化;
  - 数据是实时的, 不会像快照那样过期。

======================== 计费与 API key(重要) ========================
OpenAlex 现在按额度计费(2026), search 每次 $0.001:
    - 不带 key: 免费预算 $0.10/天  ≈ 100 次 search   (很快撞 429)
    - 带免费 key: 免费预算 $1/天    ≈ 1000 次 search  (足够一次跑完 647 个)
  所以务必注册免费 key(openalex.org -> settings), 填到 API_KEY 或设环境变量
  OPENALEX_API_KEY。647 个唯一机构 ≈ $0.65, 落在 $1 免费额度内, 实际 $0。

======================== 稳健性设计(踩过的坑) ========================
  - 429 分两种, 必须区分:
      * 短时限流(Retry-After 小): 睡一下重试;
      * 当日预算用尽(Retry-After 到 UTC 零点, 上万秒): 立即停, 别傻睡 9 小时,
        已完成的照常写出, 剩下的下次/明天带 key 续跑(缓存续点)。
  - 失败 != 空结果: 重试耗尽的瞬断/超时返回 None(本轮未解析), 绝不当成 []
    写进缓存, 否则会变成"永久假阴性"。只有 HTTP 200 的结果(哪怕是真的 0 命中
    的空列表)才进缓存。
  - 缓存自愈: 载入时丢弃"空值"条目(旧版把 429 失败错存成了空), 重新查一遍;
    真结果(非空)保留, 省掉重复调用。
  - search 是"全词 AND"、对变音敏感、按母语规范名索引; EU 表里的英文译名 / oe 转写
    / 括号缩写 / 带城市后缀常导致 0 命中(假阴性)。0 命中时按序换写法重搜(回退链):
    去国家过滤 -> 去括号(CNRS) -> oe→ö 还原变音 -> 取 ' - ' 前主段; 每步命中后仍用
    【原名】过 classify 复核, 只扩召回、不降可信度。
  - 不裸信 top-1, 用"归一 key 命中"复核, 分四档置信:
        exact  查询名归一后 == 候选的某个写法(规范名/别名/缩写)   最可信
        fuzzy  词集合 Jaccard>=0.6 且 覆盖率>=0.8                  较可信
        weak   API 给了 top-1 但没过上面阈值, 记下来但需人工看
        none   API 一条都没搜到
        pending 本轮因预算/网络没查成(不是无结果), 补额度后重跑即可
    人工只需复核 weak / pending / none 少数几条, 而非全部 4000 行。

产物列: row_id, host_openalex_id, matched_name, method, relevance, ror
=========================================================
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

# Windows 控制台默认 gbk, 打印中文/机构名会报错; line_buffering 让后台跑也能实时看进度
try:
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
except Exception:
    pass

# ============================ 配置 ============================
EU_XLSX = "EU.xlsx"                    # EU 源表(和本脚本同目录)
HOST_COL = "Host Institution(s)"       # 机构名所在列
OUT_CSV = "host_ids_resolved_api.csv"  # 输出文件
CACHE_PATH = "api_cache_institutions.json"  # API 结果本地缓存(按 名字|国家 存)

# ↓↓↓ 把你在 openalex.org 注册后拿到的 key 填这里(或设环境变量 OPENALEX_API_KEY) ↓↓↓
API_KEY = os.environ.get("OPENALEX_API_KEY", "").strip() or ""
MAILTO = "emiyafancy@gmail.com"        # 进"礼貌池", 更稳定
PER_PAGE = 25                          # 每次搜索取回多少候选(够找到精确写法即可)

# 限速: 两次请求最小间隔(秒), ~6.7/s, 压在限额内留余量
MIN_INTERVAL = 0.15
# 重试: 瞬断/5xx/短时429 的退避次数与基础等待秒
RETRY = 5
BACKOFF_BASE = 1.5
# 429 的 Retry-After 超过这个秒数, 判定为"当日预算用尽", 直接停而不是傻睡
BUDGET_STOP_THRESHOLD = 120

# 模糊匹配阈值(与旧流水线一致)
FUZZY_JACCARD = 0.60
FUZZY_COVER = 0.80

# EU 表里的国家码 -> OpenAlex 使用的国家码(英国 UK->GB, 希腊 EL->GR)
CC_MAP = {"UK": "GB", "EL": "GR"}

# 停用词: 做词集合匹配时剔除, 避免 "of/the/de" 等干扰
STOP = {"of", "the", "and", "for", "de", "la", "le",
        "des", "du", "di", "der", "und", "el"}

API_BASE = "https://api.openalex.org/institutions"
SELECT = ("id,display_name,display_name_alternatives,"
          "display_name_acronyms,country_code,ror,relevance_score")


class BudgetExhausted(Exception):
    """当日免费/预付额度用尽(429 且 Retry-After 到 UTC 零点)。用于立即停跑。"""
    def __init__(self, retry_after):
        self.retry_after = retry_after
        super().__init__(f"daily budget exhausted, resets in ~{retry_after}s")


# ======================= 归一化 helper(与旧脚本一致) =======================
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


def norm_keys(s):
    """为一个名字生成一【组】归一 key(基础形 + 德语变音几种转写 + 去变音), 任一相等即视为同名。"""
    n = norm(s)
    if not n:
        return set()
    keys = {n}
    de = n
    for a, b in [("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")]:
        de = de.replace(a, b)
    keys.add(de)
    keys.add(re.sub(r"(ae|oe|ue)", lambda m: m.group(0)[0], de))
    keys.add(strip_accents(n))
    return {k for k in keys if k}


def tokens(s):
    """把名字拆成【去停用词后的词集合】(frozenset), 供模糊匹配用(词序无关)。"""
    n = norm(s)
    return frozenset(t for t in n.split() if t not in STOP) if n else frozenset()


def parse_host(raw):
    """把 Host 单元格拆成 (主承担机构名, 国家码)。

    单机构: 'Delft University of Technology [999977366,NL]' -> ('Delft University of Technology','NL')
    多机构: 'Erlangen-Nuremberg [999995408,DE], University of Groningen'
            -> ('University of Erlangen-Nuremberg','DE')  # 取带 PIC 的第一个(主承担方)

    做法: 找第一个 '[数字,两位国家码]', 它前面的部分就是主机构名, 这个方括号里的
          国家码就是主机构国家。这样单机构/多机构一格都能正确取到主承担方,
          也不会被名字中间的括号(如 '(ETH Zurich)')误伤。
    没有合法方括号时国家码为 None(整格当机构名)。
    """
    if not isinstance(raw, str):
        return None, None
    m = re.search(r"\[\d+,([A-Z]{2})\]", raw)
    if m:
        cc = m.group(1)
        name = raw[:m.start()].strip().rstrip(",").strip()
    else:
        cc, name = None, raw.strip()
    return name or None, cc


# ======================= OpenAlex API 访问层 =======================
_last_call = [0.0]   # 用列表当可变闭包变量, 记录上次请求时刻, 做限速


def _throttle():
    """两次请求间至少间隔 MIN_INTERVAL 秒, 把速率压在限额内。"""
    wait = MIN_INTERVAL - (time.time() - _last_call[0])
    if wait > 0:
        time.sleep(wait)
    _last_call[0] = time.time()


def _retry_after(err):
    """从 429 响应里取 Retry-After 秒数(优先 header, 兜底 body 里的 retryAfter)。"""
    ra = err.headers.get("Retry-After") if err.headers else None
    if ra and str(ra).isdigit():
        return int(ra)
    try:
        return int(json.loads(err.read().decode()).get("retryAfter", 0))
    except Exception:
        return 0


def api_get(params):
    """向 /institutions 发一次 GET。

    返回: results 列表(HTTP 200, 可能为空=真的没搜到)
          None    (瞬断/5xx/短时429 重试耗尽 = 本轮失败, 调用方切勿缓存)
    抛出: BudgetExhausted(当日额度用尽, 立即停跑)
    """
    p = {**params, "select": SELECT, "per-page": PER_PAGE, "mailto": MAILTO}
    if API_KEY:
        p["api_key"] = API_KEY
    url = API_BASE + "?" + urllib.parse.urlencode(p, safe=":")
    for attempt in range(RETRY):
        _throttle()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": f"eu-match/1.0 ({MAILTO})"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read().decode("utf-8")
            if not body.strip():
                raise ValueError("empty body")
            return json.loads(body).get("results", [])
        except urllib.error.HTTPError as e:
            if e.code == 429:
                ra = _retry_after(e)
                if ra > BUDGET_STOP_THRESHOLD:      # 到 UTC 零点才恢复 = 预算用尽
                    raise BudgetExhausted(ra)
                wait = ra or BACKOFF_BASE ** attempt  # 短时限流: 睡一下再试
                print(f"    [API] 429 短时限流, {wait:.1f}s 后重试({attempt + 1}/{RETRY})")
                time.sleep(wait)
            elif e.code in (500, 502, 503, 504):
                wait = BACKOFF_BASE ** attempt
                print(f"    [API] HTTP {e.code}, {wait:.1f}s 后重试({attempt + 1}/{RETRY})")
                time.sleep(wait)
            else:                                    # 400 等: 请求本身有问题, 放弃
                print(f"    [API] HTTP {e.code} 放弃: {url}")
                return None
        except Exception as e:
            wait = BACKOFF_BASE ** attempt
            print(f"    [API] {type(e).__name__}: {e}; {wait:.1f}s 后重试({attempt + 1}/{RETRY})")
            time.sleep(wait)
    return None   # 重试耗尽 = 本轮失败(不缓存)


# ======================= 缓存 =======================
def load_cache():
    """载入缓存, 并丢弃空值条目(旧版把 429 失败错存成空 -> 让它们重查, 自愈)。"""
    if Path(CACHE_PATH).exists():
        try:
            raw = json.loads(Path(CACHE_PATH).read_text(encoding="utf-8"))
            return {k: v for k, v in raw.items() if v}   # 只保留有结果的
        except Exception:
            return {}
    return {}


def save_cache(cache):
    Path(CACHE_PATH).write_text(
        json.dumps(cache, ensure_ascii=False), encoding="utf-8")


def _restore_umlaut(s):
    """oe->ö, ue->ü, ae->ä(含首字母大写形)。OpenAlex 按母语规范名索引且对变音敏感,
    EU 表里的 'Goettingen/Koeln/Tuebingen' 需还原成 'Göttingen/Köln/Tübingen' 才搜得到。"""
    for a, b in [("oe", "ö"), ("ue", "ü"), ("ae", "ä")]:
        s = s.replace(a, b).replace(a.capitalize(), b.upper())
    return s


def _query_variants(name):
    """给一个 0 命中的机构名, 生成【有序去重】的备用查询写法(只在会改变字符串时才产生)。

    仅用于扩大召回; 命中后仍由 classify 用【原始名】做身份复核当护栏, 所以放宽写法不会
    降低最终可信度(至多落到 weak, 会被人工复核拎出来)。
    """
    variants = []

    def add(s):
        s = re.sub(r"\s+", " ", s).strip().rstrip(",").strip()
        if s and s != name and s not in variants:
            variants.append(s)

    no_paren = re.sub(r"\s*\([^)]*\)", "", name)   # 1) 去括号缩写: 'X (CNRS)' -> 'X'
    add(no_paren)
    add(_restore_umlaut(name))                     # 2) oe→ö 还原变音
    add(_restore_umlaut(no_paren))                 #    去括号 + 还原变音 组合
    if " - " in name:                              # 3) 取 ' - ' 之前主段: 'Inst - City' -> 'Inst'
        head = name.split(" - ", 1)[0]
        add(head)
        add(_restore_umlaut(head))
    return variants


def search_institutions(name, cc_oa, cache):
    """搜一个 (机构名, 归一国家码), 带缓存 + 多级 0命中回退。

    回退链(第一个非空即止): 原名+国家 -> 原名去国家 -> 各清洗写法+国家(见 _query_variants)。
    返回: results 列表(可能为空=真 0 命中) | None(本轮失败/网络瞬断, 未缓存, 留 pending)。
    只有 HTTP 200 的结果才写缓存; 网络失败绝不缓存成空, 避免"永久假阴性"。
    """
    key = f"{name}|||{cc_oa}"
    if key in cache:                 # load_cache 已保证缓存里都是有结果的
        return cache[key]

    net_fail = [False]               # 记录中途是否发生过网络失败, 用于区分 pending vs 真0命中

    def attempt(q, use_cc):
        params = {"search": q}
        if use_cc and cc_oa:
            params["filter"] = f"country_code:{cc_oa}"
        r = api_get(params)          # 可能抛 BudgetExhausted
        if r is None:
            net_fail[0] = True
        return r

    res = api_get({"search": name, **({"filter": f"country_code:{cc_oa}"} if cc_oa else {})})
    if res:                          # 主查命中
        cache[key] = res
        return res
    if res is None:
        return None                  # 主查网络失败 -> pending(不缓存)

    if cc_oa:                        # 原名去掉国家过滤回退
        r2 = attempt(name, False)
        if r2:
            cache[key] = r2
            return r2

    for q in _query_variants(name):  # 清洗写法逐个试(仍带国家过滤, 保精度)
        rv = attempt(q, True)
        if rv:
            cache[key] = rv
            return rv

    if net_fail[0]:                  # 全 0 命中但中途有网络失败 -> 判 pending, 不缓存空
        return None
    cache[key] = []                  # 确认真 0 命中(空列表也缓存)
    return []


# ======================= 结果分档(不裸信 top-1) =======================
def cand_keys(r):
    """一个候选机构的所有写法(规范名+别名+缩写)的归一 key 并集。"""
    names = [r.get("display_name")] + \
            (r.get("display_name_alternatives") or []) + \
            (r.get("display_name_acronyms") or [])
    ks = set()
    for nm in names:
        ks |= norm_keys(nm)
    return ks


def cand_token_sets(r):
    """候选机构各写法的词集合列表(模糊匹配时逐写法取最高分)。"""
    names = [r.get("display_name")] + \
            (r.get("display_name_alternatives") or []) + \
            (r.get("display_name_acronyms") or [])
    return [tokens(nm) for nm in names if nm]


def _id(r):
    """'https://openalex.org/I35440088' -> 'I35440088'。"""
    return (r.get("id") or "").rsplit("/", 1)[-1] or None


def classify(name, results):
    """把 API 候选定档: 返回 (openalex_id, matched_name, method, relevance, ror) 或 None(真无结果)。

    method: exact / fuzzy / weak。results 为空(真 0 命中)返回 None -> 由调用方记 none。
    """
    if not results:
        return None
    qkeys = norm_keys(name)
    qtok = tokens(name)

    # 档1: exact —— 查询名归一后命中候选的某个写法; 结果按相关性排序, 取第一个 exact
    for r in results:
        if qkeys & cand_keys(r):
            return (_id(r), r.get("display_name"), "exact",
                    r.get("relevance_score"), r.get("ror"))

    # 档2/3: 没有精确命中, 看 top-1(相关性最高)
    top = results[0]
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
        print("[警告] 未设置 API_KEY: 当前只有 $0.10/天(约100次)免费额度, 647 个会撞 429。")
        print("       建议去 openalex.org 注册拿 key, 填到脚本 API_KEY 或设 OPENALEX_API_KEY。")

    # 1) 读 EU 表; row_id = 行号(从0开始), 与下游按行对齐
    eu = pd.read_excel(EU_XLSX)
    eu["row_id"] = eu.index
    print(f"[EU]   {len(eu)} 行 <- {EU_XLSX}")

    # 2) 每行拆出 (主承担机构名, 国家码)
    parsed = eu[HOST_COL].map(parse_host)
    eu["inst_name"] = [p[0] for p in parsed]
    eu["cc"] = [p[1] for p in parsed]

    # 3) 对 (机构名,国家码) 去重, 每个唯一组合只调一次 API
    uniq = eu[["inst_name", "cc"]].dropna().drop_duplicates()
    cache = load_cache()
    todo = sum(1 for r in uniq.itertuples(index=False)
               if f"{r.inst_name}|||{CC_MAP.get(r.cc, r.cc)}" not in cache)
    print(f"[去重] {len(uniq)} 个唯一机构; 缓存命中 {len(uniq) - todo}, 需调 API {todo} 个")

    # 4) 逐个解析; 预算用尽则优雅停下, 已完成的照常写出
    resolved = {}   # (name, cc) -> 分档结果元组 | None(真无结果)。缺席 = pending(没查成)
    stopped = False
    try:
        for i, r in enumerate(uniq.itertuples(index=False), 1):
            cc_oa = CC_MAP.get(r.cc, r.cc)
            res = search_institutions(r.inst_name, cc_oa, cache)
            if res is None:                       # 本轮失败(网络), 留 pending
                continue
            resolved[(r.inst_name, r.cc)] = classify(r.inst_name, res)
            if i % 50 == 0:
                save_cache(cache)
                print(f"    ...已处理 {i}/{len(uniq)}")
    except BudgetExhausted as e:
        stopped = True
        h = e.retry_after / 3600
        print(f"[停止] 当日额度用尽, 约 {h:.1f} 小时后(UTC零点)重置。"
              f"已解析的照常写出, 剩余下次带 key 重跑即可(缓存续点)。")
    save_cache(cache)

    # 5) 按行映射回全部行
    #    命中分档 -> 用其结果; 真 0 命中 -> none; 没查成(缺席) -> pending
    def lookup(row):
        if pd.isna(row.inst_name):
            return pd.Series([None, None, "none", None, None])
        k = (row.inst_name, row.cc)
        if k not in resolved:
            return pd.Series([None, None, "pending", None, None])
        hit = resolved[k]
        if hit:
            return pd.Series([hit[0], hit[1], hit[2], hit[3], hit[4]])
        return pd.Series([None, None, "none", None, None])

    eu[["host_openalex_id", "matched_name", "method", "relevance", "ror"]] = \
        eu.apply(lookup, axis=1)

    # 6) 写出
    out = eu[["row_id", "host_openalex_id", "matched_name", "method", "relevance", "ror"]]
    out.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")

    ok = out["host_openalex_id"].notna().sum()
    vc = out["method"].value_counts()
    print(f"[完成] 解析成功 {ok}/{len(out)} ({ok / len(out) * 100:.1f}%)")
    print("       分档:", {k: int(v) for k, v in vc.items()})
    if stopped or (out["method"] == "pending").any():
        print("       注意: 有 pending 行(本轮没查成, 非无结果), 补额度/明天重跑即可续上。")
    print(f"       -> {OUT_CSV}  (weak/none/pending 建议人工复核)")


if __name__ == "__main__":
    main()
