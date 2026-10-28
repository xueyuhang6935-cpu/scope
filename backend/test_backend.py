"""验证认证与供应链应用入口；使用临时账号库，不访问历史业务存储。"""
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from .auth import now, password_hash, valid_password
from .main import create_app


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app({
            "AUTH_DB_PATH": str(Path(self.temp.name) / "auth.sqlite"),
            "DATA_DB_PATH": str(Path(self.temp.name) / "supply.sqlite"),
            "NODE_ENV": "test",
            "AUTH_SECRET": "isolated-test-secret",
            "ANALYST_DATABASE_URL": "invalid-unused-url",
            "ANALYST_S3_ENDPOINT": "http://invalid-unused-endpoint",
        })
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.auth = self.app.state.auth
        self.auth.query("INSERT INTO users(account,type,name,password_hash,created_at) VALUES(?,'email',?,?,?)",
                        ("one@test.invalid", "测试计划员", password_hash("password123"), now()))

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temp.cleanup()

    def test_auth_compatibility_and_boundaries(self):
        stored = subprocess.check_output(["node", "--input-type=module", "-e",
            "import {scryptSync} from 'node:crypto'; console.log('abc:'+scryptSync('password123','abc',64).toString('hex'))"], text=True).strip()
        self.assertTrue(valid_password("password123", stored))
        self.assertFalse(valid_password("wrong", stored))
        self.assertEqual(self.client.get("/api/auth/session").status_code, 401)
        self.assertEqual(self.client.post("/api/auth/login", content="{").status_code, 400)
        self.assertEqual(self.client.post("/api/auth/login", content="[]").status_code, 400)
        self.assertEqual(self.client.post("/api/auth/login", content="x" * 16385).status_code, 413)
        self.assertEqual(self.client.post("/api/auth/login", json={"account": "one@test.invalid", "password": "wrong"}).status_code, 401)
        response = self.client.post("/api/auth/login", json={"account": "one@test.invalid", "password": "password123"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("HttpOnly", response.headers["set-cookie"])
        self.assertEqual(self.client.get("/api/auth/session").json()["user"]["name"], "测试计划员")
        self.assertEqual(self.client.post("/api/auth/logout").status_code, 204)
        self.assertEqual(self.client.get("/api/auth/session").status_code, 401)

    def test_registration_and_password_reset_without_external_delivery(self):
        delivered = []
        with patch.object(self.auth, "deliver", side_effect=lambda account, kind, code: delivered.append(code)):
            response = self.client.post("/api/auth/verification-codes", json={"account": "new@test.invalid", "type": "email", "purpose": "register"})
            self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.post("/api/auth/register", json={"account": "new@test.invalid", "type": "email", "code": delivered[-1], "password": "password123"}).status_code, 201)
        self.assertEqual(self.client.post("/api/auth/login", json={"account": "new@test.invalid", "password": "password123"}).status_code, 200)
        phone = "13800000000"
        self.auth.query("INSERT INTO users(account,type,name,password_hash,created_at) VALUES(?,'phone',?,?,?)", (phone, "测试账号", password_hash("password123"), now()))
        with patch.object(self.auth, "deliver", side_effect=lambda account, kind, code: delivered.append(code)):
            self.assertEqual(self.client.post("/api/auth/verification-codes", json={"account": phone, "type": "phone", "purpose": "reset-password"}).status_code, 200)
        self.assertEqual(self.client.post("/api/auth/reset-password", json={"phone": phone, "code": delivered[-1], "password": "newpassword123"}).status_code, 200)
        self.assertEqual(self.client.post("/api/auth/login", json={"account": phone, "password": "newpassword123"}).status_code, 200)

    def test_removed_analysis_api_and_storage_independent_startup(self):
        self.assertFalse(hasattr(self.app.state, "storage"))
        health = self.client.get("/api/health")
        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json()["workspace"], "shanghai-logistics")
        self.assertEqual(health.json()["database"], "sqlite")
        for method, path in [("GET", "/api/analyst/catalog"), ("GET", "/api/analyst/workspace"), ("GET", "/api/analyst/sessions"), ("POST", "/api/analyst/analyze"), ("POST", "/api/analyst/message")]:
            self.assertEqual(self.client.request(method, path).status_code, 404)
        self.assertEqual(self.auth.query("SELECT COUNT(*) AS count FROM users")["count"], 1)

    def test_frontend_deep_links_and_private_files(self):
        for path in ["/workspace", "/account", "/risks/EVT-001?tab=plans"]:
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertIn('id="root"', response.text)
        self.assertEqual(self.client.get("/.env").status_code, 404)


if __name__ == "__main__":
    unittest.main()
