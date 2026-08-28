# EU 研究者 → OpenAlex 作者 ID 匹配算法

## 目标

把 `EU.xlsx` 里每一行的研究者姓名（`Researcher(s)` 列），唯一地对应到 SciSciNet/OpenAlex
作者库里的一个 `authorid`。核心难点是**重名**：同名同姓的作者可能有多个，必须借助
**机构**和**研究领域**这两路外部信号把候选收敛到唯一一个。

实现全部在 [data_match.py](data_match.py)

## 输入数据

| 文件 | 作用 |
|------|------|
| `EU.xlsx` | 待匹配名单：`Researcher(s)`（姓名）、`Panel`/`Domain`（领域）、host 机构 |
| `sciscinet_authors.parquet` | 作者主表：`authorid` + `display_name`（层1 精确/模糊） |
| `sciscinet_author_details.parquet` | `display_name_alternatives` 别名数组（层1 别名） |
| `host_ids_bridge.csv` | 每个 EU 行 host 机构 → OpenAlex 机构 id 的桥表（层2） |
| `sciscinet_paper_author_affiliation.parquet` | 论文-作者-机构关系（层2 机构过滤） |
| `sciscinet_authors_paperid.parquet` | 作者-论文（层3 取该作者的论文；层4 数每个候选的论文数） |
| `sciscinet_paperfields.parquet` + `sciscinet_fields.parquet` | 论文-领域、领域字典（层3 算作者的 level-1 学科分布） |

`EU.xlsx` 注册成视图 `eu` 时加了一列行号 `rid`（从 0 起），作为全流程的行主键。

## 名字归一化工具（DuckDB 宏）

匹配前先把姓名归一，避免字符层面的假性不匹配：

- **`norm(x)`** —— 连字符/破折号家族（U+002D/00AD/2010–2015/2212）统一成空格 → 去重音（é→e）
  → 小写 → 去首尾、压缩空格。**只动连字符类**，不碰句点：否则 `Cynthia. Sharma` 这类残缺记录
  会精确命中并顶掉真人 `Cynthia M. Sharma`。
- **`fl(x)`** —— 取「首名|末名（姓）」，丢掉中间名/缩写。用于最松的兜底匹配
  （`Jonathan Lawrence Marchini` → `jonathan|marchini`）。
- **`wset(x)`** —— 把名字按词拆开、排序、再拼回，解决姓名顺序颠倒
  （`Madanbabu Mohan` == `Mohan Madanbabu`）。

## 四层匹配

整体是一个**逐层收敛**的漏斗：每层只处理上一层没能唯一确定的 `rid`，一旦某行被唯一确定就
「出局」（记入 `resolved`，带 `source` 标明是哪层定的），不再进入后续层。

```
所有 EU 行
   │
   ├─ 层1 名字匹配 ── 名字唯一命中 ─────────────► 确定 (source=name_unique)
   │        │
   │        └─ 一名多人（重名/模糊/别名候选）
   │                 │
   ├─ 层2 机构消歧 ── 机构过滤后只剩 1 人 ───────► 确定 (source=round2_inst)
   │                 │
   │                 ├─ 机构后仍 >1 人（平局）
   │                 └─ 机构信息缺失（无桥/没在该机构发过文）
   │                          │  两者合并
   ├─ 层3 语义领域消歧 ── cosine 判定 ───────────► 确定 (source=round3_semantic)
   │                          │
   │                          └─ 仍无法区分（歧义未消）
   │                                   │
   └─ 层4 兜底 ┬ 有候选过语义阈值 → 其中论文数最多者 ─► 确定 (source=round4_semtie)
              └ 无候选过阈值   → 全体候选论文数最多者 ─► 确定 (source=round4_maxpapers)
```



### 层1 名字匹配 （三种匹配精度）

对 `eu.Researcher(s)` 生成候选，三种 key **精度从高到低**，前层先占坑、后层排除已占的 `rid`，
避免松匹配盖掉高精度结果：

1. **精确 `exact`**（`match_1a`）：`norm(eu) = norm(display_name)`。 也即名字一模一样
2. **别名 `alias`**（`match_1b`）：仅对精确没配上的行，展开 `display_name_alternatives`
   别名数组，用 `norm`（整名变体）**或** `wset`（词序颠倒）相等召回。整串/整词集相等，精度高。
3. **首末名模糊 `fuzzy`**（`match_1c`）：仅对精确+别名都没配上的行，`fl()` 相等。最松、fan-out 大，
   **单独不可信**，必须靠层2 机构过滤才会被接受。

**层1 直接确定**：若某 `rid` 在精确结果里只有唯一一个 `authorid`（`exact_counts == 1`），
直接确定为 `name_unique`。其余（一名多人 / 别名 / 模糊候选）进入层2。

### 层2 · 机构消歧（`match_2`）

对层1 留下的候选，用 host 机构过滤：候选 `authorid` 必须在
`sciscinet_paper_author_affiliation` 里、正好挂过该 EU 行 host 机构（`host_ids_bridge.csv` 给的
`host_openalex_id`）。

- 机构过滤后**只剩 1 个** `authorid` → 确定（`round2_inst`）。
- 机构过滤后**仍 >1 个** → 「机构平局」，带机构过滤后的候选进层3。
- **机构缺失**（该行没有 host 桥，或候选谁都没在该机构发过文）→ 带层1 全部同名候选、
  `host_id` 置空，进层3（不因缺机构就丢弃）。

