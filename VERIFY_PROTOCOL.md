# LLM 人工式作者标注协议（可复用版）

> 目的：把"人工验证 author_id 是否匹配对"这件事自动化，但保留可审计、可测量精度的
> 结构。本协议不依赖某个具体地区管线（US/CN/EU/AUS 通用），只依赖：原始名单里有人名 +
> 机构（+年份/领域可选），以及一个候选 author_id。
>
> 最后更新：2026-09-05。
>
> **落地实现与证据不在本仓库**：US/AUS 两轮验证的抽样脚本、LLM 判定结果、证据 jsonl 与
> OpenAlex 缓存已随仓库清理归档到 `D:\data_matching_archive\`（`us\verify\`、`aus\verify\`
> 与 `us\*_verify*.py` / `us\llm_pilot_oa.py` 等，目录结构与原仓库一致）。下文提到的路径
> 都指归档里的位置；重跑新一轮时在各区域新建 `verify/`（已 gitignore）。

---

## 1. 为什么是这套结构

人工验证的原始流程是：

1. 谷歌搜"学者姓名 + 机构"→ 找到本人主页/机构学者页/Google Scholar/ORCID；
2. 从这些页面挑 2~3 篇**确定属于本人**的代表作；
3. 去 OpenAlex 搜这些论文，看作者列表里有没有算法给的那个 author_id；
4. 有 → 对；没有 → 错。

这套自动化协议保留同样的逻辑，只做两处替换/加固：

- "谷歌搜索 + 判断页面归属"由 LLM 完成，但必须给出每篇代表作的来源 URL；
- "回 OpenAlex 查作者"由确定性脚本完成（DOI/题名反查），不依赖 LLM 记忆，杜绝幻觉。

关键设计：**判定只看 author id 是否出现在代表作作者列表中，不用 OpenAlex 显示名做过滤**。
同一篇论文的显示名会因来源而异（缩写、连字符、中文罗马化、改名），id 才是稳定键。

---

## 2. 判定档位（不要用二值）

纯二值（对/错）会把两类情况误判：

- OpenAlex 把同一个人拆成两个 id（改名、名字缩写、别名未合并）→ 看起来"错"，其实人是
  对的，id 体系不干净；
- 候选 id 只是没出现在我们挑的这几篇里，需要更多证据才能定罪。

因此固定用四档：

| 档位 | 定义 | 后续动作 |
|---|---|---|
| `match` | 候选 id 出现在（几乎）全部可判代表作里，且是这些论文上的主导 id | 可用 |
| `mismatch` | 候选 id 零命中，另有稳定同姓 id 出现在多数代表作里 | 记录 `likely_correct_id`，人工复核后回填 |
| `review` | 候选命中一部分，另有一个 id 命中其余（疑似 OpenAlex 拆分/改名）；或候选之外的同名 id 疑似合并了多个真人 | 人工看 |
| `inconclusive` / 证据不足 | 代表作查不到 / OpenAlex 记录缺 author id / 无法确认论文属于本人 | 补证据或留人工，不算错 |

判定时需要至少 2 篇能确认的代表作；只有 1 篇时降级为低证据结论，明确标注。

---

## 3. 完整流程

### 3.0 输入

每行至少要有：

- `rid`：原名单行号（便于回写）；
- `us_name`（或通用 `person_name`）：学者姓名；
- `institution` / `host_raw`：名单给的机构（不是 OpenAlex 匹配结果）；
- `year`（可选，用于检索消歧与区分同名）；
- `authorid`：被验证的候选 OpenAlex/SciSciNet author id；
- `source`（可选）：该行来自匹配管线的哪一层，便于按层统计。

### 3.1 取样（可选，用于估精度）

若目的是估计全量精度而不是逐行交付，建议分层抽样：

- 每层（name_unique / round2_inst / round3_semantic / round4_*）各抽若干行；
- **弱层要超采**（如 round4_maxpapers 全量只有几十行就全取），否则难例被总体占比稀释；
- 种子固定，结果可复现。

30 行 pilot 的抽样脚本：归档 `us\make_llm_pilot_sample.py`
（当前是 US 专用，换地区时改输入路径即可）。

### 3.2 LLM 取证（可以人工做，也可以让 Codex 做）

对每一行：

1. 用搜索引擎查 `"姓名" 机构`，找到本人主页、机构学者页、ORCID、Google Scholar 等；
2. **同名人很多时先消歧**：结合机构 + 领域 + 年份确认哪个是本人（例如：
   "Sun Young Park" 密歇根做健康设计 vs 韩国食品科学同名者；"Christo Wilson"
   东北大学 CS vs "Christopher J. Wilson" 医药公司）；
3. 从**确认属于本人**的来源挑 2~3 篇代表作，逐篇记录：
   - 题名；
   - DOI（有则优先）；
   - 年份；
   - 来源 URL（证据链，供人复核）；
4. 来源可靠性排序（高→低）：个人/机构主页的出版物列表、ORCID 作品、Google Scholar、
   出版商作者页；仅靠搜索引擎返回一篇同名论文不能算数；
5. 无法确认属于本人的论文直接丢弃，换一篇或判证据不足。

### 3.3 OpenAlex 确定性反查

拿 3.2 的 DOI/题名批量反查，取每篇论文的**全部** author id + 显示名：

- DOI 命中优先（OpenAlex `/works/{doi}`）；
- 无 DOI 时用题名搜索，年份 ±1 过滤，只取高置信命中；
- 不要用"作者名包含本人姓"来过滤作者——只用来辅助阅读输出；
- 输出应缓存（幂等），重跑不重复烧 API 额度。

现有工具：归档 `us\llm_pilot_oa.py`（拷回仓库即可用，只依赖标准库 + OpenAlex API），用法：

```bash
# 单篇 DOI
python us/llm_pilot_oa.py --doi 10.1088/1361-6471/ac865e

