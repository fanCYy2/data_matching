# -*- coding: utf-8 -*-
"""audit_enrich.py —— 为 round3/round4 样本拉 OpenAlex 作者档案 + 代表作,便于独立复核。

对每个 authorid:
  - /authors/{id}: 名字、别名、ORCID、机构历史(带年份)、论文/被引数
  - /works?filter=author.id:{id}&sort=cited_by_count:desc: 取前 N 篇代表作(标题/年/被引)

再和 EU 表的 eu_name / host 做初步一致性判断(名字对得上? 机构历史里有没有 host?)。
产物: <输入名>_enriched.csv,给人和后续 web 核对用。
"""
import sys, os, re, json, time, unicodedata
import urllib.request, urllib.parse
from pathlib import Path
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)

MAILTO = "emiyafancy@gmail.com"
KEY = "TOITSqA1Pb9diO6gaSUxV0"
TOPN = 6
CACHE = "audit_enrich_cache.json"
UA = {"User-Agent": f"eu-audit/1.0 ({MAILTO})"}

STOP = {"of","the","and","for","de","la","le","des","du","di","der","und","el"}

def strip_accents(s):
    return "".join(c for c in unicodedata.normalize("NFKD", str(s)) if not unicodedata.combining(c))
def norm(s):
    if not isinstance(s,str): return None
    s = re.sub(r"&"," and ",s.lower()); s = re.sub(r"[^\w\s]"," ",s,flags=re.UNICODE)
    return re.sub(r"\s+"," ",s).strip() or None
def toks(s):
    n = norm(strip_accents(s)); return frozenset(t for t in n.split() if t not in STOP) if n else frozenset()
def parse_host(raw):
    if not isinstance(raw,str): return None,None
    m = re.search(r"\[\d+,([A-Z]{2})\]",raw)
    if m: return raw[:m.start()].strip().rstrip(",").strip() or None, m.group(1)
    return raw.strip().lstrip("*").strip() or None, None
def pn(s):
    s = strip_accents(str(s)).lower(); return re.sub(r"\s+"," ",re.sub(r"[^a-z\s]"," ",s)).strip()
def name_key(full):
    p = pn(full).split(); return (p[-1], p[0][0]) if p else (None,None)

def http(url, tries=4):
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url,headers=UA),timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (429,500,502,503,504): time.sleep(1.5**i+0.5); continue
            print(f"   HTTP {e.code} on {url[:80]}"); return None
        except Exception as ex:
            time.sleep(1.5**i+0.5)
    return None

def load_cache():
    return json.loads(Path(CACHE).read_text(encoding="utf-8")) if Path(CACHE).exists() else {}
def save_cache(c):
    Path(CACHE).write_text(json.dumps(c,ensure_ascii=False),encoding="utf-8")

def fetch_authors(ids, cache):
    need = [a for a in ids if f"auth|{a}" not in cache]
    for i in range(0,len(need),50):
        batch = need[i:i+50]
        params = {"filter":"ids.openalex:"+"|".join(batch),
                  "select":"id,display_name,display_name_alternatives,orcid,works_count,cited_by_count,last_known_institutions,affiliations",
                  "per-page":50,"mailto":MAILTO,"api_key":KEY}
        url = "https://api.openalex.org/authors?"+urllib.parse.urlencode(params,safe=":|")
        data = http(url); got=set()
        if data:
            for a in data.get("results",[]):
                aid=(a.get("id") or "").rsplit("/",1)[-1]
                cache[f"auth|{aid}"]=a; got.add(aid)
        for a in batch:
            if a not in got: cache[f"auth|{a}"]=None
        time.sleep(0.2); print(f"   authors {i+len(batch)}/{len(need)}")

def fetch_topworks(aid, cache):
    key=f"works|{aid}"
    if key in cache: return cache[key]
    params={"filter":f"author.id:{aid}","sort":"cited_by_count:desc","per-page":TOPN,
            "select":"id,title,publication_year,cited_by_count,authorships","mailto":MAILTO,"api_key":KEY}
    url="https://api.openalex.org/works?"+urllib.parse.urlencode(params,safe=":|")
    data=http(url); out=[]
    if data:
        for w in data.get("results",[]):
            # 本人在这篇里登记的名字(用于看 OA 内部是否自洽)
            out.append({"title":w.get("title"),"year":w.get("publication_year"),
                        "cited":w.get("cited_by_count"),"id":(w.get("id") or "").rsplit("/",1)[-1]})
    cache[key]=out; time.sleep(0.2); return out

