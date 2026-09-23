#!/usr/bin/env python3
"""Two-service isolated lab; PostgreSQL must already be provisioned. No Runtime service."""
import argparse
import asyncio
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path


TE = Path(__file__).resolve().parents[1]
OV = Path(os.environ.get('ONTOLOGY_OV_REPO', TE.parent/'OpenViking'))
ROOT = Path(os.environ.get('ONTOLOGY_LAB_STATE', '/tmp/te-ontology-v5'))
TE_DSN = os.environ.get('ONTOLOGY_LAB_TE_DSN', 'postgresql:///ontology_v5_te?host=/tmp/te-ontology-lab&port=55439')
OV_DSN = os.environ.get('ONTOLOGY_LAB_OV_DSN', 'postgresql:///ontology_v5_ov?host=/tmp/te-ontology-lab&port=55439')
PORTS = {'te': 52210, 'ov': 52211}
MODULES = {'te': 'team_ontology.standalone:create_app', 'ov': 'openviking.ontology.standalone:create_app'}


def private(path, body):
    fd = os.open(path, os.O_WRONLY|os.O_CREAT|os.O_TRUNC, 0o600)
    with os.fdopen(fd,'w') as f:
        f.write(body if isinstance(body,str) else json.dumps(body,indent=2))


def listening(port):
    with socket.socket() as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(('127.0.0.1',port))==0


async def seed():
    import asyncpg
    sys.path.insert(0,str(OV))
    from openviking.ontology.store import Store
    store=Store(OV_DSN,native_tasks=False);await store.start()
    c=await asyncpg.connect(TE_DSN);await c.close()
    async with store.pool.acquire() as c:
        for tenant in ['ontology-demo','ontology-other']:
            await c.execute('INSERT INTO public.ov_ontology_tenants(tenant,enabled) VALUES($1,true) ON CONFLICT DO NOTHING',tenant)
            for subject,permissions in [('reviewer',['read','build','approve','publish','feedback','observe']),('agent',['read','feedback']),('outsider',['read'])]:
                await c.execute('INSERT INTO public.ov_ontology_principals VALUES($1,$2,$3,true) ON CONFLICT DO NOTHING',tenant,subject,permissions)
    await store.close()


def start():
    ROOT.mkdir(parents=True,exist_ok=True,mode=0o700)
    if any(listening(p) for p in PORTS.values()):
        raise RuntimeError('Lab port occupied; stop the recorded lab first')
    asyncio.run(seed())
    if not (ROOT/'credentials.json').exists():
        private(ROOT/'credentials.json',{name:secrets.token_urlsafe(36) for name in ['reviewer','agent','outsider','other','signing']})
    keys=json.loads((ROOT/'credentials.json').read_text())
    identities={keys[name]:{'tenant':'ontology-demo','subject':name} for name in ['reviewer','agent','outsider']}
    identities[keys['other']]={'tenant':'ontology-other','subject':'reviewer'}
    private(ROOT/'identities.json',identities)
    if 'root' not in keys:
        keys['root'] = secrets.token_urlsafe(36)
        private(ROOT/'credentials.json', keys)
    env={**os.environ,'ONTOLOGY_DEMO_MODE':'1','ONTOLOGY_DEMO_IDENTITIES':str(ROOT/'identities.json'),
         'OV_ONTOLOGY_ENABLED':'1','OV_ONTOLOGY_PG_DSN':OV_DSN,'OV_ONTOLOGY_SIGNING_SECRET':keys['signing'],
         'ONTOLOGY_LAB_ROOT_KEY':keys['root'],
         'TE_ONTOLOGY_STATE':str(ROOT/'state'),'ONTOLOGY_LAB_TE_DSN':TE_DSN,'ONTOLOGY_LAB_OV_URL':'http://127.0.0.1:52211'}
    for obsolete in ['OV_ONTOLOGY_TE_PUBLIC_KEYS', 'TE_ONTOLOGY_SIGNING_KEY_FILE']:
        env.pop(obsolete, None)
    for name in ['ov','te']:
        repo=OV if name=='ov' else TE
        with (ROOT/f'{name}.log').open('ab') as log:
            proc=subprocess.Popen([sys.executable,'-m','uvicorn',MODULES[name],'--factory','--host','127.0.0.1','--port',str(PORTS[name])],
                cwd=repo,env={**env,'PYTHONPATH':str(repo)},stdout=log,stderr=log,start_new_session=True)
        Path(f'/tmp/te_ontology_v5_{name}.pid').write_text(str(proc.pid))
        deadline=time.monotonic()+25
        while not listening(PORTS[name]) and proc.poll() is None and time.monotonic()<deadline:time.sleep(0.2)
        if not listening(PORTS[name]):raise RuntimeError(f'{name} failed; inspect {ROOT}/{name}.log')
    print(json.dumps({'services':PORTS,'state':str(ROOT),'separate_runtime':False}))


def stop():
    for name,port in PORTS.items():
        path=Path(f'/tmp/te_ontology_v5_{name}.pid')
        if not path.exists():continue
        pid=int(path.read_text());command=subprocess.run(['ps','-p',str(pid),'-o','command='],capture_output=True,text=True).stdout
        if MODULES[name] in command and str(port) in command:
            os.kill(pid,signal.SIGTERM)
            deadline=time.monotonic()+10
            while listening(port) and time.monotonic()<deadline:time.sleep(0.2)
            if listening(port):raise RuntimeError(f'{name} did not stop')
        path.unlink()
    print('Stopped recorded V5 services; PostgreSQL retained')


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('command',choices=['start','stop']);args=parser.parse_args()
    start() if args.command=='start' else stop()
