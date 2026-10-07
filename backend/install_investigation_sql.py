"""Install scoped views and a separate login. Run with --local for the bundled database."""
import argparse
import json
import os
from pathlib import Path
import secrets

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from dotenv import dotenv_values, set_key

from project.tools.investigation_sql import TABLES, SCHEMA, ROLE

CONTEXT = "NULLIF(current_setting('scope.agent_context', true), '')::jsonb"


def predicate(table, alias, factories, depth=0):
    """Resolve indirect ownership using source FKs, never a model-supplied WHERE."""
    base = f"{alias}.scenario_id = ({CONTEXT}->>'scenario')"
    if table == 'commitment_changes':
        base += f" AND {alias}.workspace_id = ({CONTEXT}->>'workspace') AND {alias}.known_at <= ({CONTEXT}->>'asOf')::timestamptz"
    else:
        base += f" AND {alias}.ingested_at <= ({CONTEXT}->>'asOf')::timestamptz"
    owner = TABLES[table][2]

    def parent(column, target):
        ref = f'p{depth + 1}'
        condition = predicate(target, ref, factories, depth + 1)
        return f'EXISTS (SELECT 1 FROM supply_demo.{target} {ref} WHERE {ref}.scenario_id={alias}.scenario_id AND {ref}.id={alias}.{column} AND {condition})'

    if isinstance(owner, tuple):
        base += ' AND ' + parent(*owner)
    elif owner == 'shipment':
        base += ' AND (' + parent('purchase_line_id', 'purchase_order_lines') + ' OR ' + parent('execution_id', 'execution_orders') + ')'
    elif owner == 'route':
        base += f' AND ({factories} ? {alias}.from_factory) AND ({factories} ? {alias}.to_factory)'
    elif owner:
        base += f' AND ({factories} ? {alias}.{owner})'
    if table == 'execution_orders':
        base += f' AND ({alias}.from_factory IS NULL OR {factories} ? {alias}.from_factory)'
    if table == 'receipt_lines':
        base += ' AND ' + parent('warehouse_id', 'warehouses')
    if table == 'line_deliveries':
        base += ' AND ' + parent('line_id', 'production_lines')
    if table == 'supplier_commitments':
        base += f" AND {alias}.known_at <= ({CONTEXT}->>'asOf')::timestamptz AND {alias}.changed_at <= ({CONTEXT}->>'asOf')::timestamptz"
    return base


def install(admin_dsn, app_dsn, password):
    app = conninfo_to_dict(app_dsn)
    with psycopg.connect(admin_dsn) as db:
        owner = app['user']
        if not db.execute('SELECT 1 FROM pg_roles WHERE rolname=%s', (ROLE,)).fetchone():
            db.execute(sql.SQL('CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS').format(sql.Identifier(ROLE)))
        memberships = db.execute('SELECT 1 FROM pg_auth_members WHERE member=(SELECT oid FROM pg_roles WHERE rolname=%s)', (ROLE,)).fetchone()
        if memberships:
            raise ValueError('专用查询角色已有成员关系，请先由管理员审查。')
        db.execute(sql.SQL('ALTER ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS PASSWORD {}').format(sql.Identifier(ROLE), sql.Literal(password)))
        db.execute(sql.SQL('ALTER ROLE {} SET default_transaction_read_only = on').format(sql.Identifier(ROLE)))
        db.execute(sql.SQL('ALTER ROLE {} SET statement_timeout = \'3s\'').format(sql.Identifier(ROLE)))
        db.execute(sql.SQL('CREATE SCHEMA IF NOT EXISTS {} AUTHORIZATION {}').format(sql.Identifier(SCHEMA), sql.Identifier(owner)))
        db.execute(sql.SQL('REVOKE ALL ON SCHEMA {} FROM PUBLIC').format(sql.Identifier(SCHEMA)))
        for source in ('supply_demo', 'sc'):
            db.execute(sql.SQL('REVOKE ALL ON ALL TABLES IN SCHEMA {} FROM {}').format(sql.Identifier(source), sql.Identifier(ROLE)))
            db.execute(sql.SQL('REVOKE ALL ON SCHEMA {} FROM {}').format(sql.Identifier(source), sql.Identifier(ROLE)))
        for table, (domain, _, _) in TABLES.items():
            factories = f"({CONTEXT}->'domains'->'{domain}')"
            source = 'sc' if table == 'commitment_changes' else 'supply_demo'
            condition = f"jsonb_array_length({factories}) > 0 AND " + predicate(table, 'r', factories)
            db.execute(f'CREATE OR REPLACE VIEW {SCHEMA}.{table} WITH (security_barrier=true) AS SELECT r.* FROM {source}.{table} r WHERE {condition}')
            db.execute(sql.SQL('ALTER VIEW {}.{} OWNER TO {}').format(sql.Identifier(SCHEMA), sql.Identifier(table), sql.Identifier(owner)))
            db.execute(sql.SQL('REVOKE ALL ON {}.{} FROM PUBLIC').format(sql.Identifier(SCHEMA), sql.Identifier(table)))
            db.execute(sql.SQL('GRANT SELECT ON {}.{} TO {}').format(sql.Identifier(SCHEMA), sql.Identifier(table), sql.Identifier(ROLE)))
        db.execute(sql.SQL('GRANT USAGE ON SCHEMA {} TO {}').format(sql.Identifier(SCHEMA), sql.Identifier(ROLE)))
    return make_conninfo(app_dsn, user=ROLE, password=password)


def main():
    parser = argparse.ArgumentParser(description='安装 Agent 专用只读角色及授权视图')
    parser.add_argument('--local', action='store_true', help='使用当前项目本地服务管理员配置，并更新本地 .env')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    env = {**dotenv_values(root / '.env'), **os.environ}
    app_dsn = env['ANALYST_DATABASE_URL']
    if args.local:
        app = conninfo_to_dict(app_dsn)
        if app.get('host') != '127.0.0.1' or app.get('port') != '55432' or app.get('dbname') != 'scope_analyst':
            raise ValueError('--local 仅适用于当前项目自带本机数据库。')
        local = json.loads((root / 'data/services/local-storage.json').read_text(encoding='utf-8'))
        admin_dsn = make_conninfo(app_dsn, user='postgres', password=local['pgAdmin'])
        existing = env.get('SUPPLY_AGENT_DATABASE_URL')
        password = conninfo_to_dict(existing).get('password') if existing else None
        dsn = install(admin_dsn, app_dsn, password or secrets.token_hex(24))
        set_key(str(root / '.env'), 'SUPPLY_AGENT_DATABASE_URL', dsn)
        print('已安装只读角色和授权视图，并配置本地查询连接；未修改模型启用开关。')
    else:
        install(env['SUPPLY_AGENT_ADMIN_DATABASE_URL'], app_dsn, env['SUPPLY_AGENT_READER_PASSWORD'])
        print('安装完成。请用专用角色的连接配置 SUPPLY_AGENT_DATABASE_URL。')


if __name__ == '__main__':
    main()
