import sys, unittest, tempfile, threading, sqlite3, json
from pathlib import Path
from datetime import datetime,timedelta
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app import Store, RuleError, JST, esc

class LendingTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.time=datetime(2026,9,12,12,tzinfo=JST)
  self.s=Store(Path(self.tmp.name)/'test.db',clock=lambda:self.time);self.s.initialize();self.s.seed()
 def p(self,device=1,user=1002,**kw):return dict(device_id=str(device),user_id=str(user),due_date=self.time.date().isoformat(),purpose='業務用',**kw)
 def prepare(self,device=1,user=1002):return self.s.prepare(1001,'loan',self.p(device,user))
 def loan(self,device=1,user=1002):return self.s.execute(1001,self.prepare(device,user))
 def sql(self,q,args=()):
  c=self.s.connect()
  try:return [tuple(r) for r in c.execute(q,args)]
  finally:c.close()
 def error(self,code,fn):
  with self.assertRaises(RuleError) as r:fn()
  self.assertEqual(r.exception.code,code)
 def race(self,tokens):
  barrier=threading.Barrier(len(tokens))
  def run(t):
   barrier.wait()
   try:return self.s.execute(1001,t)
   except RuleError as e:return e.code
  with ThreadPoolExecutor(max_workers=len(tokens)) as pool:return list(pool.map(run,tokens))
 def test_normal_loan_return(self):
  r=self.loan();self.assertEqual(self.sql('SELECT status FROM devices WHERE id=1'),[('LENT',)])
  t=self.s.prepare(1002,'return',{'lending_id':r['lending_id']});self.s.execute(1002,t)
  self.assertEqual(self.sql('SELECT status FROM devices WHERE id=1'),[('AVAILABLE',)])
  self.assertEqual(self.sql('SELECT count(*) FROM audit_log'),[(2,)])
 def test_concurrent_same_device(self):
  out=self.race([self.prepare(),self.prepare(1,1003)])
  self.assertEqual(sum(isinstance(x,dict) for x in out),1);self.assertIn('E-02',out)
  self.assertEqual(self.sql('SELECT count(*) FROM lendings WHERE device_id=1'),[(1,)])
 def test_concurrent_same_borrower(self):
  out=self.race([self.prepare(),self.prepare(5,1002)])
  self.assertEqual(sum(isinstance(x,dict) for x in out),1);self.assertIn('E-08',out)
 def test_last_device(self):
  self.sql("UPDATE devices SET status='REPAIR' WHERE status='AVAILABLE' AND id!=1")
  out=self.race([self.prepare(),self.prepare(1,1003)])
  self.assertIn('E-04',out)
 def test_idempotent_concurrent_and_restart(self):
  t=self.prepare();out=self.race([t,t]);self.assertEqual(out[0],out[1])
  self.assertEqual(self.sql('SELECT count(*) FROM audit_log'),[(1,)])
  again=Store(self.s.path,clock=lambda:self.time);self.assertEqual(again.execute(1001,t),out[0])
 def test_loan_rollback_and_retry(self):
  for point in ['loan_after_insert','after_audit']:
   with self.subTest(point=point):
    t=self.prepare()
    def fail(p):
     if p==point:raise sqlite3.OperationalError('injected test failure')
    self.s.failpoint=fail;self.error('E-15',lambda:self.s.execute(1001,t))
    self.assertEqual(self.sql('SELECT status FROM devices WHERE id=1'),[('AVAILABLE',)])
    self.assertEqual(self.sql('SELECT count(*) FROM lendings WHERE device_id=1'),[(0,)])
    self.assertEqual(self.sql('SELECT count(*) FROM audit_log'),[(0,)])
    self.assertEqual(self.sql('SELECT result FROM requests WHERE token=?',(t,)),[(None,)])
  self.s.failpoint=None;self.s.execute(1001,t)
 def test_return_rollback(self):
  r=self.loan();t=self.s.prepare(1001,'return',{'lending_id':r['lending_id']})
  self.s.failpoint=lambda _:(_ for _ in ()).throw(sqlite3.OperationalError('injected'))
  self.error('E-15',lambda:self.s.execute(1001,t))
  self.assertEqual(self.sql('SELECT returned_at FROM lendings WHERE id=?',(r['lending_id'],)),[(None,)])
  self.assertEqual(self.sql('SELECT status FROM devices WHERE id=1'),[('LENT',)])
  self.s.failpoint=None;self.s.execute(1001,t)
 def test_device_states(self):
  for device,code in [(999,'E-01'),(2,'E-02'),(3,'E-03'),(4,'E-03'),(21,'E-05')]:
   with self.subTest(device=device):self.error(code,lambda:self.prepare(device))
 def test_employee_states(self):
  for user,code in [(999,'E-06'),(1006,'E-07'),(1007,'E-07'),(1008,'E-13')]:
   with self.subTest(user=user):self.error(code,lambda:self.prepare(1,user))
  self.loan();self.error('E-08',lambda:self.prepare(5))
 def test_revalidate_after_confirmation(self):
  t=self.prepare();self.sql("UPDATE employees SET employment_status='LEAVE' WHERE id=1002")
  self.error('E-07',lambda:self.s.execute(1001,t))
 def test_ids(self):
  for v in ['', '0','-1','１','1 OR 1=1','9223372036854775808']:
   with self.subTest(v=v):self.error('E-09',lambda:Store.identifier(v))
 def test_dates_and_boundaries(self):
  for delta in [-1,0,7,8]:
   p=self.p();p['due_date']=(self.time.date()+timedelta(days=delta)).isoformat()
   if delta in [0,7]:self.s.loan_payload(p)
   else:self.error('E-11',lambda:self.s.loan_payload(p))
  for value in ['','2026-02-30','20260912','2026/09/12']:
   p=self.p();p['due_date']=value;self.error('E-10',lambda:self.s.loan_payload(p))
 def test_midnight_revalidation(self):
  self.time=self.time.replace(hour=23,minute=55);t=self.prepare();self.time+=timedelta(minutes=10)
  self.error('E-11',lambda:self.s.execute(1001,t))
 def test_purpose(self):
  for text in ['','　 ','a'*101,'a\x00b','a\nb']:
   p=self.p();p['purpose']=text;self.error('E-12',lambda:self.s.loan_payload(p))
  p=self.p();p['purpose']='😀'*100;self.assertEqual(len(self.s.loan_payload(p)['purpose']),100)
  self.assertEqual(esc('<script>"'), '&lt;script&gt;&quot;')
 def test_overdue_can_return(self):
  r=self.s.execute(1008,self.s.prepare(1008,'return',{'lending_id':1}))
  self.assertEqual(r['action'],'return');self.prepare(1,1008)
 def test_unauthorized_return_and_actor(self):
  r=self.loan();self.error('E-18',lambda:self.s.prepare(1003,'return',{'lending_id':r['lending_id']}))
  self.error('E-18',lambda:self.s.prepare(1006,'loan',self.p()))
 def test_tokens_ownership_and_expiry(self):
  t=self.prepare();self.error('E-17',lambda:self.s.execute(1002,t));self.error('E-17',lambda:self.s.execute(1001,'bogus'))
  self.time+=timedelta(minutes=31);self.error('E-17',lambda:self.s.execute(1001,t))
 def test_return_duplicates_and_old_loan(self):
  r=self.loan();a=self.s.prepare(1001,'return',{'lending_id':r['lending_id']});b=self.s.prepare(1001,'return',{'lending_id':r['lending_id']})
  result=self.s.execute(1001,a);self.assertEqual(self.s.execute(1001,a),result)
  new=self.loan(1,1003);self.error('E-14',lambda:self.s.execute(1001,b))
  self.assertEqual(self.sql('SELECT returned_at FROM lendings WHERE id=?',(new['lending_id'],)),[(None,)])
  self.error('E-14',lambda:self.s.prepare(1001,'return',{'lending_id':999}))
 def test_database_guards(self):
  self.loan()
  with self.assertRaises(sqlite3.IntegrityError):self.sql("UPDATE devices SET status='REPAIR' WHERE id=1")
  with self.assertRaises(sqlite3.IntegrityError):self.sql("INSERT INTO lendings(device_id,user_id,lent_at,due_date,purpose) VALUES(1,1003,'now','2026-09-12','test')")
 def test_inconsistent_device_rejected(self):
  self.sql("INSERT INTO lendings(device_id,user_id,lent_at,due_date,purpose) VALUES(1,1003,'now','2026-09-12','test')")
  self.error('E-20',lambda:self.prepare())
 def test_authentication_and_seed_preserves(self):
  self.assertEqual(self.s.authenticate('1001','demo1234'),1001)
  self.assertIsNone(self.s.authenticate('1001','wrong'));self.assertIsNone(self.s.authenticate('1006','demo1234'))
  self.loan();self.assertFalse(self.s.seed());self.assertEqual(len(self.s.loans(1002)),1)

if __name__=='__main__':unittest.main(verbosity=2)
