# -*- coding: utf-8 -*-
"""End-to-end ORCID candidate-verification test on gold (the decisive number).

For a sample of gold rows: fetch each candidate-with-orcid's ORCID employment/
education orgs, match them ENGLISH-name-wise to the row's host institution
(institutions_cn.csv), UNION with the OpenAlex host-survivor flag, then pick and
measure precision/coverage vs gold. Tells us the real payoff before building the
172k production fetcher.

Rules reported:
  openalex-only : at_host = host_survivor           (the current §6.1 signal)
  orcid-only    : at_host = orcid_emp_matches_host
  UNION unique  : at_host = either; accept iff exactly ONE candidate at_host
  UNION +papers : at_host = either; among at_host pick argmax papers
Coverage denom = sampled gold rows. Precision = correct / accepted.
"""
import os, sys, json, re, time
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
os.chdir("d:/data_matching/cn"); sys.path.insert(0, "d:/data_matching/cn")
import duckdb, numpy as np, pandas as pd, cn_match
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from concurrent.futures import ThreadPoolExecutor

S = int(os.environ.get("S", "120"))     # sampled gold rows
CAP = int(os.environ.get("CAP", "50"))  # max candidates-with-orcid fetched per row
T0 = time.time()
def el(): return "[%5.0fs]" % (time.time() - T0)

con = duckdb.connect(); con.sql("SET memory_limit='6GB'")
cn_df, _ = cn_match.load_cn()
gold = cn_df[cn_df["gold_authorid"].notna()][["rid", "gold_authorid"]].copy()
gold["rid"] = gold["rid"].astype(int)
gmap = dict(zip(gold["rid"], gold["gold_authorid"]))

work = pd.read_csv("cand_orcids_cn.csv")
work["rid"] = work["rid"].astype(int)
work = work[work["rid"].isin(gmap)]
# host per rid + host english names
bridge = con.sql("""SELECT CAST(row_id AS INTEGER) rid, host_openalex_id host FROM 'cn_host_ids_bridge.csv'
    WHERE host_openalex_id IS NOT NULL AND host_openalex_id<>''""").df()
rid2host = dict(zip(bridge["rid"], bridge["host"]))
inst = pd.read_csv("institutions_cn.csv")
def names_of(hid):
    r = inst[inst["host_id"] == hid]
    if r.empty: return []
    r = r.iloc[0]
    out = [r["display_name"]] + (str(r["alternatives"]).split("|") if pd.notna(r["alternatives"]) else [])
    return [x for x in out if isinstance(x, str) and x.strip()]
host_names = {hid: names_of(hid) for hid in set(rid2host.values())}

# sample rids that have >=1 candidate-with-orcid AND a host
elig = sorted(set(work["rid"]) & set(rid2host))
rng = np.random.default_rng(42)
sample = list(rng.choice(elig, size=min(S, len(elig)), replace=False))
wsamp = work[work["rid"].isin(sample)].copy()
# cap candidates per row (already sorted host_survivor desc, n_papers desc in csv)
wsamp = wsamp.groupby("rid", group_keys=False).head(CAP)
orcids = sorted(set(wsamp["orcid"]))
print("%s sample %d rids, %d cand-rows, %d distinct orcids to fetch"
      % (el(), len(sample), len(wsamp), len(orcids)), flush=True)

# ---- fetch ORCID emp+edu orgs (cached per orcid) ----
sess = requests.Session()
sess.mount("https://", HTTPAdapter(max_retries=Retry(total=5, backoff_factor=0.8,
           status_forcelist=[429,500,502,503,504], allowed_methods=["GET"]), pool_maxsize=12))
sess.headers.update({"Accept":"application/json","User-Agent":"cn-match-research/0.1 (mailto:research@example.com)"})
def orgs_from(js, key):
    out=[]
    for grp in (js or {}).get("affiliation-group", []):
        for s in grp.get("summaries", []):
            o=(s.get(key,{}) or {}).get("organization",{}) or {}
            if o.get("name"): out.append(o["name"])
    return out
def fetch(oid):
    short=str(oid).rsplit("/",1)[-1]
    res=[]
    for ep,key in [("employments","employment-summary"),("educations","education-summary")]:
        try:
            r=sess.get("https://pub.orcid.org/v3.0/%s/%s"%(short,ep), timeout=(8,30))
            if r.status_code==200: res+=orgs_from(r.json(),key)
        except Exception: pass
    return oid, res
orc_orgs={}
with ThreadPoolExecutor(max_workers=8) as ex:
    for i,(oid,res) in enumerate(ex.map(fetch, orcids)):
        orc_orgs[oid]=res
        if (i+1)%200==0: print("  fetched %d/%d (%.0fs)"%(i+1,len(orcids),time.time()-T0), flush=True)
print("%s fetched. orcids with any org: %d/%d"
      % (el(), sum(1 for v in orc_orgs.values() if v), len(orcids)), flush=True)

# ---- english-name institution match ----
def norm(s):
    s=re.sub(r"[^a-z0-9]+"," ",str(s).lower()).strip()
    return re.sub(r"\s+"," ",s)
def org_matches_host(org, hnames):
    o=norm(org)
    if len(o)<5: return False
    for n in hnames:
        hn=norm(n)
        if len(hn)<6: continue
        if o==hn or hn in o or o in hn: return True
    return False
def orcid_at_host(oid, hid):
    hn=host_names.get(hid,[])
    return any(org_matches_host(org,hn) for org in orc_orgs.get(oid,[]))

wsamp["orcid_at_host"]=[orcid_at_host(o,rid2host[r]) for o,r in zip(wsamp["orcid"],wsamp["rid"])]
wsamp["hs"]=wsamp["host_survivor"].astype(bool)
wsamp["at_union"]=wsamp["hs"]|wsamp["orcid_at_host"]

# fetch-recall: is gold among the (capped) candidate set?
gold_in_set=sum(1 for r in sample if gmap[r] in set(wsamp[wsamp["rid"]==r]["authorid"]))
N=len(sample)
print("%s gold within fetched candidate set: %d/%d = %.1f%%"%(el(),gold_in_set,N,100*gold_in_set/N), flush=True)

def evalrule(name, flagcol, tiebreak_papers):
    hit=0; acc=0
    for r in sample:
        g=wsamp[wsamp["rid"]==r]
        at=g[g[flagcol]]
        if at.empty: continue
        if not tiebreak_papers and len(at)>1: continue   # unique-only
        pick=at.sort_values(["n_papers","authorid"],ascending=[False,True]).iloc[0]
        acc+=1; hit+=int(pick["authorid"]==gmap[r])
    p=100*hit/acc if acc else 0
    print("  %-30s prec %5.1f%%  cov %3d/%d = %4.1f%%%s"
          %(name,p,hit,N,100*acc/N,"   <<< >=95%" if acc and hit/acc>=0.95 else ""))

print("="*74)
evalrule("openalex-only unique",  "hs", False)
evalrule("openalex-only +papers", "hs", True)
evalrule("orcid-only unique",     "orcid_at_host", False)
evalrule("UNION unique",          "at_union", False)
evalrule("UNION +papers",         "at_union", True)
print("="*74)
print("%s done"%el(), flush=True)
