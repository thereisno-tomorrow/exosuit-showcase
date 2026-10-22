"""Read approved sender details from stdin; never print credential material."""
import hashlib
import hmac
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import tomllib
from datetime import UTC, datetime
from urllib.parse import urlsplit


def validate(data):
    url, key = data['url'].strip(), data['key'].strip()
    p=urlsplit(url)
    if (p.scheme!='https' or p.hostname!='api2.cursor.sh' or p.port not in (None,443)
        or not p.path.startswith('/automations/') or p.username or p.password
        or p.query or p.fragment or any(ord(c)<33 or ord(c)>126 for c in url)):
        raise ValueError('URL_NOT_EXPECTED_ROUTINE_ENDPOINT')
    if not re.fullmatch(r'crsr_[A-Za-z0-9_-]{20,256}',key):
        raise ValueError('COPY_KEY_VALUE_ONLY')
    return url,key


def install(data):
    import pwd
    url,key=validate(data)
    root=pathlib.Path('/etc/gremlin-town-wake')
    config_path=root/'wake.toml'
    before=config_path.read_text()
    original=tomllib.loads(before)
    target={'participant_id':'fleet-boss-grok','enabled':True,'kind':'grok',
            'url':url,'allowed_host':'api2.cursor.sh',
            'secret_file':str(root/'grok.secret'),
            'auth_header':'Authorization','auth_prefix':'Bearer '}
    existing=[t for t in original['target'] if t['participant_id']=='fleet-boss-grok']
    if existing and existing != [target]:
        raise ValueError('EXISTING_TARGET_DIFFERS_STOP_FOR_REVIEW')
    secret_path=root/'grok.secret'
    if secret_path.exists() and not hmac.compare_digest(secret_path.read_text().strip(),key):
        raise ValueError('EXISTING_KEY_DIFFERS_STOP_FOR_REVIEW')
    account=pwd.getpwnam('gremlin-town')
    if not secret_path.exists():
        fd=os.open(secret_path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o640)
        with os.fdopen(fd,'w') as f: f.write(key+'\n');f.flush();os.fsync(f.fileno())
        os.chown(secret_path,0,account.pw_gid)
    if not existing:
        after=before+'\n\n[[target]]\n'+'\n'.join(k+' = '+('true' if v is True else json.dumps(v)) for k,v in target.items())+'\n'
        parsed=tomllib.loads(after)
        assert parsed.pop('target')==original['target']+[target]
        assert parsed=={k:v for k,v in original.items() if k!='target'}
        backup=root/('wake-before-grok-'+datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')+'.toml')
        fd=os.open(backup,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'w') as f:f.write(before)
        fd,temp=tempfile.mkstemp(prefix='.wake-grok-',dir=root)
        with os.fdopen(fd,'w') as f:f.write(after);f.flush();os.fsync(f.fileno())
        os.chmod(temp,0o640);os.chown(temp,0,account.pw_gid);os.replace(temp,config_path)
    subprocess.run(['systemctl','restart','gremlin-town-wake.service'],check=True,capture_output=True)
    subprocess.run(['systemctl','is-active','--quiet','gremlin-town-wake.service'],check=True)
    print(json.dumps({'saved':True,'target':'fleet-boss-grok','worker':'active'}))


if __name__=='__main__':
    try:
        raw=sys.stdin.read(8193)
        if len(raw)>8192:raise ValueError('INPUT_TOO_LARGE')
        install(json.loads(raw))
    except Exception as exc:
        # Error messages are never reflected: a library exception may include input.
        print(json.dumps({'saved':False,'error_type':type(exc).__name__}))
        sys.exit(1)
