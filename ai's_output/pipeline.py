# -*- coding: utf-8 -*-
"""
EU 研究者 -> OpenAlex authorid 匹配流水线 (单文件版, 逻辑与原多脚本一致)。

六轮匹配 + 精度验证:
  R1 姓名直配 / R2 +机构 / R3 +领域 / R4 no_match首末名救回 /
  R5 平局裁决(ORCID·发表量) / R6 别名增强召回 ; 末尾做独立信号交叉印证。

用法:
  python pipeline.py            # 从头跑到尾
  python pipeline.py --from 12  # 从第12步继续(前面中间产物还在时)
  python pipeline.py --only 7   # 只跑第7步
  python pipeline.py --list     # 列出所有步骤
最终产物: matched_authors_final.csv, validation_evidence.csv
依赖: pandas, duckdb, openpyxl ; r2b/r6a 需联网(OpenAlex S3, 无需API key)。
"""
import sys, re, unicodedata, urllib.request, urllib.parse
import duckdb, pandas as pd

# 源文件
EU_XLSX = "EU.xlsx"
AUTHORS = "sciscinet_authors.parquet"
DETAILS = "sciscinet_author_details.parquet"
AFF     = "sciscinet_paper_author_affiliation.parquet"
AP      = "sciscinet_authors_paperid.parquet"
PF      = "sciscinet_paperfields.parquet"
FD      = "sciscinet_fields.parquet"
S3BASE  = "https://openalex.s3.amazonaws.com/"

def duck():
    con = duckdb.connect(); con.execute("PRAGMA threads=8"); con.execute("PRAGMA memory_limit='12GB'")
    return con

# ============================================================ 归一化 / 机构解析 helper
CC_MAP = {"UK": "GB", "EL": "GR"}
STOP = {"of","the","and","for","de","la","le","des","du","di","der","und","el"}

def strip_accents(s):
    return "".join(c for c in unicodedata.normalize("NFKD", str(s)) if not unicodedata.combining(c))

def norm(s):
    """基础归一: 小写 + &->and + 去标点 + 去空白。"""
    if not isinstance(s, str): return None
    s = re.sub(r"&", " and ", s.lower())
    s = re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE)
    return re.sub(r"\s+", " ", s).strip() or None

def norm_keys(s):
    """一组归一 key(去变音/德语转写), 任一相等即视为同名。"""
    n = norm(s)
    if not n: return set()
    keys = {n}
    de = n
    for a, b in [("ä","ae"),("ö","oe"),("ü","ue"),("ß","ss")]:
        de = de.replace(a, b)
    keys.add(de)
    keys.add(re.sub(r"(ae|oe|ue)", lambda m: m.group(0)[0], de))
    keys.add(strip_accents(n))
    return {k for k in keys if k}

def tokens(s):
    n = norm(s)
    return frozenset(t for t in n.split() if t not in STOP) if n else frozenset()

def parse_host(raw):
    """'Delft University of Technology [999977366,NL]' -> (name, cc)"""
    if not isinstance(raw, str): return None, None
    m = re.search(r"\[\d+,([A-Z]{2})\]", raw)
    cc = m.group(1) if m else None
    name = re.sub(r"\s*\[\d+,[A-Z]{2}\]\s*$", "", raw).strip()
    return name or None, cc

def build_host_resolver():
    """基于本地 OpenAlex 机构字典, 返回 resolve(name,cc)->(id,matched_name,method)|None。"""
    var = pd.read_parquet("openalex_institutions_eu.parquet")
    exact, by_cc = {}, {}
    for r in var.itertuples(index=False):
        ic = (norm(r.canonical_name) == norm(r.name_variant))
        for k in norm_keys(r.name_variant):
            exact.setdefault((r.country_code, k), []).append((r.openalex_id, r.canonical_name, ic))
        by_cc.setdefault(r.country_code, []).append((tokens(r.name_variant), r.openalex_id, r.canonical_name))

    def resolve(name, cc):
        cc_oa = CC_MAP.get(cc, cc)
        cand = []
        for k in norm_keys(name):
            cand += exact.get((cc_oa, k), [])
        if cand:
            seen, u = set(), []
            for c in cand:
                if c[0] in seen: continue
                seen.add(c[0]); u.append(c)
            u.sort(key=lambda x: (not x[2],))
            return u[0][0], u[0][1], "exact"
        tset = tokens(name)
        if not tset: return None
        best, bs = None, 0.0
        for tv, oid, canon in by_cc.get(cc_oa, []):
            if not tv: continue
            inter = len(tset & tv)
            j = inter/len(tset | tv); cover = inter/len(tset)
            s = 0.6*j + 0.4*cover
            if s > bs: bs = s; best = (oid, canon, j, cover)
        if best and best[2] >= 0.6 and best[3] >= 0.8:
            return best[0], best[1], "fuzzy"
        return None
    return resolve

def resolve_hosts_for(rows_df, out_csv):
    """rows_df 含 'Host Institution(s)' 列; 解析唯一(name,cc) 并写 out_csv。"""
    resolve = build_host_resolver()
    p = rows_df["Host Institution(s)"].map(parse_host)
    rows_df = rows_df.assign(inst_name=[x[0] for x in p], cc=[x[1] for x in p])
    uniq = rows_df[["inst_name","cc"]].dropna().drop_duplicates()
    out = []
    for _, r in uniq.iterrows():
        hit = resolve(r["inst_name"], r["cc"])
        out.append({"inst_name":r["inst_name"], "cc":r["cc"],
                    "openalex_id":hit[0] if hit else None,
                    "matched_name":hit[1] if hit else None,
                    "method":hit[2] if hit else None})
    df = pd.DataFrame(out); ok = df["openalex_id"].notna().sum()
    df.to_csv(out_csv, index=False, encoding="utf-8-sig")
    print(f"机构解析: {ok}/{len(df)} ({ok/max(len(df),1)*100:.1f}%) -> {out_csv}")

