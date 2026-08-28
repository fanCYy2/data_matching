# EU 未匹配 140 行抢救汇总

data_match.py 四层跑完后剩的 140 行(matched_final 3763 + held 339 之外),本脚本用 host 机构硬准入 + fuzzyname + 语义/论文数补救。

## 一、总量
| 项 | 数 |
| --- | ---: |
| 未匹配行 | 140 |
| 其中空姓名 | 6 |
| 去重姓名 | 126 |
| **rescue_matched(论文数≥30)** | **81** |
| rescue_lowconf(待人工) | 43 |
| 剩余未定 | 16 |

## 二、rescue_matched 按来源(source)
| source | 数 | 含义 |
| --- | ---: | --- |
| rescue_host_unique | 49 | host 机构确认 + fuzzyname 唯一,直接定(最可信) |
| rescue_maxpapers | 19 | host+fuzzy 多候选,语义未定,取论文数最高 |
| rescue_semantic | 11 | host+fuzzy 多候选,v1 语义唯一选出 |
| rescue_duplicate | 2 | 与某已定案行同名同机构(同一人的另一条 grant 行),复制答案 |

## 三、rescue_lowconf 按原因(reason)
| reason | 数 | 含义 |
| --- | ---: | --- |
| host_ok_fuzzy_fail | 32 | 机构强命中但 fuzzyname 判否(可能同机构同姓他人,需人工看名字) |
| lowpaper | 11 | 机构+名字都对上但论文数 < 30(held 风格,快照太薄) |

## 四、候选 pool 分布
- 名字召回(末名=姓氏,host 准入【前】):候选作者共 1078902,覆盖 134 个 rid,pool 中位 3080/最大 66703 (按姓氏召回本就宽,靠 host+fuzzy 收口)
- **host 机构准入【后】每 rid 候选数(决策相关)**:
  - pool 1(直接可定): 16 个 rid
  - pool 2-5: 49 个 rid
  - pool 6-20: 30 个 rid
  - pool >20: 29 个 rid

