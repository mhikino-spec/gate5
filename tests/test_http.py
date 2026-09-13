import unittest, tempfile, threading, re, urllib.request, urllib.parse, urllib.error, http.cookiejar, sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app import Store,Web,ThreadingHTTPServer
class HttpTests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  cls.tmp=tempfile.TemporaryDirectory();cls.s=Store(Path(cls.tmp.name)/'http.db');cls.s.initialize();cls.s.seed()
  cls.handler=type('TestWeb',(Web,),{'store':cls.s,'sessions':{},'log_message':lambda *a:None})
  cls.server=ThreadingHTTPServer(('127.0.0.1',0),cls.handler);cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
  cls.base=f'http://127.0.0.1:{cls.server.server_port}'
 @classmethod
 def tearDownClass(cls):cls.server.shutdown();cls.server.server_close();cls.thread.join();cls.tmp.cleanup()
 def setUp(self):
  self.opener=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
 def req(self,path,data=None):
  try:r=self.opener.open(self.base+path,None if data is None else urllib.parse.urlencode(data).encode())
  except urllib.error.HTTPError as e:r=e
  with r:return r.status,r.read().decode(),r.geturl()
 def login(self):
  _,page,_=self.req('/');t=re.search('name="login_csrf" value="([^"]+)"',page)[1]
  _,page,_=self.req('/login',{'login_csrf':t,'user_id':'1001','password':'demo1234'})
  return re.search('name="csrf" value="([^"]+)"',page)[1]
 def test_unauthenticated_no_update(self):
  _,page,_=self.req('/commit',{'token':'fake'});self.assertIn('E-19',page)
 def test_get_cannot_mutate(self):
  code,page,_=self.req('/commit');self.assertEqual(code,405);self.assertIn('E-17',page)
 def test_csrf_rejection(self):
  self.login();code,page,_=self.req('/confirm',{'csrf':'fake','action':'loan'});self.assertEqual(code,403);self.assertIn('E-18',page)
 def test_unicode_csrf_rejected(self):
  self.login();code,page,_=self.req('/confirm',{'csrf':'不正','action':'loan'});self.assertEqual(code,403);self.assertIn('E-18',page)
 def test_tampered_confirmation(self):
  csrf=self.login();code,page,_=self.req('/commit',{'csrf':csrf,'token':'fake','device_id':'1'});self.assertEqual(code,422);self.assertIn('E-17',page)
 def test_http_normal_replay_html_escape(self):
  csrf=self.login();_,page,url=self.req('/confirm',{'csrf':csrf,'action':'loan','device_id':'1','user_id':'1002','due_date':self.s.clock().date().isoformat(),'purpose':'<script>alert(1)</script>'})
  self.assertIn('&lt;script&gt;',page);self.assertNotIn('<script>alert',page)
  token=urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)['token'][0]
  code,page,_=self.req('/commit',{'csrf':csrf,'token':token});self.assertEqual(code,200);self.assertIn('貸出完了',page)
  _,again,_=self.req('/commit',{'csrf':csrf,'token':token});self.assertIn('貸出完了',again)
  self.assertEqual(len(self.s.loans(1002)),1)
if __name__=='__main__':unittest.main(verbosity=2)
