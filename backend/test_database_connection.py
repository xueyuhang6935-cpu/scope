"""Database access contract; opt into the local PostgreSQL integration check."""
import os
import tempfile
import unittest
from pathlib import Path
from decimal import Decimal

from fastapi.testclient import TestClient
from .auth import now, password_hash
from .main import create_legacy_test_app as create_app


class DatabaseConnectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.env = {"AUTH_DB_PATH": str(root / "auth.sqlite"), "DATA_DB_PATH": str(root / "data.sqlite"), "NODE_ENV": "test"}

    def tearDown(self):
        self.temp.cleanup()

    def login(self, client, account="database@test.invalid"):
        auth = client.app.state.auth
        if not auth.query("SELECT id FROM users WHERE account=?", (account,)):
            auth.query("INSERT INTO users(account,type,name,password_hash,created_at) VALUES(?,'email',?,?,?)",
                       (account, "数据库测试", password_hash("password123"), now()))
        self.assertEqual(client.post("/api/auth/login", json={"account": account, "password": "password123"}).status_code, 200)

    def test_auth_and_unconfigured_server(self):
        with TestClient(create_app({**self.env, "ANALYST_DATABASE_URL": ""})) as client:
            for method, path in [("get", ""), ("post", ""), ("post", "/test"), ("get", "/preview")]:
                self.assertEqual(getattr(client, method)("/api/data/database" + path).status_code, 401)
            self.login(client)
            state = client.get("/api/data/database").json()
            self.assertFalse(state["configured"])
            self.assertFalse(state["connected"])
            self.assertIn("ANALYST_DATABASE_URL", state["message"])
            self.assertEqual(client.post("/api/data/database", json={"scenarioId": "normal"}).status_code, 503)
            self.assertEqual(client.get("/api/data/database/preview").status_code, 409)

    @unittest.skipUnless(os.getenv('SCOPE_LADE_INTEGRATION') == '1', '需要本机已导入 LaDe 的 PostgreSQL')
    def test_lade_stale_scenario_trace_preview_and_scope(self):
        with TestClient(create_app({**self.env, 'SUPPLY_MONITOR_AUTOSTART':'0'})) as client:
            self.login(client)
            user = client.app.state.auth.query('SELECT id FROM users WHERE account=?', ('database@test.invalid',))
            # 复现被清空场景的遗留配置，不能让工作空间中间件阻断新数据接入。
            client.app.state.data.db.execute('INSERT INTO database_connections VALUES(?,?,?)', (user['id'],'deleted_scenario','2026-10-01'))
            response = client.get('/api/data/database')
            self.assertEqual(response.status_code,200,response.text)
            state = response.json()
            self.assertTrue(state['connected'],state)
            self.assertEqual(state['schema'],'lade')
            self.assertEqual(state['defaultTable'],'pickup_tasks')
            self.assertFalse(state['configured'])
            batch = state['scenarioId']
            for path in ['/api/data/database/test','/api/data/database']:
                r=client.post(path,json={'scenarioId':batch})
                self.assertEqual(r.status_code,200,r.text)
            self.assertTrue(client.get('/api/data/database').json()['configured'])
            self.assertEqual(client.app.state.data.one('SELECT count(*) AS n FROM risk_monitor_configs')['n'],0)
            full=client.get('/api/data/database/preview')
            self.assertEqual(full.status_code,200,full.text)
            self.assertEqual(full.json()['total'],1424406)
            self.assertEqual(len(full.json()['rows']),20)
            other=client.get('/api/data/database/preview?page=2').json()
            self.assertFalse({r['source_row'] for r in full.json()['rows']} & {r['source_row'] for r in other['rows']})
            trace=client.get('/api/data/database/preview',params={'table':'pickup_tasks','region':'43','order_id':'2590170'}).json()
            self.assertEqual(trace['total'],1)
            self.assertEqual(trace['rows'][0]['source_row'],968475)
            self.assertIn('pickup_after_window',trace['rows'][0]['quality_flags'])
            self.assertTrue(trace['rows'][0]['eligible_sla'])
            self.assertEqual(trace['rows'][0]['accept_gps_time'],'')
            for table,total in [('quality_rules',46),('import_batches',1)]:
                r=client.get('/api/data/database/preview',params={'table':table})
                self.assertEqual(r.status_code,200,r.text)
                self.assertEqual(r.json()['total'],total)
                self.assertNotIn('password',r.text)
                if table=='import_batches':
                    self.assertNotIn('source_file',r.json()['columns'])
            for params in [{'table':'users'},{'table':'pickup_tasks;DROP TABLE lade.pickup_tasks'},{'page':0}]:
                self.assertEqual(client.get('/api/data/database/preview',params=params).status_code,400)
            self.assertEqual(client.post('/api/data/database',json={'scenarioId':'normal'}).status_code,400)
            # 单厂数据管理员不能借由物流接口读取全区域任务。
            workspace=state['workspaceId']
            client.app.state.data.db.execute("UPDATE business_roles SET factories='[\"A\"]' WHERE workspace_id=? AND role='data_admin'",(workspace,))
            self.assertEqual(client.get('/api/data/database/preview').status_code,403)
            self.assertEqual(client.post('/api/data/database/test',json={'scenarioId':batch}).status_code,403)
            client.app.state.data.db.execute("UPDATE business_roles SET factories='[\"A\",\"B\"]' WHERE workspace_id=? AND role='data_admin'",(workspace,))
            self.login(client,'other@test.invalid')
            self.assertFalse(client.get('/api/data/database').json()['configured'])
            self.assertEqual(client.get('/api/data/database/preview',headers={'X-Workspace-Id':workspace}).status_code,403)
        with TestClient(create_app({**self.env,'SUPPLY_MONITOR_AUTOSTART':'0'})) as client:
            self.login(client)
            state=client.get('/api/data/database').json()
            self.assertTrue(state['connected'])
            self.assertTrue(state['configured'])
            self.assertEqual(state['scenarioId'],batch)

    @unittest.skipUnless(os.getenv("SCOPE_DATABASE_INTEGRATION") == "1", "需要显式启用本机 PostgreSQL 集成测试")
    def test_real_database_save_isolation_preview_and_restart(self):
        with TestClient(create_app(self.env)) as client:
            self.login(client)
            state = client.get("/api/data/database").json()
            self.assertEqual(len(state["tables"]), 45)
            self.assertEqual(len(state["scenarios"]), 12)
            self.assertNotIn("password", state)
            self.assertFalse(state["configured"])
            self.assertEqual(client.post("/api/data/database/test", json={"scenarioId": "supplier_delay"}).status_code, 200)
            self.assertFalse(client.get("/api/data/database").json()["configured"])
            saved = client.post("/api/data/database", json={"scenarioId": "supplier_delay"}).json()
            self.assertTrue(saved["connected"])
            preview = client.get("/api/data/database/preview?factory=A").json()
            self.assertEqual(preview["total"], 12)
            self.assertTrue(all(row["scenario_id"] == "supplier_delay" and row["factory_id"] == "A" for row in preview["rows"]))
            p001 = next(row for row in preview["rows"] if row["material_id"] == "P001")
            self.assertEqual(Decimal(p001["quantity"]), Decimal("800"))
            for table in state["tables"]:
                result = client.get("/api/data/database/preview", params={"table": table})
                self.assertEqual(result.status_code, 200, (table, result.text))
                self.assertLessEqual(len(result.json()["rows"]), 20)
            first = client.get("/api/data/database/preview?table=production_plan_lines").json()
            second = client.get("/api/data/database/preview?table=production_plan_lines&page=2").json()
            self.assertTrue(second["rows"])
            self.assertFalse({row["id"] for row in first["rows"]} & {row["id"] for row in second["rows"]})
            for params in [{"table": "users"}, {"table": 'inventory_snapshots"; DROP TABLE x;--'}, {"page": -1}, {"factory": "invalid"}]:
                self.assertEqual(client.get("/api/data/database/preview", params=params).status_code, 400)
            self.assertEqual(client.post("/api/data/database", json={"scenarioId": "bad"}).status_code, 400)
            self.assertEqual(client.get("/api/data/database").json()["scenarioId"], "supplier_delay")
            self.login(client, "other@test.invalid")
            self.assertFalse(client.get("/api/data/database").json()["configured"])
            self.assertEqual(client.get("/api/data/database/preview").status_code, 409)
        with TestClient(create_app(self.env)) as client:
            self.login(client)
            self.assertTrue(client.get("/api/data/database").json()["connected"])
            self.assertEqual(client.get("/api/data/database").json()["scenarioId"], "supplier_delay")
        with TestClient(create_app({**self.env, "ANALYST_DATABASE_URL": "postgresql://private_user:private_secret@127.0.0.1:1/unavailable"})) as client:
            self.login(client)
            state = client.get("/api/data/database")
            self.assertTrue(state.json()["configured"])
            self.assertFalse(state.json()["connected"])
            self.assertNotIn("private_secret", state.text)
            self.assertEqual(client.post("/api/data/database", json={"scenarioId": "normal"}).status_code, 503)
            self.assertEqual(client.get("/api/data/database").json()["scenarioId"], "supplier_delay")


if __name__ == "__main__":
    unittest.main()