## 五、rescue_matched 明细(抽验用)
| rid | eu_name | authorid | source | n_papers |
| ---: | --- | --- | --- | ---: |
| 74 | Cristobal Axel Innis | A5053935606 | rescue_host_unique | 42 |
| 201 | Evanthia Soutoglou | A5064332537 | rescue_host_unique | 67 |
| 234 | Patricio Aloy Calaf | A5015088787 | rescue_maxpapers | 213 |
| 336 | Mari Johanna Ivaska | A5051981975 | rescue_maxpapers | 229 |
| 447 | Laurent, Arnaud, Philippe Yvan-Charvet | A5042081614 | rescue_maxpapers | 111 |
| 454 | Esther Lutgens Leiner | A5069173292 | rescue_maxpapers | 318 |
| 487 | Gert Fredrik Bäckhed | A5047963662 | rescue_host_unique | 315 |
| 541 | Mariana Astiz Cadenas | A5043808093 | rescue_host_unique | 52 |
| 587 | Amihai Citri | A5048298854 | rescue_host_unique | 59 |
| 714 | Ahmet Kaan Boztug | A5048616194 | rescue_host_unique | 229 |
| 770 | Maria Carla Saleh Bottegoni | A5082764484 | rescue_maxpapers | 83 |
| 854 | Nicolas, Clement Rivron | A5085037923 | rescue_maxpapers | 57 |
| 882 | Jan Teun Bousema | A5036396443 | rescue_maxpapers | 480 |
| 921 | Joseph Petrus Gerardus Sluijter | A5005834517 | rescue_host_unique | 328 |
| 931 | Jeroen Rouwkema Lieten | A5061443036 | rescue_host_unique | 140 |
| 961 | Renate Bonin-Geb.Schnabel | A5090270823 | rescue_maxpapers | 536 |
| 1076 | Simone Immler Maklakov | A5054861051 | rescue_host_unique | 104 |
| 1396 | Harald Andres Helfgott Seier | A5027885087 | rescue_host_unique | 106 |
| 1406 | Emmanuel, François, Jean Breuillard | A5024332688 | rescue_host_unique | 115 |
| 1484 | Clare Juliet Biggs | A5083240181 | rescue_maxpapers | 342 |
| 1511 | Florent, Billy Brenguier | A5011470586 | rescue_host_unique | 232 |
| 1587 | Andrew John Ridgwell | A5074849392 | rescue_semantic | 485 |
| 1617 | Amaia Cipitria Sagardia | A5072719653 | rescue_host_unique | 69 |
| 1743 | Mikael Anton Vesterinen | A5062951688 | rescue_maxpapers | 867 |
| 1765 | Alexander Retzker Menes | A5053617356 | rescue_semantic | 160 |
| 1839 | Jens Peter Hommelhoff | A5026575046 | rescue_host_unique | 515 |
| 1940 | Christophe, Marcel, Georges Galland | A5044090780 | rescue_host_unique | 119 |
| 1962 | Francesco, Ascanio Mario Marcello Zamponi | A5022080437 | rescue_host_unique | 310 |
| 1968 | Rembertus Abraham Duine | A5045225582 | rescue_maxpapers | 268 |
| 1970 | Neven, Zitomir Barisic | A5064040199 | rescue_host_unique | 182 |
| 1989 | Mark Robertus Buitelaar | A5078461700 | rescue_host_unique | 35 |
| 2096 | Gemma Clare Solomon Larsen | A5018410685 | rescue_host_unique | 169 |
| 2117 | Danielle, Anna Laurencin | A5054438179 | rescue_host_unique | 222 |
| 2209 | Katalin Barta Weissert | A5004765483 | rescue_host_unique | 120 |
| 2219 | Bernhard Christian Bayer-Skoff | A5061851293 | rescue_semantic | 105 |
| 2332 | Edit Yehudit Tshuva Goldberg | A5066437006 | rescue_host_unique | 228 |
| 2373 | Eve Hoggan Christensen | A5002561749 | rescue_semantic | 65 |
| 2426 | Christopher John Wojtan | A5008127734 | rescue_host_unique | 72 |
| 2458 | Samuel Staton | A5068183682 | rescue_host_unique | 105 |
| 2501 | David, Francois Pichardie | A5046752375 | rescue_host_unique | 111 |
| 2511 | Gabriel, Louis, Jean Peyré | A5058651667 | rescue_semantic | 371 |
| 2514 | Francis, René, Julien Bach | A5001483226 | rescue_maxpapers | 367 |
| 2533 | Ewa Asa Carolina Wahlby | A5028372092 | rescue_host_unique | 229 |
| 2538 | Albert Atserias Peri | A5003671114 | rescue_maxpapers | 151 |
| 2642 | Nikolay Akopian | A5088128757 | rescue_host_unique | 85 |
| 2687 | Christian Gunter Koos | A5026468546 | rescue_host_unique | 579 |
| 2693 | Fabrice, Denis Raineri | A5012507338 | rescue_semantic | 260 |
| 2932 | Sylvie, Jeanine Lejeune Ép Lorthois | A5075453232 | rescue_maxpapers | 77 |
| 2934 | Philippe, Guy, Marie Marmottant | A5023549418 | rescue_semantic | 153 |
| 2942 | Johannes Tiemen Padding | A5057833615 | rescue_host_unique | 269 |
| 3005 | Manuel Linares Alegret | A5082180400 | rescue_host_unique | 222 |
| 3045 | Hugues Albert Sana | A5025696341 | rescue_maxpapers | 577 |
| 3057 | David René Bernard Ehrenreich | A5085237456 | rescue_maxpapers | 595 |
| 3079 | Ine Marie J Ineke De Moortel | A5085867085 | rescue_host_unique | 168 |
| 3199 | Maria Silvana Tenreyro | A5053384308 | rescue_host_unique | 128 |
| 3296 | Tihomira Burri | A5087415376 | rescue_semantic | 245 |
| 3333 | Andrea Sangiovanni Vincentelli | A5023177719 | rescue_semantic | 84 |
| 3377 | Maria Serena Olsaretti | A5044166329 | rescue_maxpapers | 67 |
| 3466 | Johannes Martin Saxer | A5040746213 | rescue_semantic | 35 |
| 3511 | Susannne Branje | A5059439551 | rescue_host_unique | 406 |
| 3521 | Bertha De Hart | A5055750579 | rescue_host_unique | 127 |
| 3527 | Dariusz, Jacek Wojcik | A5051441410 | rescue_maxpapers | 264 |
| 3655 | Clara, Dominique, Sylvie Martin | A5045092960 | rescue_host_unique | 142 |
| 3679 | Kathryn Elizabeth Slocombe | A5012826098 | rescue_host_unique | 127 |
| 3703 | Lisa Marie De Bruine | A5007277878 | rescue_host_unique | 408 |
| 3797 | Anthony Craig Vear | A5084374680 | rescue_host_unique | 77 |
| 3799 | Thorhallur Magnusson | A5041100819 | rescue_host_unique | 117 |
| 3819 | Bobby Lee Townsend Sturm Jr | A5054217723 | rescue_host_unique | 165 |
| 3825 | Birgit Abels-Eisenlohr | A5042336921 | rescue_host_unique | 48 |
| 3845 | Lampros Malafouris | A5063140141 | rescue_host_unique | 77 |
| 3893 | Bastiaan Jeroen De Kloet | A5041131454 | rescue_host_unique | 142 |
| 3945 | Magdalena Waligórska-Huhle | A5088957675 | rescue_host_unique | 67 |
| 4066 | Bethany Aram Worzella | A5036024339 | rescue_host_unique | 60 |
| 4080 | Katell, Anne, Sophie Berthelot | A5059140142 | rescue_host_unique | 172 |
| 4185 | Emmanuel, François, Jean Breuillard | A5024332688 | rescue_host_unique | 115 |
| 4187 | Jens Peter Hommelhoff | A5026575046 | rescue_host_unique | 515 |
| 4197 | Sylvie, Jeanine Lejeune Ép Lorthois | A5075453232 | rescue_duplicate | 77 |
| 4200 | Philippe, Guy, Marie Marmottant | A5023549418 | rescue_duplicate | 153 |
| 4228 | Gert Fredrik Bäckhed | A5047963662 | rescue_host_unique | 315 |
| 4230 | Maria Carla Saleh Bottegoni | A5082764484 | rescue_semantic | 83 |
| 4234 | Patricio Aloy Calaf | A5015088787 | rescue_maxpapers | 213 |

