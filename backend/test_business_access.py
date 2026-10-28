"""RBAC tests exercise real routes, immutable scopes, and migration replay."""
import os
import uuid
import time
import unittest
import tempfile
from pathlib import Path
from fastapi.testclient import TestClient
from . import test_data_import as fixtures
from project.tests.integration import test_supply_workflow as workflow_fixtures
from .auth import now, password_hash
from .common import APIError
from .main import create_legacy_test_app as create_app


def grant(role, factories=('A','B'), domains=('inventory','purchase','production','quality','rules'), limit='0'):
    return {'role':role,'factories':sorted(factories),'domains':sorted(domains),'approval_limit':limit}


class AccessTests(unittest.TestCase):
    setUp=fixtures.DataImportTests.setUp
    tearDown=fixtures.DataImportTests.tearDown

    def setup_members(self):
        self.app=self.client.app
        for account in ('member@test.invalid','admin@test.invalid'):
            self.app.state.auth.query("INSERT INTO users(account,type,name,password_hash,created_at) VALUES(?,'email',?,?,?)",(account,account,password_hash('password123'),now()))
        self.workspace=self.client.get('/api/supply/access/me').json()['workspaceId']
        self.headers={'X-Workspace-Id':self.workspace}

    def login(self,account='data@test.invalid'):
        self.assertEqual(self.client.post('/api/auth/login',json={'account':account,'password':'password123'}).status_code,200)

    def save(self,roles,account='member@test.invalid',enabled=True,expected=200):
        old=self.app.state.access.member(self.workspace,account)
        response=self.client.post('/api/supply/access/members',headers=self.headers,json={'account':account,'roles':roles,'enabled':enabled,'version':old['version'] if old else 0})
        self.assertEqual(response.status_code,expected,response.text)
        return response.json()

    def upload(self,factory='A',domain='inventory',expected=200):
        r=self.client.post('/api/data/imports/preview',headers=self.headers,data={'domain':domain,'factory':factory,'owner':'test'},files={'file':('stock.csv','material,quantity,unit\nP001,10,件'.encode())})
        self.assertEqual(r.status_code,expected,r.text)
        if expected!=200:
            return
        key=r.json()['draftId']
        r=self.client.post(f'/api/data/imports/{key}/check',headers=self.headers,json={'mapping':{'material':'material','quantity':'quantity','unit':'unit'}})
        self.assertEqual(r.status_code,200,r.text)
        r=self.client.post(f'/api/data/imports/{key}/commit',headers=self.headers,json={'strategy':'upsert','businessKey':'material','idempotencyKey':str(uuid.uuid4()),'isolateWarnings':True})
        self.assertEqual(r.status_code,200,r.text)
        return r.json()['id']

    def test_multi_role_scopes_and_file_resource_isolation(self):
        self.setup_members()
        a=self.upload('A'); b=self.upload('B')
        self.save([grant('data_admin',['A'],['inventory']),grant('viewer',['B'],['purchase']),grant('approver',['A'],limit='100')])
        self.login('member@test.invalid')
        me=self.client.get('/api/supply/access/me',headers=self.headers).json()
        self.assertEqual(len(me['roles']),3)
        rows=self.client.get('/api/data/imports',headers=self.headers).json()
        self.assertEqual([r['id'] for r in rows],[a])
        for suffix in ('','/file'):
            self.assertEqual(self.client.get('/api/data/imports/'+b+suffix,headers=self.headers).status_code,403)
        self.assertEqual(self.client.post('/api/data/imports/'+b+'/recheck',headers=self.headers,json={}).status_code,403)
        self.upload('B',expected=403)
        self.upload('A','purchase',403)
        self.assertEqual(self.client.post('/api/data/governance/rules',headers=self.headers,json={'factory':'A'}).status_code,403)
        self.assertEqual(self.client.post('/api/supply/access/members',headers=self.headers,json={}).status_code,403)
        user=self.app.state.auth.query('SELECT id,account FROM users WHERE account=?',('member@test.invalid',))
        with self.assertRaises(APIError):
            self.app.state.access.require(user,self.workspace,'approve',['B'],amount=1)
        with self.assertRaises(APIError):
            self.app.state.access.require(user,self.workspace,'approve',['A'],amount=101)
        # Same business key in a separate personal workspace cannot overwrite A.
        personal=self.client.get('/api/supply/access/me').json()['workspaceId']
        old=self.headers; self.headers={'X-Workspace-Id':personal}; self.upload('A'); self.headers=old
        self.assertEqual(self.client.get('/api/data/imports',headers=self.headers).json()[0]['id'],a)

    def test_last_admin_self_escalation_version_revocation_and_audit(self):
        self.setup_members()
        self.save([],account='data@test.invalid',enabled=False,expected=409)
        self.save([grant('system_admin'),grant('approver',limit='10000')],account='data@test.invalid',expected=403)
        saved=self.save([grant('system_admin')],account='admin@test.invalid')
        stale=self.client.post('/api/supply/access/members',headers=self.headers,json={'account':'admin@test.invalid','roles':[],'enabled':True,'version':0})
        self.assertEqual(stale.status_code,409)
        self.login('admin@test.invalid')
        user=self.app.state.auth.query('SELECT id,account FROM users WHERE account=?',('admin@test.invalid',))
        with self.assertRaises(APIError):
            self.app.state.access.require(user,self.workspace,'approve',['A'],amount=0)
        self.assertEqual(self.client.post('/api/supply/access/policies',headers=self.headers,json={'action':'transfer','maxQuantity':'100','maxCost':'300','version':0}).status_code,200)
        self.assertEqual(self.client.post('/api/supply/access/policies',headers=self.headers,json={'action':'transfer','maxQuantity':'100','maxCost':'300','version':0}).status_code,409)
        self.save([grant('viewer',['A'])],account='data@test.invalid')
        self.login()
        self.assertEqual(self.client.get('/api/supply/access/members',headers=self.headers).status_code,403)
        self.assertEqual(self.client.post('/api/supply/access/members',headers=self.headers,json={'account':'admin@test.invalid','roles':[],'version':saved['version']}).status_code,403)
        self.login('admin@test.invalid')
        self.save([],account='data@test.invalid',enabled=False)
        history=self.client.get('/api/supply/access/audit',headers=self.headers).json()
        self.assertGreaterEqual(len(history),4)
        self.login()
        self.assertEqual(self.client.get('/api/data/imports',headers=self.headers).status_code,403)
        self.assertEqual(self.client.get('/api/supply/access/me',headers=self.headers).status_code,403)

    def test_assistant_shared_data_and_revoked_historical_result(self):
        self.setup_members(); batch=self.upload()
        self.save([grant('viewer',['A'],['inventory'])])
        self.login('member@test.invalid')
        r=self.client.post('/api/supply/threads',headers=self.headers,json={'factory':'A','requestId':str(uuid.uuid4())})
        self.assertEqual(r.status_code,200,r.text); thread=r.json()['id']
        r=self.client.post(f'/api/supply/threads/{thread}/messages',headers=self.headers,json={'text':'查询库存','requestId':str(uuid.uuid4()),'scope':{'intent':'inventory','factory':'A'}})
        self.assertEqual(r.status_code,200,r.text); task=r.json()
        r=self.client.post(f"/api/supply/tasks/{task['id']}/confirm",headers=self.headers,json={'confirmed':True,'plan':task['plan']})
        self.assertEqual(r.status_code,200,r.text)
        for _ in range(100):
            task=self.client.get(f"/api/supply/tasks/{task['id']}",headers=self.headers).json()
            if task['status'] not in ('queued','running'): break
            time.sleep(.03)
        self.assertEqual(task['status'],'completed',task)
        self.assertEqual(task['result']['records'][0]['batch_id'],batch)
        self.assertIsNone(task['result']['purchaseCount'])
        self.login();self.save([],enabled=False);self.login('member@test.invalid')
        self.assertEqual(self.client.get(f'/api/supply/threads/{thread}').status_code,403)
        self.assertEqual(self.client.get(f"/api/supply/tasks/{task['id']}").status_code,403)

    def test_existing_member_migration_is_once_and_survives_restart(self):
        self.setup_members()
        data=self.app.state.data
        data.db.execute('INSERT INTO workflow_members VALUES(?,?,?,?,?,?,?)',(self.workspace,'member@test.invalid','approver','["A"]','500','data@test.invalid',now()))
        self.app.state.access.sync()
        member=self.app.state.access.member(self.workspace,'member@test.invalid')
        self.assertEqual(member['roles'],[grant('approver',['A'],limit='500')])
        self.save([],enabled=False)
        self.app.state.access.sync()
        self.assertFalse(self.app.state.access.member(self.workspace,'member@test.invalid')['enabled'])
        root=Path(self.temp.name)
        self.client.__exit__(None,None,None)
        self.client=TestClient(create_app({'AUTH_DB_PATH':str(root/'auth.sqlite'),'DATA_DB_PATH':str(root/'supply.sqlite'),'NODE_ENV':'test'})).__enter__()
        self.login('member@test.invalid')
        self.assertEqual(self.client.get('/api/supply/access/me',headers=self.headers).status_code,403)

    def test_malformed_grants_are_rejected(self):
        self.setup_members()
        for invalid in ({'role':[]},grant('viewer',domains=[{}])):
            self.save([invalid],expected=400)


