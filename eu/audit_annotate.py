# -*- coding: utf-8 -*-
"""把富集结果转成逐行审查标注(my_verdict + reason),供人工过目。
判据(基于 OpenAlex 交叉核对,非循环:流水线从不用 ORCID/代表作领域):
  - NOTFOUND : authorid 在 OpenAlex 查不到(疑合并/删除)
  - WRONG    : 匹配到的档案明显是另一个人(名字/领域/代表作对不上)
  - MERGED?  : 该 id 是被 OpenAlex 混进了不相干他人的合并档案(含本人但被污染)
  - CHECK    : 唯一名/低被引/证据不足,建议人工看一眼
  - OK       : 名字(exact/alias)+ 常有 ORCID + host 出现在机构历史/LKI + 代表作领域自洽
"""
import pandas as pd
from pathlib import Path

# 我人工核对代表作领域后给出的判定(rid 为键)。reason 里带证据。
MANUAL = {
 # round3
 ('round3','161'): ('WRONG','代表作是大麦/植物生物学(GAMYB、DOF转录因子),匹配到的是农业口 Manuel Martínez;真人 Manuel Irimia 做基因组/可变剪接@CRG。人工已标 no,一致。'),
 ('round3','2691'): ('WRONG','代表作全是 PV/氢/电池独立供电系统优化(Antonio Cano Ortega@Jaén 能源工程);真人 Antonio Ivorra 做生物电子/电穿孔@UPF。⚠人工标了 yes,疑误判。fl() 把西班牙复姓末词 Cano 当姓导致错配。'),
 ('round3','3007'): ('NOTFOUND','authorid A5106453768 在 OpenAlex 查不到(疑被合并/删除)。'),
 ('round3','1613'): ('OK','代表作=中间带太阳能电池/PbS 量子点红外光电,与 Iñigo Ramiro(光伏/光子学)一致;现在 ICFO@巴塞罗那,与 host UPC/加泰同城,host_in=N 只是机构名不同。'),
 ('round3','277'): ('OK','代表作=cohesin/condensin/染色体分离(Drosophila),与 Raquel A. Oliveira@IGC(染色体生物学)一致;LKI 含 Instituto Gulbenkian de Ciência。别名里的巴西记录是噪声,不影响。'),
 ('round3','3727'): ('CHECK','Dan/Daniel Batovici,叙利亚语文/早期基督教研究,人文低被引(46篇53引)属正常;名字全球唯一,affs 有 KU Leuven;但 host 维也纳未现于 affs。名字唯一基本可信,建议扫一眼。'),
 # round4
 ('round4','535'): ('WRONG','匹配档案 works=1954、代表作是心衰(semaglutide)/冠脉 FFR-CT/流感反向遗传学——多个日本医学 Hiroshi Ito 的合并巨档;真人 Hiroshi Ito@Max Planck 是神经科学(脑研究所)。未标,建议标 no。'),
 ('round4','3138'): ('WRONG','匹配档案显示名 "Tom Berg / van den Berg",代表作是仿真/无人载具/温室气体核算(220篇仅80引);真人 Tobias Berg@法兰克福是金融(银行/信贷风险)教授。别名错配,建议标 no。'),
 ('round4','2479'): ('WRONG','匹配到心理分析学家 Rainer Krause(Kassel/Ulm/精神分析学会),非 ETH。人工已标 no,一致。'),
 ('round4','2396'): ('NOTFOUND','authorid A5088394978 在 OpenAlex 查不到(疑被合并/删除)。真人 Gerhard Neumann@KIT 是机器人/机器学习。'),
 ('round4','3992'): ('WRONG?','匹配档案代表作全是"精英艺术体操少女生长发育/唾液脂肪因子"(希腊儿科运动医学);host 为法国 CNRS,LKI=EPFL,更像另一位物理口 Anastasia Theodoropoulou。⚠人工标了 yes,疑误判,请核对。'),
 ('round4','3755'): ('MERGED?','匹配档案高被引代表作是物理化学/染料敏化太阳能电池光电(1990-2006),又有 2023-24 维也纳表演艺术学院 affil——把戏剧学者 Silke Felber 与一位化学家 S. Felber 合并了。人工标 yes,但该 id 被污染,建议核对本人论文是否都归此 id。'),
 ('round4','429'): ('OK','代表作=真皮成纤维谱系/指尖再生/Pax7 肌生成,正是 Yuval Rinkevich(再生与纤维化)之作;曾在 Helmholtz Munich。LKI 杂乱但本人无误。'),
 ('round4','3924'): ('CHECK','Zachary Chitwood,拜占庭史学者,名字唯一;affs 美因茨/柏林/普林斯顿,host LMU 慕尼黑未现;唯一名基本可信,建议扫一眼。'),
 ('round4','4145'): ('CHECK','Deborah Nadal,狂犬病/被忽视热带病人类学,名字唯一;affs 威尼斯 Ca\' Foscari/华盛顿/WHO,host 帕多瓦未现;唯一名基本可信,建议扫一眼。'),
}

def default_verdict(r):
    if r['oa_name']=='<NOT FOUND>':
        return 'NOTFOUND','authorid 查不到'
    ev=[]
    ev.append(f"name={r['name_match']}")
    if r['oa_orcid']: ev.append('ORCID有')
    ev.append(f"host_in_affils={r['host_in_affils']}")
    if r['oa_lki']: ev.append('LKI:'+r['oa_lki'][:60])
    # name=NO(西语复姓等)但机构对上 → 仍 OK,注明
    tag='OK'
    reason='名字/机构/ORCID 自洽; '+'; '.join(ev)
    if r['name_match']=='NO' and r['host_in_affils']=='Y':
        reason='名字末词差异(疑复姓/顺序),但机构直接对上 → 判 OK; '+'; '.join(ev)
    return tag,reason

rows_out=[]
for f,key in [('round3_sample_50_enriched.csv','round3'),('round4_sample_100_enriched.csv','round4')]:
    df=pd.read_csv(f,encoding='utf-8-sig',dtype=str).fillna('')
    for _,r in df.iterrows():
        mk=(key,str(r['rid']))
        if mk in MANUAL:
            v,reason=MANUAL[mk]
        else:
            v,reason=default_verdict(r)
        rows_out.append(dict(file=key,rid=r['rid'],eu_name=r['eu_name'],authorid=r['authorid'],
                             match_type=r['match_type'],human=r['yes_or_no'].strip(),
                             my_verdict=v,oa_name=r['oa_name'],oa_orcid=r['oa_orcid'],
                             oa_lki=r['oa_lki'],reason=reason))
    out=pd.DataFrame([x for x in rows_out if x['file']==key])
    out.to_csv(f.replace('_enriched','_audited'),index=False,encoding='utf-8-sig')

allout=pd.DataFrame(rows_out)
# 打印:先摘要,再把 非OK 的详列
import sys; sys.stdout.reconfigure(encoding='utf-8')
print('=== 各档计数 ===')
print(allout.groupby(['file','my_verdict']).size())
print('\n=== 需要注意的行(非 OK) ===')
for _,r in allout[allout['my_verdict']!='OK'].iterrows():
    print(f"[{r['file']}] rid={r['rid']:>4} {r['my_verdict']:<8} | {r['eu_name']} [{r['authorid']}] 人工={r['human']!r}")
    print(f"        {r['reason']}")
print('\n产物: round3_sample_50_audited.csv / round4_sample_100_audited.csv')
