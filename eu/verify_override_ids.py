# -*- coding: utf-8 -*-
import json, time, urllib.request, sys
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
IDS={
'GENES':'I4210108488',
'BIOPOLIS':'I4390039289',
'CIIMAR':'I4387153601',
'CentersHumanities':'I4210090579',
'DemographicBarcelona':'I4210121658',
'FCienciasID':'I4392738271',
'VUmc':'I911458345',
'ISTID':'I4387152517',
'IMDEANetworks':'I2802499160',
'IRBBellinzona':'I4387152247',
'LIB':'I4210099549',
'IGN':'I1327553481',
'MfN':'I1313606977',
'NOVAIDFCT':'I4405253347',
'PrincessMaxima':'I4210127118',
'ZRCSAZU':'I4387154574',
'RiMED':'I4210150906',
}
key=Path('../.openalex_api_key').read_text(encoding='utf-8').strip()
def get(oid):
    url=f'https://api.openalex.org/institutions/{oid}'
    req=urllib.request.Request(url,headers={'User-Agent':'host-bridge-resolver/1.0'})
    with urllib.request.urlopen(req,timeout=30) as resp: d=json.load(resp)
    return {'id':(d.get('id') or '').rsplit('/',1)[-1],'display_name':d.get('display_name'),'country_code':d.get('country_code'),'ror':d.get('ror')}
out={}
for tag,oid in IDS.items():
    try:
        out[tag]=get(oid); print(tag,oid,out[tag])
    except Exception as e: out[tag]={'id':oid,'error':f'{type(e).__name__}: {e}'}; print(tag,'ERROR',e)
    time.sleep(0.15)
Path('override_id_verify.json').write_text(json.dumps(out,ensure_ascii=False,indent=2),encoding='utf-8')
print('saved')
