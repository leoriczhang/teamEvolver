#!/usr/bin/env python3
"""Real HTTP smoke against the two-service V5 lab; fixture is explicitly reported."""
import json
import os
import time
import uuid
from pathlib import Path

import httpx

ROOT=Path(os.environ.get('ONTOLOGY_LAB_STATE','/tmp/te-ontology-v5'))
keys=json.loads((ROOT/'credentials.json').read_text());run=uuid.uuid4().hex[:10]
client=httpx.Client(timeout=30,trust_env=False);checks=[]

def call(method,path,body=None,*,te=True,user='reviewer',status=200):
    base='http://127.0.0.1:52210/te/enterprise/v1' if te else 'http://127.0.0.1:52211/api/v1/enterprise'
    r=client.request(method,base+path,json=body,headers={'Authorization':'Bearer '+keys[user]})
    if r.status_code!=status:raise AssertionError(f'{method} {path}: {r.status_code} {r.text[:200]}')
    return r.json()

def ready(job):
    deadline=time.monotonic()+30
    while time.monotonic()<deadline:
        job=call('GET','/jobs/'+job['id'])
        if job['state']=='review_ready':return job
        if job['state']=='failed':raise AssertionError(job['error'])
        time.sleep(.2)
    raise AssertionError('build timeout')

def publish(domain):
    schema=domain+'-'+run
    call('POST','/schemas',{'revision':schema,'entity_types':[domain],'predicates':{'status':{'subject_type':domain}},'rules':[{'rule_id':'recorded','predicate':'status','equals':'recorded'}],'issue_pack_revision':schema,'evidence_slots':['status']},status=201)
    entity=domain+':001'
    source=call('POST','/sources',{'source_id':schema,'revision':'r1','readers':['reviewer','agent'],'text':json.dumps([{'entity':{'entity_id':entity,'authority_namespace':'synthetic','entity_type':domain,'external_id':'001','label':domain+' 001','aliases':['example '+domain]},'quote':'recorded','assertion':{'assertion_id':schema+':status','subject':entity,'predicate':'status','value':'recorded','valid_from':'2026-01-01T00:00:00Z'}}])},status=201)
    caps=call('GET','/capabilities');key=schema
    body={'submission_key':key,'manifest':{'schema_revision':schema,'sources':[source],'expected_generation':caps['generation'],'mode':'replace','extractor':'fixture'}}
    job=call('POST','/jobs',body,status=202)
    assert job['id']==call('POST','/jobs',body,status=202)['id'];job=ready(job)
    job=call('POST',f"/jobs/{job['id']}/review",{'path':'assertions.0','decision':'approve','expected_digest':job['result']['artifact']['candidate_digest']})
    call('POST',f"/jobs/{job['id']}/prepare")
    call('POST',f"/jobs/{job['id']}/approve",{'note':'Fixture source and assertion reviewed'})
    published=call('POST',f"/jobs/{job['id']}/commit",{'commit_key':key})
    assert published['state']=='published'
    repeated=call('POST',f"/jobs/{job['id']}/commit",{'commit_key':key})
    assert repeated['result']['receipt']==published['result']['receipt']
    packet=call('POST','/context/compose',{'entity_ids':[entity]},te=False,user='agent')
    assert packet['facts'] and packet['evaluations'][0]['truth']=='true'
    outsider=call('POST','/ontology/query',{'entity_ids':[entity]},te=False,user='outsider');assert not outsider['facts']
    call('POST','/ontology/feedback',{'result_ref':str(packet['semantic_generation']),'note':'Fixture feedback'},te=False,user='agent',status=202)
    checks.append({'domain':domain,'status':'passed','generation':packet['semantic_generation'],'receipt':published['result']['receipt']})
    return job,source

first,source=publish('Shipment');second,_=publish('Procurement')
rollback=ready(call('POST','/rollback-candidates',{'generation':checks[0]['generation'],'submission_key':'rollback-'+run},status=202))
call('POST',f"/jobs/{rollback['id']}/prepare");call('POST',f"/jobs/{rollback['id']}/approve",{'note':'Historical version reviewed again'})
restored=call('POST',f"/jobs/{rollback['id']}/commit",{'commit_key':'rollback-'+run});assert restored['state']=='published'
checks.append({'name':'rollback-new-generation','status':'passed','receipt':restored['result']['receipt']})
event=call('POST','/source-events',{'event_id':'withdraw-'+run,'source_id':source['source_id'],'revision':'r1','status':'revoked'})
assert not call('POST','/ontology/query',{'entity_ids':['Shipment:001']},te=False,user='agent')['facts']
checks.append({'name':'withdrawal-blocks-access','status':'passed'})
result={'publication_auth':'trusted_root','publication_contract':'sf.ontology.commit.v2','services':2,'separate_runtime':False,'extractor':'fixture','model_calls':0,'checks':checks,'real_model':'not covered'}
output=Path('docs/ontology-integration/results/root-key-http.json');output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
print(json.dumps(result,ensure_ascii=False,indent=2))
