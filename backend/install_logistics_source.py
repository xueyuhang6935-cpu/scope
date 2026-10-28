"""安装后端专用源读取角色；不修改源表内容，不授权 Agent。"""
import argparse
import json
import os
from pathlib import Path
import secrets

from dotenv import dotenv_values, set_key
import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from .logistics_repository import ROLE, source_connection


def install(admin_dsn, app_dsn, password):
    with psycopg.connect(admin_dsn) as db:
        if db.execute('SELECT 1 FROM pg_auth_members WHERE member=(SELECT oid FROM pg_roles WHERE rolname=%s)', (ROLE,)).fetchone():
            raise ValueError('源读取角色已有成员关系，请先审查，安装未提交。')
        if not db.execute('SELECT 1 FROM pg_roles WHERE rolname=%s', (ROLE,)).fetchone():
            db.execute(sql.SQL('CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS').format(sql.Identifier(ROLE)))
        db.execute(sql.SQL('ALTER ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS PASSWORD {}').format(sql.Identifier(ROLE), sql.Literal(password)))
        db.execute(sql.SQL('ALTER ROLE {} SET default_transaction_read_only=on').format(sql.Identifier(ROLE)))
        db.execute(sql.SQL('ALTER ROLE {} SET statement_timeout=\'30s\'').format(sql.Identifier(ROLE)))
        db.execute(sql.SQL('REVOKE ALL ON ALL TABLES IN SCHEMA lade FROM {}').format(sql.Identifier(ROLE)))
        db.execute(sql.SQL('REVOKE ALL ON SCHEMA lade FROM {}').format(sql.Identifier(ROLE)))
        db.execute(sql.SQL('GRANT USAGE ON SCHEMA lade TO {}').format(sql.Identifier(ROLE)))
        db.execute(sql.SQL('GRANT SELECT ON lade.pickup_tasks,lade.import_batches TO {}').format(sql.Identifier(ROLE)))
    return make_conninfo(app_dsn, user=ROLE, password=password)


def main():
    parser = argparse.ArgumentParser(description='安装物流后端最小只读源账号')
    parser.add_argument('--local', action='store_true', help='仅使用项目自带本机数据库，并追加本机 .env 配置')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    env = {**dotenv_values(root / '.env'), **os.environ}
    app_dsn = env['ANALYST_DATABASE_URL']
    if args.local:
        app = conninfo_to_dict(app_dsn)
        if app.get('host') != '127.0.0.1' or app.get('port') != '55432' or app.get('dbname') != 'scope_analyst':
            raise ValueError('--local 仅适用于项目自带本机数据库。')
        settings = json.loads((root / 'data/services/local-storage.json').read_text(encoding='utf-8'))
        admin = make_conninfo(app_dsn, user='postgres', password=settings['pgAdmin'])
        existing = env.get('LOGISTICS_SOURCE_DATABASE_URL')
        password = conninfo_to_dict(existing).get('password') if existing else None
        dsn = install(admin, app_dsn, password or secrets.token_hex(24))
        with source_connection({'LOGISTICS_SOURCE_DATABASE_URL': dsn}):
            pass
        set_key(str(root / '.env'), 'LOGISTICS_SOURCE_DATABASE_URL', dsn)
        print('物流专用只读源账号已安装并核验，本机连接已配置；源记录未修改。')
    else:
        dsn = install(env['LOGISTICS_SOURCE_ADMIN_DATABASE_URL'], app_dsn, env['LOGISTICS_SOURCE_READER_PASSWORD'])
        with source_connection({'LOGISTICS_SOURCE_DATABASE_URL': dsn}):
            pass
        print('专用只读角色已安装并核验，请配置 LOGISTICS_SOURCE_DATABASE_URL。')


if __name__ == '__main__':
    main()
