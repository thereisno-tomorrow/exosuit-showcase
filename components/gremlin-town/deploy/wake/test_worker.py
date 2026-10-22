import hashlib
import hmac
import importlib.util
import json
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from gremlin_town.domain.policy import Participant, Policy

spec = importlib.util.spec_from_file_location("wake_worker",Path(__file__).with_name("worker.py"))
wake = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wake)
RID = "req_" + "a"*32


@pytest.fixture
def rig(tmp_path,monkeypatch):
    database=tmp_path/"hub.sqlite"
    db=sqlite3.connect(database)
    db.executescript("""
    PRAGMA user_version=2;
    CREATE TABLE requests(request_id TEXT,project TEXT,requester_id TEXT,recipient_id TEXT,
      sensitivity TEXT,created_at TEXT,revision INTEGER,mode TEXT,work_state TEXT,recovery_hold INTEGER);
    CREATE TABLE operations(request_id TEXT,operation_id TEXT,message_kind TEXT,operation_state TEXT,
      recovery_hold INTEGER,recovery_required INTEGER);
    CREATE TABLE message_links(operation_id TEXT,exact_link_verified INTEGER,trust_label TEXT);
    """)
    db.execute("INSERT INTO requests VALUES(?,?,?,?,?,?,1,'action_requested','requested',0)",
        (RID,'test','sender','hermes-wa','public-test','2026-09-21T16:00:00+00:00'))
    db.execute("INSERT INTO operations VALUES(?,'op_a','request','accepted',0,0)",(RID,))
    db.execute("INSERT INTO message_links VALUES('op_a',1,'authenticated_operation')")
    db.commit()
    policy=Policy([Participant(p,p,p+'@example.test',frozenset(['test']),frozenset(['public-test'])) for p in ['sender','hermes-wa']],frozenset([('test','sender','hermes-wa','public-test')]))
    monkeypatch.setattr(wake,'_load_participants',lambda _: (policy,()))
    hub=tmp_path/'hub.toml'
    hub.write_text('[server]\ndatabase='+json.dumps(str(database))+'\n[participants]\nfile="unused"\n')
    config=tmp_path/'wake.toml'
    config.write_text('enabled=true\nactivate_after="2026-09-21T15:00:00+00:00"\nhub_config='+json.dumps(str(hub))+'\nstate_database='+json.dumps(str(tmp_path/'wakes.sqlite'))+'\n[[target]]\nparticipant_id="hermes-wa"\nenabled=true\nkind="hermes"\nurl="http://127.0.0.1:8644/webhooks/gremlin-town-wake"\nsecret_file='+json.dumps(str(tmp_path/'secret'))+'\n')
    sent=[]
    worker=wake.Worker(config,sender=lambda *args: sent.append(args) or 'notified',clock=lambda:1000)
    yield worker,db,sent,policy,config,hub
    worker.state.close()
    db.close()


def test_delivers_id_only_and_survives_restart(rig):
    worker,db,sent,policy,config,hub=rig
    assert worker.tick()==1
    assert sent[0][1]=={'request_id':RID,'delivery_id':sent[0][2]}
    restarted=wake.Worker(config,sender=worker.sender,clock=lambda:2000)
    assert restarted.tick()==0
    restarted.state.close()
    assert len(sent)==1


@pytest.mark.parametrize('sql',[
 "UPDATE requests SET recovery_hold=1",
 "UPDATE requests SET work_state='cancelled'",
 "UPDATE requests SET work_state='acknowledged'",
 "UPDATE requests SET work_state='in_progress'",
 "UPDATE requests SET work_state='result_returned'",
 "UPDATE requests SET mode='context_only'",
 "UPDATE requests SET recipient_id='ciel'",
 "UPDATE requests SET created_at='2026-09-20T00:00:00+00:00'",
 "UPDATE operations SET operation_state='queued'",
 "UPDATE operations SET recovery_required=1",
 "UPDATE operations SET recovery_hold=1",
 "UPDATE message_links SET exact_link_verified=0",
 "UPDATE message_links SET trust_label='untrusted'",
])
def test_never_wakes_ineligible_work(rig,sql):
    worker,db,sent,*_=rig
    db.execute(sql); db.commit()
    assert worker.tick()==0
    assert sent==[]


def test_revocation_and_recovery_config(rig):
    worker,db,sent,policy,config,hub=rig
    policy.set_enabled('sender',False)
    assert worker.tick()==0
    policy.set_enabled('sender',True)
    with hub.open('a') as f: f.write('[recovery]\nnormal_clients_allowed=false\n')
    assert worker.tick()==0


def test_transport_retry_reuses_delivery_and_is_bounded(rig):
    worker,db,sent,*_=rig
    worker.sender=lambda *args: sent.append(args) or 'retry'
    for n in range(6):
        worker.clock=lambda n=n: 1000+n*1000
        worker.tick()
    assert len(sent)==5
    assert len({args[2] for args in sent})==1
    assert worker.state.execute('SELECT status FROM wakes').fetchone()[0]=='failed_attempts'


