import json,os,signal,subprocess,time
from pathlib import Path
import httpx
import runpy
smoke=runpy.run_path("scripts/ontology_smoke.py")
smoke["publish"]("Availability")
root=Path('/tmp/te-ontology-v5');keys=json.loads((root/'credentials.json').read_text())
client=httpx.Client(base_url='http://127.0.0.1:52211/api/v1/enterprise',headers={'Authorization':'Bearer '+keys['agent']},timeout=10,trust_env=False)
before=client.post('/ontology/query',json={'entity_ids':['Availability:001']});before.raise_for_status();assert before.json()['facts']
pid=int(Path('/tmp/te_ontology_v5_te.pid').read_text());cmd=subprocess.check_output(['ps','-p',str(pid),'-o','command='],text=True)
assert 'team_ontology.standalone:create_app' in cmd and '52210' in cmd
os.kill(pid,signal.SIGTERM);time.sleep(1)
try:
 httpx.get('http://127.0.0.1:52210/te/enterprise/v1/jobs',timeout=1,trust_env=False)
 raise AssertionError('TE still available')
except httpx.ConnectError:pass
after=client.post('/ontology/query',json={'entity_ids':['Availability:001']});after.raise_for_status()
assert after.json()==before.json()
result={'status':'passed','te_stopped':True,'ov_http_status':after.status_code,'generation':after.json()['semantic_generation'],'facts_equal':True,'fact_count':len(after.json()['facts']),'scope':'published non-empty facts remain queryable with TE stopped'}
Path('docs/ontology-integration/results/root-key-te-outage.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result))