# ============================================================ ERC Panel/Domain -> OpenAlex 领域
PANEL_TO_FIELDS = {
    "PE1":{"Mathematics"},"PE2":{"Physics"},"PE3":{"Physics"},"PE4":{"Chemistry","Physics"},
    "PE5":{"Chemistry","Materials science"},"PE6":{"Computer science"},
    "PE7":{"Engineering","Computer science"},"PE8":{"Engineering","Materials science"},
    "PE9":{"Physics"},"PE10":{"Geology","Environmental science","Geography"},
    "LS1":{"Biology","Chemistry"},"LS2":{"Biology"},"LS3":{"Biology"},"LS4":{"Medicine","Biology"},
    "LS5":{"Biology","Medicine","Psychology"},"LS6":{"Medicine","Biology"},"LS7":{"Medicine"},
    "LS8":{"Biology","Environmental science"},"LS9":{"Biology","Engineering"},
    "SH1":{"Economics","Business"},"SH2":{"Political science"},"SH3":{"Sociology","Geography"},
    "SH4":{"Psychology"},"SH5":{"Art","History","Philosophy"},"SH6":{"History"},
}
DOMAIN_TO_FIELDS = {
    "PE":{"Physics","Chemistry","Computer science","Mathematics","Engineering","Materials science","Geology","Environmental science","Geography"},
    "LS":{"Biology","Medicine","Psychology"},
    "SH":{"Psychology","Economics","Sociology","Political science","History","Art","Philosophy","Business","Geography"},
}
def expected_fields(panel, domain):
    pc = panel.split(" ",1)[0].split("-")[0].strip() if isinstance(panel,str) else None
    if pc in PANEL_TO_FIELDS: return PANEL_TO_FIELDS[pc]
    m = re.search(r"\(([A-Z]{2})\)", domain) if isinstance(domain,str) else None
    dc = m.group(1) if m else None
    return DOMAIN_TO_FIELDS.get(dc, set())

# ============================================================ 特征扫描 helper (DuckDB)
FIELD_SHARE = 0.15

def scan_inst(ids_df, out_path):
    con = duck(); con.register("ids", ids_df.drop_duplicates())
    con.execute(f"""CREATE TEMP TABLE t AS
        SELECT a.authorid, list(DISTINCT a.institutionid)
                 FILTER(WHERE a.institutionid IS NOT NULL AND a.institutionid<>'') AS inst_ids
        FROM '{AFF}' a JOIN ids i ON a.authorid=i.authorid GROUP BY a.authorid""")
    con.execute(f"COPY t TO '{out_path}' (FORMAT parquet)")
    print(f"  机构集合 {con.execute('SELECT count(*) FROM t').fetchone()[0]} 作者 -> {out_path}")

def scan_fields(ids_df, out_path):
    con = duck(); con.register("ids", ids_df.drop_duplicates())
    con.execute(f"""CREATE TEMP TABLE cp AS SELECT DISTINCT ap.authorid, ap.paperid
        FROM '{AP}' ap JOIN ids i ON ap.authorid=i.authorid""")
    con.execute(f"CREATE TEMP TABLE f0 AS SELECT fieldid, display_name FROM '{FD}' WHERE level=0")
    con.execute(f"""CREATE TEMP TABLE af AS
        SELECT cp.authorid, f0.display_name AS field_name, count(*) AS n
        FROM cp JOIN '{PF}' pf ON cp.paperid=pf.paperid JOIN f0 ON pf.fieldid=f0.fieldid
        GROUP BY cp.authorid, f0.display_name""")
    con.execute(f"COPY af TO '{out_path}' (FORMAT parquet)")
    print(f"  领域 {con.execute('SELECT count(DISTINCT authorid) FROM af').fetchone()[0]} 作者 -> {out_path}")

def scan_details(ids_df, out_csv, cols="d.authorid, d.orcid, d.display_name, d.works_count"):
    con = duck(); con.register("ids", ids_df[["authorid"]].drop_duplicates())
    det = con.execute(f"SELECT {cols} FROM '{DETAILS}' d JOIN ids i ON d.authorid=i.authorid").df()
    det.to_csv(out_csv, index=False, encoding="utf-8-sig")
    print(f"  details {len(det)} 作者 -> {out_csv}")

def load_inst(paths):
    d = {}
    for p in paths:
        for r in pd.read_parquet(p).itertuples(index=False):
            d.setdefault(r.authorid, set()).update(set(r.inst_ids) if r.inst_ids is not None else set())
    return d

def load_field_dict(paths):
    d = {}
    for p in paths:
        for aid, g in pd.read_parquet(p).groupby("authorid"):
            tot = g["n"].sum()
            if tot == 0: continue
            top = set(g.loc[g["n"] >= FIELD_SHARE*tot, "field_name"])
            top.add(g.sort_values("n", ascending=False)["field_name"].iloc[0])
            d.setdefault(aid, set()).update(top)
    return d

def has_orcid(x):
    return isinstance(x, str) and x.strip() not in ("", "None", "nan")

