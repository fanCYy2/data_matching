# -*- coding: utf-8 -*-
"""
resolve_host_ids_us_since1990.py  ——  US NSF 名单(us_since1990.csv)承担机构 -> OpenAlex 机构 id

与 us/resolve_host_ids_us.py 同一套思路, 但把【在线 API 反查】换成【本地 affiliations.duckdb 反查】:

  1) 输入换成 us_since1990.csv(比旧 US.csv 更全)。机构名仍在 "Organization" 列, 全部为
     美国机构。产物与 us_host_ids_bridge.csv 同构: row_id, host_openalex_id(row_id = 0 起
     的行号, 与 us_match.py 的 rid 对齐)。

  2) 匹配后端换成 affiliations.duckdb(表 affiliations: institution_id/display_name/ror/
     country_code/type/h_index/productivity/...)。相比 OpenAlex API 有两点差异:
       - 只有 display_name 一个官方名, 没有 display_name_alternatives / _acronyms。故 exact
         档只能对 display_name 归一后比对(别名召回靠"查询名多写法"来补, 见下)。
       - 没有 API 的 relevance_score / search 排序。改为本地【token 倒排索引 + Jaccard/cover】
         自建召回与打分, 平局用 productivity / h_index 兜底(优先规模大的正牌机构, 避免撞到
         同名小医院/小公司)。

  3) 【NSF 法律主体"外壳"清洗】沿用 us 版那套(治理机构前缀 / 基金会·公司后缀 / 缩写展开 /
     校区规整 / SUNY·CUNY 展开 / 截尾兜底), 一字未改 —— 这些写法既进 exact-key, 也进 fuzzy
     的候选查询 token 组, 让去壳命中也能定为 exact/fuzzy 而非被误降成 weak。

  分四档同 us 版: override / exact / fuzzy / weak(+ none 真无候选)。weak 仍写出 top-1 猜测
  id(供 us_match.py 先用), 但在审计表里标 weak 并附 jaccard/cover, 复核后钉进 OVERRIDES。

产物:
  us_since1990_host_ids_bridge.csv   (row_id, host_openalex_id)  —— us_match.py 直接读
  us_since1990_host_ids_resolved.csv (审计: PI/州/单位/分档/相似度/type/productivity/ror)
"""
import re
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path

import duckdb
import pandas as pd

try:
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
except Exception:
    pass

# ============================ 配置 ============================
US_CSV = "us_since1990.csv"
INST_COL = "Organization"          # 承担机构名所在列
DUCKDB_PATH = "affiliations.duckdb"
# 候选池国家码: 名单全为美国受资助机构, 但 OpenAlex 把美属领地单列国家码(PR/VI/GU/AS/MP),
# 只取 "US" 会把波多黎各大学等整体排除 -> 它们会错配到本土同名机构(实测 4 个 UPR 校区全被
# 判给佛州的 "Polytechnic University of Puerto Rico Miami", 其中 3 个还落在高置信 fuzzy 档)。
CC = ("US", "PR", "VI", "GU", "AS", "MP")

OUT_BRIDGE = "us_since1990_host_ids_bridge.csv"      # 与 host_ids_bridge.csv 同构
OUT_RESOLVED = "us_since1990_host_ids_resolved.csv"  # 带 PI/州/单位/分档的审计表

# 复用: 旧 US 名单(US.csv)已解析好的机构映射(含人工核定的 human 档)。
# us_since1990.csv 与旧名单机构名高度重叠, 直接复用旧表的高置信档(exact/fuzzy/human/override),
# 既省重算也把 API 兜底候选从 ~140 砍到 ~40 —— "不浪费"。旧表缺失/none 的再往下走。
PRIOR_RESOLVED = "us/us_host_ids_resolved.csv"
PRIOR_GOOD = {"exact", "fuzzy", "human", "override"}   # 旧表里可直接信的档

# API 兜底: 只对 duckdb + 旧表都拿不出高置信的机构, 才在线反查 OpenAlex(沿用 us 版整套逻辑)。
# 复用缓存, 无 key/额度用尽都优雅降级(退回 duckdb 的 top-1 弱猜)。
USE_API_FALLBACK = True
US_API_MODULE = "us/resolve_host_ids_us.py"
API_CACHE_PATH = "api_cache_institutions_us.json"      # 兜底查询缓存(复用/自建)

FUZZY_JACCARD = 0.60
FUZZY_COVER = 0.80