class MigrationTests(unittest.TestCase):
    def test_legacy_file_data_is_preserved_and_partitioned(self):
        from .data_store import DataStore
        from .auth import Auth
        with tempfile.TemporaryDirectory() as root:
            root=Path(root)
            env={'AUTH_DB_PATH':str(root/'auth.sqlite'),'DATA_DB_PATH':str(root/'supply.sqlite'),'NODE_ENV':'test'}
            auth=Auth(root/'auth.sqlite',env)
            auth.query("INSERT INTO users(account,type,name,password_hash,created_at) VALUES(?,'email',?,?,?)",('legacy@test.invalid','legacy',password_hash('password123'),now()))
            uid=auth.query('SELECT id FROM users WHERE account=?',('legacy@test.invalid',))['id']
            data=DataStore(root/'supply.sqlite')
            data.db.execute('INSERT INTO import_drafts VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',('old',uid,'old.csv','inventory','A','legacy','','[]','[]','{}','{}',now()))
            data.db.execute('INSERT INTO import_batches VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',('old-batch','old',uid,'old.csv','inventory','A','legacy','','{}','insert','material',1,1,0,now(),'old-key'))
            data.db.execute('INSERT INTO source_records VALUES(1,?,2,?,?,?,?,?,?)',('old-batch','{}','P001','10','件','10','valid'))
            data.db.execute('INSERT INTO current_records VALUES(?,?,?,1)',('inventory','A','P001'))
            data.db.execute('INSERT INTO unit_rules(material,factory,from_unit,to_unit,factor,evidence,confirmed_by,valid_from,version,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)',('P001','A','box','件','10','legacy','legacy','2026-01-01',1,now()))
            data.db.close();auth.db.close()
            for _ in range(2):
                with TestClient(create_app(env)) as client:
                    client.post('/api/auth/login',json={'account':'legacy@test.invalid','password':'password123'})
                    rows=client.get('/api/data/records').json()
                    self.assertEqual(len(rows),1,rows)
                    self.assertEqual(rows[0]['normalized_quantity'],'10')
                    rules=client.get('/api/data/governance/rules').json()
                    self.assertEqual(len(rules),1,rules)
                    self.assertEqual(rules[0]['factor'],'10')


