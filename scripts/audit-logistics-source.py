"""T02 只读核查：不启动应用、不执行导入、不修改数据库或项目外文件。"""
import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
from functools import lru_cache
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess

from dotenv import dotenv_values
import psycopg
from psycopg import sql
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('lade_import_contract', ROOT / 'scripts/import-lade.py')
IMPORT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(IMPORT)  # 仅复用列顺序和摘要算法，不调用导入入口。
TIME_FIELDS = ['accept_time', 'time_window_start', 'time_window_end', 'pickup_time',
               'accept_gps_time', 'pickup_gps_time']
PERIODS = {'development': ('07-01 00:00:00', '07-15 00:00:00'),
           'evaluation': ('08-01 00:00:00', '08-15 00:00:00')}


@lru_cache(maxsize=100000)
def reference_time(value):
    if not re.fullmatch(r'\d{2}-\d{2} \d{2}:\d{2}:\d{2}', value):
        raise ValueError('参考时间格式不符')
    return datetime.fromisoformat('2000-' + value)


def self_check():
    assert reference_time('07-02 10:00:00') == datetime(2000, 7, 2, 10)
    for value in ('', '02-30 10:00:00', '7-02 10:00:00', '07-02 24:00:00'):
        try:
            reference_time(value)
        except ValueError:
            continue
        raise AssertionError('非法日期被接受')
    assert len(IMPORT.RAW) == 19 and len(set(IMPORT.COLS)) == len(IMPORT.COLS)


def role_profile(dsn):
    if not dsn:
        return {'configured': False}
    with psycopg.connect(dsn, connect_timeout=5, row_factory=dict_row,
                         options='-c default_transaction_read_only=on') as db:
        db.execute("SET LOCAL statement_timeout='15s'")
        profile = dict(db.execute('''SELECT current_user AS role, rolsuper, rolcreatedb,
            rolcreaterole, rolbypassrls FROM pg_roles WHERE rolname=current_user''').fetchone())
        profile['transaction_read_only'] = db.execute('SHOW transaction_read_only').fetchone()['transaction_read_only']
        profile['lade_usage'] = db.execute("SELECT has_schema_privilege(current_user,'lade','USAGE') AS allowed").fetchone()['allowed']
        profile['tables'] = list(db.execute('''SELECT c.relname AS name,
            has_table_privilege(current_user,c.oid,'SELECT') AS can_select,
            has_table_privilege(current_user,c.oid,'INSERT') AS can_insert,
            has_table_privilege(current_user,c.oid,'UPDATE') AS can_update,
            has_table_privilege(current_user,c.oid,'DELETE') AS can_delete
            FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='lade' AND c.relkind='r' ORDER BY c.relname'''))
        try:
            with db.transaction():
                db.execute('SELECT order_id FROM lade.pickup_tasks LIMIT 1').fetchone()
            profile['direct_source_read'] = 'allowed'
        except psycopg.Error as exc:
            profile['direct_source_read'] = {'denied_sqlstate': exc.sqlstate}
        return profile


