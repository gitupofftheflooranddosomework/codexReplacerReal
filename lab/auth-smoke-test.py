#!/usr/bin/env python3
import http.client, importlib.util, os, re, secrets, tempfile, threading, urllib.parse
from pathlib import Path

def response_body(resp):
    return resp.status, dict(resp.getheaders()), resp.read().decode('utf-8','replace')

def post_form(conn, path, values, cookie=None):
    body=urllib.parse.urlencode(values)
    headers={'Content-Type':'application/x-www-form-urlencoded','Content-Length':str(len(body))}
    if cookie: headers['Cookie']=cookie
    conn.request('POST',path,body=body,headers=headers)
    return response_body(conn.getresponse())

def main():
    with tempfile.TemporaryDirectory(prefix='codex-auth-smoke-') as tmp:
        os.environ['CODEX_LAB_DASHBOARD_AUTH']=str(Path(tmp)/'auth.json')
        os.environ['CODEX_LAB_SCHEDULER_DB']=str(Path(tmp)/'jobs.sqlite3')
        spec=importlib.util.spec_from_file_location('sched',Path(__file__).with_name('scheduler.py'))
        sched=importlib.util.module_from_spec(spec); spec.loader.exec_module(sched)
        first=secrets.token_urlsafe(18); second=secrets.token_urlsafe(18)
        sched.initialize_dashboard_auth('mark',first)
        server=sched.ThreadingHTTPServer(('127.0.0.1',0),sched.Handler); server.daemon_threads=True
        thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        conn=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=8)
        try:
            conn.request('GET','/'); status,headers,_=response_body(conn.getresponse()); assert status==302 and headers['Location'].startswith('/login?next=')
            conn.request('GET','/login'); status,headers,body=response_body(conn.getresponse()); assert status==200 and 'Codex KVM Lab' in body and 'type="password"' in body
            status,_,body=post_form(conn,'/login',{'username':'mark','password':'not-it','next':'/'}); assert status==401 and 'Incorrect username or password' in body
            status,headers,_=post_form(conn,'/login',{'username':'mark','password':first,'next':'/'}); assert status==303
            set_cookie=headers['Set-Cookie']; assert all(x in set_cookie for x in ('Secure','HttpOnly','SameSite=Strict','Path=/'))
            cookie=set_cookie.split(';',1)[0]
            conn.request('GET','/auth/check',headers={'Cookie':cookie}); status,_,_=response_body(conn.getresponse()); assert status==204
            conn.request('GET','/change-password',headers={'Cookie':cookie}); status,_,body=response_body(conn.getresponse()); assert status==200
            csrf=re.search(r'name="csrf" value="([^"]+)"',body).group(1)
            status,headers,_=post_form(conn,'/change-password',{'csrf':csrf,'current_password':first,'new_password':second,'confirm_password':second},cookie); assert status==303
            new_cookie=headers['Set-Cookie'].split(';',1)[0]
            conn.request('GET','/auth/check',headers={'Cookie':cookie}); status,_,_=response_body(conn.getresponse()); assert status==302
            conn.request('GET','/auth/check',headers={'Cookie':new_cookie}); status,_,_=response_body(conn.getresponse()); assert status==204
            print('{"ok":true,"login":true,"secureCookie":true,"passwordChange":true,"sessionRotation":true}')
        finally:
            conn.close(); server.shutdown(); server.server_close(); thread.join(timeout=2)
if __name__=='__main__': main()
