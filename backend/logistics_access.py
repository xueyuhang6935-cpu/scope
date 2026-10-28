"""单物流空间授权；新成员无默认业务权，旧工厂授权不参与判断。"""
import json
from urllib.parse import urlsplit

from fastapi import APIRouter, Request

from .common import APIError, iso
from .common import digest, reply
from .auth import COOKIE, public_user

WORKSPACE = 'shanghai-logistics'
NAME = '上海物流运营空间'
REGIONS = '0 5 6 8 12 15 17 19 20 21 23 24 26 28 30 31 35 36 39 42 43 47 49 53 54 56 59 61 62 67'.split()
ROLES = {'investigator': ('调查员', {'view', 'investigate'}),
         'approver': ('审核人', {'view', 'approve'}),
         'data_admin': ('数据管理员', {'view', 'source.read', 'connection'}),
         'system_admin': ('系统管理员', {'admin'})}
SCHEMA = '''
CREATE TABLE IF NOT EXISTS business_spaces(id TEXT PRIMARY KEY,owner_id INTEGER NOT NULL,name TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS business_members(workspace_id TEXT NOT NULL,account TEXT NOT NULL,enabled INTEGER NOT NULL,version INTEGER NOT NULL DEFAULT 1,PRIMARY KEY(workspace_id,account));
CREATE TABLE IF NOT EXISTS business_audit(id INTEGER PRIMARY KEY,workspace_id TEXT NOT NULL,actor TEXT NOT NULL,account TEXT NOT NULL,before_json TEXT NOT NULL,after_json TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS logistics_grants(workspace_id TEXT NOT NULL,account TEXT NOT NULL,role TEXT NOT NULL,city TEXT NOT NULL,regions TEXT NOT NULL,PRIMARY KEY(workspace_id,account,role));
CREATE TABLE IF NOT EXISTS database_source_connections(workspace_id TEXT PRIMARY KEY,batch_id TEXT NOT NULL,saved_at TEXT NOT NULL);
'''