# 人工核定的机构 override(优先级最高): 机构名 -> (OpenAlex id, 规范名)。
# id 写 None 表示【本地库确实没有该实体, 显式判 none】—— 宁可留空也不要挂一个错 host:
# 错 host 会在层2 放行同名的错人, 留空只是把该行让给层3/层4 裁决。
#
# 下面 38 条来自第一轮 weak 档(40 个机构)的逐个人工查准: 先在 affiliations.duckdb 里按关键词
# 检索正解, 再结合该奖的州/PI 判定。剩下 2 个(University of Puerto Rico-Rio Piedras 等)在候选池
# 纳入美属领地后自动命中, 不必钉。
OVERRIDES = {
    # --- NSF 法律主体 / 辅助机构外壳(去壳后本地库无该壳名, 只能人工指到本体校) ---
    "Ohio State University Research Foundation -DO NOT USE": ("I52357470", "The Ohio State University"),
    "The University Corporation, Northridge": ("I157638225", "California State University, Northridge"),
    "University Corporation at Monterey Bay": ("I135369504", "California State University, Monterey Bay"),
    "CSU Fullerton Auxiliary Services Corporation": ("I142934699", "California State University, Fullerton"),
    "California State L A University Auxiliary Services Inc.": ("I27825529", "California State University Los Angeles"),
    "CSUB Auxiliary for Sponsored Programs Administration": ("I118839592", "California State University, Bakersfield"),
    "CAL POLY HUMBOLDT SPONSORED PROGRAMS FOUNDATION": ("I192389796", "Humboldt State University"),
    "University Enterprises, Incorporated": ("I43522216", "California State University, Sacramento"),  # 萨克拉门托州立的辅助公司(奖在 CA)
    "Cornell Univ - State: AWDS MADE PRIOR MAY 2010": ("I205783295", "Cornell University"),
    "University of Wisconsin General Administration Office": ("I135310074", "University of Wisconsin–Madison"),  # PI 在 Madison

    # --- 学院/医学院/分部: 本地库只收总校(或收了但写法不同) ---
    "Washington University School of Medicine": ("I204465549", "Washington University in St. Louis"),  # 弱猜误判成 University of Washington
    "Teachers College, Columbia University": ("I78577930", "Columbia University"),                     # 弱猜误判成 Columbia College Chicago
    "Joan and Sanford I. Weill Medical College of Cornell University": ("I4387153466", "Weill Cornell Medicine"),
    "Sloan Kettering Institute For Cancer Research": ("I1334819555", "Memorial Sloan Kettering Cancer Center"),
    "Alfred University NY State College of Ceramics": ("I49502546", "Alfred University"),              # 弱猜误判成 Alfred State College
    "University of Miami School of Medicine": ("I145608581", "University of Miami"),
    "Wake Forest University School of Medicine": ("I47251452", "Wake Forest University"),
    "University of New Mexico Health Sciences Center": ("I169521973", "University of New Mexico"),
    "University of Tennessee Institute of Agriculture": ("I75027704", "University of Tennessee at Knoxville"),
    "College of William & Mary Virginia Institute of Marine Science": ("I16285277", "William & Mary"),
    "Rutgers University Newark": ("I102322142", "Rutgers, The State University of New Jersey"),        # 弱猜误判成 University Hospital, Newark
    "Rutgers University Camden": ("I102322142", "Rutgers, The State University of New Jersey"),
    "University of Missouri-Saint Louis": ("I208333798", "University of Missouri–St. Louis"),           # 弱猜误判成 Saint Louis University
    "University of Colorado at Denver-Downtown Campus": ("I921990950", "University of Colorado Denver"),

    # --- 改名 / 并校: 指到现名或承继实体 ---
    "Saint Olaf College": ("I24861097", "St. Olaf College"),
    "Saint Bonaventure University": ("I117309725", "St. Bonaventure University"),                       # 弱猜误判成 Saint Louis University
    "Otterbein College": ("I197312921", "Otterbein University"),
    "Fred Hutchinson Cancer Research Center": ("I4210089486", "Fred Hutch Cancer Center"),
    "FRED HUTCHINSON CANCER CENTER": ("I4210089486", "Fred Hutch Cancer Center"),
    "Polytechnic University of New York": ("I57206974", "New York University"),                         # 2014 并入 NYU(Tandon)
    "Oregon Graduate Institute of Science & Technology": ("I165690674", "Oregon Health & Science University"),  # 2001 并入 OHSU
    "The J. David Gladstone Institutes": ("I1321430492", "Gladstone Institutes"),
    "Marine Environmental Sciences Consortium": ("I2902024922", "Dauphin Island Sea Lab"),              # MESC 即 DISL 的运营联合体(奖在 AL)
    "Carnegie Institute": ("I1331487863", "Carnegie Museum of Natural History"),                        # PI 为古生物学家(奖在 PA), 非 Carnegie Institution for Science
    "W1FB US MILITARY ACADEMY": ("I192545095", "United States Military Academy"),

    # --- 第二轮: duckdb 判成 fuzzy 但其实错的(token 相同 != 同一机构, 靠奖的州/PI 查证) ---
    # 教训: fuzzy 档的 jaccard=1.0 只说明词集相同, 同名异地机构照样满分 —— 高置信档也要抽查。
    "Washington University": ("I204465549", "Washington University in St. Louis"),          # 106 行; 误配 University of Washington(奖在 MO)
    "Purdue University": ("I219193219", "Purdue University West Lafayette"),                # 233 行; 误配 Purdue University System(主校产出更多)
    "University of Kansas Center for Research Inc": ("I146416000", "University of Kansas"),  # 59 行; 误配 KU Medical Center(KUCR 是 Lawrence 主校研究臂)
    "CUNY City College": ("I125687163", "City College of New York"),                        # 36 行; 误配佛州营利校 "City College"(citycollege.edu)
    "CUNY York College": ("I150397245", "York College, City University of New York"),       # 误配 York College of Pennsylvania
    "SUNY College at Oswego": ("I43742981", "State University of New York at Oswego"),      # 误配 SUNY College of Optometry
    "Miami University Middletown": ("I83328450", "Miami University"),                        # 俄亥俄 Miami University, 非 University of Miami
    "University of St. Thomas": ("I161515732", "University of St. Thomas - Minnesota"),     # 奖在 MN; 误配佛州 St. Thomas University
    "St Joseph's University": ("I51077184", "Saint Joseph's University"),                    # 误配 St. Joseph's Hospital
    "Cal Poly Pomona Foundation, Inc.": ("I98947143", "California State Polytechnic University"),  # cpp.edu; 误配 SLO 的辅助公司 Cal Poly Corporation
    "Rehabilitation Institute of Chicago": ("I1324242722", "Shirley Ryan AbilityLab"),       # RIC 2017 改名; 误配 Kessler(NJ)
    "Nevada System of Higher Education, Desert Research Institute": ("I60138030", "Desert Research Institute"),
    "University of Connecticut Health Center": ("I75929689", "UConn Health"),
    "The University of Texas at Brownsville": ("I2802326326", "The University of Texas Rio Grande Valley"),  # 2015 并入 UTRGV; 误配 UT Austin
    "University of Maryland Biotechnology Institute": ("I66946132", "University of Maryland, College Park"),  # UMBI 已撤销, PI 现属 College Park

    # --- affiliations.duckdb 里没有、但在线 OpenAlex 有(API 兜底查得, 多为新建的 I44xxx 号) ---
    "Insurance Institute for Business & Home Safety": ("I4405258494", "Insurance Institute for Business & Home Safety"),
    "The Forsyth Institute": ("I4405268108", "The Forsyth Institute"),
    "School for American Research": ("I2801014352", "School for Advanced Research"),   # NM, 机构现名

    # --- 本地库与在线 API 都查不到: 显式置空, 不挂错 host ---
    "TERC Inc": (None, None),
    "QEM Network HQ": (None, None),
}


