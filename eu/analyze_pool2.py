# -*- coding: utf-8 -*-
"""第2轮:去掉单token名后的候选池 + 词集倒序(wset)召回 + pool=0 名单的逐名核查。"""
import re
import unicodedata
import duckdb
import pandas as pd


def norm(x):
    s = str(x)
    s = re.sub(r"[\u002d\u00ad\u2010-\u2015\u2212]", " ", s)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", s.lower().strip())


def clean_tokens(name):
    s = str(name)
    s = re.sub(r"\([^)]*\)", " ", s)
    s = re.sub(r"(?i)\b(dr|prof|professor|mr|mrs|ms|ing)\b\.?", " ", s)
    s = re.sub(r"(?i)\b(geb|geborne|geborene|ep|nee|n[ée]e)\b\.?", " ", s)
    s = re.sub(r"(?i)\s+(jr|sr|ii|iii|iv)\s*$", " ", s)
    s = s.replace(",", " ")
    return [t for t in norm(s).split() if t]


eu = pd.read_excel("EU.xlsx")
mf = pd.read_csv("matched_final.csv")
held = pd.read_csv("held_lowpaper.csv")
done = set(mf.rid) | set(held.rid)
u = eu.loc[[r for r in eu.index if r not in done]].copy()
u.index.name = "rid"
u = u.reset_index()

named = u[u["Researcher(s)"].notna()].copy()
named["name"] = named["Researcher(s)"].astype(str)
named["toks"] = named["name"].apply(clean_tokens)
named = named[named["toks"].apply(len) >= 2]
named["given"] = named["toks"].apply(lambda t: list(dict.fromkeys(t[:-1])))
named["sur1"] = named["toks"].apply(lambda t: t[-1])
named["sur2"] = named["toks"].apply(lambda t: t[-2] if len(t) >= 2 else None)
named["wset"] = named["toks"].apply(lambda t: " ".join(sorted(t)))

nm = named[["name", "given", "sur1", "sur2", "wset"]].drop_duplicates("name")
con = duckdb.connect()
con.sql("SET memory_limit='8GB'")
con.sql("SET temp_directory='.duckdb_tmp'")
con.register("nm", nm)

# 作者表:首/末 token(排除单token名) + 词集 key
base = con.sql(
    """
    SELECT lower(regexp_extract(trim(lower(strip_accents(display_name))), '^(\\S+)', 1)) AS f,
           lower(regexp_extract(trim(lower(strip_accents(display_name))), '(\\S+)$', 1)) AS l,
           array_to_string(list_sort(string_split(trim(lower(strip_accents(display_name))), ' ')), ' ') AS w
    FROM '../sciscinet_authors.parquet'
    WHERE trim(display_name) LIKE '% %'
    """
)
con.register("base", base)

pool1 = con.sql(
    """
    SELECT n.name, count(*) AS c
    FROM nm n JOIN base b ON b.l = n.sur1 AND list_contains(n.given, b.f)
    GROUP BY n.name
    """
).df().rename(columns={"c": "p1"})
pool2 = con.sql(
    """
    SELECT n.name, count(*) AS c
    FROM nm n JOIN base b ON b.l = n.sur2 AND list_contains(n.given, b.f)
    GROUP BY n.name
    """
).df().rename(columns={"c": "p2"})
wset_hits = con.sql(
    """
    SELECT n.name, count(*) AS c
    FROM nm n JOIN base b ON b.w = n.wset
    GROUP BY n.name
    """
).df().rename(columns={"c": "wset_hit"})

pool = nm.merge(pool1, on="name", how="left")
pool = pool.merge(pool2, on="name", how="left")
pool = pool.merge(wset_hits, on="name", how="left")
for c in ["p1", "p2", "wset_hit"]:
    pool[c] = pool[c].fillna(0).astype(int)
pool["pmax"] = pool[["p1", "p2"]].max(axis=1)

print("== 放松匹配(去单token)候选池分布 ==")
print("姓名数:", len(pool))
for lo, hi, label in [(0, 0, "0"), (1, 1, "1"), (2, 5, "2-5"),
                      (6, 20, "6-20"), (21, 10**9, ">20")]:
    print(f"pool {label:<5}: {((pool['pmax'] >= lo) & (pool['pmax'] <= hi)).sum()}")
print("wset 倒序整名命中:", (pool["wset_hit"] > 0).sum())
print("pool=0 但 wset>0:", ((pool["pmax"] == 0) & (pool["wset_hit"] > 0)).sum())
print()
print("== pool=0 名单(含 wset 是否可救) ==")
for _, r in pool[pool["pmax"] == 0].sort_values("name").iterrows():
    print(f"{r['name']:<45} wset_hit={r['wset_hit']}")
print()
print("== 全部明细(p1/p2/wset/pmax) ==")
for _, r in pool.sort_values(["pmax", "name"]).iterrows():
    print(f"{r['name']:<45} p1={r['p1']:<4} p2={r['p2']:<4} wset_hit={r['wset_hit']:<3} max={r['pmax']}")
