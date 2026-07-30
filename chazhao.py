# -*- coding: utf-8 -*-
import pandas as pd

eu = pd.read_excel('EU.xlsx')
col = 'Researcher(s)'

names = eu[col].dropna().astype(str).str.strip()
names = names[names != '']

total_rows = len(names)
vc = names.value_counts()

dup_names = vc[vc > 1]                 # 出现次数 > 1 的姓名
n_dup_names = len(dup_names)           # 有多少个不同的姓名是重复的
n_dup_rows = int(dup_names.sum())      # 涉及这些重复姓名的记录条数

print('总记录数(非空 Researcher):', total_rows)
print('不同姓名数:', len(vc))
print('同名的姓名种类数(出现>1次):', n_dup_names)
print('涉及同名的记录条数:', n_dup_rows)
print()
print('出现次数最多的前 20 个同名学者:')
print(dup_names.head(20).to_string())
