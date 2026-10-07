import dotenv from 'dotenv';
import express from 'express';
import { DatabaseSync } from 'node:sqlite';
import { randomBytes, createHash, createHmac, scrypt as scryptCallback, timingSafeEqual } from 'node:crypto';
import { promisify } from 'node:util';
import { mkdirSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import nodemailer from 'nodemailer';

const scrypt = promisify(scryptCallback);
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
dotenv.config({ path: path.join(root, '.env'), quiet: true });
const dbPath = path.resolve(process.env.AUTH_DB_PATH || path.join(root, 'data', 'auth.sqlite'));
mkdirSync(path.dirname(dbPath), { recursive: true });
const db = new DatabaseSync(dbPath);
db.exec(`
  PRAGMA journal_mode = WAL;
  PRAGMA foreign_keys = ON;
  CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY, account TEXT NOT NULL UNIQUE, type TEXT NOT NULL,
    name TEXT NOT NULL, password_hash TEXT, created_at INTEGER NOT NULL
  );
  CREATE TABLE IF NOT EXISTS verification_codes (
    account TEXT NOT NULL, purpose TEXT NOT NULL, code_hash TEXT NOT NULL,
    expires_at INTEGER NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (account, purpose)
  );
  CREATE TABLE IF NOT EXISTS code_sends (
    account TEXT NOT NULL, sent_at INTEGER NOT NULL
  );
  CREATE INDEX IF NOT EXISTS code_sends_account_time ON code_sends(account, sent_at);
  CREATE TABLE IF NOT EXISTS login_attempts (
    account TEXT NOT NULL, attempted_at INTEGER NOT NULL
  );
  CREATE INDEX IF NOT EXISTS login_attempts_account_time ON login_attempts(account, attempted_at);
  CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at INTEGER NOT NULL
  );
  CREATE TABLE IF NOT EXISTS oauth_states (
    state_hash TEXT PRIMARY KEY, expires_at INTEGER NOT NULL
  );
`);

const app = express();
app.disable('x-powered-by');
app.use(express.json({ limit: '16kb' }));
if (process.env.NODE_ENV === 'production' && !process.env.AUTH_SECRET) {
  throw new Error('AUTH_SECRET must be configured in production.');
}
const frontendOrigin = process.env.FRONTEND_ORIGIN;
if (frontendOrigin) {
  app.use('/api', (req, res, next) => {
    if (req.headers.origin === frontendOrigin) {
      res.setHeader('Access-Control-Allow-Origin', frontendOrigin);
      res.setHeader('Access-Control-Allow-Credentials', 'true');
      res.setHeader('Vary', 'Origin');
      res.setHeader('Access-Control-Allow-Headers', 'Content-Type');
      res.setHeader('Access-Control-Allow-Methods', 'GET, POST, OPTIONS');
      if (req.method === 'OPTIONS') return res.status(204).end();
    }
    next();
  });
}
const secret = process.env.AUTH_SECRET || randomBytes(32).toString('hex');
const digest = value => createHash('sha256').update(value).digest('hex');
const codeDigest = (account, purpose, code) => createHmac('sha256', secret).update(`${account}:${purpose}:${code}`).digest('hex');
const now = () => Date.now();
const publicUser = user => ({ id: String(user.id), name: user.name, account: user.account, type: user.type });
const fail = (res, status, message) => res.status(status).json({ message });
const cookieName = 'scope_session';

function parseCookies(req) {
  return Object.fromEntries((req.headers.cookie || '').split(';').map(part => part.trim().split(/=(.*)/s).slice(0, 2)).filter(pair => pair.length === 2));
}

function sessionUser(req) {
  const token = parseCookies(req)[cookieName];
  if (!token) return null;
  return db.prepare(`SELECT users.* FROM sessions JOIN users ON users.id = sessions.user_id
    WHERE sessions.token_hash = ? AND sessions.expires_at > ?`).get(digest(token), now()) || null;
}

function createSession(res, user) {
  const token = randomBytes(32).toString('base64url');
  const maxAge = 7 * 24 * 60 * 60 * 1000;
  db.prepare('INSERT INTO sessions (token_hash, user_id, expires_at) VALUES (?, ?, ?)')
    .run(digest(token), user.id, now() + maxAge);
  res.cookie(cookieName, token, {
    httpOnly: true, secure: process.env.NODE_ENV === 'production',
    sameSite: process.env.AUTH_CROSS_SITE_COOKIE === 'true' ? 'none' : 'lax', maxAge, path: '/',
  });
}

async function passwordHash(password) {
  const salt = randomBytes(16).toString('hex');
  const hash = await scrypt(password, salt, 64);
  return `${salt}:${hash.toString('hex')}`;
}

async function validPassword(password, stored) {
  if (!stored) return false;
  const [salt, expectedHex] = stored.split(':');
  const expected = Buffer.from(expectedHex, 'hex');
  if (expected.length !== 64) return false;
  const actual = await scrypt(password, salt, 64);
  return timingSafeEqual(actual, expected);
}

function validAccount(account, type) {
  return type === 'phone' ? /^(1[3-9]\d{9}|\+\d{8,15})$/.test(account) :
    type === 'email' ? /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(account) : false;
}

async function deliverCode(account, type, code) {
  if (type === 'email') {
    if (!process.env.SMTP_URL || !process.env.SMTP_FROM) throw new Error('邮件服务未配置。');
    const smtpUrl = new URL(process.env.SMTP_URL);
    if (process.env.SMTP_PASSWORD) smtpUrl.password = process.env.SMTP_PASSWORD;
    if (smtpUrl.username && !smtpUrl.password) throw new Error('邮件服务未配置：缺少发件邮箱授权码。');
    const mailer = nodemailer.createTransport(smtpUrl.toString());
    const delivery = await mailer.sendMail({ from: process.env.SMTP_FROM, to: account, subject: 'Scope 验证码', text: `您的 Scope 验证码是 ${code}，5 分钟内有效。` });
    if (!delivery.accepted?.length || delivery.rejected?.length) throw new Error('邮件发送失败。');
    return;
  }
  const { TWILIO_ACCOUNT_SID: sid, TWILIO_AUTH_TOKEN: token, TWILIO_FROM: from } = process.env;
  if (!sid || !token || !from) throw new Error('短信服务未配置。');
  const response = await fetch(`https://api.twilio.com/2010-04-01/Accounts/${sid}/Messages.json`, {
    method: 'POST',
    headers: { Authorization: `Basic ${Buffer.from(`${sid}:${token}`).toString('base64')}`, 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({ To: account.startsWith('+') ? account : `+86${account}`, From: from, Body: `Scope 验证码：${code}，5 分钟内有效。` }),
  });
  if (!response.ok) throw new Error('短信发送失败，请稍后重试。');
}

function consumeCode(account, purpose, code) {
  const row = db.prepare('SELECT * FROM verification_codes WHERE account = ? AND purpose = ?').get(account, purpose);
  if (!row || row.expires_at <= now() || row.attempts >= 5) return false;
  db.prepare('UPDATE verification_codes SET attempts = attempts + 1 WHERE account = ? AND purpose = ?').run(account, purpose);
  const expected = Buffer.from(row.code_hash, 'hex');
  const actual = Buffer.from(codeDigest(account, purpose, code), 'hex');
  if (!timingSafeEqual(expected, actual)) return false;
  db.prepare('DELETE FROM verification_codes WHERE account = ? AND purpose = ?').run(account, purpose);
  return true;
}

app.post('/api/auth/verification-codes', async (req, res) => {
  const { type, purpose } = req.body || {};
  const account = String(req.body?.account || '').trim().toLowerCase();
  if (!validAccount(account, type) || !['register', 'reset-password'].includes(purpose)) return fail(res, 400, '账号或验证码用途无效。');
  const user = db.prepare('SELECT id FROM users WHERE account = ?').get(account);
  if (purpose === 'register' && user) return fail(res, 409, '该账号已注册。');
  if (purpose === 'reset-password' && !user) return fail(res, 404, '该账号尚未注册。');
  const recent = db.prepare('SELECT MAX(sent_at) AS last_sent, COUNT(*) AS count FROM code_sends WHERE account = ? AND sent_at > ?')
    .get(account, now() - 60 * 60 * 1000);
  if (recent.last_sent > now() - 60 * 1000 || recent.count >= 5) return fail(res, 429, '发送过于频繁，请稍后重试。');
  const code = String(randomBytes(4).readUInt32BE() % 1000000).padStart(6, '0');
  try {
    await deliverCode(account, type, code);
    db.prepare(`INSERT INTO verification_codes (account, purpose, code_hash, expires_at, attempts) VALUES (?, ?, ?, ?, 0)
      ON CONFLICT(account, purpose) DO UPDATE SET code_hash = excluded.code_hash, expires_at = excluded.expires_at, attempts = 0`)
      .run(account, purpose, codeDigest(account, purpose, code), now() + 5 * 60 * 1000);
    db.prepare('INSERT INTO code_sends (account, sent_at) VALUES (?, ?)').run(account, now());
    res.json({ message: '验证码已发送。' });
  } catch (error) {
    fail(res, 503, error.message?.includes('未配置') ? error.message : '验证码发送失败，请稍后重试。');
  }
});

app.post('/api/auth/register', async (req, res) => {
  const { type, code, password } = req.body || {};
  const account = String(req.body?.account || '').trim().toLowerCase();
  if (!validAccount(account, type) || !/^\d{6}$/.test(code || '') || typeof password !== 'string' || password.length < 8 || password.length > 128) {
    return fail(res, 400, '请填写有效的账号、6 位验证码和 8 至 128 位密码。');
  }
  if (db.prepare('SELECT id FROM users WHERE account = ?').get(account)) return fail(res, 409, '该账号已注册。');
  if (!consumeCode(account, 'register', code)) return fail(res, 400, '验证码错误或已过期。');
  const hash = await passwordHash(password);
  try {
    db.prepare('INSERT INTO users (account, type, name, password_hash, created_at) VALUES (?, ?, ?, ?, ?)')
      .run(account, type, account, hash, now());
    res.status(201).json({ message: '注册成功。' });
  } catch {
    fail(res, 409, '该账号已注册。');
  }
});

app.post('/api/auth/login', async (req, res) => {
  const account = String(req.body?.account || '').trim().toLowerCase();
  const password = req.body?.password;
  if (!account || typeof password !== 'string' || password.length > 128) return fail(res, 400, '请输入有效的账号和密码。');
  const windowStart = now() - 15 * 60 * 1000;
  const attempts = db.prepare('SELECT COUNT(*) AS count FROM login_attempts WHERE account = ? AND attempted_at > ?').get(account, windowStart);
  if (attempts.count >= 10) return fail(res, 429, '登录尝试过于频繁，请稍后重试。');
  const user = db.prepare('SELECT * FROM users WHERE account = ?').get(account);
  if (!user || !await validPassword(password, user.password_hash)) {
    db.prepare('INSERT INTO login_attempts (account, attempted_at) VALUES (?, ?)').run(account, now());
    return fail(res, 401, '账号或密码错误。');
  }
  db.prepare('DELETE FROM login_attempts WHERE account = ?').run(account);
  createSession(res, user);
  res.json({ user: publicUser(user) });
});

app.post('/api/auth/reset-password', async (req, res) => {
  const phone = String(req.body?.phone || '').trim();
  const { code, password } = req.body || {};
  if (!validAccount(phone, 'phone') || !/^\d{6}$/.test(code || '') || typeof password !== 'string' || password.length < 8 || password.length > 128) {
    return fail(res, 400, '请填写有效的手机号、6 位验证码和 8 至 128 位密码。');
  }
  const user = db.prepare('SELECT id FROM users WHERE account = ? AND type = ?').get(phone, 'phone');
  if (!user || !consumeCode(phone, 'reset-password', code)) return fail(res, 400, '验证码错误或已过期。');
  db.prepare('UPDATE users SET password_hash = ? WHERE id = ?').run(await passwordHash(password), user.id);
  db.prepare('DELETE FROM sessions WHERE user_id = ?').run(user.id);
  res.json({ message: '密码已重置。' });
});

app.get('/api/auth/session', (req, res) => {
  const user = sessionUser(req);
  if (!user) return fail(res, 401, '未登录。');
  res.json({ user: publicUser(user) });
});

app.post('/api/auth/logout', (req, res) => {
  const token = parseCookies(req)[cookieName];
  if (token) db.prepare('DELETE FROM sessions WHERE token_hash = ?').run(digest(token));
  res.clearCookie(cookieName, { path: '/' });
  res.status(204).end();
});

app.get('/api/auth/wechat/authorize', (req, res) => {
  const { WECHAT_APP_ID: appId, WECHAT_APP_SECRET: appSecret, WECHAT_REDIRECT_URI: redirectUri } = process.env;
  if (!appId || !appSecret || !redirectUri) return fail(res, 503, '微信登录未配置。');
  const state = randomBytes(32).toString('base64url');
  db.prepare('INSERT INTO oauth_states (state_hash, expires_at) VALUES (?, ?)').run(digest(state), now() + 10 * 60 * 1000);
  const url = new URL('https://open.weixin.qq.com/connect/qrconnect');
  url.search = new URLSearchParams({ appid: appId, redirect_uri: redirectUri, response_type: 'code', scope: 'snsapi_login', state }).toString();
  res.json({ authorizationUrl: `${url}#wechat_redirect` });
});

app.get('/api/auth/wechat/callback', async (req, res) => {
  const { code, state } = req.query;
  if (typeof code !== 'string' || typeof state !== 'string') return fail(res, 400, '微信授权失败。');
  const saved = db.prepare('SELECT expires_at FROM oauth_states WHERE state_hash = ?').get(digest(state));
  db.prepare('DELETE FROM oauth_states WHERE state_hash = ?').run(digest(state));
  if (!saved || saved.expires_at <= now()) return fail(res, 400, '微信授权已过期，请重试。');
  try {
    const tokenUrl = new URL('https://api.weixin.qq.com/sns/oauth2/access_token');
    tokenUrl.search = new URLSearchParams({ appid: process.env.WECHAT_APP_ID, secret: process.env.WECHAT_APP_SECRET, code, grant_type: 'authorization_code' }).toString();
    const token = await (await fetch(tokenUrl)).json();
    if (!token.access_token || !token.openid) throw new Error('微信授权失败。');
    const profileUrl = new URL('https://api.weixin.qq.com/sns/userinfo');
    profileUrl.search = new URLSearchParams({ access_token: token.access_token, openid: token.openid, lang: 'zh_CN' }).toString();
    const profile = await (await fetch(profileUrl)).json();
    if (profile.errcode) throw new Error('微信用户信息获取失败。');
    const account = `wechat:${token.unionid || token.openid}`;
    db.prepare(`INSERT INTO users (account, type, name, password_hash, created_at) VALUES (?, 'wechat', ?, NULL, ?)
      ON CONFLICT(account) DO UPDATE SET name = excluded.name`).run(account, profile.nickname || '微信用户', now());
    const user = db.prepare('SELECT * FROM users WHERE account = ?').get(account);
    createSession(res, user);
    res.redirect(frontendOrigin || '/');
  } catch (error) {
    fail(res, 502, error.message || '微信登录失败。');
  }
});

app.get('/api/health', (_req, res) => {
  db.prepare('SELECT 1').get();
  res.json({ status: 'ok', backend: 'express', database: 'sqlite', workspace: 'supply-chain' });
});
app.use('/api', (_req, res) => fail(res, 404, '接口不存在。'));
app.use(express.static(path.join(root, 'dist')));
app.get('*', (_req, res) => res.sendFile(path.join(root, 'dist', 'index.html')));

const port = Number(process.env.AUTH_PORT || 3002);
app.listen(port, () => console.log(`Scope server: http://localhost:${port}`));
