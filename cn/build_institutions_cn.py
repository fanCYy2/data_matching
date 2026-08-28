# -*- coding: utf-8 -*-
"""Fetch the ~319 distinct NSFC host institutions from the OpenAlex API to get
their English names/aliases (and ror/grid when present), so ORCID employment org
names can be matched host-side by ENGLISH name (ORCID orgs are English; OpenAlex
host names are English) -- no cross-lingual, no reliance on RINGGOLD ids.

Output: institutions_cn.csv (host_id, display_name, alternatives, acronyms, ror, grid, country)
"""
import os, sys, json, time
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
os.chdir("d:/data_matching/cn"); sys.path.insert(0, "d:/data_matching/cn")
import duckdb, pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from concurrent.futures import ThreadPoolExecutor

con = duckdb.connect()
hosts = con.sql("""SELECT DISTINCT host_openalex_id AS h FROM 'cn_host_ids_bridge.csv'
    WHERE host_openalex_id IS NOT NULL AND host_openalex_id <> ''""").df()["h"].tolist()
print("distinct hosts:", len(hosts), flush=True)

sess = requests.Session()
sess.mount("https://", HTTPAdapter(max_retries=Retry(total=5, backoff_factor=0.8,
           status_forcelist=[429, 500, 502, 503, 504], allowed_methods=["GET"]), pool_maxsize=8))
sess.headers.update({"User-Agent": "cn-match-research/0.1 (mailto:research@example.com)"})

def fetch(hid):
    hid = hid.strip()
    url = "https://api.openalex.org/institutions/%s?mailto=research@example.com" % hid
    try:
        r = sess.get(url, timeout=(8, 30))
        if r.status_code != 200:
            return dict(host_id=hid, err="HTTP %d" % r.status_code)
        j = r.json()
        ids = j.get("ids", {}) or {}
        return dict(
            host_id=hid,
            display_name=j.get("display_name"),
            alternatives="|".join(j.get("display_name_alternatives") or []),
            acronyms="|".join(j.get("display_name_acronyms") or []),
            ror=(j.get("ror") or "").rsplit("/", 1)[-1],
            grid=(ids.get("grid") or ""),
            country=j.get("country_code"),
        )
    except Exception as e:
        return dict(host_id=hid, err=type(e).__name__)

t0 = time.time()
rows = []
with ThreadPoolExecutor(max_workers=6) as ex:
    for i, res in enumerate(ex.map(fetch, hosts)):
        rows.append(res)
        if (i + 1) % 50 == 0:
            print("  %d/%d (%.0fs)" % (i + 1, len(hosts), time.time() - t0), flush=True)

df = pd.DataFrame(rows)
errs = df[df.get("err").notna()] if "err" in df else df.iloc[:0]
ok = df[~df.index.isin(errs.index)]
ok.drop(columns=[c for c in ["err"] if c in ok.columns]).to_csv("institutions_cn.csv", index=False, encoding="utf-8-sig")
print("=" * 60)
print("ok=%d err=%d -> institutions_cn.csv" % (len(ok), len(errs)), flush=True)
if len(errs):
    print("err sample:", errs.head(5).to_dict("records"))
print(ok[["host_id", "display_name", "acronyms", "ror"]].head(12).to_string(), flush=True)
