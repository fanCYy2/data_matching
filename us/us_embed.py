# -*- coding: utf-8 -*-
"""US 层3(v2 原生 abstract-vs-abstract)共享嵌入工具。

build_author_vectors_us.py 与 us_match.py 共用同一 bge 模型 / CLS 池化 / L2 归一,
保证【作者论文摘要质心】与【NSF award Abstract 查询向量】落在同一空间、可直接做 cosine。

与 EU 的 erc_embed.py 相比:去掉了 ERC 面板原型那一套(US 不需要外部分类体系,
两侧都有富文本:award Abstract vs 作者论文 title+abstract)。

产物文件:
  author_vectors_us.npz   ids(N,) + mat(N,384) + ndoc(N,)  —— US 候选作者论文文本质心
"""
import os
import functools
import numpy as np

EMB_MODEL = "BAAI/bge-small-en-v1.5"
AUTHORVEC_FILE = "author_vectors_us.npz"
EMB_DIM = 384


@functools.lru_cache(maxsize=1)
def _model():
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    import torch
    from transformers import AutoTokenizer, AutoModel
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    tok = AutoTokenizer.from_pretrained(EMB_MODEL)
    mdl = AutoModel.from_pretrained(EMB_MODEL).to(dev)
    mdl.eval()
    return tok, mdl, torch, dev


def embed(texts, maxlen=256, bs=64):
    """CLS 池化 + L2 归一。texts: list[str](None 视为空串)。返回 (n, 384) float32。

    maxlen:富文本(award Abstract / 论文 title+abstract)用 256;短标签(子学科名)可传 64。
    """
    texts = ["" if t is None else str(t) for t in texts]
    tok, mdl, torch, dev = _model()
    out = []
    with torch.no_grad():
        for i in range(0, len(texts), bs):
            enc = tok(texts[i:i + bs], padding=True, truncation=True,
                      max_length=maxlen, return_tensors="pt").to(dev)
            v = mdl(**enc).last_hidden_state[:, 0]          # bge: CLS pooling
            v = torch.nn.functional.normalize(v, p=2, dim=1)
            out.append(v.cpu().numpy())
    return np.vstack(out).astype("float32") if out else np.zeros((0, EMB_DIM), "float32")


def centroid(vecs):
    """一组已归一向量取均值再 L2 归一;空 -> None。"""
    if vecs is None or len(vecs) == 0:
        return None
    m = np.asarray(vecs, dtype="float32").mean(0)
    n = np.linalg.norm(m)
    return (m / n).astype("float32") if n > 0 else m.astype("float32")


def load_author_vectors(path=AUTHORVEC_FILE):
    """返回 (id2idx:dict[str,int], mat:(N,384))。"""
    d = np.load(path, allow_pickle=True)
    ids = [str(x) for x in d["ids"]]
    return {a: i for i, a in enumerate(ids)}, d["mat"].astype("float32")
