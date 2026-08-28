# -*- coding: utf-8 -*-
"""中文人名 → 罗马化(拼音)候选变体生成 —— 中国学者姓名匹配的开源库核心。

数据集(OpenAlex/SciSciNet)里中国学者的 display_name 绝大多数是【罗马化拼音】,同一个人
会以多种书写面貌出现(实测 陈永川):
    Yongchuan Chen / Chen Yong-chuan / Chen Yongchuan / Yong-chuan Chen / Yongchuan. Chen
还有少数直接是中文 `陈永川`。本模块只做一件事:把一个中文名展开成上述各种【罗马化面貌】的
归一化字符串集合(外加中文原名本身),交给 cn_match.py 用既有的 norm()/fl()/wset() 宏去 join。

开源库:pypinyin(https://github.com/mozillazg/python-pinyin,MIT)。它能正确读【复姓】
(欧阳/尉迟/长孙…),但对部分【单字多音姓】会给错(单→dan 应 shan、曾→ceng 应 zeng、
仇→chou 应 qiu、区→qu 应 ou、查→cha 应 zha…),故对这些姓做覆盖,并【同时保留】pypinyin
默认读音作为变体(作者本人若按误读罗马化也能召回)。
"""

import re

from pypinyin import pinyin, lazy_pinyin, Style

__all__ = ["romanize", "split_name", "chinese_form", "COMPOUND_SURNAMES", "SURNAME_PINYIN"]

# 常见复姓(两字姓)。只用来决定"姓/名"的切分点;拼音本身仍交给 pypinyin(它读复姓是对的)。
COMPOUND_SURNAMES = {
    "欧阳", "太史", "端木", "上官", "司马", "东方", "独孤", "南宫", "万俟", "闻人",
    "夏侯", "诸葛", "尉迟", "公羊", "赫连", "澹台", "皇甫", "宗政", "濮阳", "公冶",
    "太叔", "申屠", "公孙", "慕容", "仲孙", "钟离", "长孙", "宇文", "司徒", "鲜于",
    "司空", "闾丘", "子车", "亓官", "司寇", "巫马", "公西", "颛孙", "壤驷", "公良",
    "漆雕", "乐正", "宰父", "谷梁", "拓跋", "夹谷", "轩辕", "令狐", "段干", "百里",
    "呼延", "东郭", "南门", "羊舌", "微生", "公户", "公玉", "公仪", "梁丘", "公仲",
    "公上", "公门", "公山", "公坚", "左丘", "公伯", "西门", "第五", "东门", "南荣",
}

# 单字多音姓:姓氏正读(可能多个) —— pypinyin 默认读音会另行补上,不放这里。
# 参考通行的《百家姓多音字》整理,保留最常见的姓氏读音。
SURNAME_PINYIN = {
    "单": ["shan"],          # 单(shàn)雄信,而非 dān
    "曾": ["zeng"],          # 曾(zēng),而非 céng
    "仇": ["qiu"],           # 仇(qiú),而非 chóu
    "区": ["ou"],            # 区(ōu),而非 qū
    "查": ["zha"],           # 查(zhā),而非 chá
    "解": ["xie"],           # 解(xiè)
    "覃": ["qin", "tan"],    # 覃(qín/tán)
    "华": ["hua"],           # 华(huà 姓 / huá)
    "任": ["ren"],           # 任(rén)
    "燕": ["yan"],           # 燕(yān)
    "乐": ["yue", "le"],     # 乐(yuè)正 / 乐(lè)
    "种": ["chong"],         # 种(chóng)
    "折": ["she"],           # 折(shé)
    "员": ["yun"],           # 员(yùn)
    "冼": ["xian"],          # 冼(xiǎn)
    "缪": ["miao", "miu"],   # 缪(miào/miù)
    "隗": ["kui", "wei"],    # 隗(kuí/wěi)
    "都": ["du"],            # 都(dū)
    "阚": ["kan"],           # 阚(kàn)
    "过": ["guo"],           # 过(guō)
    "繁": ["pi", "fan"],     # 繁(pí)姓
    "召": ["shao"],          # 召(shào)
    "谌": ["shen", "chen"],  # 谌(shèn/chén)
    "尉": ["wei"],           # 尉(wèi)—非复姓尉迟时
    "朴": ["piao"],          # 朴(piáo,朝鲜族姓)
    "翟": ["zhai", "di"],    # 翟(zhái)
    "郗": ["xi", "chi"],     # 郗(xī)
    "秘": ["bi"],            # 秘(bì)
    "убий": [],              # (占位,防误编辑)
}
SURNAME_PINYIN.pop("убий", None)


def _syllable_variants(s):
    """一个拼音音节 → 其 ASCII 罗马化【变体集合】。pypinyin 的 Style.NORMAL 用 `v` 表示 ü
    (吕→lv、略→lve、女→nv),而数据集里 ü 姓/名的罗马化 u/v/yu 三式都有(吕→Lu/Lv/Lyu),
    故含 v 的音节同时给三式。其余音节只有一式。"""
    s = "".join(c for c in s.lower() if "a" <= c <= "z")   # 保留字母(v 先留着)
    if "v" in s:
        return {s.replace("v", "u"), s, s.replace("v", "yu")}
    return {s}


