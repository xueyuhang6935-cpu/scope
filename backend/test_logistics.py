"""单空间权限回归；所有写入均在临时 SQLite，源查询使用明确的只读替身。"""
import json
import tempfile
import unittest
from pathlib import Path
from contextlib import contextmanager
from unittest.mock import patch
from fastapi.testclient import TestClient
from backend.main import create_app
from backend.auth import password_hash, now
from backend.common import APIError
from backend.logistics_access import WORKSPACE, LogisticsAccess


class LogisticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = {'AUTH_DB_PATH': str(Path(self.temp.name)/'auth.sqlite'), 'DATA_DB_PATH': str(Path(self.temp.name)/'data.sqlite'), 'NODE_ENV': 'test', 'AUTH_SECRET': 'test-secret', 'ANALYST_DATABASE_URL': 'unused'}
        self.app = create_app(self.env)
        self.client = TestClient(self.app).__enter__()
        self.access, self.db = self.app.state.access, self.app.state.data.db
        for account in ('admin', 'data', 'investigator', 'approver', 'pending'):
            self.app.state.auth.query("INSERT INTO users(account,type,name,password_hash,created_at) VALUES(?,'email',?,?,?)", (account, account, password_hash('password123'), now()))
        self.access.sync()
        for account, role in [('admin','system_admin'), ('data','data_admin'), ('investigator','investigator'), ('approver','approver')]:
            self.db.execute('INSERT INTO logistics_grants VALUES(?,?,?,?,?)', (WORKSPACE, account, role, 'Shanghai', json.dumps([] if account == 'admin' else ['5'])))

    def tearDown(self):
        self.client.__exit__(None,None,None)
        self.temp.cleanup()

    def login(self, account):
        self.assertEqual(self.client.post('/api/auth/login', json={'account':account, 'password':'password123'}).status_code, 200)

    def test_singleton_no_legacy_and_pending(self):
        self.login('pending')
        self.assertEqual(self.client.get('/api/logistics/access/me').json()['roles'], [])
        for route in ('/api/supply/workspaces', '/api/supply/risks', '/api/data/connections'):
            self.assertEqual(self.client.get(route).status_code, 404)
        self.assertEqual(self.client.get('/api/logistics/data/preview').status_code, 403)
        self.assertEqual(self.client.get('/api/logistics/access/me',headers={'X-Workspace-Id':'normal'}).status_code,403)
        self.assertEqual(self.db.execute('SELECT count(*) FROM business_spaces').fetchone()[0],1)
        self.assertIsNone(self.app.state.workflow)
        self.assertIsNone(self.app.state.monitor)

    def test_local_account_switch_rotates_session_and_rejects_admin(self):
        from backend.auth import COOKIE
        self.env['LOCAL_TEST_ACCOUNT_SWITCH'] = 'true'
        self.access.env.update(self.env)
        self.app.state.auth.query("INSERT INTO users(account,type,name,password_hash,created_at) VALUES(?,'email',?,?,?)", ('reviewer@scope.test','reviewer',password_hash('password123'),now()))
        self.access.sync()
        self.db.execute('INSERT INTO logistics_grants VALUES(?,?,?,?,?)',(WORKSPACE,'reviewer@scope.test','approver','Shanghai','["5"]'))
        remote = TestClient(self.app,base_url='http://localhost',client=('192.0.2.10',12345))
        remote.post('/api/auth/login',json={'account':'admin','password':'password123'})
        self.assertEqual(remote.post('/api/logistics/access/switch-test-account',json={'account':'reviewer@scope.test'}).status_code,403)
        remote.close()
        with TestClient(self.app,base_url='http://localhost',client=('127.0.0.1',12345)) as client:
            self.assertEqual(client.get('/api/logistics/access/test-accounts').status_code,401)
            client.post('/api/auth/login',json={'account':'admin','password':'password123'})
            old=client.cookies.get(COOKIE)
            path='/api/logistics/access/switch-test-account'
            self.assertEqual(client.post(path,json={'account':'admin'}).status_code,403)
            self.assertEqual(client.post(path,json={'account':[]}).status_code,403)
            self.assertEqual(client.post(path,json={'account':'reviewer@scope.test'},headers={'origin':'https://evil.invalid'}).status_code,403)
            self.assertEqual(client.post(path,json={'account':'reviewer@scope.test'}).status_code,200)
            self.assertEqual(client.get('/api/logistics/access/me').json()['account'],'reviewer@scope.test')
            self.assertEqual(client.get('/api/logistics/access/members').status_code,403)
            self.assertEqual(client.get('/api/auth/session',headers={'cookie':f'{COOKIE}={old}'}).status_code,401)
            for key,value in [('LOCAL_TEST_ACCOUNT_SWITCH','false'),('NODE_ENV','production')]:
                before=self.access.env.get(key);self.access.env[key]=value
                self.assertFalse(client.get('/api/logistics/access/test-accounts').json()['enabled'])
                self.assertEqual(client.post(path,json={'account':'reviewer@scope.test'}).status_code,403)
                self.access.env[key]=before
            self.db.execute("INSERT INTO logistics_grants VALUES(?,?,?,?,?)",(WORKSPACE,'reviewer@scope.test','system_admin','Shanghai','[]'))
            self.assertEqual(client.post(path,json={'account':'reviewer@scope.test'}).status_code,403)
            self.db.execute("DELETE FROM logistics_grants WHERE account='reviewer@scope.test' AND role='system_admin'")
            self.db.execute("UPDATE business_members SET enabled=0 WHERE account='reviewer@scope.test'")
            self.assertEqual(client.post(path,json={'account':'reviewer@scope.test'}).status_code,403)
        # 嵌套客户端会关闭应用资源；外层 tearDown 仍可安全关闭 SQLite。

    def test_role_region_and_no_self_approval(self):
        for account in ('admin','investigator','approver'):
            self.login(account)
            self.assertEqual(self.client.get('/api/logistics/data/preview').status_code,403)
        self.access.require({'account':'approver'},'approve',['5'],requested_by='investigator')
        with self.assertRaises(APIError):
            self.access.require({'account':'approver'},'approve',['5'])
        for account, region, city, requester in [('approver','5','Shanghai','approver'), ('approver','6','Shanghai','investigator'), ('approver','5','Beijing','investigator'), ('admin','5','Shanghai','investigator')]:
            with self.assertRaises(APIError) as error:
                self.access.require({'account':account},'approve',[region],city,requester)
            self.assertEqual(error.exception.status,403)

    def test_member_validation_versions_and_persistent_revocation(self):
        self.login('admin')
        def save(account, roles, version=1, enabled=True):
            return self.client.post('/api/logistics/access/members', json=dict(account=account,roles=roles,version=version,enabled=enabled))
        self.assertEqual(save('admin',[]).status_code,409)
        self.assertEqual(save('admin',[{'role':'approver','city':'Shanghai','regions':['5']}]).status_code,403)
        for role in ([], {}, 'invalid'):
            self.assertEqual(save('data',[{'role':role,'city':'Shanghai','regions':['5']}]).status_code,400)
        self.assertEqual(save('data',[{'role':'data_admin','city':'Shanghai','regions':['999']}]).status_code,400)
        self.assertEqual(save('data',[],enabled=False).status_code,200)
        self.assertEqual(save('data',[]).status_code,409)
        LogisticsAccess(self.app.state.auth,self.app.state.data,self.env)
        self.login('data')
        self.assertEqual(self.client.get('/api/logistics/access/me').status_code,403)
        self.assertEqual(self.access.member('data')['roles'],[])
        self.assertEqual(self.db.execute('SELECT count(*) FROM business_audit').fetchone()[0],1)

    def test_preview_queries_enforce_scope_and_fail_closed(self):
        self.login('data')
        self.assertEqual(self.client.get('/api/logistics/data/preview?region=6').status_code,403)
        statements = []
        class Cursor:
            def __init__(self, rows): self.rows=rows
            def fetchall(self): return self.rows
            def fetchone(self): return self.rows[0]
        class DB:
            def execute(self, query, params=None):
                statements.append((str(query),params))
                if 'import_batches' in str(query): return Cursor([{'batch_id':'verified'}])
                if 'count(*)' in str(query): return Cursor([{'n':1}])
                return Cursor([{'region_id':'5','order_id':'example','source_row':1}])
        @contextmanager
        def connection(env): yield DB()
        with patch('backend.logistics_data.connection',connection):
            result = self.client.get('/api/logistics/data/preview').json()
            self.assertEqual(result['regions'],['5'])
            self.assertEqual(result['total'],1)
            queries = [(q,p) for q,p in statements if p]
            self.assertEqual(len(queries),2)
            for query,params in queries:
                self.assertIn('region_id=ANY(%s)',query)
                self.assertEqual(params[:3],['verified','Shanghai',['5']])
            self.assertEqual(self.client.post('/api/logistics/data/connection',json={'batchId':'verified'}).status_code,403)


if __name__ == '__main__': unittest.main()
