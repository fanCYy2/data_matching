# SciSciNet/OpenAlex 学者匹配

把四个地区资助名单（EU / US / AUS / CN）的研究者匹配到 SciSciNet/OpenAlex 作者库的
`authorid`。核心难点是重名消歧，用「姓名 → 机构 → 语义领域 → 论文数兜底」四层漏斗收敛。

## 目录结构

```
sciscinet_*.parquet        原始输入库（SciSciNet 快照，见 data.md）
1990/ … 2023/              NSF 原始 award JSON（按年份，本地数据不入库）
fuzzyname_vendored.py      西方人名等价判断的 vendored 库（US/AUS 依赖）
check_author_id.py         小工具：查 authorid 是否在库里
requirements.txt           运行环境依赖
eu/                        EU ERC 名单（data_match.py 主流水线）
us/                        US NSF 名单（us_match.py）
aus/                       AUS ARC 名单（aus_match.py）
cn/                        中国学者名单（cn_match.py）
final/                     最终交付：各名单 + author_id 列
```

US 另有一批 1995-2023 的 NSF CAREER 奖（`us_since1990.csv`，17400 行），复用同一套管线但
脚本放在仓库根目录：

```
build_us_since1990.py              从年份 JSON 过滤出 CAREER 奖，对齐 us/US.csv 列格式
resolve_host_ids_us_since1990.py   Organization → OpenAlex host id（见 HANDOFF 文档）
us/merge_us_all.py                 两批 US 结果纵向拼成全量表（加 dataset 列区分来源）
build_final_us.py                  两批结果落成 final/ 交付格式
```

`us/matched_final_us.csv` 现在是 since1990 那批的结果；2024plus 那批（US.csv 3000 行）的结果
存在 `us/_backup_US3000/`，被上面两个合并脚本读取——名字像备份，其实是活输入，别删。

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
- `PROJECT_MEMORY.md`：跨区域方法论、四国管线现状与踩过的坑（持久记忆镜像）
- `VERIFY_PROTOCOL.md`：LLM 人工式作者标注协议（可复用，与区域无关）
- `HANDOFF_us_since1990_host_ids.md`：US since1990 机构桥的解析优先级与静默错配教训
- `eu/Algorithm.md`：EU 四层匹配算法细节
- `eu/rescue_summary.md`：EU 未匹配行抢救的做法与结果
- `cn/HANDOFF.md`、`cn/METHODS_TRIED.md`：CN 侧调研过程与试错记录（对应脚本仍在 `cn/`）

验证产生的样本、证据与缓存放在各区域的 `verify/` 下，不入库（见 `.gitignore`）。