### 层3 · 语义领域消歧（`match_3` + `semantic_score`）

用「作者研究领域」和「EU 面板/领域文本」的语义相似度来区分剩下的候选。

**取数**：对每个候选 author，统计其论文在 OpenAlex **level-1 子学科**（284 个）上的
发文占比，取**占比前 3** 的子学科名（如 Astronomy / Optics / Astrophysics）。

**EU 侧文本 `eu_text`**：优先用 `Panel` 全名（去掉 `PE9 - ` 前缀 → `Universe Sciences`），
`Panel` 缺失时回退 `Domain`（去掉 `(PE)` 后缀），都缺则为 NULL（该行无法做语义比较）。

**打分**：用本地 `BAAI/bge-small-en-v1.5` 把领域名和 `eu_text` 编码成句向量（L2 归一），
`sim` = 候选前 3 领域名与 `eu_text` 的**最大 cosine**（取最像的那个领域）。

**判定（相对为主 + 绝对下限）**，对同一 `rid`：

1. 先取 `sim >= SEM_THRESH`（`0.62`）过阈值的候选；一个都不过 → 不定。
2. 只有 1 个过阈值 → 收该候选。
3. 多个过阈值，且第一名比第二名高出 `SEM_MARGIN`（`0.03`）→ 收最高分。
4. 多个过阈值但差距 `< margin` → 判为模糊。

> **以「同一 rid 内相对最高分」为主**，`SEM_THRESH` 只当「连最像的都明显不相关就不收」的安全闸。

### 层4 · 取论文数最大

层1–3 都没能唯一确定、**仍然有歧义**的 `rid`(同名候选收不敛)在这一层强制拍板,把所有还有
候选的 `rid` 都定下来(不再留人工),代价是牺牲一点精度换全覆盖。

进层4 的 `rid` 只有两类(由层3 判定逻辑决定):

- **A 类·语义近似平局**:有 ≥2 个候选过了 `SEM_THRESH`,但第一名比第二名高不出 `SEM_MARGIN`,
  分不开。这些候选**语义上都靠谱**。
- **B 类·无语义信号**:一个候选都没过阈值,或干脆算不了(没 `eu_text` / 候选没 level-1 论文)。

**关键:不丢掉层3 已算好的语义分。** 把每个候选是否过阈值(`passed = sim >= SEM_THRESH`)带进层4,
同一 `rid` 内排序键为 **`passed` 降序 → 论文数降序 → `authorid` 升序**,于是:

- A 类 → 只在**过阈值的候选**里取论文数最多者,把语义不相关的高产同名者挡在门外
  (`source=round4_semtie`,有语义背书,可信度较高)。
- B 类 → `passed` 全 `False`,退回**全体候选**取论文数最多者



## 输出

| 文件 | 内容 |
|------|------|
| `matched_final.csv` | **最终结果**：每个已唯一确定的 `rid` 一行，含 `authorid` / `match_type` / `source` |
| `match_1.csv` | 层1 全部名字候选（精确+别名+模糊），供人工核对 |
| `match_2.csv` | 层2 机构过滤后的候选 |
| `match_3.csv` | 层3 长表 + 每候选 `sim`，供人工核对语义打分 |
| `match_4.csv` | 层4 每个兜底 `rid` 选中的 `authorid`，含 `passed`(是否过语义阈值)/ `n_papers`，`source` 区分 `round4_semtie` 与 `round4_maxpapers`，供人工核对兜底决策 |

`final` 按 `rid` 去重、排序后写出，并打印筛选率与各 `source`/`match_type` 的分布。

## 人工验证抽样（`make_test_samples.py`）

没有标准答案,靠**分层抽样 + 人工核对**估精度。`make_test_samples.py` 从 `matched_final.csv`
按 `source` 分层随机抽样(种子固定、可复现),补一列可读的 `host` 机构名和一列空的
`yes_or_no`(人工填该匹配对不对)。

| 输出 | 来源层 | 抽样数 |
|------|--------|--------|
| `round2_sample_50.csv`  | `round2_inst`(层2 机构) | 50 |
| `round3_sample_50.csv`  | `round3_semantic`(层3 语义) | 50 |
| `round4_sample_100.csv` | `round4_semtie` + `round4_maxpapers`(层4 兜底) | 100 |

- `host` 机构名与 `host_id` 单一对齐:`host_id` 有值 → `openalex_institutions_eu.parquet` 的
  `canonical_name`;`host_id` 为空 → `'* '` + EU 原始 `Host Institution(s)`(`* ` 标记该行**没有
  机构过滤**,匹配只靠名字+语义,复核时更审慎)。
- 层4 兜底是全流程最低置信的一层,故抽样加倍(100)并额外并入 `n_papers`;其中
  `round4_maxpapers`(纯论文数、无语义背书)最该重点核查。

## 执行层说明（不影响匹配结果）

内存版 DuckDB 默认不溢写，直接扫 6.8GB 的 affiliation parquet 会 OOM，故设：
`temp_directory='.duckdb_tmp'`（溢写目录）、`memory_limit='12GB'`、`preserve_insertion_order=false`。