# ============================================================ 步骤 1: R1 姓名直配
def step_round1():
    eu = pd.read_excel(EU_XLSX); eu["row_id"] = eu.index
    eu["name_norm"] = eu["Researcher(s)"].map(lambda x: re.sub(r"\s+"," ",x.strip().lower()) if isinstance(x,str) and x.strip() else None)
    con = duck(); con.register("eu", eu)
    con.execute("CREATE TEMP TABLE eun AS SELECT DISTINCT name_norm FROM eu WHERE name_norm IS NOT NULL")
    con.execute(f"""CREATE TEMP TABLE cand AS
        SELECT n.name_norm, a.authorid, a.display_name, a.productivity, a.h_index, a.avg_c10
        FROM '{AUTHORS}' a JOIN eun n
          ON regexp_replace(trim(lower(a.display_name)),'\\s+',' ','g') = n.name_norm""")
    counts = con.execute("SELECT name_norm, count(DISTINCT authorid) AS n_authors FROM cand GROUP BY name_norm").df()
    cmap = dict(zip(counts.name_norm, counts.n_authors))
    single = set(counts.loc[counts.n_authors==1, "name_norm"])
    cand_df = con.execute("SELECT * FROM cand").df()
    amap = dict(zip(cand_df[cand_df.name_norm.isin(single)].drop_duplicates("name_norm").name_norm,
                    cand_df[cand_df.name_norm.isin(single)].drop_duplicates("name_norm").authorid))
    def status(nm):
        if not isinstance(nm,str): return "no_name"
        n = cmap.get(nm,0)
        return "no_match" if n==0 else ("unique_match" if n==1 else "ambiguous")
    eu["match_status"]     = eu["name_norm"].map(status)
    eu["n_candidates"]     = eu["name_norm"].map(lambda x: cmap.get(x,0) if isinstance(x,str) else 0)
    eu["matched_authorid"] = eu.apply(lambda r: amap.get(r["name_norm"]) if r["match_status"]=="unique_match" else None, axis=1)
    eu[["row_id","Researcher(s)","name_norm","match_status","n_candidates","matched_authorid"]].to_csv(
        "match_round1_result.csv", index=False, encoding="utf-8-sig")
    cand_df.to_csv("match_round1_candidates.csv", index=False, encoding="utf-8-sig")
    vc = eu["match_status"].value_counts(); tot=len(eu)
    print("R1 姓名直配:", {k:int(vc.get(k,0)) for k in ["unique_match","ambiguous","no_match","no_name"]},
          f"完成度={vc.get('unique_match',0)/tot*100:.1f}%")

# ============================================================ 步骤 2: 下载 OpenAlex 机构字典
def step_download_institutions():
    keys, token = [], None
    while True:
        url = S3BASE + "?list-type=2&prefix=data/parquet/institutions/&max-keys=1000"
        if token: url += "&continuation-token=" + urllib.parse.quote(token)
        xml = urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent":"eu-match/1.0"}), timeout=30).read().decode()
        keys += re.findall(r"<Key>([^<]+\.parquet)</Key>", xml)
        m = re.search(r"<NextContinuationToken>([^<]+)</NextContinuationToken>", xml)
        if not m: break
        token = m.group(1)
    urls = "[" + ",".join("'%s'" % (S3BASE+k) for k in keys) + "]"
    ccs = ",".join("'%s'"%c for c in ['DE','GB','FR','IT','NL','IL','SE','ES','CH','BE','AT','IE','DK','NO','FI','PT','CZ','PL','GR','HU','EE','SI','LU','CY','IS','HR'])
    con = duck(); con.execute("INSTALL httpfs; LOAD httpfs")
    print(f"读取 {len(keys)} 个 institutions 分区文件 ...")
    con.execute(f"""CREATE TEMP TABLE inst AS
        SELECT regexp_replace(id,'^https?://openalex.org/','') AS openalex_id, display_name, country_code,
               display_name_alternatives, display_name_acronyms
        FROM read_parquet({urls}, union_by_name=true) WHERE country_code IN ({ccs})""")
    con.execute("""CREATE TEMP TABLE variants AS
        SELECT openalex_id, country_code, display_name AS canonical_name, nm AS name_variant
        FROM (SELECT openalex_id, country_code, display_name,
                list_concat([display_name], coalesce(display_name_alternatives,[]), coalesce(display_name_acronyms,[])) AS names
              FROM inst), unnest(names) AS t(nm) WHERE nm IS NOT NULL AND nm<>''""")
    con.execute("COPY (SELECT DISTINCT * FROM variants) TO 'openalex_institutions_eu.parquet' (FORMAT parquet)")
    print("欧洲机构:", con.execute("SELECT count(*) FROM inst").fetchone()[0],
          " 名称变体:", con.execute("SELECT count(*) FROM variants").fetchone()[0], "-> openalex_institutions_eu.parquet")

# ============================================================ 步骤 3: 解析 ambiguous 行机构
def step_resolve_inst_r2():
    res = pd.read_csv("match_round1_result.csv"); eu = pd.read_excel(EU_XLSX)
    amb = res[res.match_status=="ambiguous"].merge(eu[["Host Institution(s)"]], left_on="row_id", right_index=True)
    resolve_hosts_for(amb, "institutions_openalex.csv")

# ============================================================ 步骤 4: ambiguous 候选作者机构集合
def _amb_candidate_ids():
    res = pd.read_csv("match_round1_result.csv"); cand = pd.read_csv("match_round1_candidates.csv")
    names = set(res.loc[res.match_status=="ambiguous","name_norm"])
    return cand[cand.name_norm.isin(names)][["authorid"]].drop_duplicates()

def step_inst_r2():
    scan_inst(_amb_candidate_ids(), "r2_author_inst_sets.parquet")