# 批量：jsonl 每行一个代表作
# {"rid":62,"cand":"A5008308261","family":"sherin","key":"w1","doi":"10.1145/..."}
# 或 {"rid":62,"cand":"A5008308261","family":"sherin","key":"w2","title":"...","year":2001}
python us/llm_pilot_oa.py --file us/verify/llm_pilot_evidence.jsonl

# 诊断：候选/疑似正确 id 的 profile（显示名、作品数、机构）
python us/llm_pilot_oa.py --authors A5008382551,A5007980040
```

`family` 只用于在输出里标出同姓作者（帮助人读），不参与判定。

### 3.4 判定与记录

LLM（或人）根据反查输出按 §2 落档，并写两列：

- `verdict`：`match / mismatch / review / inconclusive`
- `likely_correct_id`：mismatch 时填"代表作里稳定出现、疑似正确的 id"

每条结论应带证据摘要：用了哪几篇代表作、每篇命中情况、为什么这么判。

### 3.5 人工审计（保证"数据可用"的关键）

自动标注永远不直接等于金标准，可信度来自抽样人工点检：

1. `mismatch` + `review` 行全量人工复核（数量通常可控）；
2. `match` 行随机抽一部分，人工点进 OpenAlex author 页核对 profile 是否就是本人；
3. 报告自动判定子集的精度，用 Wilson 区间（小样本、接近 1 时正态近似会超 100%）；
4. 精度低于目标（如 95%）就收紧判定规则或把更多行留给人工。

只有"自动子集 + 抽样审计精度"一起交付，才算数据可用。

---

## 4. 数据/产物格式

### 代表作证据（jsonl）

```json
{"rid":62,"cand":"A5008308261","family":"sherin","key":"w1","doi":"10.1145/2330601.2330649"}
{"rid":62,"cand":"A5008308261","family":"sherin","key":"w2","title":"How students understand physics equations","year":2001}
```

### 判定结果（csv）

在输入行基础上加列：

```text
rid,us_name,authorid,source,year,host,host_raw,
verdict,likely_correct_id,note
```

`note` 里必须写清"代表作 + 命中情况 + 判据"，例如：

```text
2 篇代表作(2018 PoPETs / 2019 IMC)均在 Christo Wilson=A5072703507 名下;
候选 A5059937438=Christopher J. Wilson(Editas Medicine),是另一个真人
```

---

## 5. 已知坑与应对

| 坑 | 现象 | 应对 |
|---|---|---|
| 同名人多 | 搜到错误真人的主页 | 用机构+领域+年份消歧；来源必须能指向本人 |
| OpenAlex 改名拆分 | 同人两个 id（如 J.D. Small → J.D. Small Griswold） | 判 `review`，不判 mismatch；人工决定主 id |
| OpenAlex 别名尾巴 | 主 id 77 篇 + 别名 1 篇 | 判 `review`；通常主 id 可用 |
| 老论文缺 author id | IOP/AAS 等旧记录作者 id 全空 | 换新版论文/arXiv；实在没有判证据不足 |
| 真人没进候选池 | 层4 把同名高产者当答案（如天体物理学家被匹配成 1048 篇的心脏病学家） | 判 mismatch 并给 `likely_correct_id`；修匹配代码，而非修标注 |
| 作者显示名变体 | "P. R. McCullough" vs "Peter R McCullough" | 判定只比 id，不比名字；同姓作者信息仅供阅读 |
| live API vs 快照 | pilot 用 live OpenAlex，匹配库是 sciscinet 快照 | 回填 `likely_correct_id` 前确认该 id 在快照里存在，否则要额外做快照侧定位 |

---

## 6. 30 行 pilot 现状（2026-09-04）

US `us_since1990` 分层样本 30 行，结果：

- 23 `match` / 5 `mismatch` / 2 `review`；
- 4/4 的 round4_maxpapers 样本全错，且都是"论文数兜底选中同名高产真人"；
- 2 个 `review` 都是 OpenAlex 自身拆分（改名 / 别名尾巴），候选 id 本身是本人主 profile。

产物均在归档 `D:\data_matching_archive\` 下：

- 样本：`us\verify\llm_pilot_30.csv`
- 证据：`us\verify\llm_pilot_evidence.jsonl`
- OpenAlex 反查缓存：`us\verify\llm_pilot_oa_cache.json`
- 判定结果：`us\verify\llm_pilot_30_results.csv`
- 判定落盘脚本：`us\build_pilot_verdicts.py`
  （当前内置的是这 30 行的结论；新批次应更新判定字典，或把 verdict 单独存成输入列）

**pilot 是超采难例的样本，不是总体精度估计**；要估全量精度请按各层实际行数加权，或另抽
一次按比例的分层样本。

---

## 7. 与匹配代码的关系

本协议只做**事后验证与修正**，不替代匹配：

- 输入是匹配管线的输出（候选 `authorid`）；
- 输出是 `match / mismatch / review` + `likely_correct_id`；
- 匹配代码（四层漏斗、阈值、兜底规则）的修改由另一条线负责，本协议不约束它；
- 协议发现的系统性错误（如 round4 论文数兜底选中同名高产者）只作为修改匹配代码的依据，
  不回写、不覆盖匹配产物。

若将来要跑大批量，建议把"LLM 取证"阶段限定在自动管线无法置信的行（review / 弱层 / 低分
弃权行），而不是对全量逐行跑——成本与收益的平衡点由各地区自己定。