def _clean_cn(name):
    """只保留汉字(丢掉空格/点/英文/括注),用于切分与拼音。"""
    return "".join(c for c in str(name) if "一" <= c <= "鿿")


def split_name(name):
    """中文名 → (姓, 名)。两字复姓在 COMPOUND_SURNAMES 里则取两字姓,否则默认单字姓。
    单字名(共两字)→ 姓 1 字、名 1 字;空/无汉字 → (原串, '')。"""
    han = _clean_cn(name)
    if len(han) >= 3 and han[:2] in COMPOUND_SURNAMES:
        return han[:2], han[2:]
    if len(han) >= 2:
        return han[:1], han[1:]
    return han, ""


def _surname_readings(surname):
    """姓的罗马化候选(可能多个)。复姓:concat + spaced 两式(pypinyin 读音);
    单字姓:覆盖表读音 ∪ pypinyin 默认读音。"""
    reads = []
    if len(surname) >= 2:                       # 复姓:pypinyin 逐字(ü 变体做笛卡尔积)
        per = [sorted(_syllable_variants(x)) for x in lazy_pinyin(surname)]
        combos = [[]]
        for cand in per:
            combos = [c + [s] for c in combos for s in cand]
        for c in combos:
            reads.append("".join(c))            # "ouyang"
            reads.append(" ".join(c))           # "ou yang"
    else:                                        # 单字姓
        for r in SURNAME_PINYIN.get(surname, []):
            reads.extend(_syllable_variants(r))
        reads.extend(_syllable_variants(lazy_pinyin(surname)[0]))   # pypinyin 默认(兜底/误读)
    out = []
    for r in reads:                              # 去重保序
        if r and r not in out:
            out.append(r)
    return out


def _given_syllable_lists(given, heteronym, cap):
    """名的音节读法组合。heteronym=True 时对多音字做(有上限的)笛卡尔积。
    返回 list[list[str]],每个内层是一种读法的逐音节列表(已 ASCII 化)。"""
    if not given:
        return []
    per_char = pinyin(given, style=Style.NORMAL, heteronym=heteronym, errors="ignore")
    per_char = [sorted({v for x in cand for v in _syllable_variants(x)}) or [""]
                for cand in per_char]
    combos = [[]]
    for cand in per_char:
        combos = [c + [s] for c in combos for s in cand]
        if len(combos) > cap:                    # 截断:多音字过多时不爆炸
            combos = combos[:cap]
    return [c for c in combos if all(c)]


def romanize(name, heteronym=False, cap=8, initials=True):
    """中文名 → 罗马化归一化变体集合(小写、无分音符)。覆盖数据集里实际出现的书写面貌:
        给定名 concat/spaced × 姓,以及 姓在前/名在前 两种词序;可选首字母缩写式。
    这些字符串直接喂给 cn_match 的 norm()/wset()/fl() 宏。空名 → 空集。

    少数民族/音译名(含间隔号 ·,如 哈木拉提·吾甫尔):· 就是词边界,不能按汉姓切分。
    按 · 分段、每段整体拼音,给两种词序(Hamulati Wufuer / Wufuer Hamulati)。"""
    raw = str(name)
    if "·" in raw or "•" in raw or "･" in raw:
        segs = [_clean_cn(s) for s in re.split(r"[·•･]", raw)]
        segs = ["".join(next(iter(_syllable_variants(x))) for x in lazy_pinyin(s))
                for s in segs if s]
        out = set()
        if len(segs) >= 2:
            out.add(" ".join(segs))
            out.add(" ".join(reversed(segs)))
        elif segs:
            out.add(segs[0])
        return out

    surname, given = split_name(name)
    if not surname:
        return set()
    s_reads = _surname_readings(surname)
    g_lists = _given_syllable_lists(given, heteronym, cap)
    out = set()

    if not g_lists:                              # 只有姓(极少):给单读
        for sr in s_reads:
            out.add(sr)
        return out

    for syl in g_lists:
        g_concat = "".join(syl)                  # "yongchuan"
        g_spaced = " ".join(syl)                 # "yong chuan"
        for sr in s_reads:
            out.add(f"{g_concat} {sr}")          # 名+姓(西式):concat 名
            out.add(f"{sr} {g_concat}")          # 姓+名(中式):concat 名
            out.add(f"{g_spaced} {sr}")          # 名+姓:分写名
            out.add(f"{sr} {g_spaced}")          # 姓+名:分写名
        if initials:
            gi1 = syl[0][0]                      # 首字缩写:"y"
            gia = "".join(s[0] for s in syl)     # 全缩写(连写):"fr"
            gia_sp = " ".join(s[0] for s in syl)  # 全缩写(分写):"f r"(数据集里 "F. R. Xu" 常见)
            for sr in s_reads:
                out.add(f"{gi1} {sr}")           # "y chen"
                out.add(f"{sr} {gi1}")           # "chen y"
                if gia != gi1:
                    out.add(f"{gia} {sr}")       # "fr xu"
                    out.add(f"{sr} {gia}")       # "xu fr"
                    out.add(f"{gia_sp} {sr}")    # "f r xu"
                    out.add(f"{sr} {gia_sp}")    # "xu f r"
    return out


def chinese_form(name):
    """中文名的纯汉字形式(用于 中文↔中文 直配);无汉字 → None。"""
    han = _clean_cn(name)
    return han or None