# ============================================================ 步骤 5: R2 姓名+机构
def step_round2():
    res=pd.read_csv("match_round1_result.csv"); cand=pd.read_csv("match_round1_candidates.csv")
    inst=pd.read_csv("institutions_openalex.csv"); eu=pd.read_excel(EU_XLSX)
    a2inst=load_inst(["r2_author_inst_sets.parquet"])
    name2c=cand.groupby("name_norm")["authorid"].apply(list).to_dict()
    ikey={(r.inst_name,r.cc):r.openalex_id for r in inst.itertuples(index=False) if pd.notna(r.openalex_id)}
    hosts=eu["Host Institution(s)"].map(parse_host)
    res["r2_matched_authorid"]=None
    for idx,row in res.iterrows():
        if row.match_status!="ambiguous": continue
        name,cc=hosts.iloc[int(row.row_id)]; oa=ikey.get((name,cc))
        if not oa: continue
        hits=[a for a in name2c.get(row.name_norm,[]) if oa in a2inst.get(a,set())]
        if len(hits)==1: res.at[idx,"r2_matched_authorid"]=hits[0]
    res["match_status_r2"]=res.apply(lambda r:"unique_match" if r.match_status=="unique_match"
        else("unique_match_r2" if r.match_status=="ambiguous" and pd.notna(r.r2_matched_authorid) else r.match_status),axis=1)
    res["authorid_final"]=res.apply(lambda r:r.matched_authorid if r.match_status=="unique_match" else r.r2_matched_authorid,axis=1)
    res.to_csv("match_round2_result.csv",index=False,encoding="utf-8-sig")
    done=res.authorid_final.notna().sum()
    print(f"R2 +机构: +{(res.match_status_r2=='unique_match_r2').sum()} -> 完成度 {done/len(res)*100:.1f}%")

# ============================================================ 步骤 6: ambiguous 候选作者领域
def step_fields_r3():
    scan_fields(_amb_candidate_ids(), "r3_author_fields.parquet")

# ============================================================ 步骤 7: R3 姓名+机构+领域
def step_round3():
    res=pd.read_csv("match_round2_result.csv"); cand=pd.read_csv("match_round1_candidates.csv")
    inst=pd.read_csv("institutions_openalex.csv"); eu=pd.read_excel(EU_XLSX)
    a2inst=load_inst(["r2_author_inst_sets.parquet"]); a2field=load_field_dict(["r3_author_fields.parquet"])
    name2c=cand.groupby("name_norm")["authorid"].apply(list).to_dict()
    ikey={(r.inst_name,r.cc):r.openalex_id for r in inst.itertuples(index=False) if pd.notna(r.openalex_id)}
    hosts=eu["Host Institution(s)"].map(parse_host)
    res["r3_matched_authorid"]=None
    for idx,row in res.iterrows():
        if not(row.match_status=="ambiguous" and pd.isna(row.authorid_final)): continue
        rid=int(row.row_id); name,cc=hosts.iloc[rid]; oa=ikey.get((name,cc))
        exp=expected_fields(eu["Panel"].iloc[rid], eu["Domain"].iloc[rid])
        scored=[(a, 2*(1 if oa and oa in a2inst.get(a,set()) else 0)+(1 if exp and (a2field.get(a,set())&exp) else 0))
                for a in name2c.get(row.name_norm,[])]
        if not scored: continue
        mx=max(s for _,s in scored); top=[a for a,s in scored if s==mx]
        if mx>0 and len(top)==1: res.at[idx,"r3_matched_authorid"]=top[0]
    res["match_status_r3"]=res.apply(lambda r:r.match_status_r2 if r.match_status_r2 in
        ("unique_match","unique_match_r2") else("unique_match_r3" if r.match_status=="ambiguous" and pd.notna(r.r3_matched_authorid) else r.match_status),axis=1)
    res["authorid_final3"]=res.apply(lambda r:r.authorid_final if pd.notna(r.authorid_final) else r.r3_matched_authorid,axis=1)
    res.to_csv("match_round3_result.csv",index=False,encoding="utf-8-sig")
    done=res.authorid_final3.notna().sum()
    print(f"R3 +领域: +{(res.match_status_r3=='unique_match_r3').sum()} -> 完成度 {done/len(res)*100:.1f}%")

# ============================================================ 步骤 8: no_match 首末名召回
def step_recall_r4():
    res=pd.read_csv("match_round1_result.csv")
    nm=res[res.match_status=="no_match"][["row_id","name_norm"]].dropna()
    con=duck(); con.register("nm",nm)
    N="regexp_replace(trim(lower(display_name)),'\\s+',' ','g')"
    con.execute(f"""CREATE TEMP TABLE au AS SELECT authorid, display_name, productivity, h_index,
        strip_accents(str_split({N},' ')[1]||' '||str_split({N},' ')[len(str_split({N},' '))]) AS a_fl
        FROM '{AUTHORS}' WHERE display_name IS NOT NULL""")
    con.execute("""CREATE TEMP TABLE eu AS SELECT row_id,name_norm,
        strip_accents(str_split(name_norm,' ')[1]||' '||str_split(name_norm,' ')[len(str_split(name_norm,' '))]) AS k_fl FROM nm""")
    cand=con.execute("""SELECT e.row_id,e.name_norm,a.authorid,a.display_name,a.productivity,a.h_index
        FROM eu e JOIN au a ON e.k_fl=a.a_fl""").df()
    cand.to_csv("r4_candidates.csv",index=False,encoding="utf-8-sig")
    c=cand.groupby("row_id").authorid.nunique()
    print(f"R4 召回: {c.gt(0).sum()}/{len(nm)} (唯一={c.eq(1).sum()},多={c.gt(1).sum()})")

