# -*- coding: utf-8 -*-
"""Read-only test: does 'has Chinese papers' discriminate the gold author?
Population = v2 addressable set (1664 rids, 25887 candidate rows)."""
import sys, zipfile, re, time
import xml.etree.ElementTree as ET
import duckdb

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
T0 = time.time()
def el():
    return "[%5.0fs]" % (time.time() - T0)

con = duckdb.connect()
con.sql("SET temp_directory='D:/data_matching/cn/.duckdb_tmp'")
con.sql("SET memory_limit='12GB'")
con.sql("SET preserve_insertion_order=false")

# gold (rid, authorid) — same as v2
NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
z = zipfile.ZipFile(r"D:\data_matching\cn\cn.xlsx")
root = ET.parse(z.open("xl/sharedStrings.xml")).getroot()
shared = ["".join(t.text or "" for t in si.iter(NS + "t")) for si in root.findall(NS + "si")]
sheet = ET.parse(z.open("xl/worksheets/sheet1.xml")).getroot()
gold_rows = []
for ridx, row in enumerate(sheet.iter(NS + "row")):
    cells = {}
    for c in row.findall(NS + "c"):
        col = re.match(r"[A-Z]+", c.get("r")).group()
        v = c.find(NS + "v")
        if v is None:
            val = None
        elif c.get("t") == "s":
            val = shared[int(v.text)]
        else:
            val = v.text
        cells[col] = val
    aid = (cells.get("F") or "").strip()
    if aid:
        gold_rows.append((ridx - 1, aid))
con.execute("CREATE TEMP TABLE gold_raw(rid BIGINT, gold_authorid VARCHAR)")
con.executemany("INSERT INTO gold_raw VALUES (?, ?)", gold_rows)

con.sql("""
    CREATE TEMP TABLE gold AS
    SELECT g.rid, g.gold_authorid, h.host_openalex_id AS host
    FROM gold_raw g JOIN read_csv('D:/data_matching/cn/cn_host_ids_bridge.csv', header=true) h
      ON g.rid = CAST(h.row_id AS BIGINT)
    WHERE h.host_openalex_id IS NOT NULL AND h.host_openalex_id <> ''
""")
con.sql("""
    CREATE TEMP TABLE ehost AS
    SELECT DISTINCT authorid, institutionid
    FROM 'D:/data_matching/sciscinet_paper_author_affiliation.parquet'
    WHERE institutionid IN (SELECT DISTINCT host FROM gold)
""")
con.sql("""
    CREATE TEMP TABLE cah AS
    SELECT DISTINCT m.rid, m.authorid, g.host, g.gold_authorid
    FROM read_csv('D:/data_matching/cn/match_1_cn.csv', header=true) m
    JOIN gold g ON g.rid = m.rid
    JOIN ehost e ON e.authorid = m.authorid AND e.institutionid = g.host
    WHERE EXISTS (SELECT 1 FROM ehost e2
                  WHERE e2.authorid = g.gold_authorid AND e2.institutionid = g.host)
""")
n_rid = con.sql("SELECT count(DISTINCT rid) FROM cah").fetchone()[0]
n_cah = con.sql("SELECT count(*) FROM cah").fetchone()[0]
print("%s addressable: %d rids, %d rows" % (el(), n_rid, n_cah), flush=True)

# n_papers + zh stats per candidate author
con.sql("""
    CREATE TEMP TABLE gpc AS
    SELECT authorid, count(DISTINCT paperid) AS n_papers
    FROM 'D:/data_matching/sciscinet_authors_paperid.parquet'
    WHERE authorid IN (SELECT DISTINCT authorid FROM cah)
    GROUP BY 1
""")
print("%s scanning 92GB for zh/en per author..." % el(), flush=True)
con.sql("""
    CREATE TEMP TABLE lang_map AS
    WITH need AS (
        SELECT authorid, paperid FROM (
            SELECT authorid, paperid,
                   row_number() OVER (PARTITION BY authorid ORDER BY paperid) rk
            FROM 'D:/data_matching/sciscinet_authors_paperid.parquet'
            WHERE authorid IN (SELECT DISTINCT authorid FROM cah)
        ) WHERE rk <= 200
    )
    SELECT n.authorid, t.paperid, t.language
    FROM 'D:/data_matching/sciscinet_papertitleabstract.parquet' t
    JOIN need n ON t.paperid = n.paperid
""")
print("%s lang_map built" % el(), flush=True)
con.sql("""
    CREATE TEMP TABLE zh_stats AS
    SELECT authorid,
           count(*) FILTER (WHERE language = 'zh-cn') AS n_zh,
           count(*) FILTER (WHERE language = 'en') AS n_en,
           count(*) AS n_doc
    FROM lang_map GROUP BY authorid
""")

feat = con.sql("""
    SELECT c.rid, c.authorid, c.gold_authorid,
           COALESCE(p.n_papers, 0) AS n_papers,
           (c.authorid = c.gold_authorid) AS is_gold,
           COALESCE(z.n_zh, 0) AS n_zh, COALESCE(z.n_en, 0) AS n_en,
           COALESCE(z.n_doc, 0) AS n_doc
    FROM cah c LEFT JOIN gpc p ON p.authorid = c.authorid
    LEFT JOIN zh_stats z ON z.authorid = c.authorid
""").df()
print("%s feat rows %d" % (el(), len(feat)), flush=True)

groups = {r: g for r, g in feat.groupby("rid")}
N = len(groups)
print("=" * 66)

def by_papers(g):
    return g.sort_values(["n_papers", "authorid"], ascending=[False, True]).iloc[0]

def filt(keep):
    def f(g):
        s = g[keep(g)]
        return by_papers(s if len(s) else g)
    return f

def report(name, pick):
    hit = sum(bool(pick(g)["is_gold"]) for g in groups.values())
    print("  %-34s %4d/%d = %.1f%%" % (name, hit, N, 100.0 * hit / N), flush=True)

report("baseline argmax papers", by_papers)
for k in (1, 2, 3, 5, 10):
    report("zh>=%d + papers" % k, filt(lambda g, k=k: g["n_zh"] >= k))
for p in (0.05, 0.10, 0.20):
    report("zh_share>=%.0f%% + papers" % (p * 100),
           filt(lambda g, p=p: g["n_zh"] / g["n_doc"].clip(lower=1) >= p))
report("zh>=1 AND en>=1 + papers", filt(lambda g: (g["n_zh"] >= 1) & (g["n_en"] >= 1)))

# unique-holder auto rules
def unique_zh(k):
    def f(g):
        s = g[g["n_zh"] >= k]
        if len(s) == 1:
            return s.iloc[0]
        return by_papers(g)
    return f
for k in (1, 3):
    report("unique zh>=%d (auto, else papers)" % k, unique_zh(k))

# coverage of gold under each filter
for k in (1, 3, 5):
    n_g = sum(1 for g in groups.values() if g.loc[g["is_gold"], "n_zh"].max() >= k)
    n_f = sum(1 for g in groups.values() if (g["n_zh"] >= k).any())
    print("  filter zh>=%d: gold-covered %d/%d = %.1f%% | rows with any pass %d"
          % (k, n_g, N, 100.0 * n_g / N, n_f), flush=True)
print("=" * 66)
print("%s done" % el(), flush=True)
