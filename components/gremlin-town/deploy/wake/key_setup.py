"""Temporary loopback-only form. Secrets stay in RAM en route to protected VPS config."""
import hmac
import argparse
import json
import secrets
import subprocess
import time
import threading
from http.server import ThreadingHTTPServer as HTTPServer,BaseHTTPRequestHandler
from urllib.parse import parse_qs
from install_grok_sender import validate

PORT=0
ORIGIN=f'http://127.0.0.1:{PORT}'
csrf=secrets.token_urlsafe(32)
cookie=secrets.token_urlsafe(32)
saved=False
save_lock=threading.Lock()

class Handler(BaseHTTPRequestHandler):
    timeout=10
    def log_message(self,*args):pass
    def respond(self,status,body):
        self.send_response(status)
        self.send_header('Content-Type','text/html; charset=utf-8')
        self.send_header('Cache-Control','no-store')
        # no-referrer makes Chromium send Origin: null for ordinary form POSTs.
        self.send_header('Referrer-Policy','same-origin')
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Content-Security-Policy',"default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'")
        self.send_header('Set-Cookie',f'gt_setup={cookie}; HttpOnly; SameSite=Strict; Path=/')
        self.end_headers()
        self.wfile.write(('<!doctype html><meta charset=utf-8><title>Connect Fleet Boss to GT</title><style>body{font:18px system-ui;max-width:680px;margin:60px auto;padding:24px;background:#f4f6f7;color:#132123}label{display:block;margin-top:22px}input{box-sizing:border-box;width:100%;padding:12px;font:16px system-ui;margin-top:8px}button{background:#125749;color:white;padding:14px 22px;border:0;border-radius:8px;font:18px system-ui;margin-top:26px}p{line-height:1.6}</style>'+body).encode())
    def do_GET(self):
        if self.headers.get('Host')!=f'127.0.0.1:{PORT}' or self.path not in ('/','/save'):
            self.respond(404,'Not found');return
        if saved:
            self.respond(200,'<h1>Saved</h1><p>Fleet Boss is connected to the GT wake worker. Codex will check the first delivery. You can close this tab.</p>');return
        self.respond(200,f'''<h1>Connect Fleet Boss to GT</h1>
        <p>From <strong>Gremlin Town pickup</strong>, copy the two values below. No code or terminal needed.</p>
        <p>This setup page stays available for four hours after it is opened by Codex.</p>
        <form method="post" action="/save" autocomplete="off">
        <input type="hidden" name="csrf" value="{csrf}">
        <label>POST to<input name="url" type="url" required placeholder="Paste the complete POST to URL"></label>
        <label>key<input name="key" type="password" required autocomplete="off" placeholder="Paste the key only"></label>
        <button type="submit">Save and connect</button></form>
        <p>You can ignore <strong>header</strong>. This local form sends the values over your private SSH connection to protected GT-server configuration. The key is not saved on this PC or printed in chat.</p>''')
    def do_POST(self):
        # Browser preconnections must not block other requests; serialize only saves.
        with save_lock:
            self.save_values()
    def save_values(self):
        global saved
        # Cookies are shared across loopback ports; unrelated cookies are valid.
        # Require exactly one matching setup cookie, rejecting ambiguous duplicates.
        setup_cookies=[part.strip().partition('=')[2]
                       for part in self.headers.get('Cookie','').split(';')
                       if part.strip().partition('=')[0]=='gt_setup']
        if (self.path!='/save' or self.headers.get('Host')!=f'127.0.0.1:{PORT}'
            or self.headers.get('Origin')!=ORIGIN
            or len(setup_cookies)!=1 or not hmac.compare_digest(setup_cookies[0],cookie)):
            self.respond(403,'<h1>Open the setup page again.</h1><p><a href="/">Return to the setup form</a>, then paste both values and try again.</p>');return
        if saved:
            self.respond(200,'<h1>Already saved</h1><p>You can close this tab.</p>');return
        try:
            length=int(self.headers.get('Content-Length','0'))
            if not 0<length<=8192:raise ValueError('size')
            data=parse_qs(self.rfile.read(length).decode(),max_num_fields=3,strict_parsing=True)
            if not hmac.compare_digest(data['csrf'][0],csrf):raise ValueError('csrf')
            url,key=validate({'url':data['url'][0],'key':data['key'][0]})
        except Exception:
            self.respond(400,'<h1>Check the copied values</h1><p>Copy the complete POST to URL and the key value only—not the header. <a href="/">Try again</a>.</p>');return
        try:
            result=subprocess.run(['ssh','-o','ConnectTimeout=10','-o','BatchMode=yes','operator@hub.example.invalid','python3 /opt/gremlin-town-wake/install_grok_sender.py'],input=json.dumps({'url':url,'key':key}),text=True,capture_output=True,timeout=45)
            receipt=json.loads(result.stdout)
            if result.returncode or receipt.get('saved') is not True:raise RuntimeError('install')
        except Exception:
            self.respond(502,'<h1>Connection was not confirmed</h1><p>The values are not shown or logged. Tell Codex this message so it can check the server before retrying.</p>');return
        saved=True
        print('FLEET_BOSS_SENDER_SAVED',flush=True)
        self.respond(200,'<h1>Saved</h1><p>Fleet Boss is connected to the GT wake worker. Codex will check the first delivery. You can close this tab.</p>')

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--port',type=int,default=0)
    PORT=parser.parse_args().port
    server=HTTPServer(('127.0.0.1',PORT),Handler)
    PORT=server.server_address[1]
    ORIGIN=f'http://127.0.0.1:{PORT}'
    server.timeout=1
    print('SETUP_READY '+ORIGIN,flush=True)
    deadline=time.monotonic()+14400
    try:
        while time.monotonic()<deadline:
            server.handle_request()
            if saved:
                # Keep the success page available briefly for browser reloads.
                deadline=min(deadline,time.monotonic()+600)
    finally:
        server.server_close()
        print('SETUP_CLOSED',flush=True)
