# -*- coding: utf-8 -*-
"""把标注结果并入 CN 最终匹配结果(以完整名单为底):
  · 完整名单 = cn.xlsx 全部学者行(rid 0 起);
  · 原本已有 authorid(cn.xlsx 第6列 gold,1949 个)→ 用原来的;
  · 原本没有 → 用本次标注 pick 的 authorid;
  · 最终仍为空的 → 丢弃。
产物: 覆写 cn/matched_final_cn.csv(结构与原文件一致,含全部有 id 的行)。
"""
import os
import sys

import duckdb
import openpyxl
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
os.chdir(os.path.dirname(os.path.abspath(__file__)))

OUT = "matched_final_cn.csv"

INST_KW = ["大学", "学院", "研究所", "研究院", "中心", "医院", "实验室", "学校",
           "研究中心", "科学院", "大學", "學院", "研究員", "所", "局", "系",
           "站", "厂", "公司", "集团"]


def _looks_inst(s):
    return isinstance(s, str) and any(k in s for k in INST_KW)


# ---- 1. 完整名单 + 原有 authorid(cn.xlsx 第6列,与 cn_match.load_cn 同口径)----
wb = openpyxl.load_workbook("cn.xlsx", read_only=True, data_only=True)
ws = wb["Sheet1"]
recs = []
for i, r in enumerate(ws.iter_rows(values_only=True)):
    if i == 0:
        continue
    seq, name, c2, c3, year, aid = (list(r) + [None] * 6)[:6]
    if name is None:
        continue
    if _looks_inst(c3) and not _looks_inst(c2):
        inst, field = c3, c2
    else:
        inst, field = c2, c3
    inst = inst.strip() if isinstance(inst, str) and inst.strip() else None
    field = field.strip() if isinstance(field, str) and field.strip() else None
    aid = str(aid).strip() if aid else None
    recs.append({"rid": i - 1, "cn_name": str(name).strip(), "field_text": field,
                 "inst_text": inst, "year": year, "gold_authorid": aid})
wb.close()
cn = pd.DataFrame(recs)
print("完整名单行数:", len(cn), "| 原有 authorid:", cn["gold_authorid"].notna().sum())

# ---- 2. 标注结果(rid -> pick authorid)----
summary = pd.read_csv("annotation_final_summary.csv", dtype=str, encoding="utf-8-sig")
picked = summary[summary["authorid"].notna() & (summary["authorid"].astype(str) != "")]
pick_aid = dict(zip(picked["rid"].astype(int), picked["authorid"].astype(str)))
print("标注勾选 rid:", len(pick_aid))

# ---- 3. 逐行取 authorid:原有优先,否则标注;都没有 → 丢弃 ----
cn["authorid"] = cn.apply(
    lambda r: r["gold_authorid"] if pd.notna(r["gold_authorid"])
    else pick_aid.get(int(r["rid"])), axis=1)
out = cn[cn["authorid"].notna()].copy()
out["eu_name"] = out["cn_name"]
out["match_type"] = out.apply(
    lambda r: "original" if pd.notna(r["gold_authorid"]) else "annotation", axis=1)
out["source"] = out.apply(
    lambda r: "original_gold" if pd.notna(r["gold_authorid"])
    else "annotation_llm", axis=1)

# host 桥
bridge = pd.read_csv("cn_host_ids_bridge.csv", dtype=str)
out["host_id"] = out["rid"].astype(int).map(
    dict(zip(bridge["row_id"].astype(int), bridge["host_openalex_id"])))

# n_papers:author_details 的 works_count
con = duckdb.connect()
con.register("ids", out[["authorid"]].drop_duplicates())
wc = con.sql("""
    SELECT authorid, works_count
    FROM '../sciscinet_author_details.parquet'
    WHERE authorid IN (SELECT authorid FROM ids)
""").df()
out["n_papers"] = out["authorid"].map(
    dict(zip(wc["authorid"], wc["works_count"]))).fillna("")

cols = ["rid", "eu_name", "authorid", "match_type", "host_id", "source",
        "n_papers", "cn_name", "field_text", "inst_text", "year", "gold_authorid"]
out = out[cols]
out["_ridn"] = pd.to_numeric(out["rid"], errors="coerce")
out = out.sort_values("_ridn").drop(columns="_ridn").reset_index(drop=True)
out.to_csv(OUT, index=False, encoding="utf-8-sig")

n_orig = int((out["match_type"] == "original").sum())
n_ann = int((out["match_type"] == "annotation").sum())
print("=" * 60)
print("最终结果行数:", len(out))
print("  其中原有 authorid:", n_orig, "(gold)")
print("  其中本次标注新增:", n_ann)
print("  标注∩原有(用原有):",
      len(set(picked["rid"].astype(int)) & set(cn[cn["gold_authorid"].notna()]["rid"].astype(int))))
print("  丢弃(仍为空):", len(cn) - len(out))
print("→", OUT)