# ============================================================ 步骤 9: R4 候选特征
def step_features_r4():
    ids=pd.read_csv("r4_candidates.csv")[["authorid"]].drop_duplicates()
    scan_inst(ids,"r4_author_inst_sets.parquet"); scan_fields(ids,"r4_author_fields.parquet")

# ============================================================ 步骤 10: 解析 no_match 行机构
def step_resolve_inst_r4():
    res=pd.read_csv("match_round1_result.csv"); eu=pd.read_excel(EU_XLSX)
    nm=res[res.match_status=="no_match"].merge(eu[["Host Institution(s)"]],left_on="row_id",right_index=True)
    resolve_hosts_for(nm,"institutions_openalex_r4.csv")

# ============================================================ 步骤 11: R4 no_match 救回
def step_round4():
    res=pd.read_csv("match_round3_result.csv"); eu=pd.read_excel(EU_XLSX)
    cand=pd.read_csv("r4_candidates.csv"); inst=pd.read_csv("institutions_openalex_r4.csv")
    a2inst=load_inst(["r4_author_inst_sets.parquet"]); a2field=load_field_dict(["r4_author_fields.parquet"])
    ikey={(r.inst_name,r.cc):r.openalex_id for r in inst.itertuples(index=False) if pd.notna(r.openalex_id)}
    row2c=cand.groupby("row_id")["authorid"].apply(list).to_dict()
    hosts=eu["Host Institution(s)"].map(parse_host)
    res["r4_matched_authorid"]=None; res["r4_how"]=None
    for idx,row in res.iterrows():
        if row.match_status!="no_match": continue
        rid=int(row.row_id); cands=row2c.get(rid,[])
        if not cands: continue
        if len(cands)==1:
            res.at[idx,"r4_matched_authorid"]=cands[0]; res.at[idx,"r4_how"]="firstlast_unique"; continue
        name,cc=hosts.iloc[rid]; oa=ikey.get((name,cc)); exp=expected_fields(eu["Panel"].iloc[rid],eu["Domain"].iloc[rid])
        scored=[(a,2*(1 if oa and oa in a2inst.get(a,set()) else 0)+(1 if exp and (a2field.get(a,set())&exp) else 0)) for a in cands]
        mx=max(s for _,s in scored); top=[a for a,s in scored if s==mx]
        if mx>0 and len(top)==1: res.at[idx,"r4_matched_authorid"]=top[0]; res.at[idx,"r4_how"]="disambig"
    res["match_status_r4"]=res.apply(lambda r:r.match_status_r3 if r.match_status_r3 in
        ("unique_match","unique_match_r2","unique_match_r3") else("unique_match_r4" if r.match_status=="no_match" and pd.notna(r.r4_matched_authorid) else r.match_status_r3),axis=1)
    res["authorid_final4"]=res.apply(lambda r:r.authorid_final3 if pd.notna(r.authorid_final3) else r.r4_matched_authorid,axis=1)
    res.to_csv("match_round4_result.csv",index=False,encoding="utf-8-sig")
    done=res.authorid_final4.notna().sum()
    print(f"R4 救回: +{(res.match_status_r4=='unique_match_r4').sum()} -> 完成度 {done/len(res)*100:.1f}%")

# ============================================================ 步骤 12: R5 平局候选 + ORCID
def step_prep_r5():
    r4=pd.read_csv("match_round4_result.csv")
    unres=r4[r4.match_status_r4.isin(["ambiguous","no_match"])]
    cand1=pd.read_csv("match_round1_candidates.csv"); cand4=pd.read_csv("r4_candidates.csv")
    amb=unres[unres.match_status_r4=="ambiguous"][["row_id","name_norm"]].merge(cand1,on="name_norm")[["row_id","name_norm","authorid","productivity","h_index"]]
    nm =unres[unres.match_status_r4=="no_match"][["row_id"]].merge(cand4,on="row_id")[["row_id","name_norm","authorid","productivity","h_index"]]
    tie=pd.concat([amb,nm],ignore_index=True); tie.to_csv("r5_tie_candidates.csv",index=False,encoding="utf-8-sig")
    scan_details(tie, "r5_author_details.csv", "d.authorid, d.orcid, d.works_count, d.cited_by_count")
    print("平局候选:", len(tie))

