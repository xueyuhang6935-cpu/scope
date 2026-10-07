"""运行：python -m unittest backend.test_wechat_mock -v"""
import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from .auth import Auth, routes
from .common import APIError, reply


class WechatMockTests(unittest.TestCase):
    def test_login_session_logout_and_guards(self):
        with tempfile.TemporaryDirectory() as directory:
            auth = Auth(Path(directory) / "auth.sqlite", {"WECHAT_MOCK_LOGIN": "true"})
            app = FastAPI()

            @app.exception_handler(APIError)
            async def error(request, exc):
                return reply(exc.body, exc.status)

            app.include_router(routes(auth))
            try:
                with TestClient(app, base_url="http://localhost") as client:
                    self.assertEqual(client.get("/api/auth/wechat/authorize").json(), {"mock": True})
                    first = client.post("/api/auth/wechat/mock")
                    self.assertEqual(first.status_code, 200)
                    self.assertIn("HttpOnly", first.headers["set-cookie"])
                    self.assertEqual(client.get("/api/auth/session").json()["user"], first.json()["user"])
                    self.assertEqual(client.post("/api/auth/wechat/mock").json()["user"]["id"], first.json()["user"]["id"])
                    self.assertEqual(client.post("/api/auth/logout").status_code, 204)
                    self.assertEqual(client.get("/api/auth/session").status_code, 401)
                    self.assertEqual(client.post("/api/auth/wechat/mock", headers={"origin": "https://example.com"}).status_code, 403)
                    self.assertEqual(client.post("http://example.com/api/auth/wechat/mock").status_code, 403)
                    auth.env["NODE_ENV"] = "production"
                    self.assertEqual(client.post("/api/auth/wechat/mock").status_code, 403)
                    auth.env.pop("NODE_ENV")
                    auth.env["WECHAT_MOCK_LOGIN"] = "false"
                    self.assertEqual(client.post("/api/auth/wechat/mock").status_code, 403)
                    self.assertEqual(client.get("/api/auth/wechat/authorize").status_code, 503)
                    auth.env.update(WECHAT_APP_ID="test", WECHAT_APP_SECRET="test", WECHAT_REDIRECT_URI="https://example.com/callback")
                    self.assertIn("https://open.weixin.qq.com/connect/qrconnect?", client.get("/api/auth/wechat/authorize").json()["authorizationUrl"])
            finally:
                auth.db.close()