@unittest.skipUnless(os.getenv('SCOPE_DATABASE_INTEGRATION')=='1','Requires PostgreSQL')
class ScopedRiskTests(unittest.TestCase):
    setUp=workflow_fixtures.WorkflowTests.setUp
    tearDown=workflow_fixtures.WorkflowTests.tearDown
    login=workflow_fixtures.WorkflowTests.login
    post=workflow_fixtures.WorkflowTests.post
    select=workflow_fixtures.WorkflowTests.select
    events=workflow_fixtures.WorkflowTests.events

    def test_logistics_responsibility_and_monitor_separation(self):
        self.select();self.post('/api/supply/monitor/scan')
        event=self.events()[0]
        ws=self.client.get('/api/supply/access/me').json()['workspaceId']
        h={'X-Workspace-Id':ws}
        self.post('/api/supply/access/members',{'account':'risk-two@test.invalid','roles':[grant('logistics',['A'])],'version':0,'enabled':True})
        self.login('risk-two@test.invalid')
        body={'purchaseLineId':'PO-FOCUS','requestId':str(uuid.uuid4()),'availableAt':'2026-10-08T08:00:00+08:00'}
        self.assertEqual(self.client.post('/api/supply/monitor/commitment',headers=h,json=body).status_code,403)
        self.assertEqual(self.client.post('/api/supply/monitor',headers=h,json={'days':7}).status_code,403)
        self.assertEqual(self.client.post('/api/supply/monitor/scan',headers=h,json={}).status_code,403)
        self.assertEqual(self.client.get('/api/data/database/preview?table=inventory_snapshots&factory=B',headers=h).status_code,403)
        rows=self.client.get('/api/data/database/preview?table=inventory_snapshots',headers=h).json()['rows']
        self.assertTrue(all(r['factory_id']=='A' for r in rows))
        self.assertEqual(self.client.get('/api/data/database/preview?table=supplier_commitments',headers=h).status_code,403)
        self.login()
        self.post('/api/supply/risks/'+event['id'],{'version':event['version'],'owner':'risk-two@test.invalid'})
        self.login('risk-two@test.invalid')
        result=self.client.post('/api/supply/monitor/commitment',headers=h,json=body)
        self.assertEqual(result.status_code,200,result.text)
        self.assertNotIn('results',result.json())
        self.assertEqual(self.client.get('/api/supply/risks',headers=h).json()['latestScan'],None)