class LogisticsAccess:
    def __init__(self, auth, data, env):
        self.auth, self.data, self.env = auth, data, env
        with data.lock:
            data.db.executescript(SCHEMA)
            data.db.execute('INSERT OR IGNORE INTO business_spaces VALUES(?,?,?)', (WORKSPACE, 0, NAME))
        self.sync()

    def sync(self):
        with self.data.lock:
            for user in self.auth.query('SELECT account FROM users', all=True):
                self.data.db.execute('INSERT OR IGNORE INTO business_members VALUES(?,?,1,1)', (WORKSPACE, user['account']))

    def user(self, request):
        user = self.auth.user(request)
        if not user:
            raise APIError(401, '请先登录。')
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            origin = request.headers.get('origin')
            allowed = {str(request.base_url).rstrip('/'), self.env.get('FRONTEND_ORIGIN', '')}
            parsed = urlsplit(origin or '')
            local = self.env.get('NODE_ENV') != 'production' and parsed.scheme in ('http', 'https') and parsed.hostname in ('localhost', '127.0.0.1', '::1')
            if origin and origin not in allowed and not local:
                raise APIError(403, '请求来源不被允许。')
        explicit = request.headers.get('X-Workspace-Id') or request.query_params.get('workspaceId')
        if explicit and explicit != WORKSPACE:
            raise APIError(403, '旧工作空间已停用，请刷新页面。')
        self.sync()
        member = self.member(user['account'])
        if not member or not member['enabled']:
            raise APIError(403, '物流空间成员已停用。')
        return user

    def member(self, account):
        member = self.data.one('SELECT * FROM business_members WHERE workspace_id=? AND account=?', (WORKSPACE, account))
        if member:
            member['roles'] = [{**g, 'regions': json.loads(g['regions'])} for g in self.data.all(
                'SELECT role,city,regions FROM logistics_grants WHERE workspace_id=? AND account=? ORDER BY role', (WORKSPACE, account))]
        return member

    def require(self, user, action, regions=(), city='Shanghai', requested_by=None):
        member = self.member(user['account'])
        if not member or not member['enabled']:
            raise APIError(403, '物流空间成员已停用。')
        if action == 'approve':
            if not isinstance(requested_by, str) or not requested_by:
                raise APIError(403, '审批必须核对已保存的发起人身份。')
            if requested_by == user['account']:
                raise APIError(403, '发起人不能审批自己的建议。')
        for grant in member['roles']:
            if action in ROLES.get(grant['role'], ('', set()))[1] and grant['city'] == city and set(regions).issubset(grant['regions']):
                return grant
        raise APIError(403, '当前角色或城市、区域范围不允许此操作。')

    def allowed_regions(self, user, action):
        self.require(user, action)
        return sorted({r for g in self.member(user['account'])['roles'] if action in ROLES[g['role']][1] for r in g['regions']})

    def save(self, user, body):
        with self.data.lock:
            self.require(user, 'admin')
            account, roles, enabled = body.get('account'), body.get('roles'), body.get('enabled')
            if not isinstance(account, str) or not self.auth.query('SELECT id FROM users WHERE account=?', (account,)):
                raise APIError(400, '请选择已注册账号。')
            if type(enabled) is not bool or not isinstance(roles, list) or len(roles) > 4:
                raise APIError(400, '成员状态或角色无效。')
            normalized = []
            for grant in roles:
                if not isinstance(grant, dict) or not isinstance(grant.get('role'), str) or grant['role'] not in ROLES or grant.get('city') != 'Shanghai':
                    raise APIError(400, '角色或城市无效。')
                regions = grant.get('regions')
                if not isinstance(regions, list) or any(not isinstance(r, str) or r not in REGIONS for r in regions):
                    raise APIError(400, '区域无效。')
                if grant['role'] != 'system_admin' and not regions:
                    raise APIError(400, '业务角色至少需要一个区域。')
                if grant['role'] == 'system_admin' and regions:
                    raise APIError(400, '系统管理角色不附带数据区域权限。')
                normalized.append({'role': grant['role'], 'city': 'Shanghai', 'regions': sorted(set(regions))})
            normalized.sort(key=lambda g: g['role'])
            if len({g['role'] for g in normalized}) != len(normalized):
                raise APIError(400, '角色不能重复。')
            old = self.member(account)
            if type(body.get('version')) is not int or body['version'] != (old['version'] if old else 0):
                raise APIError(409, '成员权限已变化，请刷新。')
            old_approval = [g for g in (old or {}).get('roles', []) if g['role'] == 'approver']
            new_approval = [g for g in normalized if g['role'] == 'approver']
            if account == user['account'] and new_approval and (new_approval != old_approval or (enabled and not old['enabled'])):
                raise APIError(403, '自己的审核授权须由另一位系统管理员设置。')
            remaining = self.data.one("SELECT count(*) n FROM logistics_grants g JOIN business_members m USING(workspace_id,account) WHERE g.workspace_id=? AND g.role='system_admin' AND m.enabled=1 AND g.account<>?", (WORKSPACE, account))['n']
            if not remaining and not (enabled and any(g['role'] == 'system_admin' for g in normalized)):
                raise APIError(409, '不能撤销最后一位启用的系统管理员。')
            self.data.db.execute('BEGIN IMMEDIATE')
            try:
                self.data.db.execute('INSERT INTO business_members VALUES(?,?,?,1) ON CONFLICT(workspace_id,account) DO UPDATE SET enabled=excluded.enabled,version=version+1', (WORKSPACE, account, int(enabled)))
                self.data.db.execute('DELETE FROM logistics_grants WHERE workspace_id=? AND account=?', (WORKSPACE, account))
                for g in normalized:
                    self.data.db.execute('INSERT INTO logistics_grants VALUES(?,?,?,?,?)', (WORKSPACE, account, g['role'], g['city'], json.dumps(g['regions'])))
                self.data.db.execute('INSERT INTO business_audit(workspace_id,actor,account,before_json,after_json,created_at) VALUES(?,?,?,?,?,?)',
                                     (WORKSPACE, user['account'], account, json.dumps(old, ensure_ascii=False), json.dumps(self.member(account), ensure_ascii=False), iso()))
                self.data.db.execute('COMMIT')
            except Exception:
                self.data.db.execute('ROLLBACK')
                raise
            return self.member(account)

    def routes(self):
        router = APIRouter(prefix='/api/logistics/access', tags=['物流角色与权限'])

        def test_switch_allowed(request):
            local = {'localhost', '127.0.0.1', '::1'}
            return (self.env.get('LOCAL_TEST_ACCOUNT_SWITCH') == 'true'
                    and self.env.get('NODE_ENV') != 'production'
                    and request.url.hostname in local
                    and request.client is not None and request.client.host in local
                    and (not request.headers.get('origin') or urlsplit(request.headers['origin']).hostname in local))

        test_accounts = {'mock:wechat:local': '测试账号1', 'reviewer@scope.test': '测试账号2'}

        @router.get('/test-accounts')
        def test_options(request: Request):
            self.user(request)
            if not test_switch_allowed(request):
                return {'enabled': False, 'accounts': []}
            with self.data.lock:
                accounts = []
                for account, name in test_accounts.items():
                    member = self.member(account)
                    if member and member['enabled'] and not any(g['role'] == 'system_admin' for g in member['roles']):
                        accounts.append({'account': account, 'name': name, 'roles': [ROLES[g['role']][0] for g in member['roles']]})
                return {'enabled': True, 'accounts': accounts}

        @router.post('/switch-test-account')
        def switch_test_account(request: Request):
            actor = self.user(request)
            if not test_switch_allowed(request):
                raise APIError(403, '仅本地显式开启的测试环境可切换测试账号。')
            account = request.state.body.get('account')
            if not isinstance(account, str) or account not in test_accounts:
                raise APIError(403, '只能切换指定测试账号；管理员须使用密码登录。')
            with self.data.lock:
                member = self.member(account)
                target = self.auth.query('SELECT * FROM users WHERE account=?', (account,))
                if not target or not member or not member['enabled'] or any(g['role'] == 'system_admin' for g in member['roles']):
                    raise APIError(403, '目标账号不可用于快捷切换。')
                response = reply({'user': public_user(target)})
                with self.auth.lock:
                    self.auth.db.execute('BEGIN IMMEDIATE')
                    try:
                        self.auth.session(response, target)
                        self.auth.query('DELETE FROM sessions WHERE token_hash=?', (digest(request.cookies[COOKIE]),))
                        self.auth.db.commit()
                    except Exception:
                        self.auth.db.rollback()
                        raise
                self.data.db.execute('INSERT INTO business_audit(workspace_id,actor,account,before_json,after_json,created_at) VALUES(?,?,?,?,?,?)',
                                     (WORKSPACE, actor['account'], account, json.dumps({'action':'local-test-switch'}), json.dumps({'account':account}), iso()))
                return response

        @router.get('/me')
        def me(request: Request):
            user = self.user(request)
            return {'account': user['account'], 'workspaceId': WORKSPACE, 'workspaceName': NAME,
                    **self.member(user['account']), 'roleCatalog': {k: v[0] for k, v in ROLES.items()}, 'regionCatalog': REGIONS}

        @router.get('/members')
        def members(request: Request):
            self.require(self.user(request), 'admin')
            return [self.member(u['account']) for u in self.auth.query('SELECT account FROM users ORDER BY id', all=True)]

        @router.post('/members')
        def save(request: Request):
            return self.save(self.user(request), request.state.body)

        @router.get('/audit')
        def audit(request: Request):
            self.require(self.user(request), 'admin')
            return self.data.all('SELECT * FROM business_audit WHERE workspace_id=? ORDER BY id DESC LIMIT 200', (WORKSPACE,))

        return router
