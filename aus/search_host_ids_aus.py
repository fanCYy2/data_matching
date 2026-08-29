# -*- coding: utf-8 -*-
"""
search_host_ids_aus.py —— 逐行:学者机构名 -> OpenAlex 机构 id。就这一件事。

  读 AUS.csv 的 current-admin-organisation 列, 每个机构名调一次 OpenAlex
  /institutions 搜索(限 country_code=AU), 取匹配到的机构 id。
  同名机构去重后只搜一次(带缓存, 重跑几乎不再调 API)。

产物:
  aus_host_ids_bridge.csv    row_id,host_openalex_id   (aus_match.py 直接读)
  aus_host_ids_search.csv     row_id,code,institution,host_openalex_id,matched_name  (给人看/核对)

跑法:  cd aus && OPENALEX_API_KEY=$(cat ../.openalex_api_key) python search_host_ids_aus.py
"""
import os
import re
import json
import time
import urllib.request
import urllib.parse
from pathlib import Path

import pandas as pd

AUS_CSV = "AUS.csv"
INST_COL = "current-admin-organisation"        # 机构名所在列
OUT_BRIDGE = "aus_host_ids_bridge.csv"
OUT_AUDIT = "aus_host_ids_search.csv"
CACHE_PATH = "api_cache_institutions_aus.json"

API_KEY = (os.environ.get("OPENALEX_API_KEY", "").strip()
           or (Path(".openalex_api_key").read_text(encoding="utf-8").strip()
               if Path(".openalex_api_key").exists() else ""))
MAILTO = "emiyafancy@gmail.com"
API_BASE = "https://api.openalex.org/institutions"


def _norm(s):
    """粗归一, 只为判「搜到的名字是不是就是原机构名」: 小写 / 去开头 The / 去标点 / 压空白。"""
    s = re.sub(r"^\s*the\s+", "", str(s).strip(), flags=re.IGNORECASE).lower()
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def search_one(name):
    """搜一个机构名, 返回 (openalex_id, display_name) 或 (None, None)。
    取前 5 条, 有名字完全对上的就用它, 否则用相关度第一条。"""
    params = {"search": name, "filter": "country_code:AU",
              "select": "id,display_name", "per-page": 5, "mailto": MAILTO}
    if API_KEY:
        params["api_key"] = API_KEY
    url = API_BASE + "?" + urllib.parse.urlencode(params, safe=":")

    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": f"aus-host/1.0 ({MAILTO})"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                results = json.loads(resp.read().decode("utf-8")).get("results", [])
            break
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < 3:
                wait = int(e.headers.get("Retry-After") or 0) or 2 ** attempt
                print(f"    [API] HTTP {e.code}, {wait}s 后重试")
                time.sleep(wait)
                continue
            print(f"    [API] HTTP {e.code} 放弃: {name}")
            return (None, None)
        except Exception as e:
            if attempt < 3:
                time.sleep(2 ** attempt)
                continue
            print(f"    [API] {type(e).__name__}: {e}; 放弃: {name}")
            return (None, None)
    else:
        return (None, None)

    if not results:
        return (None, None)
    nn = _norm(name)
    hit = next((r for r in results if _norm(r.get("display_name")) == nn), results[0])
    return (hit["id"].rsplit("/", 1)[-1], hit.get("display_name"))


def main():
    if not API_KEY:
        print("[提示] 没设 API key(OPENALEX_API_KEY 或 ../.openalex_api_key), 免费匿名额度较低。")

    df = pd.read_csv(AUS_CSV, dtype=str)
    df["row_id"] = df.index
    df["institution"] = df[INST_COL].map(
        lambda v: v.strip() if isinstance(v, str) and v.strip() else None)
    print(f"[AUS] {len(df)} 行, 机构列 = {INST_COL}")

    cache = {}
    if Path(CACHE_PATH).exists():
        try:
            cache = json.loads(Path(CACHE_PATH).read_text(encoding="utf-8"))
        except Exception:
            cache = {}

    uniq = sorted(df["institution"].dropna().unique())
    todo = [nm for nm in uniq if nm not in cache]
    print(f"[去重] {len(uniq)} 个唯一机构; 缓存已有 {len(uniq) - len(todo)}, 需搜 {len(todo)} 个")

    for i, nm in enumerate(todo, 1):
        oid, disp = search_one(nm)
        cache[nm] = [oid, disp]
        print(f"  {i:>3}/{len(todo)}  {'OK ' if oid else '-- '} {nm}  ->  {disp or '(无结果)'}")
        time.sleep(0.1)                                     # 轻限速
        if i % 20 == 0:                                     # 边搜边存, 中断不白跑
            Path(CACHE_PATH).write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    Path(CACHE_PATH).write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")

    df["host_openalex_id"] = df["institution"].map(lambda nm: (cache.get(nm) or [None, None])[0])
    df["matched_name"] = df["institution"].map(lambda nm: (cache.get(nm) or [None, None])[1])

    df[["row_id", "host_openalex_id"]].to_csv(OUT_BRIDGE, index=False, encoding="utf-8-sig")
    cols = [c for c in ["row_id", "code", "institution", "host_openalex_id", "matched_name"]
            if c in df.columns]
    df[cols].to_csv(OUT_AUDIT, index=False, encoding="utf-8-sig")

    ok = int(df["host_openalex_id"].notna().sum())
    print(f"[完成] {ok}/{len(df)} 行拿到机构 id ({ok / len(df) * 100:.1f}%)")
    print(f"       -> {OUT_BRIDGE}")
    print(f"       -> {OUT_AUDIT}  (建议扫一眼 host_openalex_id 为空的行)")


if __name__ == "__main__":
    main()
