# -*- coding: utf-8 -*-
"""分析 EU 140 个未匹配行:量化各补救路径(清洗、多姓氏、别名、快照缺失)可召回的数量。"""
import re
import unicodedata
import duckdb
import pandas as pd


def norm(x):
    """与 data_match.py 的 norm() 一致:连字符族->空格、去重音、小写、压缩空格。"""
    s = str(x)
    s = re.sub(r"[\u002d\u00ad\u2010-\u2015\u2212]", " ", s)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"\s+", " ", s.lower().strip())
    return s


def clean_tokens(name):
    """去掉称谓(Dr./Prof. 等)、逗号,拆成 token 列表。"""
    s = str(name)
    s = re.sub(r"\([^)]*\)", " ", s)                      # 括号昵称 (Charissa)/(Elise)
    s = re.sub(r"(?i)\b(dr|prof|professor|mr|mrs|ms|ing)\b\.?", " ", s)
    s = re.sub(r"(?i)\b(geb|geborne|geborene|ep|nee|n[ée]e)\b\.?", " ", s)  # 婚后/婚前标记
    s = re.sub(r"(?i)\s+(jr|sr|ii|iii|iv)\s*$", " ", s)   # 后缀
    s = s.replace(",", " ")
    return [t for t in norm(s).split() if t]


eu = pd.read_excel("EU.xlsx")
mf = pd.read_csv("matched_final.csv")
held = pd.read_csv("held_lowpaper.csv")
done = set(mf.rid) | set(held.rid)
u = eu.loc[[r for r in eu.index if r not in done]].copy()
u.index.name = "rid"
u = u.reset_index()
bridge = pd.read_csv("host_ids_bridge.csv")
host_map = dict(zip(bridge.row_id, bridge.host_openalex_id))
u["host_oa_id"] = u["rid"].map(host_map)

print("未匹配行:", len(u), "| 有 host_openalex_id:", int(u["host_oa_id"].notna().sum()))

named = u[u["Researcher(s)"].notna()].copy()
named["name"] = named["Researcher(s)"].astype(str)
named["toks"] = named["name"].apply(clean_tokens)
named["k_exact"] = named["name"].apply(norm)
named["k_fl"] = named["toks"].apply(lambda t: (t[0] + "|" + t[-1]) if t else "")
named["surnames"] = named["toks"].apply(lambda t: set(t[-3:]))

all_surnames = sorted(set().union(*named["surnames"]))
print("去重姓名:", named["name"].nunique(), "| 候选姓氏(末3 token):", len(all_surnames))

con = duckdb.connect()
con.sql("SET memory_limit='8GB'")
con.sql("SET temp_directory='.duckdb_tmp'")
con.sql(r"""
    CREATE MACRO norm(x) AS
    regexp_replace(
        trim(lower(strip_accents(
            regexp_replace(x, '[\x{002D}\x{00AD}\x{2010}-\x{2015}\x{2212}]', ' ', 'g')
        ))),
        '\s+', ' ', 'g')
""")

# Q1: 候选姓氏在作者表 display_name 末 token 里是否存在(一次全表扫描)
surname_present = set(
    con.execute(
        """
        SELECT DISTINCT lower(regexp_extract(display_name, '(\\S+)$', 1)) AS surname
        FROM '../sciscinet_authors.parquet'
        WHERE lower(regexp_extract(display_name, '(\\S+)$', 1)) IN (SELECT unnest(?))
        """,
        [all_surnames],
    ).df()["surname"]
)
print("Q1 姓氏存在性完成,命中:", len(surname_present))

# Q2: 清洗后整名在作者表 display_name 精确命中
name_rows = named[["name", "k_exact"]].drop_duplicates()
con.register("unmatched_names", name_rows)
exact_hits = set(
    con.sql(
        """
        SELECT DISTINCT u.k_exact
        FROM unmatched_names u
        JOIN '../sciscinet_authors.parquet' sci
          ON norm(sci.display_name) = u.k_exact
        """
    ).df()["k_exact"]
)
print("Q2 整名精确命中完成:", len(exact_hits))

# Q3: fl key(首|末)在作者表命中
fl_rows = named[["name", "k_fl"]].drop_duplicates()
con.register("unmatched_fl", fl_rows)
fl_hits = set(
    con.sql(
        """
        SELECT DISTINCT u.k_fl
        FROM unmatched_fl u
        JOIN '../sciscinet_authors.parquet' sci
          ON (regexp_extract(norm(sci.display_name), '^(\\S+)', 1) || '|' ||
              regexp_extract(norm(sci.display_name), '(\\S+)$', 1)) = u.k_fl
        """
    ).df()["k_fl"]
)
print("Q3 首|末 key 命中完成:", len(fl_hits))

# Q4: 别名表里整名/末 token 命中(对 Q1 缺的姓氏再查别名,避免漏)
missing_surnames = sorted(set(all_surnames) - surname_present)
alias_surname_hits = set()
if missing_surnames:
    pat = r"(?i)\b(" + "|".join(re.escape(s) for s in missing_surnames) + r")\b"
    alias_surname_hits = set(
        con.execute(
            """
            SELECT DISTINCT lower(regexp_extract(norm(alt), '(\\S+)$', 1)) AS surname
            FROM (
                SELECT unnest(string_split(display_name_alternatives, ',')) AS alt
                FROM '../sciscinet_author_details.parquet'
                WHERE display_name_alternatives IS NOT NULL
                  AND regexp_matches(display_name_alternatives, ?)
            )
            WHERE lower(regexp_extract(norm(alt), '(\\S+)$', 1)) IN (SELECT unnest(?))
            """,
            [pat, missing_surnames],
        ).df()["surname"]
    )
print("Q4 别名姓氏命中完成:", len(alias_surname_hits))

# 逐行归类
rows = []
for _, r in named.iterrows():
    surs = r["surnames"] & (surname_present | alias_surname_hits)
    rows.append(
        {
            "rid": r["rid"],
            "name": r["name"],
            "host_oa_id": r["host_oa_id"],
            "exact_hit": r["k_exact"] in exact_hits,
            "fl_hit": r["k_fl"] in fl_hits,
            "surname_hits": sorted(surs),
        }
    )

res = pd.DataFrame(rows)
try:
    res.to_csv("unmatched_140_diag.csv", index=False, encoding="utf-8-sig")
    print("明细已保存 unmatched_140_diag.csv")
except PermissionError:
    print("(无法写 unmatched_140_diag.csv,仅打印明细)")

print()
print("== 各补救路径可覆盖的去重姓名数(126 个姓名中) ==")
print("整名精确命中(清洗后):", res.drop_duplicates("name")["exact_hit"].sum())
print("首|末 key 命中:", res.drop_duplicates("name")["fl_hit"].sum())
print("姓氏存在于快照(作者表/别名):", res.drop_duplicates("name")["surname_hits"].apply(bool).sum())
print("完全无姓氏命中(快照里查无此人):",
      res.drop_duplicates("name")["surname_hits"].apply(lambda s: not s).sum())

print()
print("== 逐名明细 ==")
for _, r in res.drop_duplicates("name").sort_values("name").iterrows():
    print(f"{r['name']:<45} exact={int(r['exact_hit'])} fl={int(r['fl_hit'])} "
          f"surname={r['surname_hits'] if r['surname_hits'] else '--'}")