def _ov_key(s):
    """override 匹配用的归一 key: 压掉空白/大小写差异, 避免写法差异漏配。"""
    return re.sub(r"\s+", " ", str(s)).strip().lower()


OVERRIDES_NORM = {_ov_key(k): v for k, v in OVERRIDES.items()}

STOP = {"of", "the", "and", "for", "de", "la", "le",
        "des", "du", "di", "der", "und", "el", "at"}


# ======================= 归一化 helper(与 us 版一致) =======================
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
    任一与候选 display_name 归一相等即判 exact —— 让这些清洗命中也算精确。截尾/SUNY 属启发式,
    不进 exact-key(只扩召回, 命中后由 top-1 相似度定 fuzzy/weak, 供人工复核)。"""
    keys = set()
    for v in (s, _strip_wrapper(s), _canon(s), _expand_abbr(s),
              _drop_at(s), _strip_campus(s), _decampus(s)):
        n = norm(v)
        if n:
            keys.add(n)
    return keys


def tokens(s):
    n = norm(s)
    return frozenset(t for t in n.split() if t not in STOP) if n else frozenset()


def qtok_variants(name):
    """fuzzy 用的【查询名】token 组: 原名 + 确定性清洗形 + 截尾/SUNY·CUNY 启发式形。
    每个非空 token 组各算一次召回, 取全体最优(镜像 us 版 _query_variants 的回退写法链)。"""
    forms = {name, _canon(name), _strip_wrapper(name), _drop_at(name), _decampus(name)}
    forms |= set(_truncations(name))
    forms |= set(_suny_cuny(name))
    out = []
    for f in forms:
        t = tokens(f)
        if t:
            out.append(t)
    return out


# ======================= 本地 affiliations.duckdb 候选库 =======================
class Candidates:
    """把 affiliations 表(US 子集)载进内存, 建三套索引:
       key2idx: norm(display_name) -> [cand idx]   —— exact 档
       inv:     token -> {cand idx}                 —— fuzzy 倒排召回
       cands[idx] = (institution_id, display_name, ror, type, productivity, h_index, token_set)
    平局(同名/同相似度)一律用 (productivity, h_index) 兜底, 偏向规模大的正牌机构。"""

    def __init__(self, db_path, cc):
        ccs = [cc] if isinstance(cc, str) else list(cc)
        con = duckdb.connect(db_path, read_only=True)
        rows = con.execute(
            "SELECT institution_id, display_name, ror, type, "
            "COALESCE(productivity,0), COALESCE(h_index,0) "
            f"FROM affiliations WHERE country_code IN ({','.join('?' * len(ccs))}) "
            "AND display_name IS NOT NULL",
            ccs).fetchall()
        con.close()

        self.cands = []
        self.key2idx = defaultdict(list)
        self.inv = defaultdict(set)
        for iid, dn, ror, typ, prod, h in rows:
            idx = len(self.cands)
            tk = tokens(dn)
            self.cands.append((iid, dn, ror, typ, int(prod), int(h), tk))
            k = norm(dn)
            if k:
                self.key2idx[k].append(idx)
            for t in tk:
                self.inv[t].add(idx)
        self.n = len(self.cands)

    def _rank(self, idx):
        """平局排序键: productivity 优先, 再 h_index。"""
        c = self.cands[idx]
        return (c[4], c[5])

    def exact(self, qkeys):
        """qkeys 命中任一候选 display_name 归一 key -> 取 productivity 最大的候选。"""
        hits = []
        for k in qkeys:
            hits.extend(self.key2idx.get(k, ()))
        if not hits:
            return None
        return max(hits, key=self._rank)

    def fuzzy(self, name):
        """token 倒排召回 + Jaccard/cover 打分, 返回 (idx, jaccard, cover) | None。
        多写法查询组取全体最优; 平局用 (jaccard, cover, productivity, h_index)。"""
        best = None       # (jaccard, cover, prod, h, idx)
        for qt in qtok_variants(name):
            cset = set()
            for t in qt:
                cset.update(self.inv.get(t, ()))
            for idx in cset:
                tv = self.cands[idx][6]
                inter = len(qt & tv)
                if not inter:
                    continue
                j = inter / len(qt | tv)
                c = inter / len(qt)
                key = (j, c, self.cands[idx][4], self.cands[idx][5])
                if best is None or key > best[:4]:
                    best = (j, c, self.cands[idx][4], self.cands[idx][5], idx)
        if best is None:
            return None
        return (best[4], best[0], best[1])


def classify(name, cands):
    """返回 (institution_id, matched_name, method, jaccard, cover, type, productivity, ror)
    或 None(真无候选)。method: exact / fuzzy / weak。查询名用 norm_keys_query(含去壳/展开)。"""
    idx = cands.exact(norm_keys_query(name))
    if idx is not None:
        c = cands.cands[idx]
        return (c[0], c[1], "exact", 1.0, 1.0, c[3], c[4], c[2])

    fz = cands.fuzzy(name)
    if fz is None:
        return None
    idx, j, cov = fz
    c = cands.cands[idx]
    method = "fuzzy" if (j >= FUZZY_JACCARD and cov >= FUZZY_COVER) else "weak"
    return (c[0], c[1], method, round(j, 3), round(cov, 3), c[3], c[4], c[2])


# ======================= 复用: 旧 US 映射 =======================
def load_prior():
    """载入旧 US.csv 已解析好的映射: _ov_key(inst_name) -> (id, matched_name, method, ror)。
    只保留高置信档(PRIOR_GOOD)供直接复用; 旧表 none/weak 不复用(留给 duckdb/API 重判)。"""
    p = Path(PRIOR_RESOLVED)
    if not p.exists():
        print(f"[复用] 未找到 {PRIOR_RESOLVED}, 跳过旧映射复用")
        return {}
    pr = pd.read_csv(p, dtype=str)
    out = {}
    for _, r in pr.iterrows():
        nm = r.get("inst_name")
        mth = r.get("method")
        hid = r.get("host_openalex_id")
        if not isinstance(nm, str) or not nm.strip():
            continue
        if mth not in PRIOR_GOOD or not isinstance(hid, str) or not hid.strip():
            continue
        out.setdefault(_ov_key(nm), (hid.strip(), r.get("matched_name"), mth, r.get("ror")))
    print(f"[复用] 旧表高置信映射 {len(out)} 个机构 <- {PRIOR_RESOLVED}")
    return out


# ======================= API 兜底(沿用 us 版整套在线反查) =======================
class ApiFallback:
    """按需从 us/resolve_host_ids_us.py 加载在线反查逻辑, 只对 duckdb+旧表都不确定的机构调用。
    复用其缓存/限速/分档; 无 key/额度用尽/加载失败一律优雅降级(disabled)。"""

    def __init__(self):
        self.A = None
        self.cache = {}
        self.exhausted = False
        self.n_calls = 0
        if not USE_API_FALLBACK:
            return
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location("_us_api", US_API_MODULE)
            A = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(A)
            A.CACHE_PATH = API_CACHE_PATH          # 用本脚本的缓存文件
            self.A = A
            self.cache = A.load_cache()
            if not A.API_KEY:
                print("[API兜底] 未设 OPENALEX_API_KEY/.openalex_api_key, 免费额度极少, 可能很快 429")
            print(f"[API兜底] 已就绪(缓存命中 {len(self.cache)} 条)")
        except Exception as e:
            print(f"[API兜底] 加载失败({type(e).__name__}: {e}), 跳过, 仅用 duckdb+旧表")
            self.A = None

    @property
    def active(self):
        return self.A is not None and not self.exhausted

    def resolve(self, name):
        """返回 (id, matched_name, method, ror) 或 None。method: exact/fuzzy/weak。"""
        if not self.active:
            return None
        try:
            res = self.A.search_institutions(name, self.cache)
            self.n_calls += 1
        except self.A.BudgetExhausted as e:
            self.exhausted = True
            print(f"[API兜底] 当日额度用尽(约 {e.retry_after / 3600:.1f}h 后重置), 停止在线兜底")
            return None
        if res is None:
            return None
        hit = self.A.classify(name, res)           # (id, name, method, relevance, ror)
        if hit is None:
            return None
        return (hit[0], hit[1], hit[2], hit[4])

    def save(self):
        if self.A is not None:
            self.A.save_cache(self.cache)


# ============================ 三层解析: duckdb -> 旧表 -> API ============================
def resolve_one(nm, cands, prior, api):
    """对一个机构名给出最终记录:
       (id, matched_name, method, source, jaccard, cover, type, productivity, ror)
    优先级: OVERRIDES > 旧表高置信 > duckdb 高置信(exact/fuzzy) > API 高置信 > 弱猜(duckdb>API)。"""
    ov = OVERRIDES_NORM.get(_ov_key(nm))                       # 0) 人工 override
    if ov:
        if ov[0] is None:                                      # 人工确认本地库无该实体: 显式留空
            return (None, None, "none", "manual", None, None, None, None, None)
        return (ov[0], ov[1], "override", "manual", None, None, None, None, None)

    pr = prior.get(_ov_key(nm))                                # 1) 复用旧表高置信
    if pr:
        return (pr[0], pr[1], pr[2], "prior", None, None, None, None, pr[3])

    dk = classify(nm, cands)                                   # 2) duckdb 本地
    if dk and dk[2] in ("exact", "fuzzy"):
        return (dk[0], dk[1], dk[2], "duckdb", dk[3], dk[4], dk[5], dk[6], dk[7])

    if api is not None and api.active:                         # 3) API 兜底(只查不确定的)
        ap = api.resolve(nm)
        if ap and ap[2] in ("exact", "fuzzy"):
            return (ap[0], ap[1], ap[2], "api", None, None, None, None, ap[3])

    if dk:                                                     # 4) 弱猜: 优先 duckdb top-1
        return (dk[0], dk[1], "weak", "duckdb", dk[3], dk[4], dk[5], dk[6], dk[7])
    return (None, None, "none", "none", None, None, None, None, None)


# ============================ 主流程 ============================
def main():
    if not Path(DUCKDB_PATH).exists():
        sys.exit(f"[错误] 找不到 {DUCKDB_PATH}")

    df = pd.read_csv(US_CSV, dtype=str)
    df["row_id"] = df.index
    print(f"[US]   {len(df)} 行 <- {US_CSV}")

    df["inst_name"] = df[INST_COL].map(
        lambda v: v.strip() if isinstance(v, str) and v.strip() else None)
    n_null = int(df["inst_name"].isna().sum())
    if n_null:
        print(f"[空机构] {n_null} 行无 {INST_COL}, host_openalex_id 将留空")

    cands = Candidates(DUCKDB_PATH, CC)
    print(f"[候选库] {cands.n} 个 {CC} 机构 <- {DUCKDB_PATH}"
          f"(唯一归一名 {len(cands.key2idx)})")

    prior = load_prior()
    api = ApiFallback()

    uniq = sorted(df["inst_name"].dropna().unique())
    print(f"[去重] {len(uniq)} 个唯一机构, 三层解析中(duckdb -> 旧表 -> API)...")

    resolved = {}       # name -> 记录元组
    for i, nm in enumerate(uniq, 1):
        resolved[nm] = resolve_one(nm, cands, prior, api)
        if api.active and i % 50 == 0:
            api.save()
    api.save()
    if api.A is not None:
        print(f"[API兜底] 本轮在线调用 {api.n_calls} 个机构")

    NULL = (None, None, "none", "none", None, None, None, None, None)

    def lookup(row):
        nm = row["inst_name"]
        if pd.isna(nm) or nm is None:
            return pd.Series(list(NULL))
        return pd.Series(list(resolved.get(nm, NULL)))

    cols = ["host_openalex_id", "matched_name", "method", "source",
            "jaccard", "cover", "type", "productivity", "ror"]
    df[cols] = df.apply(lookup, axis=1)

    # 产物1: 与 host_ids_bridge.csv 同构(us_match.py 直接读)
    df[["row_id", "host_openalex_id"]].to_csv(OUT_BRIDGE, index=False, encoding="utf-8-sig")

    # 产物2: 审计表(带 PI/州/单位/分档/来源/相似度)
    audit_cols = ["row_id", "AwardNumber", "PrincipalInvestigator", "OrganizationState",
                  "inst_name", "host_openalex_id", "matched_name", "method", "source",
                  "jaccard", "cover", "type", "productivity", "ror"]
    df[[c for c in audit_cols if c in df.columns]].to_csv(
        OUT_RESOLVED, index=False, encoding="utf-8-sig")

    ok = int(df["host_openalex_id"].notna().sum())
    vc = df["method"].value_counts()
    # 唯一机构口径(不受各机构行数加权)
    um = pd.Series([resolved[nm][2] for nm in uniq])
    src = pd.Series([resolved[nm][3] for nm in uniq])
    print(f"[完成] 行级解析成功 {ok}/{len(df)} ({ok / len(df) * 100:.1f}%)")
    print("       行级分档:", {k: int(v) for k, v in vc.items()})
    print(f"       机构级分档({len(uniq)}):", {k: int(v) for k, v in um.value_counts().items()})
    print("       机构级来源:", {k: int(v) for k, v in src.value_counts().items()})
    high = int((um.isin(["exact", "fuzzy", "override"])).sum())
    print(f"       机构级高置信 {high}/{len(uniq)} ({high / len(uniq) * 100:.1f}%)")
    print(f"       -> {OUT_BRIDGE} (row_id,host_openalex_id)")
    print(f"       -> {OUT_RESOLVED} (weak/none 建议人工复核, 查准后钉 OVERRIDES)")


if __name__ == "__main__":
    main()
