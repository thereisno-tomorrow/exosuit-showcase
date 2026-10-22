"""Approved one-time Hermes WA webhook setup; contains no credentials.

Run on the GT host after staging worker.py and receiver-prompt.txt beside it.
Does not activate the wake service or restart GT/Ciel.
"""
import hashlib
import json
import os
from pathlib import Path
import pwd
import secrets
import subprocess
from datetime import datetime, UTC

root=Path(__file__).resolve().parent
etc=Path('/etc/gremlin-town-wake')
assert not etc.exists(), 'Inspect existing wake configuration before changing it'
inspect=json.loads(subprocess.check_output(['docker','inspect','hermes-wa']))[0]
assert inspect['HostConfig']['NetworkMode']=='hermes-wa_default'
assert not inspect['HostConfig']['PortBindings'], 'Do not expose webhook publicly'
assert any(m['Source']=='/srv/hermes-example' and m['Destination']=='/opt/data' for m in inspect['Mounts'])
ip=inspect['NetworkSettings']['Networks']['hermes-wa_default']['IPAddress']
assert ip.startswith('172.18.'), 'Review changed bridge before proceeding'
secret=secrets.token_urlsafe(32)
stamp=datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')
prompt=(root/'receiver-prompt.txt').read_text()
container_script=r'''
import sys,json,pathlib,yaml,os,shutil,hashlib
data=json.load(sys.stdin)
home=pathlib.Path('/opt/data')
cfg=home/'config.yaml'; before=cfg.read_bytes(); current=yaml.safe_load(before)
platforms=current.setdefault('platforms',{})
assert not platforms.get('webhook'), 'Existing webhook config needs explicit merge review'
subs_path=home/'webhook_subscriptions.json'
subs=json.loads(subs_path.read_text()) if subs_path.exists() else {}
assert 'gremlin-town-wake' not in subs, 'Wake route already exists'
backup=home/('gt-wake-backup-'+data['stamp']); backup.mkdir(mode=0o700)
shutil.copy2(cfg,backup/'config.yaml'); os.chmod(backup/'config.yaml',0o600)
if subs_path.exists():
    shutil.copy2(subs_path,backup/'webhook_subscriptions.json')
    os.chmod(backup/'webhook_subscriptions.json',0o600)
platforms['webhook']={'enabled':True,'extra':{'host':'0.0.0.0','port':8644,'rate_limit':10,'max_body_bytes':1024}}
subs['gremlin-town-wake']={'description':'GT request-ID-only wake for hermes-wa',
 'events':['gt.request.ready'],'secret':data['secret'],'prompt':data['prompt'],
 'skills':['gremlin-town'],'deliver':'telegram','enabled':True,
 'toolsets':['hermes-telegram']}  # Nic, 29 Sep 2026: a woken run has his Telegram tools and replies there
def atomic(path,blob):
    tmp=path.with_name(path.name+'.gt-wake-new')
    fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'wb') as f: f.write(blob); f.flush(); os.fsync(f.fileno())
    os.replace(tmp,path)
atomic(subs_path,json.dumps(subs,indent=2).encode())
atomic(cfg,yaml.safe_dump(current,sort_keys=False,allow_unicode=True).encode())
result=yaml.safe_load(cfg.read_text()); result['platforms'].pop('webhook')
original=yaml.safe_load(before); original.setdefault('platforms',{}).pop('webhook',None)
assert result==original, 'Unrelated configuration changed'
print(json.dumps({'backup':str(backup),'config_before_sha256':hashlib.sha256(before).hexdigest(),'config_after_sha256':hashlib.sha256(cfg.read_bytes()).hexdigest(),'route':'gremlin-town-wake'}))
'''
# Keep protected payload in memory/stdin, never argv, logs or tool output.
process=subprocess.run(['docker','exec','-i','-u','1000:1000','hermes-wa','/opt/hermes/.venv/bin/python','-c',container_script],input=json.dumps({'secret':secret,'prompt':prompt,'stamp':stamp}),text=True,capture_output=True)
if process.returncode:
    raise RuntimeError('Hermes setup failed; inspect nonsecret local failure details before retrying')
print(process.stdout.strip())
account=pwd.getpwnam('gremlin-town')
etc.mkdir(mode=0o750); os.chown(etc,0,account.pw_gid)
secret_path=etc/'hermes.secret'
fd=os.open(secret_path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o640)
with os.fdopen(fd,'w') as f: f.write(secret+'\n')
os.chown(secret_path,0,account.pw_gid)
cutoff=datetime.now(UTC).isoformat()
config=f'''enabled = true
hub_config = "/etc/gremlin-town/config.toml"
state_database = "/var/lib/gremlin-town-wake/wakes.sqlite3"
activate_after = "{cutoff}"
include_existing = ["req_dfd2efee0e0d45c095fffb443f02f57c"]

[[target]]
participant_id = "hermes-wa"
enabled = true
kind = "hermes"
url = "http://{ip}:8644/webhooks/gremlin-town-wake"
secret_file = "/etc/gremlin-town-wake/hermes.secret"
'''
(etc/'wake.toml').write_text(config); os.chmod(etc/'wake.toml',0o640); os.chown(etc/'wake.toml',0,account.pw_gid)
state=Path('/var/lib/gremlin-town-wake'); state.mkdir(mode=0o700); os.chown(state,account.pw_uid,account.pw_gid)
subprocess.run(['docker','restart','hermes-wa'],check=True,capture_output=True)
print(json.dumps({'hermes_restarted':True,'target':'hermes-wa','private_ip':ip,'activate_after':cutoff,'worker_started':False}))
