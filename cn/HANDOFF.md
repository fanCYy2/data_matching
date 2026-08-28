# 中国学者匹配 —— 调研交接文档

> 日期:2026-08-28 · 目标:把 CN 匹配准确率推到 95%+
> 一句话现状:**"加合著者/领域信号提精度"这条直觉被数据否掉了**。四层里现在
> 靠 argmax 论文数兜底(71% 的行走这条),已跑了大量探针定位病根。

> **⛔ 更新 2026-08-28(标题方案已跑,失败)**:`validate_title_cn.py`(S=524,GPU)
> 跑完,**title-content 也 regress**:baseline argmax papers 86.5%,所有
> `filter title-sim>=floor+argmax papers` 变体(floor 0.40–0.60)= 79.2/65.8/71.0/80.3/86.1%
> **全部 ≤ baseline**,纯 argmax title-sim 仅 31.3%,oracle 仍 93.9%。**病因=判别度≈0**:
> 真人 sim mean 0.449(p25 0.414)vs 冒名者 0.404(p75 0.442),差 0.045 且大面积重叠,
> 没有 floor 能留真人踢冒名者。跨语种英文标题 blob×中文领域短语被通用学术英语淹没。
> **field-content 整条路(tags+titles)到此为止,别再试标题/摘要变体。下一步 → §6 退路。**

> **⛔ 更新 2026-08-29(v2 聚类原型已跑,失败,field-content 最终判死)**:`validate_v2_cn.py`
> 全量(1664 可解决区)复刻 EU/US v2 的"同语言内容比较":中文领域文本聚类(K=50/100/200/400)
> 当面板,簇原型用 gold 真人的英文摘要质心建(近似 oracle 构造),候选作者摘要质心去比。
> 结果:baseline 85.9%;argmax v2-sim 仅 42–47%;acc@1(候选≥2)38–41%、零并列;
> 所有 `filter sim>=floor+papers` 变体 83.6–85.8% **全 ≤ baseline**。判别度 gold 0.84–0.86
> vs nongold 0.74–0.77,均值差 ~0.09 但重叠仍大(gold p25 ≈ nongold p75)。
> **给了最干净的实验条件(同语言 + 真答案建原型)真人都只 ~40% 排第一 → 领域/内容信号
> 在"同机构同名人"内部确实劈不开,共享属性诅咒第三次实证。field-content 整条路正式关闭,
> 不再试任何变体。主攻只剩 ORCID(§6.2/§8)。**

> **⛔ 更新 2026-08-29(2)(sciscinet_papertitleabstract 评估,中文维度也判死)**:该表
> (122,351,775 行;paperid/title/abstract_inverted_index/language;无年份/DOI/引用/机构)
> 之前已被标题实验和 v2 摘要实验用过。新增「中文论文存在性」离线评测(`probe_zh_cn.py`,
> zh>=1..10 / zh_share / unique-zh / zh∧en)全部 ≤ baseline 85.9%(69.8–84.1%),
> gold 在 zh>=1 过滤内覆盖仅 11.8% → 内容/语种维度彻底关闭。数据事实:gold 作者覆盖
> 99.7%,全表 zh-cn 366 万篇(96% 真中文标题),gold 行 31% 有 zh-cn 论文。**该表剩余价值 =
> 唯一本地 authorid→论文清单来源,供「外部检索锚点」(中文/英文标题+单位+年度 → 教师主页/
> ORCID) 路线,与 §6.2 ORCID 外部键同向;内部打分不再用。**

---

## 0. 怎么继续(给下一次)

```bash
cd d:/data_matching/cn
# 下一步就跑这个(gold 采样验证,~10 分钟,GPU):
S=600 K=16 python validate_title_cn.py
```
看输出里 `filter title-sim>=X + argmax papers` / `argmax title-sim` 有没有**明显超过
baseline 86% 并接近 oracle 94%**。超过 → 值得接进 `cn_match.py` 层3;没超过 → 见 §6 退路。

评测随时跑:`python eval_cn.py`(读已有的 `match_*_cn.csv`,不重训)。
全量重匹配(慢,会重算嵌入+大 join):`python cn_match.py`。

---

## 1. 指标与现状(`eval_cn.py` 实测)

真值:`cn.xlsx` 第6列 1949 个 authorid(1997–2009),仅评测用。

- **指标A 层1候选召回**:全部 1905/1949 = **97.7%**;可解析 98.0%。
  → **召回不是问题**。但候选池巨大:中位 202、均值 1874、90分位 7092 个同名候选/行。
- **指标B 端到端准确**:1332/1949 = **68.3%**。
- 按来源拆:
  | source | 命中/覆盖 | 精度 |
  |---|---|---|
  | round2_inst(机构唯一) | 100/121 | 82.6% |
  | round3_semantic(领域) | 53/177 | **29.9%** ← 语义层几乎没用 |
  | round4_maxpapers(论文数兜底) | 910/1208 | 75.3% |
  | round4_semtie | 269/442 | 60.9% |