## 六、rescue_lowconf 明细(待人工)
| rid | eu_name | authorid | cname | reason | n_papers |
| ---: | --- | --- | --- | --- | ---: |
| 178 | Ehud Itzhak Qimron | A5024578974 | Yossef Itzhak | host_ok_fuzzy_fail | 137 |
| 211 | Thore Rickard Hakan Sandberg | A5049256903 | Sverre Sandberg | host_ok_fuzzy_fail | 417 |
| 264 | Meritxell Huch Ortega | A5026367289 | R. Huch | host_ok_fuzzy_fail | 584 |
| 316 | Sophie Geneviève Elisabeth Martin Benton | A5012652688 | Nicholas G. Martin | host_ok_fuzzy_fail | 2663 |
| 417 | Soni Savai | A5061019810 | Rajkumar Savai | host_ok_fuzzy_fail | 243 |
| 467 | Georgios Garinis | A5031038259 | George A. Garinis | host_ok_fuzzy_fail | 73 |
| 476 | Luciana Isabella Peduto | A5080359435 | Lucie Peduto | host_ok_fuzzy_fail | 28 |
| 1030 | Adriana Maria (Charissa) De Bekker | A5039654436 | Mireille N. Bekker | host_ok_fuzzy_fail | 186 |
| 1081 | Barbara Stecher-Letsch | A5000730993 | Santa Bárbara | host_ok_fuzzy_fail | 168 |
| 1106 | Sylvia Maria Cremer-Sixt | A5015575955 | Michael Sixt | host_ok_fuzzy_fail | 157 |
| 1136 | Asaf Vardi | A5000059818 | Moshe Y. Vardi | host_ok_fuzzy_fail | 901 |
| 1208 | Martha Gerdina Vijver | A5062981502 | Marc J. van de Vijver | host_ok_fuzzy_fail | 516 |
| 1262 | Pascale Andrée Simone Lapujade Daran | A5033414409 | Jean‐Marc Daran | host_ok_fuzzy_fail | 180 |
| 1952 | Felix Emilio Rico Camps | A5049250189 | Pelayo Camps | host_ok_fuzzy_fail | 306 |
| 2162 | Liv Haahr Hornekaer | A5083204894 | S. Haahr | host_ok_fuzzy_fail | 84 |
| 2336 | Andrii Andrey Klymchenko | A5086625921 | Andrey S. Klymchenko | host_ok_fuzzy_fail | 396 |
| 2338 | Dzmitry Shchukin | A5024535129 | Dmitry G. Shchukin | host_ok_fuzzy_fail | 310 |
| 2567 | Johannes Maria Broersen | A5001106217 | Jan Broersen | host_ok_fuzzy_fail | 120 |
| 2844 | Laoise Maria Cunningham | A5043648150 | Michael L. Cunningham | host_ok_fuzzy_fail | 437 |
| 2963 | Beatrix Rowlinson | A5072917215 | A. Rowlinson | host_ok_fuzzy_fail | 308 |
| 3153 | Christina Felfe De Ormeno | A5110017785 | Rajib Lal De | host_ok_fuzzy_fail | 35 |
| 3266 | Elias - Ilias Dinas - Ntinas | A5040629621 | Elias Dinas | host_ok_fuzzy_fail | 152 |
| 3311 | Nadia Capus | A5024319314 | Nadja Capus | host_ok_fuzzy_fail | 68 |
| 3384 | Rebecca Anna Empson Mannerfelt | A5000813061 | Rebecca Empson | host_ok_fuzzy_fail | 38 |
| 3467 | Clara Laetitia Ter Hoeven | A5022558761 | Rolph van der Hoeven | host_ok_fuzzy_fail | 125 |
| 3496 | Simcha Jong Kon Chin | A5072957389 | Nyuk Ling Chin | host_ok_fuzzy_fail | 231 |
| 3622 | Judith Burkart Natalucci | A5089432101 | Judith M. Burkart | host_ok_fuzzy_fail | 150 |
| 3697 | Maria Catharina Wichers | A5087751292 | Marieke Wichers | host_ok_fuzzy_fail | 308 |
| 3708 | Alberdina Cornelia Krabbendam | A5051708068 | Lydia Krabbendam | host_ok_fuzzy_fail | 384 |
| 3762 | Johanna Tummers | A5061491560 | Philippe Tummers | host_ok_fuzzy_fail | 93 |
| 4043 | Elisabeth (Elise) Van Nederveen Meerkerk | A5070504139 | Aart J. Nederveen | host_ok_fuzzy_fail | 552 |
| 4172 | Rebecca Anna Empson Mannerfelt | A5000813061 | Rebecca Empson | host_ok_fuzzy_fail | 38 |
| 262 | Dr. Debora Gasperini | A5040252580 |  | lowpaper | 26 |
| 892 | Esteban Gurzov Amarelo | A5071473485 |  | lowpaper | 1 |
| 1314 | Martinus Kool | A5053312672 |  | lowpaper | 16 |
| 1513 | Amaelle Adeline Landais Israel | A5045405213 |  | lowpaper | 3 |
| 2110 | Anne-Clémence Corminboeuf Wodrich | A5112550221 |  | lowpaper | 3 |
| 2525 | Mikolaj Konstanty Bojanczyk | A5108790838 |  | lowpaper | 1 |
| 2701 | Antoine, Sébastien Girard | A5101910928 |  | lowpaper | 15 |
| 3044 | Marijke Haverkorn Van Rijsewijk | A5081754940 |  | lowpaper | 13 |
| 3148 | Joachim Konrad Mierendorff | A5002661648 |  | lowpaper | 23 |
| 3465 | Heinz Christoph Steinhardt | A5072312199 |  | lowpaper | 25 |
| 3793 | Emine Fisek Turem | A5087607624 |  | lowpaper | 22 |

## 七、剩余未定名单(快照真缺失 / 空姓名 -> 人工)
| rid | eu_name | host_id |
| ---: | --- | --- |
| 203 | Madanbabu Mohan | I4210087105 |
| 279 | Asimina Gkouti | I205582932 |
| 617 | Suliann Benhamed-Daghighi-Ardekani | I1294671590 |
| 976 | Jahn Frederik Froen | I1333353642 |
| 1080 | Bastiaan  Elie Dutilh | I4210107832 |
| 3040 | Hendrik Jurgen Hildebrandt | I2799784675 |
| 3092 | Albert Van Leeuwen | I4405262988 |
| 3959 | Edeltraud Aspoeck | I15766117 |
| 4036 | Marinos Sarigiannis | I8901234 |
| 4049 | Aikaterini Charvati | I8087733 |
| 4163 | (空姓名) | I887064364 |
| 4179 | (空姓名) | I145847075 |
| 4205 | (空姓名) | I32597200 |
| 4239 | (空姓名) | I145847075 |
| 4240 | (空姓名) | I887064364 |
| 4241 | (空姓名) | I32597200 |
