"""仅供后端快照构建使用的专用只读源连接。"""
from contextlib import contextmanager
from datetime import datetime
import re

import psycopg
from psycopg.rows import dict_row

from .common import APIError

ROLE = 'scope_logistics_source_reader'
RAW_FIELDS = ('order_id region_id city courier_id accept_time time_window_start '
              'time_window_end lng lat aoi_id aoi_type pickup_time pickup_gps_time '
              'pickup_gps_lng pickup_gps_lat accept_gps_time accept_gps_lng accept_gps_lat ds').split()


def reference_time(value, field, *, business=False, optional=False):
    if optional and value in ('', None):
        return None
    pattern = r'2000-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}' if business else r'[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}'
    try:
        if not isinstance(value, str) or not re.fullmatch(pattern, value):
            raise ValueError()
        return datetime.fromisoformat(value if business else '2000-' + value)
    except ValueError:
        # 错误不包含源值，避免未来事件或连接信息进入异常正文/日志。
        raise APIError(400 if business else 409, f'{field} 日期格式或参考时间无效。') from None


@contextmanager
def source_connection(env):
    dsn = env.get('LOGISTICS_SOURCE_DATABASE_URL')
    if not dsn:
        raise APIError(503, '未配置物流后端专用只读源连接。')
    try:
        with psycopg.connect(dsn, connect_timeout=5, row_factory=dict_row) as db:
            db.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            db.execute("SET LOCAL statement_timeout='30s'")
            db.execute("SET LOCAL lock_timeout='3s'")
            profile = db.execute('''SELECT current_user AS role, rolsuper, rolcreatedb,
                rolcreaterole, rolbypassrls,
                EXISTS(SELECT 1 FROM pg_auth_members WHERE member=pg_roles.oid) AS memberships,
                has_schema_privilege(current_user,'lade','CREATE') AS source_create,
                EXISTS(SELECT 1 FROM pg_namespace WHERE nspname NOT LIKE 'pg_%'
                    AND nspname <> 'information_schema'
                    AND has_schema_privilege(current_user,oid,'CREATE')) AS schema_create,
                has_database_privilege(current_user,current_database(),'CREATE') AS database_create
                FROM pg_roles WHERE rolname=current_user''').fetchone()
            unsafe = profile['role'] != ROLE or any(profile[k] for k in (
                'rolsuper', 'rolcreatedb', 'rolcreaterole', 'rolbypassrls', 'memberships', 'source_create', 'schema_create', 'database_create'))
            tables = db.execute('''SELECT c.relname,
                has_table_privilege(current_user,c.oid,'SELECT') AS readable,
                has_table_privilege(current_user,c.oid,'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') AS writable
                FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                WHERE n.nspname='lade' AND c.relkind='r' ''').fetchall()
            required = {'pickup_tasks', 'import_batches'}
            if unsafe or any(t['writable'] for t in tables) or not required.issubset({t['relname'] for t in tables if t['readable']}):
                raise APIError(503, '物流源账号不满足专用最小只读权限要求。')
            yield db
    except psycopg.Error:
        raise APIError(503, '物流源连接不可用，请检查服务或专用只读权限。') from None


class LogisticsRepository:
    def __init__(self, env):
        self.env = env

    @contextmanager
    def read(self, batch_id, city, regions):
        with source_connection(self.env) as db:
            batch = db.execute('''SELECT batch_id,source_sha256,content_sha256,
                reference_year,actual_year,year_status FROM lade.import_batches
                WHERE batch_id=%s AND verified_at IS NOT NULL''', (batch_id,)).fetchone()
            if (not batch or batch['reference_year'] != 2000 or batch['actual_year'] is not None
                    or batch['year_status'] != 'undisclosed'
                    or any(not re.fullmatch('[0-9a-f]{64}', batch[k] or '') for k in ('source_sha256', 'content_sha256'))):
                raise APIError(409, '批次未核验或参考时间、版本元数据无效。')
            # 不使用完成日期、事后资格或质量标签过滤。服务严格解析后按接单时间选取。
            with db.cursor(name='logistics_snapshot_source') as cursor:
                cursor.execute('SELECT batch_id,source_file,source_row,' + ','.join(RAW_FIELDS) +
                               ' FROM lade.pickup_tasks WHERE batch_id=%s AND city=%s AND region_id=ANY(%s)',
                               (batch_id, city, regions))
                yield batch, cursor
