"""Workspace RBAC. Role scopes are evaluated independently, never cross-joined."""
import json
from contextvars import ContextVar
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Request
from .common import APIError, iso

current_scope = ContextVar('business_scope', default=None)
ROLES = {
    'planner': ('生产计划员', {'view', 'data.read', 'plan', 'risk.update', 'scan'}),
    'logistics': ('采购／物流人员', {'view', 'data.read', 'event.update'}),
    'approver': ('审批人', {'view', 'data.read', 'approve'}),
    'data_admin': ('数据管理员', {'view', 'data.read', 'data.write', 'connection'}),
    'system_admin': ('系统管理员', {'view', 'admin', 'monitor', 'policy'}),
    'viewer': ('只读成员', {'view', 'data.read'}),
}
DOMAINS = {'inventory', 'purchase', 'production', 'quality', 'rules'}


class BusinessAccess:
    def __init__(self, auth, data, monitor):
        self.auth, self.data, self.monitor = auth, data, monitor
        with data.lock:
            data.db.executescript('''
                CREATE TABLE IF NOT EXISTS business_spaces(id TEXT PRIMARY KEY,owner_id INTEGER NOT NULL,name TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS business_members(workspace_id TEXT NOT NULL,account TEXT NOT NULL,enabled INTEGER NOT NULL,version INTEGER NOT NULL DEFAULT 1,PRIMARY KEY(workspace_id,account));
                CREATE TABLE IF NOT EXISTS business_roles(workspace_id TEXT NOT NULL,account TEXT NOT NULL,role TEXT NOT NULL,factories TEXT NOT NULL,domains TEXT NOT NULL,approval_limit TEXT NOT NULL,PRIMARY KEY(workspace_id,account,role));
                CREATE TABLE IF NOT EXISTS business_migrations(key TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS business_audit(id INTEGER PRIMARY KEY,workspace_id TEXT NOT NULL,actor TEXT NOT NULL,account TEXT NOT NULL,before_json TEXT NOT NULL,after_json TEXT NOT NULL,created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS business_policies(workspace_id TEXT NOT NULL,action TEXT NOT NULL,max_quantity TEXT NOT NULL,max_cost TEXT NOT NULL,version INTEGER NOT NULL,PRIMARY KEY(workspace_id,action));
            ''')
        self.sync()

    def ensure_space(self, key, owner, name):
        with self.data.lock:
            if self.data.one('SELECT 1 FROM business_spaces WHERE id=?', (key,)):
                return
            account = self.auth.query('SELECT account FROM users WHERE id=?', (owner,))
            if not account:
                return
            self.data.db.execute('SAVEPOINT bootstrap_space')
            try:
                self.data.db.execute('INSERT INTO business_spaces VALUES(?,?,?)', (key,owner,name))
                self.data.db.execute('INSERT INTO business_members VALUES(?,?,1,1)', (key,account['account']))
                # Explicit bootstrap grants; ownership itself is never an authorization bypass.
                for role in ('system_admin','data_admin','planner'):
                    self.data.db.execute('INSERT INTO business_roles VALUES(?,?,?,?,?,?)', (key,account['account'],role,'["A","B"]',json.dumps(sorted(DOMAINS)),'0'))
                self.data.db.execute('RELEASE SAVEPOINT bootstrap_space')
            except Exception:
                self.data.db.execute('ROLLBACK TO SAVEPOINT bootstrap_space')
                self.data.db.execute('RELEASE SAVEPOINT bootstrap_space')
                raise

    def sync(self):
        with self.data.lock:
            for row in self.data.all("SELECT DISTINCT user_id FROM supply_threads WHERE workspace_id LIKE 'local-%'"):
                self.ensure_space('local-'+str(row['user_id']),row['user_id'],'本地数据空间')
            for c in self.data.all('SELECT * FROM risk_monitor_configs'):
                self.ensure_space(c['workspace_id'],c['user_id'],c['scenario_id'])
            for m in self.data.all('SELECT * FROM workflow_members'):
                marker = 'member:' + m['workspace_id'] + ':' + m['account']
                if self.data.one('SELECT 1 FROM business_migrations WHERE key=?',(marker,)):
                    continue
                if not self.data.one('SELECT 1 FROM business_spaces WHERE id=?',(m['workspace_id'],)):
                    continue
                self.data.db.execute('SAVEPOINT migrate_member')
                try:
                    self.data.db.execute('INSERT OR IGNORE INTO business_members VALUES(?,?,?,1)',(m['workspace_id'],m['account'],int(m['role']!='revoked')))
                    role = 'system_admin' if m['role']=='admin' else m['role']
                    if role in ROLES:
                        self.data.db.execute('INSERT OR IGNORE INTO business_roles VALUES(?,?,?,?,?,?)',(m['workspace_id'],m['account'],role,m['factories'],json.dumps(sorted(DOMAINS)),m['approval_limit']))
                    self.data.db.execute('INSERT INTO business_migrations VALUES(?)',(marker,))
                    self.data.db.execute('RELEASE SAVEPOINT migrate_member')
                except Exception:
                    self.data.db.execute('ROLLBACK TO SAVEPOINT migrate_member')
                    self.data.db.execute('RELEASE SAVEPOINT migrate_member')
                    raise

    def default_space(self,user):
        self.ensure_space('local-'+str(user['id']),user['id'],'本地数据空间')
        if self.data.one('SELECT 1 FROM database_connections WHERE user_id=?',(user['id'],)):
            try:
                c = self.monitor.config(user['id'])
            except APIError as exc:
                if exc.status != 409:
                    raise
                # 已删除的场景不能阻断数据接入页，允许从本地空间重新连接。
            else:
                self.ensure_space(c['workspace_id'],user['id'],c['scenario_id'])
                return c['workspace_id']
        key = 'local-' + str(user['id'])
        self.ensure_space(key,user['id'],'本地数据空间')
        return key

    def grants(self,user,workspace):
        self.sync()
        member = self.data.one('SELECT * FROM business_members WHERE workspace_id=? AND account=?',(workspace,user['account']))
        if not member or not member['enabled']:
            raise APIError(403,'未加入该工作空间或成员已停用。')
        return [{**r,'factories':json.loads(r['factories']),'domains':json.loads(r['domains'])} for r in self.data.all('SELECT * FROM business_roles WHERE workspace_id=? AND account=?',(workspace,user['account']))]

    def require(self,user,workspace,action='view',factories=(),domain=None,amount=None,event=None):
        matches=[]
        for g in self.grants(user,workspace):
            if action not in ROLES[g['role']][1] or not set(factories).issubset(g['factories']):
                continue
            if domain and domain not in g['domains']:
                continue
            if amount is not None and Decimal(str(amount)) > Decimal(g['approval_limit']):
                continue
            if action=='event.update' and (not event or event['owner'] != user['account']):
                continue
            matches.append(g)
        if not matches:
            raise APIError(403,'当前角色、工厂、数据域、责任关系或审批额度不允许此操作。')
        # Do not take an approval limit from a different scope/role.
        return max(matches,key=lambda g:Decimal(g['approval_limit']))

    def can(self,*args,**kwargs):
        try:
            self.require(*args,**kwargs)
            return True
        except APIError:
            return False

    def workspace(self,user):
        scope = current_scope.get()
        return scope['workspace'] if scope and scope['user']['id']==user['id'] else self.default_space(user)

    def request_scope(self,request):
        user = self.monitor.authorize(request)
        explicit=request.headers.get('X-Workspace-Id') or request.query_params.get('workspaceId')
        workspace = explicit or self.default_space(user)
        self.require(user,workspace)
        return {'user':user,'workspace':workspace,'explicit':bool(explicit)}

    def member(self,workspace,account):
        m=self.data.one('SELECT * FROM business_members WHERE workspace_id=? AND account=?',(workspace,account))
        if m:
            m['roles']=[{**r,'factories':json.loads(r['factories']),'domains':json.loads(r['domains'])} for r in self.data.all('SELECT role,factories,domains,approval_limit FROM business_roles WHERE workspace_id=? AND account=?',(workspace,account))]
        return m

    def save(self,user,workspace,body):
        with self.data.lock:
            self.require(user,workspace,'admin',['A','B'])
            account=body.get('account')
            if not isinstance(account,str) or not self.auth.query('SELECT id FROM users WHERE account=?',(account,)):
                raise APIError(400,'请填写已注册账号。')
            roles,enabled=body.get('roles'),body.get('enabled',True)
            if not isinstance(roles,list) or len(roles)>len(ROLES) or type(enabled) is not bool:
                raise APIError(400,'角色列表或成员状态无效。')
            normalized=[]
            for g in roles:
                if not isinstance(g,dict) or not isinstance(g.get('role'),str) or g['role'] not in ROLES:
                    raise APIError(400,'业务角色无效。')
                fs,ds=g.get('factories'),g.get('domains')
                if not isinstance(fs,list) or not fs or any(f not in ('A','B') for f in fs) or not isinstance(ds,list) or any(not isinstance(d,str) or d not in DOMAINS for d in ds):
                    raise APIError(400,'工厂或数据域范围无效。')
                if g['role']=='system_admin' and set(fs)!={'A','B'}:
                    raise APIError(400,'系统管理员须管理完整工作空间。')
                try:
                    limit=Decimal(str(g.get('approvalLimit',g.get('approval_limit','0'))))
                    if not limit.is_finite() or not 0<=limit<=1000000:
                        raise ValueError()
                except (ValueError,InvalidOperation):
                    raise APIError(400,'审批额度须为 0 至 1000000 的有限金额。') from None
                normalized.append({'role':g['role'],'factories':sorted(set(fs)),'domains':sorted(set(ds)),'approval_limit':str(limit) if g['role']=='approver' else '0'})
            if len({g['role'] for g in normalized})!=len(normalized):
                raise APIError(400,'角色不能重复。')
            old=self.member(workspace,account)
            if body.get('version') != (old['version'] if old else 0):
                raise APIError(409,'成员权限已变化，请刷新后重试。')
            remaining=self.data.one("SELECT count(*) AS n FROM business_roles r JOIN business_members m USING(workspace_id,account) WHERE r.workspace_id=? AND r.role='system_admin' AND m.enabled=1 AND r.account<>?",(workspace,account))['n']
            if not remaining and not (enabled and any(g['role']=='system_admin' for g in normalized)):
                raise APIError(409,'不能停用或撤销最后一位系统管理员。')
            # Administrators may delegate but cannot grant themselves business approval.
            if account==user['account'] and any(g['role']=='approver' for g in normalized) and normalized!= (old or {}).get('roles'):
                raise APIError(403,'自己的业务审批授权必须由另一位系统管理员设置。')
            self.data.db.execute('BEGIN IMMEDIATE')
            try:
                self.data.db.execute('INSERT INTO business_members VALUES(?,?,?,1) ON CONFLICT(workspace_id,account) DO UPDATE SET enabled=excluded.enabled,version=version+1',(workspace,account,int(enabled)))
                self.data.db.execute('DELETE FROM business_roles WHERE workspace_id=? AND account=?',(workspace,account))
                for g in normalized:
                    self.data.db.execute('INSERT INTO business_roles VALUES(?,?,?,?,?,?)',(workspace,account,g['role'],json.dumps(g['factories']),json.dumps(g['domains']),g['approval_limit']))
                self.data.db.execute('INSERT INTO business_audit(workspace_id,actor,account,before_json,after_json,created_at) VALUES(?,?,?,?,?,?)',(workspace,user['account'],account,json.dumps(old,ensure_ascii=False),json.dumps({'enabled':enabled,'roles':normalized},ensure_ascii=False),iso()))
                self.data.db.execute('COMMIT')
            except Exception:
                self.data.db.execute('ROLLBACK')
                raise
            return self.member(workspace,account)

    def routes(self):
        router=APIRouter(prefix='/api/supply/access',tags=['角色与权限'])

        @router.get('/me')
        def me(request:Request):
            user=self.monitor.authorize(request)
            self.default_space(user)
            self.sync()
            workspace=self.workspace(user)
            gs=self.grants(user,workspace)
            spaces=self.data.all('SELECT s.* FROM business_spaces s JOIN business_members m ON m.workspace_id=s.id WHERE m.account=? AND m.enabled=1 ORDER BY s.name,s.id',(user['account'],))
            capabilities={action:sorted({f for g in gs if action in ROLES[g['role']][1] for f in g['factories']}) for action in sorted({a for _,acts in ROLES.values() for a in acts})}
            return {'account':user['account'],'workspaceId':workspace,'workspaces':spaces,'roles':gs,'capabilities':capabilities,'roleCatalog':{k:v[0] for k,v in ROLES.items()},'domains':sorted(DOMAINS)}

        @router.get('/members')
        def members(request:Request):
            user=self.monitor.authorize(request); workspace=self.workspace(user)
            self.require(user,workspace,'admin',['A','B'])
            return [self.member(workspace,r['account']) for r in self.data.all('SELECT account FROM business_members WHERE workspace_id=? ORDER BY account',(workspace,))]

        @router.post('/members')
        def save(request:Request):
            user=self.monitor.authorize(request)
            return self.save(user,self.workspace(user),request.state.body)

        @router.get('/audit')
        def audit(request:Request):
            user=self.monitor.authorize(request); workspace=self.workspace(user)
            self.require(user,workspace,'admin',['A','B'])
            return self.data.all('SELECT * FROM business_audit WHERE workspace_id=? ORDER BY id DESC LIMIT 200',(workspace,))

        @router.get('/policies')
        def policies(request:Request):
            user=self.monitor.authorize(request); workspace=self.workspace(user)
            self.require(user,workspace,'policy')
            return self.data.all('SELECT * FROM business_policies WHERE workspace_id=? ORDER BY action',(workspace,))

        @router.post('/policies')
        def policy(request:Request):
            user=self.monitor.authorize(request); workspace=self.workspace(user)
            self.require(user,workspace,'policy',['A','B'])
            b=request.state.body
            try:
                q,c=Decimal(str(b.get('maxQuantity'))),Decimal(str(b.get('maxCost')))
                if b.get('action') not in ('transfer','expedite') or not q.is_finite() or not c.is_finite() or not 0<q<=1000000 or not 0<=c<=1000000:
                    raise ValueError()
            except (ValueError,InvalidOperation):
                raise APIError(400,'审批规则数量或金额无效。') from None
            with self.data.lock:
                old=self.data.one('SELECT * FROM business_policies WHERE workspace_id=? AND action=?',(workspace,b['action']))
                if b.get('version')!=(old['version'] if old else 0):
                    raise APIError(409,'审批规则版本已变化。')
                self.data.db.execute('BEGIN IMMEDIATE')
                try:
                    self.data.db.execute('INSERT INTO business_policies VALUES(?,?,?,?,1) ON CONFLICT(workspace_id,action) DO UPDATE SET max_quantity=excluded.max_quantity,max_cost=excluded.max_cost,version=version+1',(workspace,b['action'],str(q),str(c)))
                    self.data.db.execute('INSERT INTO business_audit(workspace_id,actor,account,before_json,after_json,created_at) VALUES(?,?,?,?,?,?)',(workspace,user['account'],'policy:'+b['action'],json.dumps(old),json.dumps(b),iso()))
                    self.data.db.execute('COMMIT')
                except Exception:
                    self.data.db.execute('ROLLBACK'); raise
            return {'saved':True}
        return router
