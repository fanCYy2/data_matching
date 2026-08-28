# -*- coding: utf-8 -*-
"""
离线预计算:把 ERC 分类数据集(SIRIS-Lab/erc-classification-dataset)的 test_panels 划分
(每个 ERC 面板 ~50 条干净单标签样本)嵌入后按面板取质心 -> 28 个"面板原型向量"。
产物 erc_panel_prototypes.npz 供 data_match.py 层3(v2)用。

用 test_panels(单标签、均衡、~100/panel)而非 train(LLM 伪标注、有噪声)建原型。
"""
import numpy as np
import pandas as pd
from huggingface_hub import hf_hub_download

from erc_embed import embed, centroid, PROTO_FILE

DATASET = "SIRIS-Lab/erc-classification-dataset"
SPLIT = "data/test_panels-00000-of-00001.parquet"

df = pd.read_parquet(hf_hub_download(DATASET, SPLIT, repo_type="dataset"))
# label 是 list;test_panels 为单标签,取第一个
df["panel"] = df["label"].map(lambda x: list(x)[0] if len(x) else None)
df = df.dropna(subset=["panel"]).copy()
df["doc"] = df["title"].fillna("").astype(str) + ". " + df["abstract"].fillna("").astype(str)

labels, mats = [], []
for panel, g in df.groupby("panel"):
    v = centroid(embed(g["doc"].tolist(), maxlen=256))
    labels.append(panel)
    mats.append(v)

mat = np.vstack(mats).astype("float32")
np.savez(PROTO_FILE, labels=np.array(labels, dtype=object), mat=mat)
print(f"已保存 {PROTO_FILE}:{len(labels)} 个面板原型,维度 {mat.shape}")
for l in labels:
    print("  ", l)
