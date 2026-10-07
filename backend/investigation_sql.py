"""Backend assembly for the dedicated read-only SQL connection."""
from contextlib import contextmanager
import json

import psycopg
from psycopg.rows import dict_row

from project.tools.investigation_sql import (
    SCHEMA, ROLE, TABLES, FUNCTIONS, NODES, CAST_TYPES, validate_sql,
    DatabaseQueryError, InvestigationSQL as SQLTool,
)


@contextmanager
def restricted_connection(env, scope):
    try:
        dsn = env.get('SUPPLY_AGENT_DATABASE_URL')
        if not dsn:
            raise ValueError('尚未配置专用只读查询连接 SUPPLY_AGENT_DATABASE_URL。')
        with psycopg.connect(dsn, connect_timeout=3, row_factory=dict_row) as db:
            db.execute('SET TRANSACTION READ ONLY')
            db.execute("SET LOCAL statement_timeout = '3s'")
            db.execute("SET LOCAL lock_timeout = '1s'")
            db.execute("SET LOCAL search_path = pg_catalog, agent_read")
            role = db.execute('SELECT current_user AS name, rolsuper, rolcreaterole, rolcreatedb, rolbypassrls FROM pg_roles WHERE rolname=current_user').fetchone()
            if role['name'] != ROLE or any(role[k] for k in ('rolsuper', 'rolcreaterole', 'rolcreatedb', 'rolbypassrls')):
                raise ValueError('查询连接必须使用受限的 scope_agent_reader 角色。')
            db.execute("SELECT set_config('scope.agent_context', %s, true)", (json.dumps(scope),))
            yield db
    except psycopg.Error as exc:
        raise DatabaseQueryError(exc.sqlstate) from None


class InvestigationSQL(SQLTool):
    """Compatibility constructor; environment and driver remain in the backend."""

    def __init__(self, env, authorize):
        super().__init__(lambda scope: restricted_connection(env, scope), authorize)