def main():
    inputs=[p for p in sys.argv[1:] if Path(p).exists()] or ["round3_sample_50.csv","round4_sample_100.csv"]
    cache=load_cache()
    frames=[]
    for path in inputs:
        df=pd.read_csv(path,encoding="utf-8-sig",dtype=str).fillna("")
        df["__file"]=Path(path).name
        frames.append(df)
    allids=sorted({r["authorid"] for f in frames for _,r in f.iterrows() if r["authorid"]})
    print(f"共 {len(allids)} 个唯一 authorid,拉档案...")
    fetch_authors(allids,cache); save_cache(cache)

    for df,path in zip(frames,inputs):
        rows=[]
        for _,r in df.iterrows():
            aid=r["authorid"]; a=cache.get(f"auth|{aid}")
            rec=dict(r)
            if not a:
                rec.update(oa_name="<NOT FOUND>",oa_alts="",oa_orcid="",oa_works="",oa_cited="",
                           oa_lki="",oa_affils="",host_in_affils="",name_match="",top_works="")
                rows.append(rec); continue
            works=fetch_topworks(aid,cache)
            eu_fam,eu_ini=name_key(r["eu_name"])
            names=[a.get("display_name","")]+list(a.get("display_name_alternatives") or [])
            nmatch=any(name_key(n)==(eu_fam,eu_ini) for n in names if n)
            # 更宽:姓氏一致即可(名首字母不同也标出来)
            fam_only=any(name_key(n)[0]==eu_fam for n in names if n)
            affils=a.get("affiliations") or []
            affil_names=[(x.get("institution") or {}).get("display_name","") for x in affils]
            host_name,host_cc=parse_host(r.get("host",""))
            ht=toks(host_name) if host_name else frozenset()
            host_in=False
            if ht:
                for an in affil_names:
                    at=toks(an)
                    if at and (ht<=at or at<=ht or (ht&at and len(ht&at)/len(ht|at)>=0.5)):
                        host_in=True; break
            lki=[(i.get("display_name","")) for i in (a.get("last_known_institutions") or [])]
            rec.update(
                oa_name=a.get("display_name",""),
                oa_alts=" | ".join(a.get("display_name_alternatives") or []),
                oa_orcid=(a.get("orcid") or "").replace("https://orcid.org/",""),
                oa_works=a.get("works_count",""),
                oa_cited=a.get("cited_by_count",""),
                oa_lki=" | ".join(lki),
                oa_affils=" | ".join(f"{n}{y}" for n,y in
                    [((x.get('institution') or {}).get('display_name',''),
                      '('+str(min(x.get('years') or [0]))+'-'+str(max(x.get('years') or [0]))+')' if x.get('years') else '')
                     for x in affils][:10]),
                host_in_affils="Y" if host_in else ("N" if ht else "?"),
                name_match="exact" if nmatch else ("family" if fam_only else "NO"),
                top_works=" || ".join(f"[{w['year']}|{w['cited']}c] {w['title']}" for w in works if w.get('title')),
            )
            rows.append(rec)
        out=pd.DataFrame(rows)
        lead=["rid","eu_name","authorid","match_type","host","name_match","host_in_affils",
              "oa_name","oa_alts","oa_orcid","oa_lki","oa_works","yes_or_no"]
        cols=[c for c in lead if c in out.columns]+[c for c in out.columns if c not in lead]
        out=out[cols]
        outp=str(Path(path).with_name(Path(path).stem+"_enriched.csv"))
        out.to_csv(outp,index=False,encoding="utf-8-sig")
        print(f"[{Path(path).name}] -> {Path(outp).name}  ({len(out)} 行)")
        # 快速异常统计
        print("   name_match:",out["name_match"].value_counts().to_dict())
        print("   host_in_affils:",out["host_in_affils"].value_counts().to_dict())
    save_cache(cache)

if __name__=="__main__":
    main()
