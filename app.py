"""Gate5 PC lending demo. Python 3.10+ / SQLite / standard library only."""
from __future__ import annotations
import argparse
import hashlib
import hmac
import html
import http.cookies
import json
import os
import secrets
import sqlite3
import threading
import time
import unicodedata
from contextlib import closing
from datetime import datetime, timedelta, timezone, date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

JST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parent
MESSAGES = {
 'E-01':'このIDは存在しません。',
 'E-02':'この端末は現在利用中です。別の端末を選択してください。',
 'E-03':'この端末は利用停止中です。別の端末を選択してください。',
 'E-04':'貸出できるPCがありません。',
 'E-05':'貸出対象のノートPCを選択してください。',
 'E-06':'社員情報が見つかりません。社員IDを確認してください。',
 'E-07':'この社員は貸出対象外のため、貸出できません。',
 'E-08':'貸出上限は1人1台です。貸出中のPCを返却してください。',
 'E-09':'端末または社員IDを正しく指定してください。',
 'E-10':'返却予定日を有効な日付で入力してください。',
 'E-12':'利用目的は空白のみを避け、使用できる文字で1～100文字で入力してください。',
 'E-13':'未返却の延滞PCがあります。返却後にお申し込みください。',
 'E-14':'返却対象の貸出がないか、すでに返却済みです。貸出状況を確認してください。',
 'E-15':'処理を完了できませんでした。時間をおいて再度お試しください。',
 'E-17':'確認情報が無効です。申請画面からやり直してください。',
 'E-18':'この操作を行う権限がありません。',
 'E-19':'ログインの有効期限が切れました。再度ログインしてください。',
 'E-20':'貸出情報を確認できません。情報システム部へお問い合わせください。',
}

class RuleError(Exception):
    def __init__(self, code, message=None):
        self.code = code
        self.message = message or MESSAGES[code]
        super().__init__(self.message)

def esc(value): return html.escape(str(value), quote=True)
def now(): return datetime.now(JST)
def password_hash(password, salt):
    return hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), 120000).hex()

SCHEMA = '''
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS employees(
 id INTEGER PRIMARY KEY, name TEXT NOT NULL, department TEXT NOT NULL,
 employment_status TEXT NOT NULL CHECK(employment_status IN ('ACTIVE','LEAVE','RETIRED')),
 role TEXT NOT NULL CHECK(role IN ('MEMBER','ADMIN')), salt TEXT NOT NULL, password_hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS devices(
 id INTEGER PRIMARY KEY, asset_no TEXT NOT NULL UNIQUE, model_name TEXT NOT NULL,
 device_type TEXT NOT NULL CHECK(device_type IN ('LAPTOP','TABLET','MONITOR')),
 status TEXT NOT NULL CHECK(status IN ('AVAILABLE','LENT','REPAIR','DISPOSED')),
 purchased_at TEXT);
CREATE TABLE IF NOT EXISTS lendings(
 id INTEGER PRIMARY KEY, device_id INTEGER NOT NULL REFERENCES devices(id),
 user_id INTEGER NOT NULL REFERENCES employees(id), lent_at TEXT NOT NULL,
 due_date TEXT NOT NULL, returned_at TEXT, purpose TEXT NOT NULL CHECK(length(purpose) BETWEEN 1 AND 100));
CREATE UNIQUE INDEX IF NOT EXISTS one_device_open ON lendings(device_id) WHERE returned_at IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS one_user_open ON lendings(user_id) WHERE returned_at IS NULL;
CREATE TABLE IF NOT EXISTS requests(
 token TEXT PRIMARY KEY, actor_id INTEGER NOT NULL REFERENCES employees(id),
 action TEXT NOT NULL CHECK(action IN ('loan','return')), payload TEXT NOT NULL,
 created_at TEXT NOT NULL, result TEXT);
CREATE TABLE IF NOT EXISTS audit_log(
 id INTEGER PRIMARY KEY, token TEXT NOT NULL UNIQUE REFERENCES requests(token),
 actor_id INTEGER NOT NULL REFERENCES employees(id), action TEXT NOT NULL,
 lending_id INTEGER NOT NULL REFERENCES lendings(id), occurred_at TEXT NOT NULL);
CREATE TRIGGER IF NOT EXISTS protect_lent_device BEFORE UPDATE OF status ON devices
 WHEN OLD.status='LENT' AND NEW.status!='LENT'
 AND EXISTS(SELECT 1 FROM lendings WHERE device_id=OLD.id AND returned_at IS NULL)
 BEGIN SELECT RAISE(ABORT, 'open lending protects device state'); END;
'''