# ============================================================ 步骤 13: R5 平局裁决
def step_round5():
    res=pd.read_csv("match_round4_result.csv"); eu=pd.read_excel(EU_XLSX)
    tie=pd.read_csv("r5_tie_candidates.csv"); det=pd.read_csv("r5_author_details.csv")
    a2inst=load_inst(["r2_author_inst_sets.parquet","r4_author_inst_sets.parquet"])
    a2field=load_field_dict(["r3_author_fields.parquet","r4_author_fields.parquet"])
    ikey={}
    for p in ["institutions_openalex.csv","institutions_openalex_r4.csv"]:
        for r in pd.read_csv(p).itertuples(index=False):
            if pd.notna(r.openalex_id): ikey[(r.inst_name,r.cc)]=r.openalex_id
    prod={r.authorid:(r.productivity if pd.notna(r.productivity) else 0) for r in tie.itertuples(index=False)}
    orcid={r.authorid:has_orcid(r.orcid) for r in det.itertuples(index=False)}
    row2c=tie.groupby("row_id")["authorid"].apply(list).to_dict()
    hosts=eu["Host Institution(s)"].map(parse_host)
    res["r5_matched_authorid"]=None; res["r5_conf"]=None
    for idx,row in res.iterrows():
        if row.match_status_r4 not in("ambiguous","no_match"): continue
        rid=int(row.row_id); cands=row2c.get(rid,[])
        if not cands: continue
        name,cc=hosts.iloc[rid]; oa=ikey.get((name,cc)); exp=expected_fields(eu["Panel"].iloc[rid],eu["Domain"].iloc[rid])
        scored=[(a,2*(1 if oa and oa in a2inst.get(a,set()) else 0)+(1 if exp and (a2field.get(a,set())&exp) else 0)) for a in cands]
        mx=max(s for _,s in scored); top=[a for a,s in scored if s==mx]
        if len(top)==1:
            res.at[idx,"r5_matched_authorid"]=top[0]; res.at[idx,"r5_conf"]="high"; continue
        wo=[a for a in top if orcid.get(a,False)]
        if len(wo)==1:
            res.at[idx,"r5_matched_authorid"]=wo[0]; res.at[idx,"r5_conf"]="medium"; continue
        ss=sorted(top,key=lambda a:prod.get(a,0),reverse=True)
        p1=prod.get(ss[0],0); p2=prod.get(ss[1],0) if len(ss)>1 else 0
        if p1>=5 and p1>=2*max(p2,1):
            res.at[idx,"r5_matched_authorid"]=ss[0]; res.at[idx,"r5_conf"]="low"
    res["match_status_r5"]=res.apply(lambda r:r.match_status_r4 if r.match_status_r4 in
        ("unique_match","unique_match_r2","unique_match_r3","unique_match_r4") else("unique_match_r5" if pd.notna(r.r5_matched_authorid) else r.match_status_r4),axis=1)
    res["authorid_final5"]=res.apply(lambda r:r.authorid_final4 if pd.notna(r.authorid_final4) else r.r5_matched_authorid,axis=1)
    res.to_csv("match_round5_result.csv",index=False,encoding="utf-8-sig")
    _write_final(res,"match_status_r5","authorid_final5")
    done=res.authorid_final5.notna().sum()
    print(f"R5 裁决: +{(res.match_status_r5=='unique_match_r5').sum()} -> 完成度 {done/len(res)*100:.1f}%")

# ============================================================ 步骤 14: 解析全部行 host
def step_resolve_all_hosts():
    eu=pd.read_excel(EU_XLSX); eu["row_id"]=eu.index
    resolve=build_host_resolver(); out=[]
    for _,r in eu.iterrows():
        name,cc=parse_host(r["Host Institution(s)"])
        oa=resolve(name,cc) if name else None
        out.append({"row_id":r["row_id"],"host_openalex_id":oa[0] if oa else None})
    df=pd.DataFrame(out); df.to_csv("val_host_ids.csv",index=False)
    print(f"全部行 host 解析: {df.host_openalex_id.notna().sum()}/{len(df)} -> val_host_ids.csv")

# ============================================================ 步骤 15: R6 别名增强召回
def step_recall_r6():
    r5=pd.read_csv("match_round5_result.csv")
    nm=r5[(r5.match_status_r5=="no_match")&(r5.name_norm.notna())][["row_id","name_norm"]]
    con=duck(); con.register("nm",nm)
    con.execute(r"""CREATE MACRO nf(x) AS trim(regexp_replace(regexp_replace(
        strip_accents(replace(lower(x),'ß','ss')),'[^a-z0-9]+',' ','g'),'\s+',' ','g'))""")
    con.execute(r"CREATE MACRO fl(s) AS str_split(s,' ')[1]||' '||str_split(s,' ')[len(str_split(s,' '))]")
    con.execute(r"CREATE MACRO srt(s) AS array_to_string(list_sort(str_split(s,' ')),' ')")
    con.execute("CREATE TEMP TABLE euk AS SELECT row_id,name_norm,nf(name_norm) kf,fl(nf(name_norm)) kfl,srt(nf(name_norm)) ks FROM nm")
    con.execute(f"""CREATE TEMP TABLE av AS
        SELECT authorid, display_name AS variant FROM '{DETAILS}' WHERE display_name IS NOT NULL
        UNION ALL SELECT authorid, unnest(from_json(display_name_alternatives,'["VARCHAR"]')) AS variant
        FROM '{DETAILS}' WHERE display_name_alternatives IS NOT NULL AND display_name_alternatives NOT IN ('','[]')""")
    con.execute("CREATE TEMP TABLE avk AS SELECT authorid,variant,nf(variant) kf,fl(nf(variant)) kfl,srt(nf(variant)) ks FROM av WHERE nf(variant)<>''")
    cand=con.execute("""
        SELECT DISTINCT e.row_id,e.name_norm,a.authorid,a.variant,'full' match_key FROM euk e JOIN avk a ON e.kf=a.kf
        UNION SELECT DISTINCT e.row_id,e.name_norm,a.authorid,a.variant,'firstlast' FROM euk e JOIN avk a ON e.kfl=a.kfl
        UNION SELECT DISTINCT e.row_id,e.name_norm,a.authorid,a.variant,'sorted' FROM euk e JOIN avk a ON e.ks=a.ks""").df()
    cand.to_csv("r6_candidates.csv",index=False,encoding="utf-8-sig")
    c=cand.groupby("row_id").authorid.nunique()
    print(f"R6 召回: {c.gt(0).sum()}/{len(nm)} (唯一={c.eq(1).sum()},多={c.gt(1).sum()})")

# ============================================================ 步骤 16: R6 候选特征
def step_features_r6():
    ids=pd.read_csv("r6_candidates.csv")[["authorid"]].drop_duplicates()
    scan_inst(ids,"r6_author_inst_sets.parquet"); scan_fields(ids,"r6_author_fields.parquet")
    scan_details(ids,"r6_author_details.csv")