- 错误分解:**召回到了但选错(消歧失败)= 573**;根本没召回 = 38。
  → **病在消歧,不在召回。** 573 个错全部 n_cand≥10(大池子里挑错)。
- 全量 4602 行的 source 分布:**round4_maxpapers 3278(71%!)**、round4_semtie 675、
  round2_inst 354、round3_semantic 295。host_id 有值 4255/4602。
  其中 round4_maxpapers **2985 行是"多个同名候选都在同一 host 机构"**,靠论文数硬掰。

**核心失败模式**(probe 确认):同机构多个同名者,现在选论文数最多的那个,
但真人往往论文更少 —— 225 个可解决区错误里 **100%** 是"真人论文数 < 被选中的同名者"
(中位:真人 271 篇 vs 选错的 786 篇)。**高产同名者遮蔽真人。**

---

## 2. 试过且**失败**的方案(别重复劳动)

所有数字在 gold 上实测。**baseline = 在候选里 argmax 全局论文数。**

| 方案 | 结果 | 结论 |
|---|---|---|
| host 论文数(候选在本机构发文数) | 84.1% | **< baseline 86.4%** |
| host 合著者数(合作者也在本机构的个数) | 78.8% | **< baseline**,越机构化越差 |
| bge-m3 阈值过滤(子学科名×中文领域,任意 floor) | 60–68% | **< baseline 74%(l3/4 全体)** |
| 离散化 top-K 最近子学科过滤 | 61–64% | **< baseline** |
| last_known_institution 救回丢失的 gold | 0/234 | 无效 |

**为什么机构/合著信号失效**(probe2 实测):
- `raw_affiliation_string` **整列 100% 为空**(原始机构文本匹配这条路直接作废)。
- `institutionid` **40% 的边缺失** → 任何"按机构计数"都在系统性漏计;全局论文数不受影响。

**为什么领域(field-tag)信号失效**(probe3 + `diag_field.py` 实测):
- 错误对里 66% 真人与选错者是**不同领域**(top-3 子学科零重叠)—— 信号本应存在。
- **但 OpenAlex 的作者子学科标签本身噪声极大、且无法从 NSFC 中文领域文本预测**:
  - `高分子材料学科` 的真人被 OpenAlex 标成 `Composite material / Organic chemistry`,不是 Polymer science;
  - `固体无机化学` 的真人被标成 `Artificial intelligence / Mathematical analysis`(纯垃圾)。
  - bge-m3 **其实读得懂中文术语**(色谱分析→Chromatography、环境化学→Environmental chemistry
    都对),死因是**目标标签脏**,不是翻译不准。

---

## 3. 已验证的天花板(oracle)

用 gold **自己的**子学科当"完美领域"过滤,再 argmax 论文数:

| 可解决区(gold 存活 host filter,1664 行) | 精度 |
|---|---|
| baseline argmax 论文数 | 85.9% |
| **oracle 领域过滤** | **94.1%** |

→ **领域信号确实值 +8 个点**,但 oracle 靠的是 gold 自己的(脏)标签自洽,
**无法从外部数据复现**。这就是为什么要换"干净的领域目标"(见下)。

另一个关键数:**可解决区(gold 在 host)= 87.7%**;剩下 12.3% 的 gold 根本不在 host 名下
(机构数据缺口,救不回)。全体 68% 里,很大一块是这些 noinst 行在几千个候选里瞎猜。

---

## 4. 选定的下一步:**标题内容匹配**(`validate_title_cn.py`,未跑)

**动机**:bge-m3 读得懂中文领域词,缺的是**干净的领域目标**。用候选作者的**真实论文标题**
(你有 92GB `sciscinet_papertitleabstract.parquet`,W-id 直连)代替 OpenAlex 的脏标签,
绕开 §2 的两个死因。这就是 EU 侧 `layer3-v2` 的思路,CN 从没建过。

**脚本做什么**:gold 的可解决区采样 S 个 rid → 每个 host 存活候选取 K 篇标题拼接 → bge-m3
向量 → 对中文领域文本算 cosine → 对比 baseline(86%)/oracle(94%),并扫过滤 floor。
1 个作者只嵌 1 次(拼接,max_length=256),够快。

**判据**:某个 title 变体明显 >86% 且接近 94% → 接进 `cn_match.py` 的层3(把语义层从
"过阈值+margin 才定档"的罕见决胜项,改成"标题相似度过滤 + 论文数决胜")。
**注意**:标题多为英文、领域词为中文,靠 bge-m3 跨语种;这是本方案唯一的赌点。

---

## 5. 文件清单(都在 `d:/data_matching/cn/`)

**生产代码(未改动)**
- `cn_match.py` —— 四层主流水线。层1 拼音变体→exact/alias/fuzzy;层2 host 机构闸;
  层3 v1 语义(子学科名×中文领域,bge-m3);层4 论文数兜底。**本次没动它。**
- `chinese_names.py` —— 中文名→拼音变体(pypinyin + 多音姓覆盖)。
- `eval_cn.py` —— 评测指标A/B,产 `eval_cn_errors.csv`。
- `sweep_thresh_cn.py` —— 原有:层3 阈值×margin 网格(证明 bge-m3 决胜精度只有 29.9%)。