class Store:
    def __init__(self, path, clock=now, failpoint=None):
        self.path = str(path)
        self.clock = clock
        self.failpoint = failpoint  # Test injection only; no HTTP switch.

    def connect(self):
        c = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute('PRAGMA foreign_keys=ON')
        return c

    def initialize(self):
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as c:
            c.execute('PRAGMA journal_mode=WAL')
            c.executescript(SCHEMA)

    def seed(self):
        """Insert fictional demo records only into an empty database; never reset existing data."""
        c = self.connect()
        try:
            c.execute('BEGIN IMMEDIATE')
            if c.execute('SELECT count(*) FROM employees').fetchone()[0]:
                c.rollback(); return False
            people = [(1001,'佐藤 花子','情報システム部','ACTIVE','ADMIN'),
                      (1002,'山田 太郎','営業部','ACTIVE','MEMBER'),
                      (1003,'鈴木 葵','企画部','ACTIVE','MEMBER'),
                      (1004,'田中 蓮','総務部','ACTIVE','MEMBER'),
                      (1005,'高橋 美咲','開発部','ACTIVE','MEMBER'),
                      (1006,'伊藤 直樹','営業部','LEAVE','MEMBER'),
                      (1007,'渡辺 光','企画部','RETIRED','MEMBER'),
                      (1008,'小林 翔','総務部','ACTIVE','MEMBER')]
            for person in people:
                salt = secrets.token_hex(16)
                c.execute('INSERT INTO employees VALUES(?,?,?,?,?,?,?)', (*person, salt, password_hash('demo1234',salt)))
            for i in range(1,21):
                status = {2:'LENT',3:'REPAIR',4:'DISPOSED'}.get(i,'AVAILABLE')
                c.execute('INSERT INTO devices VALUES(?,?,?,?,?,?)',
                          (i,f'PC-{i:04}', 'ThinkPad X13' if i%2 else 'Latitude 5340','LAPTOP',status,'2025-04-01'))
            c.execute('INSERT INTO devices VALUES(21,?,?,?,?,?)',('TB-0001','iPad','TABLET','AVAILABLE','2025-04-01'))
            d = self.clock().date()
            c.execute('INSERT INTO lendings(device_id,user_id,lent_at,due_date,purpose) VALUES(2,1008,?,?,?)',
                      ((self.clock()-timedelta(days=9)).isoformat(),(d-timedelta(days=2)).isoformat(),'延滞動作確認用の架空データ'))
            c.commit(); return True
        except Exception:
            c.rollback(); raise
        finally:c.close()

    def user(self, actor):
        with closing(self.connect()) as c:
            row = c.execute('SELECT id,name,department,role,employment_status FROM employees WHERE id=?',(actor,)).fetchone()
            return dict(row) if row else None

    def authenticate(self, user_id, password):
        try: user_id = self.identifier(user_id)
        except RuleError: return None
        with closing(self.connect()) as c:
            row=c.execute('SELECT * FROM employees WHERE id=?',(user_id,)).fetchone()
        if row and row['employment_status']=='ACTIVE' and hmac.compare_digest(row['password_hash'],password_hash(password,row['salt'])):
            return row['id']
        return None

    @staticmethod
    def identifier(value):
        value = str(value)
        if not value.isascii() or not value.isdigit() or len(value)>19 or not 0<int(value)<=9223372036854775807:
            raise RuleError('E-09')
        return int(value)

    def actor_check(self,c,actor):
        row=c.execute('SELECT * FROM employees WHERE id=?',(actor,)).fetchone()
        if not row or row['employment_status']!='ACTIVE': raise RuleError('E-18')
        return row

    def loan_payload(self,payload):
        device=self.identifier(payload.get('device_id',''))
        user=self.identifier(payload.get('user_id',''))
        raw=payload.get('due_date','').strip()
        try:
            due=date.fromisoformat(raw)
            if due.isoformat()!=raw:raise ValueError()
        except (ValueError,TypeError):raise RuleError('E-10')
        d=self.clock().date()
        if not d<=due<=d+timedelta(days=7):
            raise RuleError('E-11',f'返却予定日は{d:%Y/%m/%d}から{d+timedelta(days=7):%Y/%m/%d}までで入力してください。')
        purpose=payload.get('purpose','').strip(' \u3000\t\r\n')
        if not 1<=len(purpose)<=100 or any(unicodedata.category(ch)=='Cc' for ch in purpose):raise RuleError('E-12')
        return {'device_id':device,'user_id':user,'due_date':due.isoformat(),'purpose':purpose}

    def loan_check(self,c,p):
        device=c.execute('SELECT * FROM devices WHERE id=?',(p['device_id'],)).fetchone()
        if not device:raise RuleError('E-01')
        if device['device_type']!='LAPTOP':raise RuleError('E-05')
        if device['status']=='LENT':
            remaining=c.execute("SELECT count(*) FROM devices WHERE device_type='LAPTOP' AND status='AVAILABLE'").fetchone()[0]
            raise RuleError('E-02' if remaining else 'E-04')
        if device['status']!='AVAILABLE':raise RuleError('E-03')
        if c.execute('SELECT 1 FROM lendings WHERE device_id=? AND returned_at IS NULL',(device['id'],)).fetchone():raise RuleError('E-20')
        user=c.execute('SELECT * FROM employees WHERE id=?',(p['user_id'],)).fetchone()
        if not user:raise RuleError('E-06')
        if user['employment_status']!='ACTIVE':raise RuleError('E-07')
        loans=c.execute('SELECT due_date FROM lendings WHERE user_id=? AND returned_at IS NULL',(user['id'],)).fetchall()
        if any(row['due_date']<self.clock().date().isoformat() for row in loans):raise RuleError('E-13')
        if loans:raise RuleError('E-08')
        return device,user

    def return_check(self,c,actor,p):
        user=self.actor_check(c,actor)
        loan=c.execute('SELECT l.*,d.asset_no,d.status FROM lendings l JOIN devices d ON d.id=l.device_id WHERE l.id=?',(p['lending_id'],)).fetchone()
        if not loan or loan['returned_at'] is not None:raise RuleError('E-14')
        if user['role']!='ADMIN' and loan['user_id']!=actor:raise RuleError('E-18')
        if loan['status']!='LENT':raise RuleError('E-20')
        return loan

    def prepare(self,actor,action,payload):
        if action not in ('loan','return'):raise RuleError('E-17')
        p=self.loan_payload(payload) if action=='loan' else {'lending_id':self.identifier(payload.get('lending_id',''))}
        c=self.connect()
        try:
            c.execute('BEGIN IMMEDIATE');self.actor_check(c,actor)
            if action=='loan':self.loan_check(c,p)
            else:self.return_check(c,actor,p)
            token=secrets.token_urlsafe(32)
            c.execute('INSERT INTO requests VALUES(?,?,?,?,?,NULL)',(token,actor,action,json.dumps(p,ensure_ascii=False),self.clock().isoformat()))
            c.commit();return token
        except Exception:c.rollback();raise
        finally:c.close()

    def intent(self,actor,token):
        with closing(self.connect()) as c:
            r=c.execute('SELECT * FROM requests WHERE token=? AND actor_id=?',(token,actor)).fetchone()
            if not r:raise RuleError('E-17')
            return dict(r)

    def execute(self,actor,token):
        c=self.connect()
        try:
            # Serializes the entire read-check-write operation across devices AND borrowers.
            c.execute('BEGIN IMMEDIATE');self.actor_check(c,actor)
            r=c.execute('SELECT * FROM requests WHERE token=? AND actor_id=?',(token,actor)).fetchone()
            if not r:raise RuleError('E-17')
            if r['result']:
                result=json.loads(r['result']);c.commit();return result
            if self.clock()-datetime.fromisoformat(r['created_at'])>timedelta(minutes=30):raise RuleError('E-17')
            p=json.loads(r['payload']);timestamp=self.clock().isoformat()
            if r['action']=='loan':
                p=self.loan_payload(p)  # Revalidate date at commit, including midnight changes.
                device,user=self.loan_check(c,p)
                cur=c.execute('INSERT INTO lendings(device_id,user_id,lent_at,due_date,purpose) VALUES(?,?,?,?,?)',
                              (p['device_id'],p['user_id'],timestamp,p['due_date'],p['purpose']))
                lending_id=cur.lastrowid
                if self.failpoint:self.failpoint('loan_after_insert')
                c.execute("UPDATE devices SET status='LENT' WHERE id=?",(p['device_id'],))
                result={'action':'loan','lending_id':lending_id,'asset_no':device['asset_no'], 'name':user['name'], 'due_date':p['due_date']}
            else:
                loan=self.return_check(c,actor,p);lending_id=loan['id']
                c.execute('UPDATE lendings SET returned_at=? WHERE id=? AND returned_at IS NULL',(timestamp,lending_id))
                if self.failpoint:self.failpoint('return_after_update')
                c.execute("UPDATE devices SET status='AVAILABLE' WHERE id=?",(loan['device_id'],))
                result={'action':'return','lending_id':lending_id,'asset_no':loan['asset_no'],'returned_at':timestamp}
            c.execute('INSERT INTO audit_log(token,actor_id,action,lending_id,occurred_at) VALUES(?,?,?,?,?)',(token,actor,r['action'],lending_id,timestamp))
            if self.failpoint:self.failpoint('after_audit')
            c.execute('UPDATE requests SET result=? WHERE token=?',(json.dumps(result,ensure_ascii=False),token))
            c.commit();return result
        except RuleError:c.rollback();raise
        except sqlite3.Error:c.rollback();raise RuleError('E-15')
        except Exception:c.rollback();raise
        finally:c.close()

    def devices(self):
        with closing(self.connect()) as c:return [dict(r) for r in c.execute("SELECT * FROM devices WHERE device_type='LAPTOP' ORDER BY asset_no")]

    def loans(self,actor):
        with closing(self.connect()) as c:
            user=self.actor_check(c,actor)
            rows=c.execute('SELECT l.*,d.asset_no,e.name FROM lendings l JOIN devices d ON d.id=l.device_id JOIN employees e ON e.id=l.user_id WHERE returned_at IS NULL AND (? OR user_id=?) ORDER BY due_date,l.id',(user['role']=='ADMIN',actor))
            return [dict(r) for r in rows]