# ============================================================ 步骤 17: R6 别名救回
def step_round6():
    res=pd.read_csv("match_round5_result.csv"); eu=pd.read_excel(EU_XLSX)
    cand=pd.read_csv("r6_candidates.csv"); host=pd.read_csv("val_host_ids.csv"); det=pd.read_csv("r6_author_details.csv")
    a2inst=load_inst(["r6_author_inst_sets.parquet"]); a2field=load_field_dict(["r6_author_fields.parquet"])
    a2orcid={r.authorid:has_orcid(r.orcid) for r in det.itertuples(index=False)}
    hid=dict(zip(host.row_id,host.host_openalex_id))
    full_by={}; all_by={}
    for r in cand.itertuples(index=False):
        all_by.setdefault(r.row_id,set()).add(r.authorid)
        if r.match_key=="full": full_by.setdefault(r.row_id,set()).add(r.authorid)
    res["r6_matched_authorid"]=None; res["r6_conf"]=None
    for idx,row in res.iterrows():
        if row.match_status_r5!="no_match": continue
        rid=int(row.row_id); cands=all_by.get(rid,set())
        if not cands: continue
        full=full_by.get(rid,set()); h=hid.get(rid); exp=expected_fields(eu["Panel"].iloc[rid],eu["Domain"].iloc[rid])
        def sc(a): return 2*(1 if isinstance(h,str) and h in a2inst.get(a,set()) else 0)+(1 if exp and (a2field.get(a,set())&exp) else 0)
        if len(full)==1:
            a=next(iter(full)); res.at[idx,"r6_matched_authorid"]=a; res.at[idx,"r6_conf"]="high" if sc(a)>0 else "medium"; continue
        pool=full if full else cands
        scored=[(a,sc(a)) for a in pool]; mx=max(s for _,s in scored); top=[a for a,s in scored if s==mx]
        if mx>0 and len(top)==1:
            res.at[idx,"r6_matched_authorid"]=top[0]; res.at[idx,"r6_conf"]="medium"; continue
        if full:
            wo=[a for a in top if a2orcid.get(a,False)]
            if len(wo)==1: res.at[idx,"r6_matched_authorid"]=wo[0]; res.at[idx,"r6_conf"]="low"
    res["match_status_r6"]=res.apply(lambda r:r.match_status_r5 if r.match_status_r5!="no_match"
        else("unique_match_r6" if pd.notna(r.r6_matched_authorid) else "no_match"),axis=1)
    res["authorid_final6"]=res.apply(lambda r:r.authorid_final5 if pd.notna(r.authorid_final5) else r.r6_matched_authorid,axis=1)
    res.to_csv("match_round6_result.csv",index=False,encoding="utf-8-sig")
    _write_final(res,"match_status_r6","authorid_final6")
    done=res.authorid_final6.notna().sum()
    print(f"R6 别名救回: +{(res.match_status_r6=='unique_match_r6').sum()} -> 完成度 {done/len(res)*100:.1f}%")

# --- 写最终交付 (R5/R6 共用) ---
def _write_final(res, status_col, aid_col):
    rb={"unique_match":"R1_name","unique_match_r2":"R2_name+inst","unique_match_r3":"R3_name+inst+field",
        "unique_match_r4":"R4_recover","unique_match_r5":"R5_tiebreak","unique_match_r6":"R6_alias"}
    fin=pd.DataFrame({"row_id":res.row_id,"Researcher(s)":res["Researcher(s)"],
        "authorid":res[aid_col],"resolved_by":res[status_col].map(lambda m:rb.get(m,"unresolved"))})
    def conf(i):
        m=res.loc[i,status_col]
        if m in("unique_match","unique_match_r2","unique_match_r3"): return "high"
        if m=="unique_match_r4": return "high" if res.loc[i,"r4_how"]=="disambig" else "medium"
        if m=="unique_match_r5": return res.loc[i,"r5_conf"]
        if m=="unique_match_r6": return res.loc[i,"r6_conf"]
        return ""
    fin["confidence"]=[conf(i) for i in res.index]
    fin["final_state"]=[ "matched" if rb_!="unresolved" else st
                         for rb_,st in zip(fin.resolved_by, res["match_status"]) ]
    fin.to_csv("matched_authors_final.csv",index=False,encoding="utf-8-sig")

# ============================================================ 步骤 18: 验证准备
def step_val_prep():
    fin=pd.read_csv("matched_authors_final.csv")
    ids=set(fin.loc[fin.authorid.notna(),"authorid"])
    have=set()
    for p in ["r2_author_inst_sets.parquet","r4_author_inst_sets.parquet","r6_author_inst_sets.parquet"]:
        have|=set(pd.read_parquet(p).authorid)
    pd.DataFrame({"authorid":sorted(ids-have)}).to_csv("_val_need_feat.csv",index=False)
    scan_details(pd.DataFrame({"authorid":sorted(ids)}), "val_matched_details.csv",
                 "d.authorid, d.orcid, d.display_name, d.works_count, d.cited_by_count")
    print("需补扫特征:", len(ids-have))

# ============================================================ 步骤 19: 补扫 R1 作者特征
def step_val_features():
    ids=pd.read_csv("_val_need_feat.csv")
    scan_inst(ids,"val_inst_sets.parquet"); scan_fields(ids,"val_fields.parquet")

