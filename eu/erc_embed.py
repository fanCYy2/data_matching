# -*- coding: utf-8 -*-
"""
C2 语义领域打分(v2)共享工具。被 build_panel_prototypes.py / build_author_vectors.py /
data_match.py 共用,保证三者的 bge 模型、池化(CLS)、归一化(L2)、设备(cuda/cpu)完全一致。

产物文件:
  erc_panel_prototypes.npz  labels(P,) + mat(P,384)     —— 28 个 ERC 面板的原型质心
  author_vectors.npz        ids(N,)   + mat(N,384) + ndoc(N,) —— 候选作者论文文本质心
"""
import os
import re
import functools
import numpy as np
import pandas as pd

EMB_MODEL = "BAAI/bge-small-en-v1.5"
PROTO_FILE = "erc_panel_prototypes.npz"
AUTHORVEC_FILE = "author_vectors.npz"
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
    """CLS 池化 + L2 归一。texts: list[str]。返回 (n, 384) float32。"""
    texts = list(texts)
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


def norm_panel(s):
    """面板名规范化:小写、& -> and、压空格。用于 eu_text <-> 数据集 panel 标签的字符串直配。"""
    return re.sub(r"\s+", " ", str(s).lower().replace("&", "and")).strip()


def load_prototypes(path=PROTO_FILE):
    """返回 (labels:list[str], mat:(P,384), norm2idx:dict[str,int])。"""
    d = np.load(path, allow_pickle=True)
    labels = [str(x) for x in d["labels"]]
    mat = d["mat"].astype("float32")
    return labels, mat, {norm_panel(l): i for i, l in enumerate(labels)}


def load_author_vectors(path=AUTHORVEC_FILE):
    """返回 (id2idx:dict[str,int], mat:(N,384))。"""
    d = np.load(path, allow_pickle=True)
    ids = [str(x) for x in d["ids"]]
    return {a: i for i, a in enumerate(ids)}, d["mat"].astype("float32")


def score_semantic_v2(long_df, keys=("rid", "eu_name", "authorid", "match_type", "host_id")):
    """层3 语义打分(v2)。data_match.py 与 eval_sem_v2.py 共用同一实现,避免漂移。

    输入 long_df:match_3() 的长表(每 (rid,authorid) 最多 3 行),需含列
    rid, authorid, eu_text, field_name(可空)以及 keys 里的标识列。
    返回:每 (rid,authorid) 一行 + 列 sim(v2 语义分;NaN = 无法打分)。

    打分 sim = cosine(作者表征, 该 rid 的 eu_text 面板原型):
      - 作者表征:作者在 author_vectors.npz 里 -> 论文文本质心(富信号);
                  否则 -> 其 top-3 field_name 质心(回退,和原型同一目标空间,保证同 rid 可比)。
      - eu_text -> 原型:规范化后与 28 个面板标签字符串直配;不中则取 eu_text 向量最近的原型
                  (Domain 级粗文本也能落到最接近的细面板)。
    需 PROTO_FILE 与 AUTHORVEC_FILE 存在。
    """
    keys = list(keys)
    df = long_df.copy()
    labels, pmat, norm2i = load_prototypes()
    aid2i, amat = load_author_vectors()

    # eu_text(每 rid 一个)-> 原型向量
    eu_texts = [t for t in pd.unique(df["eu_text"].dropna())]
    need_near = [t for t in eu_texts if norm_panel(t) not in norm2i]
    near_vec = dict(zip(need_near, embed(need_near, maxlen=64))) if need_near else {}
    eu_proto = {}
    for t in eu_texts:
        k = norm_panel(t)
        eu_proto[t] = pmat[norm2i[k]] if k in norm2i else pmat[int((pmat @ near_vec[t]).argmax())]

    # 无文本作者的回退表征:其 top-3 field_name 的质心
    per = (df.dropna(subset=["field_name"])
             .groupby("authorid")["field_name"].apply(lambda s: list(pd.unique(s))))
    fb_authors = [a for a in pd.unique(df["authorid"]) if a not in aid2i]
    fb_names = sorted({n for a in fb_authors if a in per.index for n in per[a]})
    fnv = dict(zip(fb_names, embed(fb_names, maxlen=64))) if fb_names else {}
    fb_vec = {}
    for a in fb_authors:
        if a in per.index:
            fb_vec[a] = centroid(np.vstack([fnv[n] for n in per[a]]))

    et = df.dropna(subset=["eu_text"]).groupby("rid")["eu_text"].first().to_dict()

    def rep(a):
        return amat[aid2i[a]] if a in aid2i else fb_vec.get(a)

    base = df[keys].drop_duplicates().reset_index(drop=True)
    sims = []
    for r, a in zip(base["rid"], base["authorid"]):
        t = et.get(r)
        v = rep(a)
        if not isinstance(t, str) or t not in eu_proto or v is None:
            sims.append(np.nan)
        else:
            sims.append(float(v @ eu_proto[t]))
    base["sim"] = sims
    return base
