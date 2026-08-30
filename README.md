# SciSciNet/OpenAlex 学者匹配

把四个地区资助名单（EU / US / AUS / CN）的研究者匹配到 SciSciNet/OpenAlex 作者库的
`authorid`。核心难点是重名消歧，用「姓名 → 机构 → 语义领域 → 论文数兜底」四层漏斗收敛。

## 目录结构

```
sciscinet_*.parquet        原始输入库（SciSciNet 快照，见 data.md）
fuzzyname_vendored.py      西方人名等价判断的 vendored 库（US/AUS 依赖）
check_author_id.py         小工具：查 authorid 是否在库里
peek_parquet.py            小工具：预览任意 parquet 结构
requirements.txt           运行环境依赖
eu/                        EU ERC 名单（data_match.py 主流水线）
us/                        US NSF 名单（us_match.py）
aus/                       AUS ARC 名单（aus_match.py）
cn/                        中国学者名单（cn_match.py）
final/                     最终交付：各名单 + author_id 列
```

## 运行环境

```bash
uv venv --python 3.14 .venv
uv pip install -r requirements.txt
```

语义层使用本地 `BAAI/bge-small-en-v1.5`（transformers 首次运行会自动下载）。
机器无 GPU 时自动回退 CPU。大表扫描会用到内存溢写（`.duckdb_tmp`），建议 12GB+ 内存。

## 复现流程（各区域）

每区域链路一致：`resolve_host_ids_*.py` 解析 host 机构 → `*_match.py` 四层匹配 →
`matched_final_*.csv`（+ `held_lowpaper_*.csv` 低产待人工）→ 拼回原名单得 `final/*_final.csv`。

```bash
# EU（从 eu/ 目录运行）
python data_match.py

# US
python us_match.py

# AUS
python aus_match.py

# CN（cn/ 目录；先建名字索引可大幅提速）
python build_name_index_cn.py
python cn_match.py
```

各主脚本的产物与参数见脚本头部注释；语义层 v2（摘要文本相似度）需要先跑
`extract_abstracts_*.py` + `build_author_vectors_*.py` 生成作者向量，缺失时自动回退 v1
（子学科名×领域文本）。

### 最终交付拼装

`final/*_final.csv` = 原始名单全表 + `author_id` 列，`author_id` 来自：

| 区域 | 来源 |
|---|---|
| eu_final.csv | `eu/matched_final.csv` + `eu/rescue_matched.csv` + 低产/未匹配留空 |
| us_final.csv | `us/matched_final_us.csv` + 低产留空 |
| aus_final.csv | `aus/matched_final_aus.csv` + 低产留空 |
| cn_final.csv | `cn/matched_final_cn.csv`（含原有 gold 与标注结果） |

## 文档

- `data.md`：原始 parquet 的表结构与行数
- `eu/Algorithm.md`：EU 四层匹配算法细节
- `cn/HANDOFF.md`、`cn/METHODS_TRIED.md`：CN 侧调研过程与试错记录
