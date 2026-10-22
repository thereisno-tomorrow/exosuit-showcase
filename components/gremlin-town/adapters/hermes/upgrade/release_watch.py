"""Read-only fortnightly upstream release notice. Never installs or tests a candidate."""
import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import urllib.request

API = 'https://api.github.com/repos/NousResearch/hermes-agent/releases/latest'
INTERVAL = timedelta(days=14)

def version(name):
    match = re.search(r'\bv(\d+)\.(\d+)\.(\d+)\b', name)
    if not match:
        raise ValueError('release version unavailable')
    return tuple(map(int, match.groups()))

def release():
    req = urllib.request.Request(API, headers={'User-Agent': 'Exosuit-Hermes-release-check', 'Accept': 'application/vnd.github+json'})
    with urllib.request.urlopen(req, timeout=30) as response:
        value = json.loads(response.read(2_000_001))
    if value.get('draft') or value.get('prerelease') or not isinstance(value.get('id'), int):
        raise ValueError('stable release metadata unavailable')
    version(value['name'])
    if not value['html_url'].startswith('https://github.com/NousResearch/hermes-agent/releases/tag/'):
        raise ValueError('unexpected release URL')
    return {k: value[k] for k in ('id', 'name', 'tag_name', 'html_url', 'published_at')}

def inspect_live():
    raw = subprocess.run(['docker', 'inspect', 'hermes-wa'], capture_output=True, text=True, check=True, timeout=20)
    item = json.loads(raw.stdout)[0]
    if not item['State']['Running']:
        raise RuntimeError('Diablo is not running; release check stopped')
    # Only public runtime fields; never read environment values or private chat content.
    status = json.loads(Path('/srv/hermes-example/gateway_state.json').read_text())
    stamp = datetime.fromisoformat(status['updated_at'])
    if (datetime.now(timezone.utc) - stamp).total_seconds() > 180:
        raise RuntimeError('Diablo runtime status is stale; release check stopped')
    return status['code_version']

def save(path, value):
    temp = path.with_suffix('.pending')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, (json.dumps(value, indent=2) + '\n').encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temp, path)

def notice(candidate, installed, notified):
    if version(candidate['name']) <= version('v' + installed) or str(candidate['id']) in notified:
        return None
    return (f"Hermes update available: {candidate['name']}. Diablo currently runs {installed}. "
            "Practice tests have not run for this release. Reply here to request those tests; "
            "a live upgrade still needs your approval. Nothing was changed.\n" + candidate['html_url'])

def send(message, target):
    subprocess.run(['docker', 'exec', '-i', 'hermes-wa', '/opt/hermes/.venv/bin/hermes',
                    'send', '--to', target, '--quiet', '--file', '-'], input=message,
                   text=True, capture_output=True, check=True, timeout=60)

def run(root, preview=False):
    now = datetime.now(timezone.utc)
    config = json.loads((root / 'config.json').read_text())
    if not re.fullmatch(r'discord:[0-9]{10,22}', config.get('target', '')):
        raise ValueError('configure an operator-owned Discord delivery target')
    if not preview and config.get('delivery_enabled') is not True:
        raise ValueError('notification delivery requires explicit delivery_enabled configuration')
    state_path = root / 'state.json'
    state = json.loads(state_path.read_text())
    if not preview and now < datetime.fromisoformat(state['next_due_at']):
        print(json.dumps({'due': False, 'next_due_at': state['next_due_at']}))
        return
    if not preview:
        due = datetime.fromisoformat(state['next_due_at'])
        while due <= now:
            due += INTERVAL
        state['next_due_at'] = due.isoformat()
        save(state_path, state)
    try:
        candidate = release()
        installed = inspect_live()
    except Exception as error:
        if not preview:
            state['last_check'] = {'checked_at': now.isoformat(), 'failed': type(error).__name__, 'live_changed': False}
            save(state_path, state)
            send('The fortnightly Hermes release check failed. Nothing was changed. Ask Codex to inspect hermes-release-watch.service on the hub.', config['target'])
        raise RuntimeError('release check failed; no installation or automatic repair') from None
    message = notice(candidate, installed, state.get('notifications', {}))
    result = {'checked_at': now.isoformat(), 'installed': installed, 'latest': candidate,
              'new_release': message is not None, 'practice_tested': False, 'live_changed': False}
    if preview:
        print(json.dumps({**result, 'preview': True}))
        return
    state['last_check'] = result
    if message:
        # Persist intent before sending: uncertain delivery never triggers an automatic duplicate.
        state.setdefault('notifications', {})[str(candidate['id'])] = 'delivery-pending'
    save(state_path, state)
    if message:
        try:
            send(message, config['target'])
        except Exception:
            state['notifications'][str(candidate['id'])] = 'delivery-uncertain; manual inspection required'
            save(state_path, state)
            raise RuntimeError('release notice delivery failed or is uncertain; no automatic resend') from None
        state['notifications'][str(candidate['id'])] = 'sent'
        save(state_path, state)
    print(json.dumps({**result, 'next_due_at': state['next_due_at'], 'notice_sent': bool(message)}))

if __name__ == '__main__':
    import fcntl
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--preview', action='store_true', help='Read-only immediate check; no sends or state updates')
    args = parser.parse_args()
    with (args.root / 'check.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        run(args.root, args.preview)
