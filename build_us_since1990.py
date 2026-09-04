import json, glob, os, re, csv, time, collections

t0 = time.time()
YEARS = [str(y) for y in range(1990, 2024)]
OUT = r'd:\data_matching\us_since1990.csv'

FECD_RE = re.compile(r'faculty early career development', re.I)
# CAREER 作为奖项标记：CAREER: 冒号（覆盖 "CAREER:"、"SusChEM: CAREER:" 等前缀形态）或 (CAREER)
TITLE_RE = re.compile(r'\bcareer\s*:|\(\s*career\s*\)', re.I)
# 明确的前身/他类奖：即便带 1045 也不算 CAREER（\s+ 容忍多空格）
EXCLUDE_RE = re.compile(r'presidential faculty fellow|young investigator|\bpyi\b|\bnyi\b|presidential young|career\s+advancement\s+award|career[\s-]+awareness', re.I)

def title_is_career(t):
    tl = (t or '').strip()
    if FECD_RE.search(tl):
        return True
    return bool(TITLE_RE.search(tl))

def fmt_date(s):
    # 'YYYY-MM-DD' -> 'MM/DD/YYYY'
    if not s: return ''
    m = re.match(r'(\d{4})-(\d{2})-(\d{2})', s)
    return f'{m.group(2)}/{m.group(3)}/{m.group(1)}' if m else s

def fmt_money(v):
    try: return '${:,.2f}'.format(float(v))
    except: return ''

def excel(s):
    return f'="{s}"' if s else ''

HEADER = ['AwardNumber','Title','NSFOrganization','Program(s)','StartDate','LastAmendmentDate',
          'PrincipalInvestigator','State','Organization','AwardInstrument','ProgramManager','EndDate',
          'AwardedAmountToDate','Co-PIName(s)','PIEmailAddress','OrganizationStreet','OrganizationCity',
          'OrganizationState','OrganizationZip','OrganizationPhone','NSFDirectorate',
          'ProgramElementCode(s)','ProgramReferenceCode(s)','ARRAAmount','Abstract']

rows = []
by_year = collections.Counter()
by_signal = collections.Counter()   # title_only / code_only / both
strict_title_samples = []
nsf_ids = set()
total = 0
excluded_contract = 0

for yr in YEARS:
    if not os.path.isdir(yr): continue
    for f in glob.glob(yr + '/*.json'):
        total += 1
        try:
            d = json.load(open(f, encoding='utf-8'))
        except Exception:
            continue
        title = d.get('awd_titl_txt') or ''
        refs = d.get('pgm_ref') or []
        th = title_is_career(title)
        ch = any((r.get('pgm_ref_code') == '1045') for r in refs)
        if not (th or ch):
            continue
        # 剔除行政支持合同（Contract-BOA/Task Order 等），CAREER 只发 grant/interagency
        if 'contract' in (d.get('awd_istr_txt') or '').lower():
            excluded_contract += 1
            continue
        # code_only 时排除明确的前身/他类奖
        if ch and not th and EXCLUDE_RE.search(title):
            continue
        # title 命中但恰好是被排除项（如 career advancement award），也剔除
        if th and EXCLUDE_RE.search(title) and not FECD_RE.search(title) and not re.search(r'\bcareer\s*:', title, re.I):
            continue

        if th and ch: by_signal['both'] += 1
        elif th: by_signal['title_only'] += 1
        else: by_signal['code_only'] += 1
        if th and len(strict_title_samples) < 40:
            strict_title_samples.append((yr, title[:70]))
        by_year[yr] += 1

        # 选 PI 与 co-PI
        pis = d.get('pi') or []
        pi = next((p for p in pis if (p.get('pi_role') or '').lower().startswith('principal')), pis[0] if pis else {})
        copis = [p for p in pis if p is not pi and 'co-pi' in (p.get('pi_role') or '').lower() or
                 (p is not pi and (p.get('pi_role') or '').lower().startswith('co'))]
        copis = [p for p in pis if p is not pi]
        if pi.get('nsf_id'): nsf_ids.add(pi['nsf_id'])

        inst = d.get('inst') or {}
        pgm_ele = d.get('pgm_ele') or []
        prog_names = ', '.join(e.get('pgm_ele_name','') for e in pgm_ele if e.get('pgm_ele_name'))
        ele_codes = ', '.join(e.get('pgm_ele_code','') for e in pgm_ele if e.get('pgm_ele_code'))
        ref_codes = ', '.join(r.get('pgm_ref_code','') for r in refs if r.get('pgm_ref_code'))
        copi_str = '; '.join(
            (p.get('pi_full_name','') + (' ' + p['pi_email_addr'] if p.get('pi_email_addr') else '')).strip()
            for p in copis)

        rows.append([
            excel(d.get('awd_id','')),
            title,
            d.get('div_abbr','') or '',
            prog_names,
            fmt_date(d.get('awd_eff_date')),
            fmt_date(d.get('awd_max_amd_letter_date')),
            pi.get('pi_full_name',''),
            inst.get('inst_state_code',''),
            inst.get('inst_name',''),
            d.get('awd_istr_txt','') or '',
            d.get('po_sign_block_name','') or '',
            fmt_date(d.get('awd_exp_date')),
            fmt_money(d.get('awd_amount')),
            copi_str,
            pi.get('pi_email_addr',''),
            inst.get('inst_street_address',''),
            inst.get('inst_city_name',''),
            inst.get('inst_state_code',''),
            excel(inst.get('inst_zip_code','')),
            excel(inst.get('inst_phone_num','')),
            d.get('dir_abbr','') or '',
            ele_codes,
            ref_codes,
            fmt_money(d.get('awd_arra_amount')),
            d.get('awd_abstract_narration','') or '',
        ])

# 按 award 号排序（稳定、可复现）
rows.sort(key=lambda r: r[0])
with open(OUT, 'w', newline='', encoding='utf-8-sig') as f:
    w = csv.writer(f)
    w.writerow(HEADER)
    w.writerows(rows)

print(f'elapsed {time.time()-t0:.1f}s  scanned={total}')
print(f'WROTE {len(rows)} rows -> {OUT}')
print(f'unique PI nsf_id = {len(nsf_ids)}')
print(f'excluded admin contracts = {excluded_contract}')
print('signals:', dict(by_signal))
print('--- by year ---')
for y in YEARS:
    if by_year[y]: print(f'  {y}: {by_year[y]}')
print('--- strict title samples (self-check false positives) ---')
for y,t in strict_title_samples: print(f'  {y}: {t!r}')
