# -*- coding: utf-8 -*-
"""
make_verify_sample.py —— 从 host_ids_bridge.csv 随机抽 800 条做人工准确性复核

产物: verify_sample_800.csv, 每行包含人工判断所需的全部上下文:
  - EU 原始 Host 单元格 (host_raw) 与解析出的主机构名/国家 (inst_name, cc)
  - 桥接给出的 OpenAlex id (host_openalex_id) 及可点击链接 (openalex_url)
  - 匹配到的机构规范名 (matched_name)、分档 (method)、相关性 (relevance)、ror
  - 两列留空: verdict(填 对/错/存疑)、note(备注)

抽样口径: 只从"桥接实际给了 id"的行里抽(none/pending 空分配不在准确性复核范围内)。
随机种子固定(42)以便复现。
"""
import json
import time
import urllib.request
import urllib.parse

import pandas as pd

from resolve_host_ids_api import parse_host  # 复用同一套 Host 解析逻辑

SEED = 42
N = 800
EU_XLSX = "EU.xlsx"
HOST_COL = "Host Institution(s)"
BRIDGE = "host_ids_bridge.csv"
RESOLVED = "host_ids_resolved_api.csv"
OUT = "verify_sample_800.csv"

# 1) EU 原表: row_id + 原始 Host 文本 + 解析出的 主机构名/国家
eu = pd.read_excel(EU_XLSX)
eu["row_id"] = eu.index
parsed = eu[HOST_COL].map(parse_host)
eu["inst_name"] = [p[0] for p in parsed]
eu["cc"] = [p[1] for p in parsed]
eu = eu.rename(columns={HOST_COL: "host_raw"})[["row_id", "host_raw", "inst_name", "cc"]]

# 2) 桥接 (row_id -> host_openalex_id)
bridge = pd.read_csv(BRIDGE)

# 3) 解析结果里的名称/分档/证据列
resolved = pd.read_csv(RESOLVED)[
    ["row_id", "matched_name", "method", "relevance", "ror"]
]

# 4) 合并
df = eu.merge(bridge, on="row_id", how="left").merge(resolved, on="row_id", how="left")

# 5) 只抽有 id 的行(桥接真正断言了一个匹配)
has_id = df[df["host_openalex_id"].notna()].copy()
print(f"[总计] 桥接行 {len(df)}, 有 id 的 {len(has_id)}, 无 id(none/pending) {len(df) - len(has_id)}")

n = min(N, len(has_id))
sample = has_id.sample(n=n, random_state=SEED).sort_values("row_id")

# 6) 有些行的 id 来自 data_match.py 而非 API 解析(method=none), matched_name 为空。
#    直接按 id 从 OpenAlex 拉规范名 + 国家, 让人工不必逐个点链接。
def fetch_names(ids):
    """按 openalex id 批量取 (display_name, country_code); 失败的留空。"""
    out = {}
    for i in range(0, len(ids), 50):
        batch = ids[i:i + 50]
        flt = "openalex:" + "|".join(batch)
        url = ("https://api.openalex.org/institutions?"
               + urllib.parse.urlencode({
                   "filter": flt,
                   "select": "id,display_name,country_code",
                   "per-page": 50,
                   "mailto": "emiyafancy@gmail.com",
               }, safe=":|"))
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "eu-verify/1.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            for r in data.get("results", []):
                oaid = (r.get("id") or "").rsplit("/", 1)[-1]
                out[oaid] = (r.get("display_name"), r.get("country_code"))
        except Exception as e:
            print(f"    [warn] 取名失败({flt[:40]}...): {type(e).__name__}: {e}")
        time.sleep(0.2)
    return out


missing = sample[sample["matched_name"].isna()]
if len(missing):
    ids = sorted(set(missing["host_openalex_id"].astype(str)))
    print(f"[补名] {len(missing)} 行无 matched_name(id 来自 data_match), 按 {len(ids)} 个 id 联网补规范名...")
    names = fetch_names(ids)
    fill = sample["host_openalex_id"].astype(str).map(lambda x: (names.get(x) or (None, None))[0])
    sample["matched_name"] = sample["matched_name"].fillna(fill)
    # 这些行标注来源, 便于人工区分它们不是 API 解析给的
    sample.loc[sample["method"] == "none", "method"] = "data_match"

# 7) 可点击的 OpenAlex 链接, 方便人工核对候选机构
sample["openalex_url"] = "https://openalex.org/" + sample["host_openalex_id"].astype(str)

# 8) 人工填写列
sample["verdict"] = ""   # 对 / 错 / 存疑
sample["note"] = ""

cols = ["row_id", "host_raw", "inst_name", "cc", "host_openalex_id",
        "matched_name", "method", "relevance", "ror", "openalex_url",
        "verdict", "note"]
sample[cols].to_csv(OUT, index=False, encoding="utf-8-sig")

print(f"[抽样] {n} 条 -> {OUT}")
print("[分档分布]")
print(sample["method"].value_counts().to_string())
