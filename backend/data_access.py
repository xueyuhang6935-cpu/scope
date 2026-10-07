"""File-data migration and resource checks; creator IDs remain audit identities."""
from fastapi import Request
from .common import APIError
from .business_access import current_scope


def workspace():
    scope=current_scope.get()
    if not scope:
        raise APIError(403,'缺少业务工作空间。')
    return scope['workspace']


def migrate(data,access):
    with data.lock:
        if data.one("SELECT 1 FROM business_migrations WHERE key='file-workspaces-v1'"):
            return
        data.db.execute('BEGIN IMMEDIATE')
        try:
            for table in ('import_drafts','import_batches','unit_rules','material_rules'):
                if 'workspace_id' not in {r['name'] for r in data.all(f'PRAGMA table_info({table})')}:
                    data.db.execute(f"ALTER TABLE {table} ADD COLUMN workspace_id TEXT NOT NULL DEFAULT ''")
            owners=data.all('SELECT DISTINCT user_id FROM import_drafts UNION SELECT DISTINCT user_id FROM import_batches')
            spaces=[]
            for owner in owners:
                uid=owner['user_id']
                c=data.one('SELECT c.* FROM risk_monitor_configs c JOIN database_connections d ON d.user_id=c.user_id AND d.scenario_id=c.scenario_id WHERE c.user_id=?',(uid,))
                key=c['workspace_id'] if c else 'local-'+str(uid)
                access.ensure_space(key,uid,c['scenario_id'] if c else '本地数据空间')
                spaces.append(key)
                for table in ('import_drafts','import_batches'):
                    data.db.execute(f'UPDATE {table} SET workspace_id=? WHERE user_id=?',(key,uid))
            data.db.execute('ALTER TABLE current_records RENAME TO current_records_old')
            data.db.execute('CREATE TABLE current_records(domain TEXT NOT NULL,factory TEXT NOT NULL,business_key TEXT NOT NULL,source_record_id INTEGER NOT NULL REFERENCES source_records(id),workspace_id TEXT NOT NULL,PRIMARY KEY(workspace_id,domain,factory,business_key))')
            data.db.execute('INSERT INTO current_records SELECT c.*,b.workspace_id FROM current_records_old c JOIN source_records r ON r.id=c.source_record_id JOIN import_batches b ON b.id=r.batch_id')
            data.db.execute('DROP TABLE current_records_old')
            # Formerly global rules are preserved as legacy evidence; existing
            # data-owning workspaces receive independent copies of those rules.
            for table in ('unit_rules','material_rules'):
                rows=data.all(f'SELECT * FROM {table}')
                for row in rows:
                    for key in spaces:
                        fields=[k for k in row if k not in ('id','version','workspace_id')]
                        version=data.one(f'SELECT coalesce(max(version),0)+1 AS n FROM {table}')['n']
                        data.db.execute(f"INSERT INTO {table}({','.join(fields)},version,workspace_id) VALUES({','.join('?' for _ in range(len(fields)+2))})",(*[row[k] for k in fields],version,key))
            data.db.execute("INSERT INTO business_migrations VALUES('file-workspaces-v1')")
            data.db.execute('COMMIT')
        except Exception:
            data.db.execute('ROLLBACK')
            raise


class DataGuard:
    def __init__(self,data,access):
        self.data,self.access=data,access

    def check(self,row,write=False):
        scope=current_scope.get()
        if not row or row['workspace_id']!=scope['workspace']:
            raise APIError(404,'对象不存在或不在当前工作空间。')
        self.access.require(scope['user'],scope['workspace'],'data.write' if write else 'data.read',[row['factory']],domain=row.get('domain','rules'))
        return row

    def visible(self,rows):
        result=[]
        for row in rows:
            try:
                self.check(row)
                result.append(row)
            except APIError:
                pass
        return result

    def batch(self,batch_id,write=False):
        return self.check(self.data.one('SELECT * FROM import_batches WHERE id=?',(batch_id,)),write)

    def issue(self,issue_id,write=False):
        issue=self.data.one('SELECT batch_id FROM quality_issues WHERE id=?',(issue_id,))
        if not issue:
            raise APIError(404,'质量问题不存在。')
        return self.batch(issue['batch_id'],write)

    async def route(self,request:Request):
        # Multipart fields are checked by preview after parsing, before persistence.
        scope=current_scope.get()
        action='data.write' if request.method=='POST' else 'data.read'
        self.access.require(scope['user'],scope['workspace'],action)
        p=request.path_params
        if p.get('draft_id'):
            self.check(self.data.one('SELECT * FROM import_drafts WHERE id=?',(p['draft_id'],)),True)
        if p.get('batch_id'):
            self.batch(p['batch_id'],request.method=='POST')
        if p.get('issue_id'):
            batch=self.issue(p['issue_id'],request.method=='POST')
            if request.url.path.endswith('/repair'):
                self.access.require(scope['user'],scope['workspace'],'data.write',[batch['factory']],domain='rules')
        if request.method=='POST' and request.url.path.endswith(('/governance/rules','/governance/material-rules')):
            self.access.require(scope['user'],scope['workspace'],'data.write',[request.state.body.get('factory')],domain='rules')
