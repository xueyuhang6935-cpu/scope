import hashlib
import hmac
import re
import secrets
import smtplib
import sqlite3
import threading
import time
from email.message import EmailMessage
from urllib.parse import unquote, urlencode, urlsplit

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response

from .common import APIError, digest, reply

COOKIE = "scope_session"
SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,account TEXT NOT NULL UNIQUE,type TEXT NOT NULL,name TEXT NOT NULL,password_hash TEXT,created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS verification_codes(account TEXT NOT NULL,purpose TEXT NOT NULL,code_hash TEXT NOT NULL,expires_at INTEGER NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(account,purpose));
CREATE TABLE IF NOT EXISTS code_sends(account TEXT NOT NULL,sent_at INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS code_sends_account_time ON code_sends(account,sent_at);
CREATE TABLE IF NOT EXISTS login_attempts(account TEXT NOT NULL,attempted_at INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS login_attempts_account_time ON login_attempts(account,attempted_at);
CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY,user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,expires_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS oauth_states(state_hash TEXT PRIMARY KEY,expires_at INTEGER NOT NULL);
"""


def now():
    return int(time.time() * 1000)


def password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    value = hashlib.scrypt(password.encode(), salt=salt.encode(), n=16384, r=8, p=1, dklen=64)
    return salt + ":" + value.hex()


def valid_password(password, stored):
    try:
        salt, expected = stored.split(":")
        return len(bytes.fromhex(expected)) == 64 and hmac.compare_digest(password_hash(password, salt), stored)
    except (AttributeError, ValueError):
        return False


def valid_account(account, kind):
    pattern = r"(1[3-9]\d{9}|\+\d{8,15})" if kind == "phone" else r"[^\s@]+@[^\s@]+\.[^\s@]+" if kind == "email" else None
    return bool(pattern and re.fullmatch(pattern, account, flags=re.ASCII))


def public_user(user):
    return {"id": str(user["id"]), **{k: user[k] for k in ("name", "account", "type")}}


class Auth:
    def __init__(self, path, env):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.lock = threading.RLock()
        self.env = env
        if env.get("NODE_ENV") == "production" and not env.get("AUTH_SECRET"):
            raise RuntimeError("AUTH_SECRET must be configured in production.")
        self.secret = env.get("AUTH_SECRET") or secrets.token_hex(32)

    def query(self, sql, args=(), all=False):
        with self.lock:
            cursor = self.db.execute(sql, args)
            if not cursor.description:
                return None
            rows = cursor.fetchall()
            return [dict(r) for r in rows] if all else (dict(rows[0]) if rows else None)

    def user(self, request):
        token = request.cookies.get(COOKIE)
        return self.query("SELECT users.* FROM sessions JOIN users ON users.id=sessions.user_id WHERE token_hash=? AND expires_at>?", (digest(token), now())) if token else None

    def session(self, response, user):
        token = secrets.token_urlsafe(32)
        self.query("INSERT INTO sessions VALUES(?,?,?)", (digest(token), user["id"], now() + 604800000))
        response.set_cookie(COOKIE, token, max_age=604800, httponly=True, secure=self.env.get("NODE_ENV") == "production", samesite="none" if self.env.get("AUTH_CROSS_SITE_COOKIE") == "true" else "lax", path="/")

    def code_hash(self, account, purpose, code):
        return hmac.new(self.secret.encode(), f"{account}:{purpose}:{code}".encode(), hashlib.sha256).hexdigest()

    def consume(self, account, purpose, code):
        with self.lock:
            row = self.query("SELECT * FROM verification_codes WHERE account=? AND purpose=?", (account, purpose))
            if not row or row["expires_at"] <= now() or row["attempts"] >= 5:
                return False
            self.query("UPDATE verification_codes SET attempts=attempts+1 WHERE account=? AND purpose=?", (account, purpose))
            if not hmac.compare_digest(row["code_hash"], self.code_hash(account, purpose, code)):
                return False
            self.query("DELETE FROM verification_codes WHERE account=? AND purpose=?", (account, purpose))
            return True

    def deliver(self, account, kind, code):
        env = self.env
        if kind == "email":
            if not env.get("SMTP_URL") or not env.get("SMTP_FROM"):
                raise ValueError("邮件服务未配置。")
            url = urlsplit(env["SMTP_URL"])
            password = env.get("SMTP_PASSWORD") or unquote(url.password or "")
            if url.username and not password:
                raise ValueError("邮件服务未配置：缺少发件邮箱授权码。")
            message = EmailMessage()
            message["From"], message["To"], message["Subject"] = env["SMTP_FROM"], account, "Scope 验证码"
            message.set_content(f"您的 Scope 验证码是 {code}，5 分钟内有效。")
            cls = smtplib.SMTP_SSL if url.scheme == "smtps" else smtplib.SMTP
            with cls(url.hostname, url.port or (465 if url.scheme == "smtps" else 587), timeout=30) as smtp:
                smtp.ehlo()
                if url.scheme != "smtps" and smtp.has_extn("starttls"):
                    smtp.starttls()
                    smtp.ehlo()
                if url.username:
                    smtp.login(unquote(url.username), password)
                if smtp.send_message(message):
                    raise ValueError("邮件发送失败。")
            return
        sid, token, sender = (env.get(k) for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM"))
        if not all((sid, token, sender)):
            raise ValueError("短信服务未配置。")
        response = httpx.post(f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json", auth=(sid, token), data={"To": account if account.startswith("+") else "+86" + account, "From": sender, "Body": f"Scope 验证码：{code}，5 分钟内有效。"}, timeout=30)
        response.raise_for_status()


def routes(auth):
    router = APIRouter(prefix="/api/auth")

    @router.post("/verification-codes")
    def verification(request: Request):
        b = request.state.body
        account, kind, purpose = str(b.get("account") or "").strip().lower(), b.get("type"), b.get("purpose")
        if not valid_account(account, kind) or purpose not in ("register", "reset-password"):
            raise APIError(400, "账号或验证码用途无效。")
        with auth.lock:
            user = auth.query("SELECT id FROM users WHERE account=?", (account,))
            if purpose == "register" and user:
                raise APIError(409, "该账号已注册。")
            if purpose == "reset-password" and not user:
                raise APIError(404, "该账号尚未注册。")
            recent = auth.query("SELECT MAX(sent_at) AS last_sent,COUNT(*) AS count FROM code_sends WHERE account=? AND sent_at>?", (account, now() - 3600000))
            if (recent["last_sent"] or 0) > now() - 60000 or recent["count"] >= 5:
                raise APIError(429, "发送过于频繁，请稍后重试。")
            code = f"{secrets.randbelow(1000000):06}"
            try:
                auth.deliver(account, kind, code)
            except Exception as exc:
                raise APIError(503, str(exc) if "未配置" in str(exc) else "验证码发送失败，请稍后重试。") from exc
            auth.query("INSERT INTO verification_codes VALUES(?,?,?,?,0) ON CONFLICT(account,purpose) DO UPDATE SET code_hash=excluded.code_hash,expires_at=excluded.expires_at,attempts=0", (account, purpose, auth.code_hash(account, purpose, code), now() + 300000))
            auth.query("INSERT INTO code_sends VALUES(?,?)", (account, now()))
        return {"message": "验证码已发送。"}

    @router.post("/register")
    def register(request: Request):
        b = request.state.body
        account, kind = str(b.get("account") or "").strip().lower(), b.get("type")
        code, password = b.get("code"), b.get("password")
        if not valid_account(account, kind) or not isinstance(code, str) or not re.fullmatch(r"[0-9]{6}", code) or not isinstance(password, str) or not 8 <= len(password) <= 128:
            raise APIError(400, "请填写有效的账号、6 位验证码和 8 至 128 位密码。")
        if auth.query("SELECT id FROM users WHERE account=?", (account,)):
            raise APIError(409, "该账号已注册。")
        if not auth.consume(account, "register", code):
            raise APIError(400, "验证码错误或已过期。")
        try:
            auth.query("INSERT INTO users(account,type,name,password_hash,created_at) VALUES(?,?,?,?,?)", (account, kind, account, password_hash(password), now()))
        except sqlite3.IntegrityError:
            raise APIError(409, "该账号已注册。")
        return reply({"message": "注册成功。"}, 201)

    @router.post("/login")
    def login(request: Request):
        b = request.state.body
        account, password = str(b.get("account") or "").strip().lower(), b.get("password")
        if not account or not isinstance(password, str) or len(password) > 128:
            raise APIError(400, "请输入有效的账号和密码。")
        with auth.lock:
            attempts = auth.query("SELECT COUNT(*) AS count FROM login_attempts WHERE account=? AND attempted_at>?", (account, now() - 900000))
            if attempts["count"] >= 10:
                raise APIError(429, "登录尝试过于频繁，请稍后重试。")
            user = auth.query("SELECT * FROM users WHERE account=?", (account,))
            if not user or not valid_password(password, user["password_hash"]):
                auth.query("INSERT INTO login_attempts VALUES(?,?)", (account, now()))
                raise APIError(401, "账号或密码错误。")
            auth.query("DELETE FROM login_attempts WHERE account=?", (account,))
            response = reply({"user": public_user(user)})
            auth.session(response, user)
            return response

    @router.post("/reset-password")
    def reset(request: Request):
        b = request.state.body
        phone, code, password = str(b.get("phone") or "").strip(), b.get("code"), b.get("password")
        if not valid_account(phone, "phone") or not isinstance(code, str) or not re.fullmatch(r"[0-9]{6}", code) or not isinstance(password, str) or not 8 <= len(password) <= 128:
            raise APIError(400, "请填写有效的手机号、6 位验证码和 8 至 128 位密码。")
        user = auth.query("SELECT id FROM users WHERE account=? AND type='phone'", (phone,))
        if not user or not auth.consume(phone, "reset-password", code):
            raise APIError(400, "验证码错误或已过期。")
        auth.query("UPDATE users SET password_hash=? WHERE id=?", (password_hash(password), user["id"]))
        auth.query("DELETE FROM sessions WHERE user_id=?", (user["id"],))
        return {"message": "密码已重置。"}

    @router.get("/session")
    def session(request: Request):
        user = auth.user(request)
        if not user:
            raise APIError(401, "未登录。")
        return {"user": public_user(user)}

    @router.post("/logout")
    def logout(request: Request):
        token = request.cookies.get(COOKIE)
        if token:
            auth.query("DELETE FROM sessions WHERE token_hash=?", (digest(token),))
        response = Response(status_code=204)
        response.delete_cookie(COOKIE, path="/")
        return response

    def mock_allowed(request):
        local_hosts = {"localhost", "127.0.0.1", "::1"}
        origin = request.headers.get("origin")
        return (auth.env.get("WECHAT_MOCK_LOGIN") == "true"
                and auth.env.get("NODE_ENV") != "production"
                and request.url.hostname in local_hosts
                and (not origin or urlsplit(origin).hostname in local_hosts))

    @router.post("/wechat/mock")
    def mock_login(request: Request):
        if not mock_allowed(request):
            raise APIError(403, "本地模拟登录未启用或当前访问地址不是本机。")
        account = "mock:wechat:local"
        auth.query("INSERT INTO users(account,type,name,password_hash,created_at) VALUES(?,'wechat',?,NULL,?) ON CONFLICT(account) DO NOTHING", (account, "本地微信测试用户", now()))
        user = auth.query("SELECT * FROM users WHERE account=?", (account,))
        response = reply({"user": public_user(user)})
        auth.session(response, user)
        return response

    @router.get("/wechat/authorize")
    def authorize(request: Request):
        env = auth.env
        if mock_allowed(request):
            return {"mock": True}
        if not all(env.get(k) for k in ("WECHAT_APP_ID", "WECHAT_APP_SECRET", "WECHAT_REDIRECT_URI")):
            raise APIError(503, "微信登录未配置。")
        state = secrets.token_urlsafe(32)
        auth.query("INSERT INTO oauth_states VALUES(?,?)", (digest(state), now() + 600000))
        params = {"appid": env["WECHAT_APP_ID"], "redirect_uri": env["WECHAT_REDIRECT_URI"], "response_type": "code", "scope": "snsapi_login", "state": state}
        return {"authorizationUrl": "https://open.weixin.qq.com/connect/qrconnect?" + urlencode(params) + "#wechat_redirect"}

    @router.get("/wechat/callback")
    def callback(request: Request):
        code, state = request.query_params.get("code"), request.query_params.get("state")
        if code is None or state is None:
            raise APIError(400, "微信授权失败。")
        with auth.lock:
            saved = auth.query("SELECT expires_at FROM oauth_states WHERE state_hash=?", (digest(state),))
            auth.query("DELETE FROM oauth_states WHERE state_hash=?", (digest(state),))
        if not saved or saved["expires_at"] <= now():
            raise APIError(400, "微信授权已过期，请重试。")
        try:
            with httpx.Client(timeout=30) as client:
                token = client.get("https://api.weixin.qq.com/sns/oauth2/access_token", params={"appid": auth.env.get("WECHAT_APP_ID"), "secret": auth.env.get("WECHAT_APP_SECRET"), "code": code, "grant_type": "authorization_code"}).json()
                if not token.get("access_token") or not token.get("openid"):
                    raise ValueError()
                profile = client.get("https://api.weixin.qq.com/sns/userinfo", params={"access_token": token["access_token"], "openid": token["openid"], "lang": "zh_CN"}).json()
                if profile.get("errcode"):
                    raise ValueError()
            account = "wechat:" + (token.get("unionid") or token["openid"])
            auth.query("INSERT INTO users(account,type,name,password_hash,created_at) VALUES(?,'wechat',?,NULL,?) ON CONFLICT(account) DO UPDATE SET name=excluded.name", (account, profile.get("nickname") or "微信用户", now()))
            response = RedirectResponse(auth.env.get("FRONTEND_ORIGIN") or "/", status_code=302)
            auth.session(response, auth.query("SELECT * FROM users WHERE account=?", (account,)))
            return response
        except Exception as exc:
            raise APIError(502, "微信登录失败。") from exc

    return router
