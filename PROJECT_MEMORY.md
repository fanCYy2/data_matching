# 项目记忆(资助名单 → OpenAlex 作者/机构 匹配)

> 本文件是 Claude 持久记忆的**显式镜像**,便于随仓库版本化、随时查阅。
> 源目录: `~/.claude/projects/d--data-matching/memory/`。若两处不一致,以代码现状为准
> (记忆是"某时点观察",不是实时状态;引用的 file:line/行为可能已过时,断言前先核代码)。
> 最后同步: 2026-09-04。

## 目录
- [0. 用户偏好(务必遵守)](#0-用户偏好务必遵守)
- [1. 通用方法论(跨国共识)](#1-通用方法论跨国共识)
- [2. US(NSF)管线](#2-usnsf管线)
- [3. CN(NSFC 杰青)管线](#3-cnnsfc-杰青管线)
- [4. EU(ERC)管线](#4-euerc-管线)
- [5. AUS(澳 ARC)管线](#5-aus澳-arc管线)

---

## 0. 用户偏好(务必遵守)

- **始终用中文回复**(`reply-in-chinese`)。用户母语中文,曾多次因回成日文而明确不满。代码注释
  沿用仓库既有中文风格。除非用户显式要求换语言。
- **git commit 不加 `Co-Authored-By: Claude ...` 尾行**(`no-claude-coauthor`)。用户希望提交历史
  看起来是自己独立完成,不标注 AI 协作。即使默认指引要求加,也省略。

---

## 1. 通用方法论(跨国共识)

四国(US/CN/EU/AUS)共用一套 **四层漏斗** 把"资助名单里的 PI/机构名"匹配到 OpenAlex authorid:
1. **层1 姓名召回**(exact / alias / 罗马化变体)撑候选池;
2. **层2 机构消歧**(host 桥: PI 承担机构 → OpenAlex host id,再看候选是否在该机构发过文);
3. **层3 领域/语义消歧**(作者论文向量 × 资助文本向量, bge-small);
4. **层4 兜底**(候选论文数 argmax)。

**反复验证的核心教训 —— "共享属性诅咒"**:核心难 case = 同机构、同名、同大领域的多个真人。
凡是**候选之间共享的属性**(机构计数、领域标签、标题内容、论文数 margin…)天生劈不开他们。
CN 项目把这些信号全部实测证伪(见 §3)。**唯一干净的判据是"非共享外部键"**:ORCID、
时间窗(活跃年段)、基金致谢号。领域匹配弱不是实现问题,是全世界消歧文献的共识
(OpenAlex 官方 6 信号里 topics 只是次要辅助;WhoIsWho 赢家靠合著图+venue+关键词语义)。

**机构 host 桥是共同的质量杠杆**:host 覆盖越全,层2 越能把大批"层4 兜底"上移成干净的机构匹配。
host 桥脚本 `resolve_host_ids_*.py` 系出同门(见各国小节),产物统一为 `*_host_ids_bridge.csv`
(`row_id, host_openalex_id`)+ `*_host_ids_resolved.csv`(审计)。

---

## 2. US(NSF)管线

### 2.1 数据集: US.csv 与 us_since1990.csv(`us-career-since1990`)
- `us/US.csv` = 仅 3000 行 2024–2027 新 award,**只作 25 列格式模板**。
- `us_since1990.csv`(根目录)= 从 1990–2023 年份文件夹(388,999 个 NSF award JSON)过滤出的
  **NSF CAREER 奖**,17,400 奖 / 15,248 唯一 PI,列对齐 US.csv 以复用管线。可复现脚本
  `build_us_since1990.py`。注意 `wc -l` 会因 Abstract 内嵌换行虚高(105319 物理行),pandas 解析
  为 17400 条。
- **CAREER 识别 = 并集(标题标记 OR pgm_ref 代码 1045)**,再做 3 处精度处理:
  - 标题标记只认 `CAREER:` / `(CAREER)` / `Faculty Early Career Development`(收紧;`\bcareer\b`
    会误命中 1990–92 的 "Career Advancement Award" 等别的奖);
  - `pgm_ref_code == 1045`(官方 CAREER):**1995–1996 首批标题未统一、是纯研究标题,只能靠 1045
    识别**;1997 起标题统一带 `CAREER:` 前缀;
  - 排除前身奖 PYI/NYI(Presidential Faculty Fellows / Young Investigator);剔除 `Contract` 类
    instrument(发给承包商),保留 `Interagency Agreement`(政府实验室的真 CAREER)。
  - Why: CAREER 1995 才设立(1990–94 空);"career" 作普通词假阳性严重;code 与 title 两信号各有
    盲区,必须并集。

### 2.2 机构 host 桥(`us-host-bridge`)
- 脚本 `us/resolve_host_ids_us.py`,照 CN 版结构改。机构在 `Organization` 列,全部 country_code=US,
  619 个唯一机构(旧 US.csv 口径)。产物 `us_host_ids_bridge.csv` + `us_host_ids_resolved.csv` +
  `api_cache_institutions_us.json`。
- **US 特有清洗 = 去 NSF 法律主体"外壳" + 校区/缩写归一**。`_canon` 链: 去 leading The + 治理前缀
  (Regents of / Board of Trustees of / President and Fellows of…)+ 基金会/公司/治理后缀
  (Research Corp / Research Foundation / Foundation / Auxiliary Services / Enterprises / Trustees /
  Inc)→ 去 " at " → 去校区标记(Main Campus / University Park / Campus)→ 缩写展开
  (Univ/Tech/Inst/Res)。启发式兜底(仅扩召回、判 fuzzy/weak): SUNY/CUNY 校区展开、裸城市渐进截尾。
  确定性清洗形并入 exact-key,原名优先搜,清洗只在 0 命中补召。
- **关键坑**: OpenAlex `/institutions?search` 是**全词 AND**,`University of Virginia Main Campus`
  会要求候选含 "Main""Campus" → 0 命中。第一版(仅去壳)= 86.9%,补空格分隔校区词/at/缩写规则后精确
  命中 Colorado Boulder/UVA/PSU/Scripps/RIT/Stony Brook。
- **计费漏修复**: `load_cache` 原样照抄 EU/CN 版会丢弃 `[]`(确认 none)条目 → 每次重跑把 none 机构
  的整条回退变体链(~8 次调用/个)重烧。本版改成持久化全部条目(缺席 key=pending),重跑只查真 pending。
  **另一坑**: `BudgetExhausted` 一抛 break 主循环,字母序靠后的机构(含已缓存)全变 pending,需离线
  遍历 uniq 从缓存重建输出才拿得回。
- **API key**: 用户 free key 在 `us/.openalex_api_key`(和根 `.openalex_api_key`,已 gitignore)。
  free 额度 ~$1/天≈1000 次 search,UTC 零点重置。
- **最终状态(完成)**: **97.0%(2911/3000);exact 2503 + fuzzy 167 + weak 241 行**。唯一机构 619:
  exact 474 / fuzzy 39 / weak 40 / none 66(34 人名 fellowship 本就无 host + 32 机构多为真不在
  OpenAlex 的小 LLC / 零星改名学院)。胜第一版 86.9%、也胜 CN 93.4%。

#### 2.2b us_since1990 的机构桥(`us-since1990-host-bridge`,2026-09-04 完成)
- 脚本 `resolve_host_ids_us_since1990.py`(根目录):同一套去壳归一链,但后端换成本地
  `affiliations.duckdb`(110553 机构)。优先级 OVERRIDES(53 条)> 复用旧表高置信档(317 个)
  > duckdb(exact / token 倒排 Jaccard-cover fuzzy)> API 兜底。产物
  `us_since1990_host_ids_bridge.csv` + `_resolved.csv`。
- **结果**:机构级高置信 **569/574 (99.1%)**,weak 清零,行级 17397/17400;重跑零联网。
- **两个静默错配坑(比 weak 更危险)**:
  ①**美属领地另有国家码**:OpenAlex 把 PR/VI/GU/AS/MP 单列,候选池写死 `country_code='US'` 会把
  波多黎各大学整体排除 → 4 个 UPR 校区错配到佛州分校,**3 个还落在高置信 fuzzy 档**。
  ②**fuzzy jaccard=1.0 只是词集相同,不等于同一机构**:`Washington University`(MO)→ 误配
  University of Washington(106 行)、`Purdue University` → 误配 System 而非 West Lafayette 主校
  (233 行)、`CUNY City College` → 误配佛州营利校(36 行)等 15 处。
  **教训:只审 weak 不够,高置信档要按行数降序抽查**(错配集中在少数大机构)。
- **两个可复用审计手法**:exact 档用 `key2idx` 查同名碰撞(全库仅 1 处);复用/prior 档算
  inst_name↔matched_name 的 token 覆盖率,<0.75 的人工看(36 个全是正确去壳,零误报)。
- OVERRIDES 里 `(None, None)` = 人工确认查不到,**显式判 none**:宁可留空让层3/层4 裁决,也不要
  挂错 host 在层2 放行同名错人。详见 `HANDOFF_us_since1990_host_ids.md`。

### 2.3 层3 语义(`us-layer3-abstract-semantic`)
- **US 不套 EU 的 ERC 面板**(ERC 是欧洲分类体系,与 NSF 结构对不上);NSF 每条 award 自带整段
  `Abstract`,两侧都有富文本,直接 abstract-vs-abstract。
- `us/us_match.py`: v2(默认,需 `author_vectors_us.npz`)= 作者论文 title+abstract 质心 × 该行 NSF
  `Abstract` 向量 cosine;作者无英文摘要 → 回退 top-3 子学科名质心(同 bge 空间可比)。v1(回退)=
  子学科名 × `Title+Program(s)`。共享嵌入 `us/us_embed.py`(bge-small-en-v1.5, CLS+L2)。
- **离线重建顺序**: 先跑一次 `us_match.py`(自动 v1)→ `extract_abstracts_us.py`(扫 92GB, en-only,
  K=30/作者)→ `build_author_vectors_us.py`(→ npz, K=20)→ 再跑 `us_match.py` 自动切 v2。
- **已标定**(真值=871 个机构确认 rid): 纯 v2 语义 **acc@1=89% 且零并列**(层4 max-papers 仅 79.2%,
  胜 10 点)。定 **`SEM_THRESH_V2=0.68`(≈正确 p10)、`SEM_MARGIN_V2=0.00`**(任何正 margin 都把语义
  近并列推给更弱的层4 论文数,反降总正确)。
- **v2 上线效果**(全量 3000): 层3 解析 351→1151,层4 兜底 968→168。
- **留坑**: 银真值≠金(89% 是估计,ORCID 交叉核验是升级路);maxlen=256 截断长摘要;bootstrap 顺序
  依赖(US.csv 变了要重来)。

#### 2.3b us_since1990 全量跑通(2026-09-04)
- 用新机构桥(§2.2b)跑 17400 奖。`us_match.py` 输入是环境变量可配,**无需改代码**:
  `US_SOURCE_CSV=../us_since1990.csv US_HOST_BRIDGE=../us_since1990_host_ids_bridge.csv`。
- v2 bootstrap 全程本地/GPU:`extract_abstracts_us.py` 对 92GB 只扫一次 **72 秒**(262688 篇英文
  摘要)→ `build_author_vectors_us.py` **23623 个作者向量**。
- **v1→v2**:层3 语义 785→**3810**,层4 兜底 3314→**56**(论文数兜底彻底清零);总匹配率
  90.4%→89.1%,低产出队列 1416→1649。
- **总率反降不是回退**:matched+held 两版恒等 17153,差异全是 233 行移进低产出队列。A/B:两版都
  匹配的 15363 行 95.5% 同一作者;**改判 699 行 100% 来自层4 兜底或弱 v1 语义,92.7% 改判到论文数
  更少的作者**(中位 280→110 篇)—— "高产同名者遮蔽真人"被纠正的典型特征。
- 阈值沿用旧标定 0.68/0.00(同任务可迁移);`eval_sem_us.py` 已不在仓库,要重标需重建。
- **欠账**:低产出队列 1649 + 未匹配 247(可照 EU rescue 思路救);层2 机构平局 4016 行全靠语义裁决。
  `us/_backup_US3000/` 存的是 2024plus 那批(US.csv 3000 行)的最终结果,`merge_us_all.py` 与
  `build_final_us.py` 直接读它,**不是可删的备份**;本数据集 v1 结果(`us/_v1_since1990/`)已归档到
  `D:\data_matching_archive\`。

---

## 3. CN(NSFC 杰青)管线

> CN 是**最难**的一国:NSFC 侧只有(姓名+机构+中文领域+年度),**没有论文列表**。见 `cn/HANDOFF.md`。

### 3.1 机构 host 桥(`cn-host-bridge`)
- `cn.xlsx`(4603 位杰青,列: 序号/姓名/依托单位/研究领域/年度/第6列部分 OpenAlex **author** id)。
  脚本 `resolve_host_ids_cn.py`(复用 API 反查 + 四档)。三点适配:
  - **2019/2020 两年「依托单位」与「研究领域」整列错位**(真机构名在研究领域列),按年度取对的列,
    修正 600 行;1994–1996 早期 212 行两列皆空 → host 留空。
  - 中科院所常需去「中国科学院」前缀才搜得到(做成回退查询 + 查询侧 key,去前缀命中仍判 exact)。
  - 高频错配写进 `OVERRIDES`。
- 产物 `cn_host_ids_bridge.csv` + `cn_host_ids_resolved.csv`。覆盖 **93.4%(4301/4603)**;
  exact 4159 / override 61 / weak 81 / none 302(军队院所/更名所待复核)。

### 3.2 消歧信号调研:全部证伪(`cn-disambig-negatives`)—— **别重跑这些实验**
- 端到端 68.3%,71% 的行靠层4 全局论文数兜底。核心失败 = **高产同名者遮蔽真人**(真人论文数中位
  271 vs 被选中冒名者 786)。
- 试过且都 < baseline(argmax 全局论文数)的:host 论文数 84.1% / host 合著者数 78.8%(因
  `raw_affiliation_string` 整列空、`institutionid` 40% 缺失);bge-m3 领域过滤 60–68%(OpenAlex 子学科
  标签本身脏,且无法从中文领域文本预测——bge-m3 读得懂中文,死因是目标标签脏);"先洗干净标签再比领域"
  变体(`cn/diag_cleanfield_cn.py`)洗后 gold 绝对 cosine 中位仅 0.517 < 阈值,净≈0;标题内容匹配
  (`cn/validate_title_cn.py`)全部 ≤ baseline(真人 vs 冒名者 title-sim 差 0.045 且大面积重叠)。
  **field-content 整条路(tags+titles+abstract)到此为止。**
- **天花板**: oracle(gold 自己的子学科过滤)94.1%,但靠脏标签自洽、外部不可复现。可解决区(gold 在
  host)只有 87.7%。
- 退路: ①置信度门控封顶(`n_hs==1` 唯一干净信号只 82.6%,到 95% 的切片覆盖<1%);②ORCID 是唯一能到
  95% 的路(见 §3.3);③年度窗口缺 paper-year 表未动。

### 3.3 ORCID 路(唯一出路,但候选核验也撞墙)(`cn-orcid-path`)
- 用户拍板:"95%"=auto 子集精度;ORCID 平行推进;走公开 API + 候选核验。
- **ORCID 是干净键**: 真人 87% 有 orcid;0 冲突;orcid→authorid 全库 1:1。**但 orcid 列本身不消歧**
  (每行中位 ~15 个同名候选都带各自 orcid),必须靠 ORCID 档案(姓名+机构履历)核验。
- 内部 harness 已建(`cn/orcid_harness_cn.py` → `cand_orcids_cn.csv`)。ORCID 公开 API v3.0 免 token,
  但**必须 requests+Retry 退避+Session**(urllib 裸调 SSL-EOF 频发)。字段填充率(60 gold 样本):
  aff_any 51.7%、**中文名 other-names CJK 仅 1.7%(信号死)**、works 无用(NSFC 侧无论文清单)。
- **⛔ 端到端实测(`verify_e2e_cn.py`)失败**: ORCID 机构在 OpenAlex host 之上**零增益**。UNION unique
  = 93%@36% 与 openalex-only 一字不差。根因(非 bug): 核心难 case = 同机构多个同名人,他们**共享**该
  机构,机构信号天生不可分(同"共享属性诅咒")。故 86.9% 天花板作废(它前提是"认出本人 orcid",而候选
  核验只能靠共享机构)。
- **当前定论**: ORCID 候选核验封顶 ~36% 覆盖、~93% 精度。现实可交付 = 高精度 auto 子集(host 唯一,
  ~36%)+ 其余人工队列。

### 3.4 领域匹配文献调研(`cn-field-matching-research`)
- 别人也解决不了: OpenAlex/WhoIsWho/GNN 都是**双侧都有完整论文元数据**的配对/聚类;本项目 NSFC 侧无
  论文列表 → 任务不对称,SOTA 迁移不过来。
- 中文字符信号(文献 #1)在本 gold 上是 dud: gold 真人只 8.3% 有 CJK 形(精英杰青英文发表为主),作
  剪枝仅值 ~4%。
- **两条没试、文献背书、"非共享键"的路**(都卡在缺数据):①**时间窗/活跃年段**(直击"高产晚出道者遮蔽
  真人",但全表无 year 列);②**基金致谢/资助号链接**(NSF-award-number in funding-acknowledgement +
  DOI 当键,但数据无 funding 字段)。

### 3.5 提速 + 覆盖(`cn-match-speed-and-coverage`)
- **卡死主因 = `match_1b` 末尾的 OR-join**(`norm=… OR wset=…`,DuckDB 无法哈希 → 退化块嵌套循环,
  125× 慢)。修法: **拆成两个等值 join 的 UNION**。改完 128s;再物化名字索引
  (`build_name_index_cn.py` → `name_index_cn.parquet`,建一次 41s)→ 71s。
- **保守设计自动匹配仅 1.8%**(name_unique 罗马化中文名几乎不可能全球唯一,正常);候选池被撑到中位
  963/行。
- **松紧权衡(关键)**: **alias 层对"host 唯一自动子集"有害**。exact-only 池中位 229 / host 唯一自动
  覆盖 **8.1%@82.6%**;加 alias 后覆盖砸到 1.8%、精度也降。**杠杆=解耦**: auto 判定用紧口径
  (exact-only),召回池仍用松口径(现状代码两者共用松池,是覆盖崩塌主因)。
- **人工标注 harness 已建**(`make_annotation_sheets_cn.py`): 把"从 963 候选检索"变"从 ≤15 host 存活
  候选核验"(s≤15 占 76%)。含质检暗桩(答案在隐藏 `annotation_key_cn.csv`)。下一步待建: 回收
  pick→grade + 并入 matched_final(source='human')。

---

## 4. EU(ERC)管线

### 4.1 层3 语义 v2: ERC 面板原型(`layer3-v2-erc-semantic`)
- 从 v1(作者前3 子学科名 × 面板名)升级为 **v2**: 作者论文(title+abstract)质心 × **ERC 分类数据集**
  建的面板原型 cosine。默认开,`SEM_V2=0` 回退。
- **数据源**: `sciscinet_papertitleabstract.parquet`(~92GB,1.224 亿行;`paperid` 就是 **OpenAlex
  W-id**,与其它 sciscinet 表 join key 全一致,无需 MAG↔OpenAlex 对照;`abstract_inverted_index` 需
  还原)。ERC 面板标签 `SIRIS-Lab/erc-classification-dataset`(HF),用 **test_panels 划分**建原型
  (train 是 LLM 伪标注、有噪声,不用)。
- **离线重建**: extract_abstracts.py → build_author_vectors.py(→ author_vectors.npz)+
  build_panel_prototypes.py(→ erc_panel_prototypes.npz)→ eval_sem_v2.py 标定。共享嵌入 `erc_embed.py`
  (CLS+L2, `score_semantic_v2`)。
- **标定**: `SEM_THRESH_V2=0.80`、`SEM_MARGIN_V2=0.01`。
- **验证(ORCID 独立真值)**: v2 严格 acc@1 **91% 且零并列**(v1 仅 62%,16 个真人与错候选并列分不开——
  v2 核心价值是消除并列)。全量 A/B: 层3 语义 227→308,ORCID 可判定精度 83%→87%。

### 4.2 同名者补召 → host 准入(`homonym-reopen-host-fl`)
- `data_match.py` 的 `homonym_reopen`(L1→L2 交接)从旧**双触发**改成**单一 host 准入规则**(未 ORCID
  A/B 验证)。
- **病根**: `match_1c` 的首末名 `fl` 召回有 `rid NOT IN r1_ea` 门 —— 只要精确/别名给某行配上任何候选,
  `fl` 就不为这行跑。真人常以中间名/缩写另存(`norm` 保留句点、词集配不上,只 `fl` 能召回),于是空壳
  stub 占位,真人永远进不了候选池。
- **使能事实**: `host_ids_bridge.csv` 现 **100% 覆盖** → host 从弱佐证升级为覆盖全体的强准入闸。
- **修法**: 对每个已有精确/别名候选的 rid,把「`fl` 相同 + 在该行 host 机构发过文 + 不在现有候选 +
  `fuzzyname_vendored.names_match` 复核」的作者并入候选池;name_unique 因此多出候选就 withdraw 交
  L2/L3/L4 裁决。**host 发文事实是唯一准入闸**(不按论文数最高拍板)。
- **待办**: ①跑 ORCID A/B;②`Algorithm.md` 那节仍描述旧 A/B,与代码不一致待同步。**回归向量**:
  affiliation 表数据缺口(真人论文没挂上该 host、同名者挂上了 → L2 错选)。

### 4.3 未匹配 140 行抢救(`eu-rescue-unmatched`)
- 四层跑完剩 140 行(EU.xlsx 4242)。`eu/rescue_unmatched_eu.py`(只新增文件,不动 matched_final):
  **纯 surname 等值召回(不卡首名)** → **host 硬准入** → **fuzzyname 正反变体复核 + _NICK 等价组** →
  平局走 L3 v1 语义 → L4 论文数(PAPER_FLOOR=30)。
- **结果**: rescue_matched 81 + rescue_lowconf 43 + 剩 16。EU 匹配率 → **90.6%**。78/81 有 ORCID,抽验
  近乎全对。
- **两个精度修复(值得记)**: ①**fuzzyname 子串巧合误配**("e lejeune" 是 "jeanine lejeune lorthois"
  子串)→ 加 `name_anchor_ok` 护栏(单字母缩写 token 首字母必须命中某 EU token 首字母);②**重复 grant
  行传播**(一人多项目 → 按 (清洗名, host) 把已定案高置信行复制给重复行)。

---

## 5. AUS(澳 ARC)管线(`aus-pipeline`)

- 镜像 US 结构(host 桥 + 无 ERC 面板,层3 用作者子学科名 × 领域标签)。5 个文件在 `aus/`。栏目映射:
  lead-investigator→名字、current-admin-organisation→host、primary-field-of-research→领域、
  grant-summary→v2 查询文本。
- **三个特有点**: ①**姓名带学术头衔**(THE 关键改动): 每行带 Prof/A/Prof/Dr/Em/Prof 等前缀,加
  `striptitle` DuckDB 宏剥头衔(斜杠复合如 a/prof 也识别,每单元须跟空白避免吃真名);②**领域带 ANZSRC
  编码**(`5205 - ...`,剥 "NNNN - " 前缀);③机构干净(55 个唯一澳洲大学,CC=AU)。
- **v2 已上线**: extract → build_author_vectors(30604 作者向量)→ 重跑。v1→v2: 层3 干净选出 617→882
  (+43%),纯论文数兜底≈清零,总匹配率 93.8%→**96.9%**。**阈值 `THRESH 0.55 / MARGIN 0.02`**:网格显示
  THRESH 0.30~0.55 几乎不 bind、分辨全靠 margin(0.02 是 0-wrong 操作点),占位值恰在最优格(有证据支撑,
  但真值仅 42 rid,标定用回退子学科质心分、偏挤)。
- **质量瓶颈 = host 桥没跑完**(4.2% 覆盖,受 OpenAlex 免费额度限;6 所最大的大学全 pending → 54% 行落
  层4 兜底)。**跑完 host 桥是首要质量杠杆**。⚠️ 用户自己的 resolver(解 announcement-admin-organisation
  第二机构列)更强,`resolve_host_ids_aus.py` 与其同名会覆盖,**勿 clobber 用户的 dual-column 桥**。