CSS='''
:root{font-family:"Yu Gothic",Meiryo,sans-serif;color:#172b41;background:#f3f6fa;font-size:16px}*{box-sizing:border-box}body{margin:0}header{background:#102b47;color:white;padding:18px max(24px,calc((100vw - 1120px)/2));display:flex;align-items:center;justify-content:space-between;gap:20px}header strong{font-size:20px}header small{color:#bed0e1}nav{display:flex;align-items:center;gap:22px}nav a{color:#fff;text-decoration:none}main{max-width:1120px;margin:30px auto;padding:0 22px}h1{font-size:28px;margin:8px 0}h2{font-size:19px;margin:0 0 16px}.subtitle{color:#53677d;margin:8px 0 25px}.grid{display:grid;grid-template-columns:1fr 1fr;gap:24px}.panel{background:white;border:1px solid #dbe3eb;border-radius:10px;padding:24px;margin-bottom:22px}.summary{display:flex;gap:16px;margin:24px 0}.metric{background:#fff;border:1px solid #dbe3eb;padding:14px 22px;border-radius:8px;flex:1}.metric strong{display:block;font-size:27px;color:#164f80}.metric span{font-size:14px;color:#53677d}label{display:block;font-size:15px;font-weight:bold;margin:0 0 8px}input,select,textarea{width:100%;padding:11px;border:1px solid #afbecd;border-radius:5px;font:inherit;background:white;color:#172b41}textarea{height:90px;resize:vertical}.field{margin:0 0 18px}.hint{font-size:13px;color:#53677d;line-height:1.6}.button,button{background:#135f9e;border:1px solid #135f9e;border-radius:5px;color:white;padding:11px 20px;font:inherit;font-weight:bold;cursor:pointer;display:inline-block;text-decoration:none}.secondary{background:white;color:#164f80}.compact{padding:7px 12px;font-size:14px}button:disabled{opacity:.5;cursor:not-allowed}.actions{display:flex;justify-content:flex-end;gap:12px;margin-top:20px}.alert{padding:16px 20px;border-radius:7px;margin:20px 0;border:1px solid #eca8a4;background:#fff1f0;color:#9b201d;line-height:1.65}.alert strong{display:block;margin-bottom:3px}.success{background:#edf8f1;border-color:#8fc6a4;color:#165d35}.badge{padding:3px 8px;border-radius:5px;font-size:13px;white-space:nowrap;background:#e5f4ec;color:#1b6846}.lent{background:#e9f0fb;color:#215b8e}.stop{background:#edf0f3;color:#5e6871}.overdue{color:#ac2525;font-weight:bold}table{width:100%;border-collapse:collapse;font-size:14px}th{text-align:left;color:#526b81;font-size:13px;background:#f5f8fb}td,th{padding:12px 10px;border-bottom:1px solid #e1e7ed}dl{display:grid;grid-template-columns:130px 1fr;margin:0;gap:0}dt,dd{margin:0;padding:16px 6px;border-bottom:1px solid #e1e7ed}dt{color:#53677d}dd{overflow-wrap:anywhere}.scroll{max-height:475px;overflow:auto}.topline{display:flex;justify-content:space-between;gap:12px}.login{max-width:480px;margin:55px auto}.inline{display:inline}.inline button{background:transparent;color:white;border-color:#647c95}a{color:#145d95}.empty{padding:25px;color:#53677d}.result{max-width:800px;margin:30px auto}footer{color:#6b7e91;text-align:center;font-size:12px;margin:28px}.step{font-size:14px;color:#57718a;letter-spacing:.03em;margin-bottom:12px}@media(max-width:760px){.grid{grid-template-columns:1fr}header{padding:16px;flex-wrap:wrap}main{padding:0 14px}.summary{gap:8px}.metric{padding:12px}table{font-size:13px}nav{gap:12px}dl{grid-template-columns:100px 1fr}}
'''