def audit(output):
    env = {**dotenv_values(ROOT / '.env'), **os.environ}
    result = {'checked_at': datetime.now(timezone(timedelta(hours=8))).isoformat(),
              'git_head': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
              'script_sha256': IMPORT.file_hash(Path(__file__)), 'periods': PERIODS}
    output.mkdir(parents=True, exist_ok=False)
    def save():
        (output / 'source-audit.json').write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    try:
        result['application_role'] = role_profile(env.get('ANALYST_DATABASE_URL'))
        result['agent_role'] = role_profile(env.get('SUPPLY_AGENT_DATABASE_URL'))
        with psycopg.connect(env['ANALYST_DATABASE_URL'], connect_timeout=5, row_factory=dict_row,
                             options='-c default_transaction_read_only=on') as db:
            db.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            db.execute("SET LOCAL statement_timeout='180s'")
            db.execute("SET LOCAL lock_timeout='3s'")
            result['transaction_read_only'] = db.execute('SHOW transaction_read_only').fetchone()['transaction_read_only']
            result['columns'] = list(db.execute('''SELECT table_name,column_name,data_type,is_nullable
                FROM information_schema.columns WHERE table_schema='lade'
                ORDER BY table_name,ordinal_position'''))
            result['constraints'] = list(db.execute('''SELECT c.relname AS table_name,con.conname,
                pg_get_constraintdef(con.oid) AS definition FROM pg_constraint con
                JOIN pg_class c ON c.oid=con.conrelid JOIN pg_namespace n ON n.oid=c.relnamespace
                WHERE n.nspname='lade' ORDER BY c.relname,con.conname'''))
            result['indexes'] = list(db.execute("SELECT tablename,indexname,indexdef FROM pg_indexes WHERE schemaname='lade' ORDER BY tablename,indexname"))
            batches = list(db.execute('SELECT * FROM lade.import_batches ORDER BY batch_id'))
            result['batches'] = []
            actual_columns = [c for c in result['columns'] if c['table_name'] == 'pickup_tasks']
            result['schema_checks'] = {
                'column_order_matches': [c['column_name'] for c in actual_columns] == IMPORT.COLS,
                'raw_19_text_not_null': all(any(c['column_name'] == name and c['data_type'] == 'text' and c['is_nullable'] == 'NO' for c in actual_columns) for name in IMPORT.RAW),
                'no_stored_reference_time_columns': not any(c['column_name'].endswith('_reference') for c in actual_columns),
            }
            for batch in batches:
                item = {k: v for k, v in batch.items() if k not in ('field_mapping_markdown', 'audit_summary', 'source_file', 'evidence_sha256')}
                result['batches'].append(item)
                item['mapping_sha256'] = hashlib.sha256(batch['field_mapping_markdown'].encode()).hexdigest()
                source = Path(batch['source_file'])
                item['source_file'] = {'exists': source.is_file()}
                if source.is_file():
                    item['source_file'].update(bytes=source.stat().st_size, sha256=IMPORT.file_hash(source))
                    item['source_file']['matches_import'] = (item['source_file']['sha256'] == batch['source_sha256'] and source.stat().st_size == batch['source_bytes'])
                item['audit_files'] = {}
                for relative, expected in batch['evidence_sha256'].items():
                    path = ROOT.parent / relative
                    observed = IMPORT.file_hash(path) if path.is_file() else None
                    item['audit_files'][relative] = {'expected_sha256': expected, 'actual_sha256': observed, 'matches': observed == expected}
                item['import_script_sha256'] = IMPORT.file_hash(ROOT / 'scripts/import-lade.py')
                item['keys'] = dict(db.execute('''SELECT count(*) AS rows,count(DISTINCT order_id) AS distinct_order_ids,
                    count(DISTINCT (city,order_id)) AS distinct_business_keys,
                    min(source_row) AS first_source_row,max(source_row) AS last_source_row
                    FROM lade.pickup_tasks WHERE batch_id=%s''', (batch['batch_id'],)).fetchone())
                item['quality_rules'] = list(db.execute('SELECT code,description,affected_rows,action FROM lade.quality_rules WHERE batch_id=%s ORDER BY code', (batch['batch_id'],)))
                item['region5_support'] = {}
                for name, (start, end) in PERIODS.items():
                    # 仅检验案例支持，不导出冻结评价的逐题答案或任务清单。
                    moment = start[:3] + '02 10:00:00'
                    near_end = start[:3] + '02 10:30:00'
                    item['region5_support'][name] = dict(db.execute('''SELECT
                        count(DISTINCT courier_id) AS couriers,count(DISTINCT aoi_id) AS aois,
                        bool_or(accept_time<=%(t)s AND pickup_time>%(t)s) AS has_pending,
                        bool_or(accept_time<=%(t)s AND pickup_time>%(t)s AND time_window_end<%(t)s) AS has_overdue,
                        bool_or(accept_time<=%(t)s AND pickup_time>%(t)s AND time_window_end BETWEEN %(t)s AND %(h)s) AS has_due_soon,
                        count(*) FILTER (WHERE accept_gps_time='' OR pickup_gps_time='') AS missing_event_gps,
                        count(*) FILTER (WHERE accept_spatial_review_required OR pickup_spatial_review_required) AS spatial_review
                        FROM lade.pickup_tasks WHERE batch_id=%(batch)s AND region_id='5'
                        AND accept_time<%(end)s AND pickup_time>=%(start)s''',
                        {'batch': batch['batch_id'], 'start': start, 'end': end, 't': moment, 'h': near_end}).fetchone())
                times = {f: {'empty': 0, 'invalid': 0, 'min': None, 'max': None} for f in TIME_FIELDS}
                regions, cities, empties, flags, checks = Counter(), Counter(), Counter(), Counter(), Counter()
                coverage = {p: {'accepted_by_day_region': Counter(), 'completed_by_day_region': Counter(),
                                'lifecycle_overlap_by_region': Counter(), 'features': Counter()} for p in PERIODS}
                cross_split = []
                digest, rows = hashlib.sha256(), 0
                statement = sql.SQL('SELECT {} FROM lade.pickup_tasks WHERE batch_id=%s ORDER BY source_row').format(sql.SQL(',').join(map(sql.Identifier, IMPORT.COLS)))
                with db.cursor(name='t02_readonly') as cur:
                    cur.itersize = 10000
                    cur.execute(statement, (batch['batch_id'],))
                    for row in cur:
                        IMPORT.digest_row(digest, [row[c] for c in IMPORT.COLS])
                        rows += 1
                        cities[row['city']] += 1
                        regions[row['region_id']] += 1
                        empties.update(c for c in IMPORT.RAW if row[c] == '')
                        flags.update(row['quality_flags'] or [])
                        parsed = {}
                        for field in TIME_FIELDS:
                            value = row[field]
                            if not value:
                                times[field]['empty'] += 1
                                continue
                            try:
                                parsed[field] = reference_time(value)
                            except ValueError:
                                times[field]['invalid'] += 1
                                continue
                            times[field]['min'] = min(times[field]['min'] or value, value)
                            times[field]['max'] = max(times[field]['max'] or value, value)
                        a, p = parsed.get('accept_time'), parsed.get('pickup_time')
                        w0, w1 = parsed.get('time_window_start'), parsed.get('time_window_end')
                        checks['pickup_before_accept'] += bool(a and p and p < a)
                        checks['window_not_positive'] += bool(w0 and w1 and w1 <= w0)
                        checks['ds_invalid_or_mismatch'] += not (re.fullmatch(r'\d{4}', row['ds']) and p and row['ds'] == p.strftime('%m%d'))
                        for event in ('accept', 'pickup'):
                            gps = parsed.get(event + '_gps_time')
                            actual = parsed.get(event + '_time')
                            checks[event + '_gps_after_event'] += bool(gps and actual and gps > actual)
                        memberships = []
                        for name, (start, end) in PERIODS.items():
                            target = coverage[name]
                            if a and start <= row['accept_time'] < end:
                                target['accepted_by_day_region'][row['accept_time'][:5] + '/' + row['region_id']] += 1
                            if p and start <= row['pickup_time'] < end:
                                target['completed_by_day_region'][row['pickup_time'][:5] + '/' + row['region_id']] += 1
                            if a and p and row['accept_time'] < end and row['pickup_time'] >= start:
                                memberships.append(name)
                                target['lifecycle_overlap_by_region'][row['region_id']] += 1
                                target['features']['cross_day'] += a.date() != p.date()
                                target['features']['missing_event_gps'] += not row['accept_gps_time'] or not row['pickup_gps_time']
                                target['features']['zero_duration'] += a == p
                                target['features']['accept_after_window'] += bool(w1 and a > w1)
                        if len(memberships) > 1:
                            cross_split.append({'city': row['city'], 'order_id': row['order_id'], 'source_row': row['source_row'], 'region_id': row['region_id']})
                        if rows % 250000 == 0:
                            print(f'只读核查已读 {rows:,} 行', flush=True)
                item.update(observed_rows=rows, observed_content_sha256=digest.hexdigest(),
                            content_matches_import=digest.hexdigest() == batch['content_sha256'],
                            cities=dict(cities), regions=dict(regions), empty_fields=dict(empties),
                            times=times, temporal_checks=dict(checks), quality_flag_counts=dict(flags),
                            coverage=coverage, cross_split_tasks=cross_split)
                item['checks'] = {
                    'row_count_matches': rows == batch['row_count'],
                    'unique_business_key': rows == item['keys']['distinct_business_keys'],
                    'source_rows_contiguous': item['keys']['first_source_row'] == 2 and item['keys']['last_source_row'] == rows + 1,
                    'core_times_valid': all(times[f]['empty'] == times[f]['invalid'] == 0 for f in TIME_FIELDS[:4]),
                    'rule_counts_match': all(flags[r['code']] == r['affected_rows'] for r in item['quality_rules']) and not (set(flags) - {r['code'] for r in item['quality_rules']}),
                }
            valid = bool(result['batches']) and all(result['schema_checks'].values())
            for item in result['batches']:
                valid = valid and all(item['checks'].values()) and item['content_matches_import']
                valid = valid and (not item['source_file']['exists'] or item['source_file']['matches_import'])
                valid = valid and all(v['matches'] for v in item['audit_files'].values())
                valid = valid and all(item['temporal_checks'][k] == 0 for k in ('pickup_before_accept', 'window_not_positive', 'ds_invalid_or_mismatch'))
            result['status'] = 'completed' if valid else 'failed_validation'
    except Exception as exc:
        result.update(status='failed', error_type=type(exc).__name__)  # 不保存连接串或原始异常正文。
        save()
        raise RuntimeError('只读核查失败，见脱敏结果中的 error_type') from None
    save()
    if result['status'] != 'completed':
        raise RuntimeError('契约核查不一致，见脱敏结果')
    print('核查完成：' + str(output / 'source-audit.json'))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--self-check', action='store_true')
    args = parser.parse_args()
    self_check()
    if args.self_check:
        print('参考时间解析及字段契约自检通过')
    else:
        audit(args.output or ROOT / 'test-results/logistics-t02' / datetime.now(timezone(timedelta(hours=8))).strftime('%Y%m%d-%H%M%S'))