def test_cancelled_before_delivery_suppressed(rig):
    worker,db,sent,*_=rig
    original=worker.eligible
    n=0
    def eligible():
        nonlocal n
        n+=1
        if n==2:
            db.execute("UPDATE requests SET work_state='cancelled'"); db.commit()
        return original()
    worker.eligible=eligible
    assert worker.tick()==0
    assert sent==[]


def test_future_schema_stops(rig):
    worker,db,*_=rig
    db.execute('PRAGMA user_version=4')
    with pytest.raises(RuntimeError): worker.tick()


def test_real_http_signature_and_redirect_refusal(tmp_path):
    key='test-secret-with-32-characters-only'
    secret=tmp_path/'key'; secret.write_text(key)
    observed=[]
    class Handler(BaseHTTPRequestHandler):
        redirect=False
        def do_POST(self):
            body=self.rfile.read(int(self.headers['Content-Length']))
            observed.append((body,dict(self.headers)))
            if self.redirect:
                self.send_response(302); self.send_header('Location','http://127.0.0.1:1/secret'); self.end_headers(); return
            expected=hmac.new(key.encode(),b'1000.'+body,hashlib.sha256).hexdigest()
            assert self.headers['X-Webhook-Signature-V2']==expected
            assert self.headers['X-Request-ID']=='delivery-test'
            assert self.headers['X-GitHub-Event']=='gt.request.ready'
            self.send_response(202); self.end_headers(); self.wfile.write(b'{"status":"accepted"}')
        def log_message(self,*args): pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
    target={'kind':'hermes','url':f'http://127.0.0.1:{server.server_port}/webhooks/gremlin-town-wake','secret_file':str(secret)}
    try:
        assert wake.send(target,{'request_id':RID},'delivery-test',1000)=='notified'
        Handler.redirect=True
        assert wake.send(target,{'request_id':RID},'delivery-test',1000)=='failed_http_302'
        assert len(observed)==2
    finally:
        server.shutdown(); thread.join(); server.server_close()


@pytest.mark.parametrize('url',['http://example.com/webhooks/gremlin-town-wake','http://169.254.169.254/webhooks/gremlin-town-wake','http://0.0.0.0/webhooks/gremlin-town-wake','http://127.0.0.1/webhooks/gremlin-town-wake?secret=x','https://evil.example/path'])
def test_reject_unreviewed_hermes_destinations(tmp_path,url):
    with pytest.raises(ValueError): wake.validate_endpoint({'kind':'hermes','url':url,'secret_file':str(tmp_path/'secret')})


@pytest.mark.parametrize('change', [None, 'backend', 'hash', 'message', 'thread', 'actor'])
def test_schema_three_wake_requires_matching_durable_link(rig, change):
    worker, db, sent, _, _, _ = rig
    for statement in (
        "ALTER TABLE requests ADD COLUMN backend_binding TEXT DEFAULT 'github:test/repo'",
        "ALTER TABLE requests ADD COLUMN thread_id TEXT DEFAULT 'github:test/repo:issue:7'",
        "ALTER TABLE operations ADD COLUMN backend_binding TEXT DEFAULT 'github:test/repo'",
        "ALTER TABLE operations ADD COLUMN provider_message_id TEXT DEFAULT 'github:test/repo:issue:7'",
        "ALTER TABLE operations ADD COLUMN provider_thread_id TEXT DEFAULT 'github:test/repo:issue:7'",
        "ALTER TABLE operations ADD COLUMN payload_sha256 TEXT DEFAULT 'exact-hash'",
        "ALTER TABLE operations ADD COLUMN actor_id TEXT DEFAULT 'sender'",
        "ALTER TABLE message_links ADD COLUMN request_id TEXT",
        "ALTER TABLE message_links ADD COLUMN provider_message_id TEXT DEFAULT 'github:test/repo:issue:7'",
        "ALTER TABLE message_links ADD COLUMN provider_thread_id TEXT DEFAULT 'github:test/repo:issue:7'",
        "ALTER TABLE message_links ADD COLUMN content_sha256 TEXT DEFAULT 'exact-hash'",
        "ALTER TABLE message_links ADD COLUMN actor_id TEXT DEFAULT 'sender'",
    ):
        db.execute(statement)
    db.execute('UPDATE message_links SET request_id=?', (RID,))
    db.execute('PRAGMA user_version=3')
    changes = {
        'backend': "UPDATE operations SET backend_binding='agentmail'",
        'hash': "UPDATE message_links SET content_sha256='tampered'",
        'message': "UPDATE message_links SET provider_message_id='other'",
        'thread': "UPDATE requests SET thread_id='other'",
        'actor': "UPDATE message_links SET actor_id='other'",
    }
    if change:
        db.execute(changes[change])
    db.commit()
    assert worker.tick() == (0 if change else 1)
    assert len(sent) == (0 if change else 1)
