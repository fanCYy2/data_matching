# -*- coding: utf-8 -*-
"""只把「我判 OK 且人工列为空」的行写入 yes;绝不覆盖已有人工值;
我判非 OK 的一律不写(留空给人工)。逐单元格改,最大化保持原文件格式。
"""
import csv, io, sys
from pathlib import Path
sys.stdout.reconfigure(encoding='utf-8')

PAIRS = [('round3_sample_50.csv','round3_sample_50_audited.csv'),
         ('round4_sample_100.csv','round4_sample_100_audited.csv')]

DRY = '--write' not in sys.argv

for orig, aud in PAIRS:
    # 我判 OK 的 rid 集合
    ok = set()
    with open(aud, encoding='utf-8-sig', newline='') as f:
        for row in csv.DictReader(f):
            if row['my_verdict'].strip() == 'OK':
                ok.add(str(row['rid']).strip())

    raw = Path(orig).read_bytes()
    bom = raw.startswith(b'\xef\xbb\xbf')
    text = raw.decode('utf-8-sig')
    rd = list(csv.reader(io.StringIO(text)))
    header = rd[0]
    yi = [i for i,h in enumerate(header) if h.strip() == 'yes_or_no'][0]
    ri = [i for i,h in enumerate(header) if h.strip() == 'rid'][0]

    changed, skip_nonempty, skip_nonok = [], 0, 0
    for row in rd[1:]:
        if not row:
            continue
        rid = row[ri].strip()
        cur = row[yi].strip() if yi < len(row) else ''
        if rid in ok:
            if cur == '':
                if not DRY:
                    while len(row) <= yi:
                        row.append('')
                    row[yi] = 'yes'
                changed.append(rid)
            else:
                skip_nonempty += 1      # 我判OK但人工已填 → 保留人工
        else:
            skip_nonok += 1             # 我判非OK → 不写

    print(f'=== {orig} (BOM={bom}) ===')
    print(f'  将填 yes 的行数: {len(changed)}')
    print(f'  rid: {" ".join(changed)}')
    print(f'  我判OK但人工已有值→保留: {skip_nonempty} 行')
    print(f'  我判非OK→留空: {skip_nonok} 行')

    if not DRY:
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator='\r\n', quoting=csv.QUOTE_MINIMAL)
        w.writerows(rd)
        out = buf.getvalue()
        data = ('﻿' + out) if bom else out
        Path(orig).write_bytes(data.encode('utf-8'))
        print('  已写回。')
    print()