class Web(BaseHTTPRequestHandler):
    store: Store
    sessions = {}
    session_lock=threading.Lock()
    def log_message(self,fmt,*args):
        # Never log form values, credentials or session cookies.
        print(f'{self.command} {urlsplit(self.path).path} {args[1] if len(args)>1 else ""}',flush=True)

    def session(self):
        cookie=http.cookies.SimpleCookie()
        try:cookie.load(self.headers.get('Cookie',''))
        except http.cookies.CookieError:return None
        sid=cookie.get('gate5')
        if not sid:return None
        with self.session_lock:
            s=self.sessions.get(sid.value)
            if not s or time.time()-s['time']>1800:
                self.sessions.pop(sid.value,None);return None
            return s

    def output(self,body,status=200,cookie=None):
        data=body.encode('utf-8');self.send_response(status)
        self.send_header('Content-Type','text/html; charset=utf-8')
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('X-Frame-Options','DENY')
        self.send_header('Content-Security-Policy',"default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        self.send_header('Content-Length',str(len(data)))
        if cookie:self.send_header('Set-Cookie',cookie)
        self.end_headers();self.wfile.write(data)

    def page(self,content,session=None):
        nav=''
        if session:
            user=self.store.user(session['actor']) or {'name':'利用者'}
            nav=f'<nav><a href="/">貸出申請</a><a href="/returns">返却</a><small>{esc(user["name"])}</small><form class="inline" action="/logout" method="post">{self.hidden("csrf",session["csrf"])}<button class="compact">ログアウト</button></form></nav>'
        return f'<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>共用PC貸出 | Gate5</title><style>{CSS}</style></head><body><header><strong>IT資産管理 <small> / 共用PC</small></strong>{nav}</header><main>{content}</main><footer>Gate5 ローカル動作確認版 · 架空のデモデータ</footer><script>document.querySelectorAll("form[data-once]").forEach(f=>f.addEventListener("submit",()=>{{f.querySelector("button[type=submit]").disabled=true;}}));</script></body></html>'

    @staticmethod
    def hidden(key,value):return f'<input type="hidden" name="{esc(key)}" value="{esc(value)}">'
    @staticmethod
    def alert(error):return f'<div class="alert" role="alert"><strong>{esc(error.code)} · 処理を進められません</strong>{esc(error.message)}</div>' if error else ''

    def login(self,error=None):
        pre=secrets.token_urlsafe(24)
        content=f'<section class="panel login"><h1>ログイン</h1><p class="subtitle">社員IDとパスワードを入力してください。</p>{self.alert(error)}<form method="post" action="/login">{self.hidden("login_csrf",pre)}<div class="field"><label for="uid">社員ID</label><input id="uid" name="user_id" autocomplete="username" required></div><div class="field"><label for="pw">パスワード</label><input id="pw" name="password" type="password" autocomplete="current-password" required></div><div class="actions"><button>ログイン</button></div></form><p class="hint">動作確認用：1001 / demo1234（管理者）<br>1002 / demo1234（一般社員）</p></section>'
        self.output(self.page(content),cookie=f'gate5_login={pre}; HttpOnly; SameSite=Strict; Path=/')

    def loan_page(self,s,error=None,values=None):
        v=values or {};devices=self.store.devices();available=sum(x['status']=='AVAILABLE' for x in devices)
        if not error and not available:error=RuleError('E-04')
        opts='<option value="">選択してください</option>';rows=''
        for d in devices:
            label={'AVAILABLE':'貸出可能','LENT':'利用中','REPAIR':'利用停止','DISPOSED':'利用停止'}[d['status']]
            cls={'AVAILABLE':'','LENT':'lent','REPAIR':'stop','DISPOSED':'stop'}[d['status']]
            opts+=f'<option value="{d["id"]}" {"disabled" if d["status"]!="AVAILABLE" else ""} {"selected" if str(v.get("device_id"))==str(d["id"]) else ""}>{esc(d["asset_no"])} / {esc(d["model_name"])} · {label}</option>'
            rows+=f'<tr><td><strong>{esc(d["asset_no"])}</strong></td><td>{esc(d["model_name"])}</td><td><span class="badge {cls}">{label}</span></td></tr>'
        content=f'<div class="topline"><div><h1>共用PCを借りる</h1><p class="subtitle">空き状況を確認して、貸出内容を入力してください。</p></div></div><div class="summary"><div class="metric"><span>貸出可能</span><strong>{available} 台</strong></div><div class="metric"><span>利用中</span><strong>{sum(x["status"]=="LENT" for x in devices)} 台</strong></div><div class="metric"><span>利用停止</span><strong>{sum(x["status"] in ("REPAIR","DISPOSED") for x in devices)} 台</strong></div></div>{self.alert(error)}<div class="grid"><section class="panel"><div class="step">01 入力　→　02 確認　→　03 完了</div><h2>貸出申請</h2><form method="post" action="/confirm">{self.hidden("csrf",s["csrf"])}{self.hidden("action","loan")}<div class="field"><label for="device">PC</label><select id="device" name="device_id" required>{opts}</select></div><div class="field"><label for="borrower">借用者の社員ID</label><input id="borrower" name="user_id" value="{esc(v.get("user_id",s["actor"]))}" required><p class="hint">代理申請では、PCを利用する方の社員IDを入力してください。</p></div><div class="field"><label for="due">返却予定日</label><input id="due" name="due_date" type="date" value="{esc(v.get("due_date",(self.store.clock().date()+timedelta(days=7)).isoformat()))}" required><p class="hint">当日から7日後まで。当日返却も可能です。</p></div><div class="field"><label for="purpose">利用目的</label><textarea id="purpose" name="purpose" required>{esc(v.get("purpose",""))}</textarea><p class="hint">1〜100文字で入力してください。</p></div><div class="actions"><button {"disabled" if not available else ""}>貸出する</button></div></form></section><section class="panel"><h2>PCの現在の状態</h2><div class="scroll"><table><thead><tr><th>資産番号</th><th>機種</th><th>状態</th></tr></thead><tbody>{rows}</tbody></table></div></section></div>'
        self.output(self.page(content,s),422 if error else 200)

    def confirm_page(self,s,token):
        r=self.store.intent(s['actor'],token);p=json.loads(r['payload'])
        if r['result']:return self.result_page(s,token)
        if r['action']=='loan':
            device=next((x for x in self.store.devices() if x['id']==p['device_id']),None);u=self.store.user(p['user_id'])
            if not device:raise RuleError('E-01')
            if not u:raise RuleError('E-06')
            pairs=[('PC',device['asset_no']+' / '+device['model_name']),('借用者',str(u['id'])+' '+u['name']),('返却予定日',p['due_date']),('利用目的',p['purpose'])]
        else:
            with closing(self.store.connect()) as c:
                loan=c.execute('SELECT l.*,d.asset_no FROM lendings l JOIN devices d ON d.id=l.device_id WHERE l.id=?',(p['lending_id'],)).fetchone()
            pairs=[('返却対象',loan['asset_no']),('貸出ID',loan['id']),('返却予定日',loan['due_date'])]
        details=''.join(f'<dt>{esc(k)}</dt><dd>{esc(v)}</dd>' for k,v in pairs)
        back='/edit?token='+token if r['action']=='loan' else '/returns'
        self.output(self.page(f'<section class="panel result"><div class="step">01 入力　→　02 確認　→　03 完了</div><h1>{"貸出" if r["action"]=="loan" else "返却"}内容の確認</h1><p class="subtitle">内容を確認して「確定」を押してください。</p><dl>{details}</dl><form action="/commit" method="post" data-once>{self.hidden("csrf",s["csrf"])}{self.hidden("token",token)}<div class="actions"><a class="button secondary" href="{esc(back)}">戻る</a><button type="submit">確定</button></div></form></section>',s))

    def result_page(self,s,token):
        r=self.store.intent(s['actor'],token)
        if not r['result']:raise RuleError('E-17')
        result=json.loads(r['result']);loan=result['action']=='loan'
        message=f'{result["asset_no"]} を貸し出しました。返却予定日は {result["due_date"].replace("-","/")} です。' if loan else f'{result["asset_no"]} を返却しました。'
        info=f'<dt>借用者</dt><dd>{esc(result["name"])}</dd><dt>返却予定日</dt><dd>{esc(result["due_date"])}</dd>' if loan else f'<dt>返却日時</dt><dd>{esc(result["returned_at"])}</dd>'
        self.output(self.page(f'<section class="panel result"><div class="step">03 完了</div><h1>{"貸出" if loan else "返却"}完了</h1><div class="alert success" role="status"><strong>手続きが完了しました</strong>{esc(message)}</div><dl><dt>資産番号</dt><dd>{esc(result["asset_no"])}</dd><dt>貸出ID</dt><dd>{result["lending_id"]}</dd>{info}</dl><div class="actions"><a class="button secondary" href="/">貸出申請へ</a><a class="button" href="/returns">返却画面へ</a></div></section>',s))

    def returns_page(self,s,error=None):
        rows=''
        for l in self.store.loans(s['actor']):
            overdue=l['due_date']<self.store.clock().date().isoformat()
            rows+=f'<tr><td>{l["id"]}</td><td><strong>{esc(l["asset_no"])}</strong></td><td>{esc(l["name"])}</td><td>{esc(l["due_date"])} {"<span class=overdue>延滞</span>" if overdue else ""}</td><td><form action="/confirm" method="post">{self.hidden("csrf",s["csrf"])}{self.hidden("action","return")}{self.hidden("lending_id",l["id"])}<button class="compact">返却する</button></form></td></tr>'
        body=f'<table><thead><tr><th>貸出ID</th><th>PC</th><th>借用者</th><th>返却予定日</th><th>操作</th></tr></thead><tbody>{rows}</tbody></table>' if rows else '<p class="empty">返却対象のPCはありません。</p>'
        self.output(self.page(f'<h1>PCを返却する</h1><p class="subtitle">未返却のPCを選択してください。</p>{self.alert(error)}<section class="panel">{body}</section>',s),422 if error else 200)

    def redirect(self,url,cookie=None):
        self.send_response(303);self.send_header('Location',url);self.send_header('Cache-Control','no-store')
        if cookie:self.send_header('Set-Cookie',cookie)
        self.end_headers()

    def do_GET(self):
        s=self.session();path=urlsplit(self.path).path;query=parse_qs(urlsplit(self.path).query)
        if path in ('/commit','/confirm','/logout'):
            return self.output(self.page(self.alert(RuleError('E-17')),s),405)
        if not s or path=='/login':return self.login()
        try:
            if path=='/':return self.loan_page(s)
            if path=='/returns':return self.returns_page(s)
            token=query.get('token',[''])[0]
            if path=='/review':return self.confirm_page(s,token)
            if path=='/result':return self.result_page(s,token)
            if path=='/edit':
                r=self.store.intent(s['actor'],token)
                return self.loan_page(s,values=json.loads(r['payload']))
            self.output(self.page('<h1>ページが見つかりません</h1>',s),404)
        except RuleError as e:self.output(self.page(self.alert(e)+'<a href="/">申請画面へ</a>',s),422)
        except sqlite3.Error:self.output(self.page(self.alert(RuleError('E-15')),s),503)

    def do_POST(self):
        if self.headers.get('Host') not in (f'127.0.0.1:{self.server.server_port}',f'localhost:{self.server.server_port}'):
            return self.output(self.page(self.alert(RuleError('E-18'))),403)
        try:
            length=int(self.headers.get('Content-Length','0'))
            if length<0 or length>16384:raise ValueError()
            raw=parse_qs(self.rfile.read(length).decode('utf-8'),keep_blank_values=True,strict_parsing=True)
            if any(len(v)!=1 for v in raw.values()):raise ValueError()
            form={k:v[0] for k,v in raw.items()}
        except (ValueError,UnicodeDecodeError):return self.output(self.page(self.alert(RuleError('E-17'))),400)
        path=urlsplit(self.path).path
        if path=='/login':
            cookie=http.cookies.SimpleCookie()
            try:cookie.load(self.headers.get('Cookie',''))
            except http.cookies.CookieError:return self.login(RuleError('E-17'))
            pre=cookie.get('gate5_login')
            if not pre or not hmac.compare_digest(pre.value.encode(),form.get('login_csrf','').encode()):return self.login(RuleError('E-17'))
            actor=self.store.authenticate(form.get('user_id',''),form.get('password',''))
            if not actor:return self.login(RuleError('E-18','社員IDまたはパスワードが正しくないか、利用対象外です。'))
            sid=secrets.token_urlsafe(32)
            with self.session_lock:self.sessions[sid]={'actor':actor,'csrf':secrets.token_urlsafe(32),'time':time.time()}
            return self.redirect('/',f'gate5={sid}; HttpOnly; SameSite=Strict; Path=/')
        s=self.session()
        if not s:return self.login(RuleError('E-19'))
        if not hmac.compare_digest(s['csrf'].encode(),form.get('csrf','').encode()):return self.output(self.page(self.alert(RuleError('E-18')),s),403)
        try:
            if path=='/logout':
                with self.session_lock:
                    for key,value in list(self.sessions.items()):
                        if value is s:del self.sessions[key]
                return self.redirect('/login','gate5=; Max-Age=0; HttpOnly; SameSite=Strict; Path=/')
            if path=='/confirm':
                token=self.store.prepare(s['actor'],form.get('action',''),form)
                return self.redirect('/review?token='+token)
            if path=='/commit':
                if set(form)!=set(('token','csrf')):raise RuleError('E-17')
                token=form.get('token','');self.store.execute(s['actor'],token)
                return self.redirect('/result?token='+token)
            raise RuleError('E-17')
        except RuleError as e:
            if e.code=='E-18':return self.output(self.page(self.alert(e)+'<a href="/login">ログインへ</a>',s),403)
            if path=='/confirm' and form.get('action')=='return':return self.returns_page(s,e)
            values=form
            if path=='/commit':
                try:
                    r=self.store.intent(s['actor'],form.get('token',''))
                    if r['action']=='return':return self.returns_page(s,e)
                    values=json.loads(r['payload'])
                except RuleError:values={}
            return self.loan_page(s,e,values)
        except sqlite3.Error:return self.output(self.page(self.alert(RuleError('E-15')),s),503)

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db',default=str(ROOT/'data/gate5.sqlite3'))
    parser.add_argument('--port',type=int,default=8765)
    parser.add_argument('--seed',action='store_true',help='空DBに架空のデモデータを作成。既存データは変更しない')
    args=parser.parse_args();store=Store(args.db);store.initialize()
    if args.seed:print('Demo seed:',store.seed(),flush=True)
    Web.store=store;server=ThreadingHTTPServer(('127.0.0.1',args.port),Web)
    print(f'Local URL: http://127.0.0.1:{args.port}',flush=True)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close()

if __name__=='__main__':main()