**本次调研脚本(一次性,可留作证据)**
- `coauthor_probe.py` —— 探针,当前停在 probe4(oracle 94.1%)。曾迭代过 co-author/coverage/field 版本。
- `sweep_filter_cn.py` —— field-filter floor 扫描(当前是离散 top-K 版,证明都 < baseline)。
- `diag_field.py` —— 打印 领域文本 vs gold 真实子学科 vs bge-m3 映射(暴露标签脏)。
- `validate_title_cn.py` —— **标题内容匹配验证,下一步跑这个。**

**数据产物**
- `match_1_cn.csv`(375MB,层1 全候选)、`match_3_cn.csv`(166MB,含每候选 `sim`)、
  `match_4_cn.csv`、`matched_final_cn.csv`、`eval_cn_errors.csv`、`cn_host_ids_bridge.csv`。

---

## 6. 若标题方案也不行 —— 退路(按优先级)

1. **置信度门控** —— ❌ **已试,封顶(`gate_cn.py`,2026-08-28)**:唯一干净信号 host 唯一存活
   只 82.6%(institutionid 40% 缺边 + host 桥偶错钉死);加"也是论文数最大"到 92.9% 但覆盖仅 4.3%;
   到 95% 的切片覆盖<1%。论文数 margin 是假置信陷阱(领先 100+ 篇仍只 72.8%)。
   **内部门控最多 85%@50% 覆盖 / 93%@4% 覆盖,够不到 95%+有用覆盖。**
2. **ORCID 外部键** —— ✅ **键完美干净,唯一能到 95% 的路**:gold 真人 **85.9%(1675/1949)有 orcid**,
   **0 冲突、orcid→authorid 全库 1:1**。拿到杰青 ORCID 即唯一命中(~99% 精度/86% 覆盖)。
   **瓶颈纯在外部获取**(全库仅 7.6% 有 orcid,不能反查名字)。可行:枚举 recalled 同名候选里带 orcid 的
   (真候选 88% 带),取其 ORCID 档案对 NSFC 名+机构核验消歧。**← 下一步主攻这条。**
3. **年度窗口**:NSFC 获批年度约束候选活跃期。**但当前数据集没有 paper-year 表**,
   需先找到论文年份来源才能做。

---

## 7. 待你拍板的问题

- **"95% 准确率"的定义**:是 *auto 自动判的子集精度*,还是 *全量端到端(指标B)*?
  —— 直接决定要不要走 §6.1 的人工路由(牺牲覆盖换精度)。
- 是否愿意为 §6.2 补 ORCID(需要外部抓取/对照)。

> **✅ 用户已拍板(2026-08-28)**:"95%"= **auto 子集精度**(可牺牲覆盖,难行路由人工);
> ORCID **平行推进**,获取路线 = **公开 API + 候选核验**;先搭纯内部 harness。见 §8。

---

## 8. ORCID 路(选定主攻)—— 内部 harness 已完成

**判决**:内部消歧信号全部榨干(§2 field-content + §6.1 置信度门控均封顶,`gate_cn.py` 实测
内部门控最多 85%@50% / 93%@4%,够不到 95%+有用覆盖)。**ORCID 是唯一能到 auto 95% 的路。**

**ORCID 键干净度(实测 gold=1949)**:真人 87.2% 有 orcid;**0 冲突**;**orcid→authorid 全库 1:1**。

**关键陷阱**:**orcid 列本身不消歧** —— 每行中位 ~15 个同名候选都带 orcid(各是不同真人),
"恰好1个带orcid"只 2.1%。故必须靠 **ORCID 档案(姓名+机构履历)核验**挑出哪个 orcid 属于杰青。

**候选核验天花板 = 100% 精度 @ 86.9% 覆盖**(gold ∈ recalled∧带orcid候选 = 1693/1949)。

**已建**(`orcid_harness_cn.py`,cwd 无关):
- `cand_orcids_cn.csv` —— API worklist:每行"带 orcid 的同名候选"。577,699 行 / **去重 172,148 个 orcid**;
  其中 **host 存活带 orcid 仅 23,671**(7x 小)。
- 评测器 + 免费信号(sole host-survivor-with-orcid 86%@31%,不够)。

**下一步(外向,待点头)= ORCID 公开 API 档案核验**:
1. **分级抓**:先抓 host 存活 orcid(2.4万,OpenAlex 有边),再对缺边行用宽 worklist 靠 ORCID
   独立机构履历补回被 OpenAlex 40% 缺边丢掉的真人。
2. **前置缺口**:数据集**无带 ROR 的机构表**(只有 OpenAlex institutionid)。要 ROR↔ROR 干净核验,
   需先补 OpenAlex institutions(id→ROR→名,~10万行);否则退化为 ORCID 英文机构名×NSFC 中文机构名。
3. 核验通过才定档(auto),其余人工 —— 契合 auto 子集精度口径。
