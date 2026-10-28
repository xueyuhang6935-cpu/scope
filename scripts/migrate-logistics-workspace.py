"""备份后将本机旧模拟空间迁为唯一物流空间；必须停服并显式给出初始管理员。"""
import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import sys
import secrets

from dotenv import dotenv_values
import psycopg
from psycopg import sql

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.logistics_access import SCHEMA, WORKSPACE, NAME, REGIONS
from backend.auth import password_hash, now


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def migrate(admin, investigator, reviewer, apply):
    env = {**dotenv_values(ROOT / '.env'), **os.environ}
    paths = {'auth': Path(env.get('AUTH_DB_PATH') or ROOT / 'data/auth.sqlite').resolve(),
             'business': Path(env.get('DATA_DB_PATH') or ROOT / 'data/supply.sqlite').resolve()}
    paths['checkpoints'] = paths['business'].with_name('supply-graph.sqlite')
    for p in paths.values():
        if not p.is_relative_to(ROOT.resolve()):
            raise ValueError('仅支持当前项目内数据库；外部存储须单独制定迁移。')
    for host in ('127.0.0.1', '::1'):
        with socket.socket(socket.AF_INET6 if ':' in host else socket.AF_INET) as sock:
            sock.settimeout(1)
            if sock.connect_ex((host, int(env.get('AUTH_PORT') or 3002))) == 0:
                raise ValueError('请先停止当前后端（包括 IPv6 和重载子进程），避免迁移期间旧进程写入。')
    with sqlite3.connect(paths['auth'].as_uri() + '?mode=ro', uri=True) as auth:
        users = auth.execute('SELECT id,account FROM users ORDER BY id').fetchall()
    if admin not in {u[1] for u in users}:
        raise ValueError('管理员必须是已有注册账号。')
    if investigator not in {u[1] for u in users} or len({admin, investigator, reviewer}) != 3:
        raise ValueError('调查员必须已存在，三个账号必须不同。')
    if reviewer in {u[1] for u in users} or not reviewer.endswith('@scope.test'):
        raise ValueError('审核测试账号必须是新的 @scope.test 邮箱。')
    with psycopg.connect(env['ANALYST_DATABASE_URL'], options='-c default_transaction_read_only=on', connect_timeout=5) as pg:
        pg.execute("SET LOCAL statement_timeout='30s'")
        counts = {}
        for schema in ('sc', 'supply_demo'):
            for (table,) in pg.execute('SELECT tablename FROM pg_tables WHERE schemaname=%s ORDER BY tablename', (schema,)).fetchall():
                counts[schema + '.' + table] = pg.execute(sql.SQL('SELECT count(*) FROM {}.{}').format(sql.Identifier(schema), sql.Identifier(table))).fetchone()[0]
        if any(v for k, v in counts.items() if k != 'sc.schema_versions'):
            raise ValueError('PostgreSQL 仍有模拟记录，需先建立按空间删除及完整备份方案，不能直接清空。')
        source = pg.execute('SELECT batch_id,row_count,content_sha256 FROM lade.import_batches ORDER BY batch_id').fetchall()
        source_count = pg.execute('SELECT count(*) FROM lade.pickup_tasks').fetchone()[0]
    db = sqlite3.connect(paths['business'])
    db.row_factory = sqlite3.Row
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    old_spaces = [dict(r) for r in db.execute('SELECT * FROM business_spaces WHERE id<>?', (WORKSPACE,))]
    report = {'source_before': source, 'source_count_before': source_count, 'postgres_counts': counts,
              'old_spaces': old_spaces, 'user_count': len(users), 'admin': admin,
              'assignments': {admin: ['system_admin'], investigator: ['investigator', 'data_admin'], reviewer: ['approver']}, 'regions': REGIONS,
              'sqlite_counts_before': {t: db.execute('SELECT count(*) FROM "'+t+'"').fetchone()[0] for t in sorted(tables)}}
    for table in ('import_batches', 'import_drafts', 'source_records', 'current_records', 'unit_rules', 'material_rules'):
        if table in tables and report['sqlite_counts_before'][table]:
            raise ValueError('发现用户导入或治理数据，不能作为模拟记录直接删除：' + table)
    batches = {r[0] for r in db.execute('SELECT DISTINCT batch_id FROM database_source_connections')}
    if len(batches) > 1 or (batches and not batches.issubset({r[0] for r in source})):
        raise ValueError('已保存批次冲突或不存在，需先明确物流批次。')
    if not apply:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        db.close()
        return
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='logistics_grants'").fetchone() and db.execute('SELECT count(*) FROM logistics_grants').fetchone()[0]:
        raise ValueError('已经存在物流授权；迁移不会覆盖或恢复被撤销的角色。')
    stamp = datetime.now(timezone(timedelta(hours=8))).strftime('%Y%m%d-%H%M%S')
    backup = ROOT / 'data/backups' / ('single-logistics-' + stamp)
    backup.mkdir(parents=True, exist_ok=False)
    report['backups'] = {}
    for name, path in paths.items():
        if not path.exists():
            continue
        dest = backup / (name + '.sqlite')
        with sqlite3.connect(path.as_uri()+'?mode=ro', uri=True) as src, sqlite3.connect(dest) as target:
            src.backup(target)
            if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('备份完整性检查失败')
        report['backups'][name] = {'file': dest.name, 'sha256': sha(dest)}
    manifest = backup / 'migration.json'
    def save():
        manifest.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    report['status'] = 'backed_up'
    save()
    # 现有 PG 业务记录全空，不执行 PG 删除；保留源表及所有兼容表结构。
    db.executescript(SCHEMA)
    db.execute('ATTACH DATABASE ? AS accounts', (str(paths['auth']),))
    password = secrets.token_urlsafe(24)
    # 凭据先安全落盘，避免提交后进程中断造成新账号无法登录；路径已被 Git 忽略。
    credentials = backup / 'test-account-credentials.txt'
    credentials.write_text('测试账号1：本地微信登录（原账号）\n测试账号2：' + reviewer + '\n密码：' + password + '\n', encoding='utf-8')
    if paths['checkpoints'].exists():
        db.execute('ATTACH DATABASE ? AS checkpoints', (str(paths['checkpoints']),))
    cleared = ('supply_tasks', 'supply_threads', 'investigation_tool_calls', 'investigation_stages',
               'risk_monitor_configs', 'database_connections', 'workflow_members', 'workflow_permission_history',
               'business_roles', 'business_members', 'business_policies', 'business_migrations', 'business_audit',
               'database_source_connections', 'business_spaces')
    try:
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('BEGIN IMMEDIATE')
        db.execute("INSERT INTO accounts.users(account,type,name,password_hash,created_at) VALUES(?,'email','测试账号2',?,?)", (reviewer, password_hash(password), now()))
        db.execute("UPDATE accounts.users SET name='测试账号1' WHERE account=?", (investigator,))
        for table in cleared:
            if table in tables:
                db.execute('DELETE FROM "'+table+'"')
        if paths['checkpoints'].exists():
            for (table,) in db.execute("SELECT name FROM checkpoints.sqlite_master WHERE type='table'").fetchall():
                if table in ('checkpoints', 'writes', 'checkpoint_blobs', 'checkpoint_writes'):
                    db.execute('DELETE FROM checkpoints."'+table+'"')
        owner = next(u[0] for u in users if u[1] == admin)
        db.execute('INSERT INTO business_spaces VALUES(?,?,?)', (WORKSPACE, owner, NAME))
        for _, account in users:
            db.execute('INSERT INTO business_members VALUES(?,?,1,1)', (WORKSPACE, account))
        db.execute('INSERT INTO business_members VALUES(?,?,1,1)', (WORKSPACE, reviewer))
        for account, roles in report['assignments'].items():
            for role in roles:
                db.execute('INSERT INTO logistics_grants VALUES(?,?,?,?,?)', (WORKSPACE, account, role, 'Shanghai', json.dumps([] if role == 'system_admin' else REGIONS)))
        if batches:
            db.execute('INSERT INTO database_source_connections VALUES(?,?,?)', (WORKSPACE, next(iter(batches)), datetime.now(timezone.utc).isoformat()))
        db.execute('INSERT INTO business_audit(workspace_id,actor,account,before_json,after_json,created_at) VALUES(?,?,?,?,?,?)',
                   (WORKSPACE, 'offline-explicit-migration', admin, json.dumps({'old_space_count': len(old_spaces)}), json.dumps({'assignments': report['assignments'], 'regions': REGIONS}), datetime.now(timezone.utc).isoformat()))
        db.commit()
        report['status'] = 'completed'
        report['workspace_count_after'] = db.execute('SELECT count(*) FROM business_spaces').fetchone()[0]
        report['cleared_counts_after'] = {t: db.execute('SELECT count(*) FROM "'+t+'"').fetchone()[0] for t in cleared if t in tables}
        report['integrity_after'] = db.execute('PRAGMA integrity_check').fetchone()[0]
    except Exception:
        db.rollback()
        report['status'] = 'failed_rolled_back'
        raise
    finally:
        save()
        db.close()
    print(json.dumps({'status': report['status'], 'backup': str(backup), 'workspace_count': report['workspace_count_after'], 'source_count': source_count}, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--admin', required=True)
    parser.add_argument('--investigator', required=True)
    parser.add_argument('--reviewer', required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    try:
        migrate(args.admin, args.investigator, args.reviewer, args.apply)
    except Exception as exc:
        print('迁移失败：' + (str(exc) if isinstance(exc, ValueError) else type(exc).__name__), file=sys.stderr)
        sys.exit(1)
