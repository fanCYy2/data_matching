# 交接: us_since1990.csv 机构名 → OpenAlex host id 映射(已完成)

日期: 2026-09-04 · 分支 `us`

## 目标与结论
把 `us_since1990.csv`(17400 个 NSF CAREER 奖)的 `Organization` 列映射到 OpenAlex host id,
产物与旧 `us_host_ids_bridge.csv` 同构(`row_id, host_openalex_id`),供 `us_match.py` 层2 直接读。

**已完成**: 机构级高置信 **569/574 (99.1%)**,weak 档清零,行级 **17397/17400**(剩 3 行是
TERC / QEM Network,本地库与在线 API 都查不到,显式判 none)。脚本重跑**零联网**、确定性。

## 脚本: `resolve_host_ids_us_since1990.py`(仓库根目录, 自包含)
- 复用 `us/resolve_host_ids_us.py` 那套 NSF 去壳归一链(治理前缀/基金会公司后缀/缩写展开/
  校区规整/SUNY·CUNY/截尾兜底), 一字未改。
- **解析优先级**(`resolve_one()`): `OVERRIDES`(人工, 现 53 条)> 复用旧表
  `us/us_host_ids_resolved.csv` 高置信档(317 个机构)> 本地 `affiliations.duckdb`
  (exact / token 倒排 + Jaccard-cover fuzzy, 阈值 0.60/0.80)> API 兜底 > duckdb top-1 弱猜。
- `OVERRIDES` 的 value 写 `(None, None)` 表示**人工确认查不到, 显式判 none** —— 宁可留空让
  层3/层4 裁决, 也不要挂错 host 在层2 放行同名错人。
- 产物: `us_since1990_host_ids_bridge.csv` / `us_since1990_host_ids_resolved.csv`(审计)/
  `api_cache_institutions_us.json`(API 缓存, 现 23 条)。

## 本轮修掉的两类【静默错配】(比 weak 档更危险, 值得记)
1. **美属领地另有国家码**。OpenAlex 把 PR/VI/GU/AS/MP 单列, 候选池原写死 `country_code='US'`,
   把波多黎各大学整体排除 → 4 个 UPR 校区全被错配到佛州分校 "Polytechnic University of Puerto
   Rico Miami", **其中 3 个还落在高置信 fuzzy 档**。改 `CC = ("US","PR","VI","GU","AS","MP")`。
2. **fuzzy jaccard=1.0 只说明词集相同, 不等于同一机构**。抽查按行数降序排的高置信档揪出:
   - `Washington University`(奖在 MO)→ 误配 University of Washington,**106 行**
   - `Purdue University` → 误配 Purdue University System(应为产出更多的 West Lafayette 主校),**233 行**
   - `University of Kansas Center for Research Inc` → 误配 KU Medical Center(应为 Lawrence 主校),**59 行**
   - `CUNY City College` → 误配佛州营利校 "City College"(citycollege.edu),**36 行**
   - 另有 Miami University Middletown / SUNY Oswego / CUNY York / UT Brownsville / Cal Poly Pomona 等 11 处
   **教训: 只审 weak 档不够, 高置信档必须按行数降序抽查** —— 错配集中在少数大机构。

## 两个可复用的批量审计手法
- **exact 档同名碰撞**: 拿 `Candidates.key2idx` 找"归一名对应 >1 个候选"的机构(靠 productivity
  拍板的都要看)。本次全库仅 1 处(Bethel University, 1 行), 说明 exact 档干净。
- **复用/prior 档**: 算 `inst_name` 与 `matched_name` 的 token 覆盖率, 挑 <0.75 的人工看。
  本次 36 个可疑全是**正确**去壳(Georgia Tech Research Corporation→Georgia Institute of
  Technology 等), 零误报 —— 旧 API 表可信。

## 已跑完的匹配管线(2026-09-04)
`us_match.py` 的输入本来就是环境变量可配, **没改代码**:
```bash
cd us && US_SOURCE_CSV=../us_since1990.csv US_HOST_BRIDGE=../us_since1990_host_ids_bridge.csv python us_match.py
```
旧 US.csv(3000 行)的结果在 `us/_backup_US3000/`(`merge_us_all.py`/`build_final_us.py` 会读它,
别当备份删);本数据集的 v1 结果已归档到 `D:\data_matching_archive\us\_v1_since1990\`。

**v2 bootstrap 顺序**(照 `us-layer3-abstract-semantic`, 全程 GPU/本地, 无联网):
v1 跑一次 → `extract_abstracts_us.py`(92GB 单次半连接扫描, **72 秒**, 得 262688 篇英文摘要)
→ `build_author_vectors_us.py`(278411 篇 → **23623 个作者向量**, npz)→ 再跑一次自动切 v2。

| 口径 | v1(子学科名 × Title+Program) | v2(作者摘要质心 × award Abstract) |
|---|---|---|
| 层1 名字唯一 | 1665 | 1665 |
| 层2 机构闸 | 9973 | 9973 |
| 层3 语义 | 785 | **3810** |
| 层4 兜底(论文数/近并列) | 3003 + 311 | **0 + 56** |
| 匹配成功 | 15737 (90.4%) | 15504 (**89.1%**) |
| 低产出待人工(<30 篇) | 1416 | 1649 |

**总匹配率反降 1.3 点不是回退**: 两版 matched+held 恒等于 17153, 差异全是 233 行从 matched 移进
低产出队列 —— v2 挑中的是论文更少的**真人**。A/B 佐证: 两版都匹配的 15363 行里 95.5% 同一作者,
**改判的 699 行 100% 来自层4 兜底(593 语义并列 + 65 论文数)或弱 v1 语义(41)**, 且 **92.7% 改判到
论文数更少的作者**(中位 280 → 110 篇)—— 正是"高产同名者遮蔽真人"被纠正的特征。

**阈值**沿用旧数据集标定值 `SEM_THRESH_V2=0.68 / SEM_MARGIN_V2=0.00`(同一 abstract-vs-abstract
任务, 可迁移);新数据集未重新标定, 想复核需要重建 `eval_sem_us.py`(仓库里已没有)。

## 剩下的欠账
- **低产出队列 1649 行**(<30 篇论文闸)+ **未匹配 247 行**: 可照 `eu-rescue-unmatched` 的思路做
  抢救(surname 召回 + host 硬准入 + fuzzy 锚定护栏 + 重复行传播), EU 那次把匹配率从 87% 拉到 90.6%。
- **层2 机构平局 4016 行**: 同机构多个同名候选, 现在全靠 v2 语义裁决 —— 这是精度上限所在。
- v2 的 89% acc@1 是旧数据集上的**银真值**估计, 非金标; ORCID 交叉核验仍是升级路。

## 相关文件
- `resolve_host_ids_us_since1990.py`(本次脚本)· `us/resolve_host_ids_us.py`(去壳链 + API 兜底来源)
- `us/us_host_ids_resolved.csv`(被复用的旧映射)· `affiliations.duckdb` · `us_since1990.csv`
- `build_us_since1990.py`(数据集构建, 见 `us-career-since1990`)
