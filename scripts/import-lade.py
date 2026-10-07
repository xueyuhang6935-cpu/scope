"""将已审计的 LaDe-P 原始任务与逐行质量判定原子导入 PostgreSQL。"""
import argparse
import csv
import gzip
import hashlib
import itertools
import json
import sys
from collections import Counter
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb
from dotenv import dotenv_values

PROJECT = Path(__file__).resolve().parents[1]
ROOT = PROJECT.parent
SOURCE = ROOT / 'data/pickup_sh.csv'
AUDIT = ROOT / 'outputs/lade_quality_20261006'
REPORT = AUDIT / 'PostgreSQL导入核验.json'
RAW = 'order_id region_id city courier_id accept_time time_window_start time_window_end lng lat aoi_id aoi_type pickup_time pickup_gps_time pickup_gps_lng pickup_gps_lat accept_gps_time accept_gps_lng accept_gps_lat ds'.split()
QUALITY = {
    'eligible_duration': 'boolean', 'eligible_sla': 'boolean', 'eligible_lifecycle': 'boolean',
    'eligible_stop_spatial': 'boolean', 'eligible_accept_spatial': 'boolean', 'eligible_pickup_spatial': 'boolean',
    'accept_gps_distance_km': 'numeric', 'accept_spatial_review_required': 'boolean',
    'pickup_gps_distance_km': 'numeric', 'pickup_spatial_review_required': 'boolean',
    'year_status': 'text', 'quality_flags': 'text[]', 'duration_minutes': 'numeric',
    'late_minutes': 'numeric', 'is_late': 'boolean',
}
COLS = ['batch_id', 'source_file', 'source_row'] + RAW + list(QUALITY)


def check(condition, message):
    if not condition:
        raise ValueError(message)


def file_hash(path):
    with path.open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def quality_value(name, value):
    kind = QUALITY[name]
    if kind == 'boolean':
        check(value in ('True', 'False', ''), '质量布尔值不合法：' + name)
        return {'True': True, 'False': False, '': None}[value]
    if kind == 'numeric':
        return Decimal(value) if value else None
    if kind == 'text[]':
        return value.split(';') if value else []
    return value


def digest_row(digest, row):
    # 类型规范化后有序摘要，避免 Decimal 的 117.0/117 展示差异。
    values = [format(v.normalize(), 'f') if isinstance(v, Decimal) else v for v in row]
    digest.update(json.dumps(values, ensure_ascii=False, separators=(',', ':')).encode('utf-8') + b'\n')


def source_rows(batch_id):
    with SOURCE.open(encoding='utf-8-sig', newline='') as f, gzip.open(
            AUDIT / '全量记录判定.csv.gz', 'rt', encoding='utf-8-sig', newline='') as q:
        raw, quality = csv.DictReader(f), csv.DictReader(q)
        check(raw.fieldnames == RAW, '原始字段结构变化，需重新审计')
        check(set(quality.fieldnames) == set(['source_file', 'source_row', 'order_id', 'city', 'region_id', 'courier_id'] + list(QUALITY)), '质量字段结构变化')
        for line, (r, a) in enumerate(itertools.zip_longest(raw, quality), 2):
            check(r is not None and a is not None, '原始文件与审计文件行数不一致')
            check(None not in r and all(v is not None for v in r.values()), 'CSV列数不一致')
            check(int(a['source_row']) == line and a['source_file'] == 'data/pickup_sh.csv', '来源行号不一致')
            check(all(r[c] == a[c] for c in ['order_id', 'city', 'region_id', 'courier_id']), '质量判定与任务不匹配')
            yield [batch_id, a['source_file'], line] + [r[c] for c in RAW] + [quality_value(c, a[c]) for c in QUALITY]