# ============================================================ 步骤 20: 精度验证 + 并回最终文件
def step_validate():
    fin=pd.read_csv("matched_authors_final.csv").rename(columns={"Researcher(s)":"researcher"})
    m=fin[fin.resolved_by!="unresolved"].copy()
    det=pd.concat([pd.read_csv("val_matched_details.csv")[["authorid","orcid","display_name"]],
                   pd.read_csv("r6_author_details.csv")[["authorid","orcid","display_name"]]],
                  ignore_index=True).drop_duplicates("authorid")
    host=pd.read_csv("val_host_ids.csv"); eu=pd.read_excel(EU_XLSX); eu["row_id"]=eu.index
    a2name={r.authorid:r.display_name for r in det.itertuples(index=False)}
    a2orcid={r.authorid:has_orcid(r.orcid) for r in det.itertuples(index=False)}
    a2inst=load_inst(["r2_author_inst_sets.parquet","r4_author_inst_sets.parquet","val_inst_sets.parquet","r6_author_inst_sets.parquet"])
    a2field=load_field_dict(["r3_author_fields.parquet","r4_author_fields.parquet","val_fields.parquet","r6_author_fields.parquet"])
    hid=dict(zip(host.row_id,host.host_openalex_id))
    exp_map={r.row_id:expected_fields(r.Panel,r.Domain) for r in eu.itertuples(index=False)}
    nvs=lambda s: strip_accents(norm(s)) if norm(s) else None
    rows=[]
    for r in m.itertuples(index=False):
        aid=r.authorid; dn=a2name.get(aid,""); h=hid.get(r.row_id); exp=exp_map.get(r.row_id,set())
        ne=(norm(r.researcher) is not None and nvs(r.researcher)==nvs(dn))
        ic=bool(h) and (h in a2inst.get(aid,set())); fc=bool(exp) and bool(a2field.get(aid,set())&exp); oc=a2orcid.get(aid,False)
        rows.append({"row_id":r.row_id,"researcher":r.researcher,"authorid":aid,"openalex_name":dn,
                     "resolved_by":r.resolved_by,"confidence":r.confidence,
                     "name_exact":ne,"inst_corrob":ic,"field_corrob":fc,"orcid":oc,
                     "indep_support":int(ic)+int(fc)+int(oc)})
    ev=pd.DataFrame(rows)
    ev["risk_flag"]=["suspect" if (not ne and s==0) else ("weak" if s==0 else "ok") for ne,s in zip(ev.name_exact,ev.indep_support)]
    ev.to_csv("validation_evidence.csv",index=False,encoding="utf-8-sig")
    print("========== 独立印证率 (按轮次) ==========")
    for rb in ["R1_name","R2_name+inst","R3_name+inst+field","R4_recover","R5_tiebreak","R6_alias"]:
        g=ev[ev.resolved_by==rb]
        if len(g): print(f"{rb:<20} n={len(g):<5} 名字精确={g.name_exact.mean():.0%} 机构={g.inst_corrob.mean():.0%} "
                          f"领域={g.field_corrob.mean():.0%} ORCID={g.orcid.mean():.0%} ≥1独立={ (g.indep_support>=1).mean():.0%}")
    finf=pd.read_csv("matched_authors_final.csv")
    finf=finf[[c for c in finf.columns if c not in ("name_exact","inst_corrob","field_corrob","orcid","indep_support","risk_flag")]]
    finf=finf.merge(ev[["row_id","authorid","name_exact","inst_corrob","field_corrob","orcid","indep_support","risk_flag"]],
                    on=["row_id","authorid"],how="left")
    finf.to_csv("matched_authors_final.csv",index=False,encoding="utf-8-sig")
    n=len(ev); robust=((ev.name_exact)|(ev.indep_support>=1)).sum(); susp=((~ev.name_exact)&(ev.indep_support==0)).sum()
    print(f"稳健 {robust}/{n} ({robust/n*100:.1f}%); 疑似(suspect) {susp}. 证据列已并回 matched_authors_final.csv")

# ============================================================ 编排
STEPS = [
    ("R1 姓名直配", step_round1),
    ("下载OpenAlex机构字典(联网)", step_download_institutions),
    ("解析ambiguous行机构名->ID", step_resolve_inst_r2),
    ("ambiguous候选机构集合[重扫]", step_inst_r2),
    ("R2 姓名+机构", step_round2),
    ("ambiguous候选领域[重扫]", step_fields_r3),
    ("R3 姓名+机构+领域", step_round3),
    ("no_match首末名召回[重扫]", step_recall_r4),
    ("R4候选特征[重扫]", step_features_r4),
    ("解析no_match行机构名->ID", step_resolve_inst_r4),
    ("R4 no_match救回", step_round4),
    ("R5平局候选+ORCID[重扫]", step_prep_r5),
    ("R5 平局裁决", step_round5),
    ("解析全部行host(R6/验证需要)", step_resolve_all_hosts),
    ("R6别名增强召回[重扫]", step_recall_r6),
    ("R6候选特征[重扫]", step_features_r6),
    ("R6 别名救回", step_round6),
    ("验证准备[重扫]", step_val_prep),
    ("补扫R1作者特征[重扫]", step_val_features),
    ("精度验证+并回最终文件", step_validate),
]

def main():
    frm, only = 1, None
    if "--list" in sys.argv:
        for i,(d,_) in enumerate(STEPS,1): print(f"{i:>2}. {d}")
        return
    if "--from" in sys.argv: frm=int(sys.argv[sys.argv.index("--from")+1])
    if "--only" in sys.argv: only=int(sys.argv[sys.argv.index("--only")+1])
    import time
    for i,(desc,fn) in enumerate(STEPS,1):
        if only and i!=only: continue
        if not only and i<frm: continue
        print(f"\n{'='*66}\n[{i:>2}/{len(STEPS)}] {desc}\n{'='*66}",flush=True)
        t=time.time(); fn(); print(f"   完成 {time.time()-t:.0f}s",flush=True)
    print("\n完成 -> matched_authors_final.csv, validation_evidence.csv")

if __name__=="__main__":
    main()