def main(verify_only=False):
    summary = json.loads((AUDIT / '审计摘要.json').read_text(encoding='utf-8'))
    source_sha = file_hash(SOURCE)
    expected_sha = next(x['sha256'] for x in summary['input_files'] if Path(x['path']).name == SOURCE.name)
    check(source_sha == expected_sha, '原始文件与质量审计版本不符，拒绝导入')
    batch_id = 'pickup_sh_' + source_sha
    evidence_paths = [AUDIT / name for name in ['全量记录判定.csv.gz', '质量规则统计.csv', '审计摘要.json', '字段映射.md', 'audit_lade.py']]
    hashes = {str(p.relative_to(ROOT)): file_hash(p) for p in evidence_paths}
    with (AUDIT / '质量规则统计.csv').open(encoding='utf-8-sig', newline='') as f:
        rules = list(csv.DictReader(f))
    mapping_text = (AUDIT / '字段映射.md').read_text(encoding='utf-8')
    audit_digest = hashlib.sha256()
    dsn = dotenv_values(PROJECT / '.env')['ANALYST_DATABASE_URL']
    selected_columns = sql.SQL(',').join(map(sql.Identifier, COLS))
    with psycopg.connect(dsn, connect_timeout=5) as db:
        db.execute("SET LOCAL lock_timeout='5s'")
        db.execute("SET LOCAL statement_timeout='180s'")
        check(db.execute('SELECT current_database()').fetchone()[0] == 'scope_analyst', '数据库名称不符合预期')
        # 串行化同一导入批次，重复执行只验证，不重复插入。
        db.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', (batch_id,))
        if not verify_only:
            db.execute('CREATE SCHEMA IF NOT EXISTS lade')
            db.execute('''CREATE TABLE IF NOT EXISTS lade.import_batches (
                batch_id text PRIMARY KEY, source_file text NOT NULL, source_sha256 text UNIQUE NOT NULL,
                source_bytes bigint NOT NULL, row_count bigint NOT NULL,
                imported_at timestamptz NOT NULL DEFAULT now(), verified_at timestamptz,
                actual_year integer, reference_year integer NOT NULL, year_status text NOT NULL,
                source_url text NOT NULL, official_revision text NOT NULL,
                evidence_sha256 jsonb NOT NULL, field_mapping_markdown text NOT NULL,
                audit_summary jsonb NOT NULL, content_sha256 text
            )''')
            raw_defs = sql.SQL(',').join(sql.SQL('{} text NOT NULL').format(sql.Identifier(c)) for c in RAW)
            quality_defs = sql.SQL(',').join(sql.SQL('{} {}').format(sql.Identifier(c), sql.SQL(kind)) for c, kind in QUALITY.items())
            db.execute(sql.SQL('''CREATE TABLE IF NOT EXISTS lade.pickup_tasks (
                batch_id text NOT NULL REFERENCES lade.import_batches(batch_id),
                source_file text NOT NULL, source_row bigint NOT NULL CHECK(source_row>=2),
                {}, {}, PRIMARY KEY(batch_id,source_row)
            )''').format(raw_defs, quality_defs))
            db.execute('''CREATE TABLE IF NOT EXISTS lade.quality_rules (
                batch_id text NOT NULL REFERENCES lade.import_batches(batch_id),
                code text NOT NULL, description text NOT NULL, affected_rows bigint NOT NULL,
                action text NOT NULL, PRIMARY KEY(batch_id,code)
            )''')
        existing = db.execute('SELECT source_sha256,evidence_sha256,row_count FROM lade.import_batches WHERE batch_id=%s', (batch_id,)).fetchone()
        if existing:
            check(existing == (source_sha, hashes, summary['rows']), '已存在批次的版本信息不一致，拒绝覆盖')
        else:
            check(not verify_only, '该批次尚未导入')
            db.execute('''INSERT INTO lade.import_batches
                (batch_id,source_file,source_sha256,source_bytes,row_count,actual_year,reference_year,year_status,
                 source_url,official_revision,evidence_sha256,field_mapping_markdown,audit_summary)
                VALUES (%s,%s,%s,%s,%s,NULL,2000,'undisclosed',%s,%s,%s,%s,%s)''',
                (batch_id, str(SOURCE), source_sha, SOURCE.stat().st_size, summary['rows'],
                 'https://huggingface.co/datasets/Cainiao-AI/LaDe-P', summary['official_revision'],
                 Jsonb(hashes), mapping_text, Jsonb(summary)))
            with db.cursor() as cur:
                cur.executemany('INSERT INTO lade.quality_rules VALUES (%s,%s,%s,%s,%s)',
                                [(batch_id,r['规则'],r['说明'],int(r['行数']),r['处理']) for r in rules])
        counts = Counter()
        count = 0
        if existing:
            for row in source_rows(batch_id):
                digest_row(audit_digest, row)
                counts.update(row[COLS.index('quality_flags')])
                count += 1
        else:
            with db.cursor().copy(sql.SQL('COPY lade.pickup_tasks ({}) FROM STDIN').format(selected_columns)) as copy:
                for row in source_rows(batch_id):
                    copy.write_row(row)
                    digest_row(audit_digest, row)
                    counts.update(row[COLS.index('quality_flags')])
                    count += 1
                    if count % 250000 == 0:
                        print(f'已导入 {count:,} 条，尚未提交', flush=True)
            db.execute('CREATE INDEX IF NOT EXISTS pickup_tasks_order_idx ON lade.pickup_tasks(city,order_id)')
            db.execute('CREATE INDEX IF NOT EXISTS pickup_tasks_region_accept_idx ON lade.pickup_tasks(city,region_id,accept_time)')
            db.execute('CREATE INDEX IF NOT EXISTS pickup_tasks_courier_idx ON lade.pickup_tasks(city,courier_id)')
            db.execute("COMMENT ON TABLE lade.pickup_tasks IS 'LaDe-P上海全量原始任务；原始19列均为text并保留空字符串；quality_flags不等于隔离；来源行号含表头，从2开始。'")
            db.execute("COMMENT ON COLUMN lade.pickup_tasks.order_id IS '原始任务编号，不重编；关联证据时同时保留batch_id与source_row。'")
            db.execute("COMMENT ON COLUMN lade.pickup_tasks.quality_flags IS '上一轮全量审计的规则代码数组；含GPS缺失、跨日、超时等，规则解释见lade.quality_rules。'")
            for c in ['accept_time','pickup_time','time_window_start','time_window_end','pickup_gps_time','accept_gps_time']:
                db.execute(sql.SQL('COMMENT ON COLUMN lade.pickup_tasks.{} IS {}').format(
                    sql.Identifier(c),sql.Literal('原始MM-DD HH:mm:ss文本，空字符串仍保留；真实年份未披露，不将参考年视为真实年份。')))
        check(count == summary['rows'], '导入行数与审计行数不一致')
        check(dict(counts) == {k:v for k,v in summary['rule_counts'].items() if v}, '逐行质量标记与审计统计不符')
        print('正在从数据库读回全量字段和质量判定，计算有序内容摘要', flush=True)
        actual_digest, actual_count = hashlib.sha256(), 0
        with db.cursor(name='lade_verify') as cur:
            cur.itersize = 10000
            cur.execute(sql.SQL('SELECT {} FROM lade.pickup_tasks WHERE batch_id=%s ORDER BY source_row').format(selected_columns), (batch_id,))
            for row in cur:
                digest_row(actual_digest, row)
                actual_count += 1
        check(actual_count == count and actual_digest.digest() == audit_digest.digest(), '数据库全量回读与输入内容不一致')
        db_rules = db.execute('SELECT code,description,affected_rows,action FROM lade.quality_rules WHERE batch_id=%s ORDER BY code', (batch_id,)).fetchall()
        check(db_rules == sorted((r['规则'],r['说明'],int(r['行数']),r['处理']) for r in rules), '规则字典不一致')
        check(file_hash(SOURCE) == source_sha, '导入期间原文件改变')
        check(all(file_hash(p) == hashes[str(p.relative_to(ROOT))] for p in evidence_paths), '导入期间审计依据改变')
        if not existing:
            db.execute('UPDATE lade.import_batches SET content_sha256=%s,verified_at=now() WHERE batch_id=%s', (actual_digest.hexdigest(),batch_id))
        else:
            stored = db.execute('SELECT content_sha256 FROM lade.import_batches WHERE batch_id=%s', (batch_id,)).fetchone()[0]
            check(stored == actual_digest.hexdigest(), '已导入数据的摘要不一致')
        stats = db.execute('''SELECT count(*),count(DISTINCT order_id),min(source_row),max(source_row),
            count(*) FILTER (WHERE eligible_duration), count(*) FILTER (WHERE eligible_sla),
            count(*) FILTER (WHERE (accept_gps_time='' OR pickup_gps_time='') AND eligible_sla)
            FROM lade.pickup_tasks WHERE batch_id=%s''', (batch_id,)).fetchone()
        check(stats == (count,count,2,count+1,summary['eligible_duration'],summary['eligible_sla'],summary['gps_missing_but_sla_eligible']), '最终汇总核对失败')
    result = {'核验时间':datetime.now(timezone(timedelta(hours=8))).isoformat(),
              '数据库':'scope_analyst', '任务表':'lade.pickup_tasks','批次':batch_id,
              '动作':'已存在批次只验证' if existing else '已提交导入', '记录数':count,
              '原始字段数':len(RAW), '质量规则数':len(rules),'来源行号范围':[2,count+1],
              '原文件SHA256':source_sha,'全量内容SHA256':actual_digest.hexdigest(),
              '全量读回一致':True,'质量规则计数一致':True,'原始文件未改变':True,
              '实际年份':None,'相对计算参考年':2000,'GPS缺失但时效可计算':stats[-1]}
    REPORT.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False,indent=2),flush=True)


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--verify-only',action='store_true',help='不修改数据库，仅全量核验已存在的批次')
    args=parser.parse_args()
    try:
        main(args.verify_only)
    except Exception as exc:
        # 不打印包含数据库连接串或原始凭据的异常内容。
        print('导入或核验失败：'+type(exc).__name__+((': '+str(exc)) if isinstance(exc,ValueError) else '')+'；未提交的数据库修改自动回滚。',file=sys.stderr)
        sys.exit(1)
