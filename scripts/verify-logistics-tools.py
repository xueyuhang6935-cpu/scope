"""T07真实源快照工具核验：源库只读，账号/run仅写临时SQLite。"""
import argparse
from datetime import datetime, timezone, timedelta
import json
import os
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import dotenv_values
from fastapi.testclient import TestClient

from backend.auth import now, password_hash
from backend.logistics_access import WORKSPACE
from backend.logistics_repository import source_connection
from backend.main import create_app
from scripts.reference_logistics_metrics import reference


def verify(output, report_check=False):
    env = {**dotenv_values(ROOT / '.env'), **os.environ}
    as_of = '2000-07-02 10:00:00'
    with source_connection(env) as db:
        batch = db.execute('SELECT batch_id,source_sha256,content_sha256 FROM lade.import_batches WHERE verified_at IS NOT NULL ORDER BY imported_at DESC LIMIT 1').fetchone()
        assert batch
        raw = db.execute('''SELECT batch_id,source_row,order_id,city,region_id,courier_id,aoi_id,
            accept_time,pickup_time,time_window_start,time_window_end FROM lade.pickup_tasks
            WHERE batch_id=%s AND city='Shanghai' AND region_id='5'
            AND ('2000-'||accept_time)::timestamp<=%s::timestamp''', (batch['batch_id'], as_of)).fetchall()
    independent = reference(raw, as_of)
    proof = {'task': 'T08' if report_check else 'T07', 'checked_at': datetime.now(timezone(timedelta(hours=8))).isoformat(),
             'business_as_of': as_of, 'region_ids': ['5'], 'tools': {}, 'source_version': dict(batch)}
    with tempfile.TemporaryDirectory() as temp:
        app = create_app({**env, 'AUTH_DB_PATH': str(Path(temp)/'auth.sqlite'),
                          'DATA_DB_PATH': str(Path(temp)/'business.sqlite'), 'NODE_ENV': 'test'})
        with TestClient(app) as client:
            account, password = 't07-reader', 'temporary-test-password'
            app.state.auth.query("INSERT INTO users(account,type,name,password_hash,created_at) VALUES(?,'email','T07',?,?)", (account, password_hash(password), now()))
            app.state.access.sync()
            app.state.data.db.execute('INSERT INTO logistics_grants VALUES(?,?,?,?,?)', (WORKSPACE, account, 'investigator', 'Shanghai', '["5"]'))
            app.state.data.db.execute('INSERT INTO database_source_connections VALUES(?,?,?)', (WORKSPACE, batch['batch_id'], 'verification'))
            assert client.post('/api/auth/login', json={'account': account, 'password': password}).status_code == 200
            snapshot = client.post('/api/logistics/snapshots', json={'batch_id': batch['batch_id'], 'city': 'Shanghai', 'region_ids': ['5'], 'business_as_of': as_of})
            assert snapshot.status_code == 200, snapshot.text
            created = client.post('/api/logistics/runs', json={'snapshot_id': snapshot.json()['snapshot_id'], 'idempotency_key': 'real-source-check'})
            assert created.status_code == 202, created.text
            run_id = created.json()['run_id']
            if report_check:
                # 不订阅事件、退出创建请求后，执行器仍自动生成工具证据与报告。
                for _ in range(200):
                    running = client.get(f'/api/logistics/runs/{run_id}').json()
                    if running['status'] in ('completed', 'failed'):
                        break
                    time.sleep(.05)
                assert running['status'] == 'completed', running
                saved = client.get(f'/api/logistics/runs/{run_id}/events').json()
                assert saved['report_ready']
                assert [r['seq'] for r in saved['rows']] == list(range(1, len(saved['rows'])+1))
                assert sum(r['kind'] == 'report_ready' for r in saved['rows']) == 1
                assert client.get(f'/api/logistics/runs/{run_id}/events?after=2').json()['rows'] == saved['rows'][2:]
                audit = client.get(f'/api/logistics/runs/{run_id}/calls').json()['rows']
                assert len(audit) == 3
                assert all(r['request_id'] == created.headers['X-Request-ID'] for r in audit)
                proof['t09_background'] = {'status': running['status'], 'events': len(saved['rows']),
                                           'saved_calls': len(audit), 'report_ready': True, 'cursor_replay': True}
            def call(name, args=None):
                response = client.post(f'/api/logistics/runs/{run_id}/tools/{name}', json=args or {})
                assert response.status_code == 200, response.text
                result = response.json()
                assert result['status'] != 'error'
                proof['tools'][name] = {'status': result['status'], 'total_rows': result['total_rows'],
                                        'returned_rows': result['returned_rows'], 'truncated': result['truncated']}
                return result
            summary = call('get_snapshot_summary')
            for name, metric in summary['data']['metrics'].items():
                assert metric['value'] == independent[name], name
            proof['metrics'] = {name: metric['value'] for name, metric in summary['data']['metrics'].items()}
            tasks = call('get_pending_tasks', {'page_size': 20})
            assert tasks['total_rows'] == independent['backlog']
            assert [r['order_id'] for r in tasks['rows']] == independent['order'][:20]
            assert all(not {'pickup_time', 'ds', 'quality_flags', 'duration_minutes', 'is_late'} & row.keys() for row in tasks['rows'])
            for dimension in ('courier_id', 'aoi_id'):
                groups = call('get_risk_distribution', {'dimension': dimension, 'page_size': 20})
                actual = [{dimension: g[dimension], **{k: v['value'] for k, v in g['metrics'].items()}} for g in groups['rows']]
                assert actual == independent['groups'][dimension][:20]
                assert groups['total_rows'] == len(independent['groups'][dimension])
                assert all(v['denominator'] == independent[k] for g in groups['rows'] for k, v in g['metrics'].items())
            workload = call('get_courier_workload', {'sort': 'backlog_desc'})
            assert workload['denominator']['backlog'] == independent['backlog']
            quality = call('get_quality_summary')
            assert quality['data']['visible_tasks'] == independent['visible_tasks']
            assert quality['data']['quality']['pickup_gps']['not_applicable'] == independent['backlog']
            evidence = call('get_evidence', {'evidence_id': tasks['rows'][0]['evidence_id']})
            assert evidence['rows'][0]['record']['order_id'] == tasks['rows'][0]['order_id']
            assert evidence['fingerprint'] == tasks['fingerprint']
            if report_check:
                base = f'/api/logistics/runs/{run_id}'
                response = client.post(base+'/report', json={})
                assert response.status_code == 200, response.text
                report = response.json()
                for name, metric in report['summary']['metrics'].items():
                    assert metric['value'] == independent[name]
                assert [r['order_id'] for r in report['focus_tasks']] == independent['order'][:20]
                for row in report['focus_tasks']:
                    saved = client.get(base+'/evidence/'+row['evidence_id'])
                    assert saved.status_code == 200
                    assert saved.json()['record']['order_id'] == row['order_id']
                    assert not {'pickup_time','ds','is_late','duration_minutes'} & saved.json()['record'].keys()
                for key in ('summary_evidence_id', 'tasks_evidence_id', 'quality_evidence_id'):
                    saved = client.get(base+'/evidence/'+report[key])
                    assert saved.status_code == 200
                markdown = client.get(base+'/report/markdown')
                page = client.get(base+'/report/view')
                assert markdown.status_code == page.status_code == 200
                assert client.get(base+'/report').json() == report
                assert client.post(base+'/report', json={}).json() == report
                output.parent.mkdir(parents=True, exist_ok=True)
                output.with_suffix('.md').write_text(markdown.text, encoding='utf-8')
                output.with_suffix('.html').write_text(page.text, encoding='utf-8')
                output.with_suffix('.report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
                calls = client.get(base+'/calls?page_size=100').json()['rows']
                assert all(c['status'] != 'started' and c['elapsed_ms'] is not None for c in calls)
                proof['report'] = {'report_id': report['report_id'], 'focus_tasks': len(report['focus_tasks']),
                    'all_task_references_resolved': True, 'independent_numbers_match': True,
                    'download_and_page_use_saved_report': True, 'idempotent_generation': True,
                    'audit_calls': len(calls), 'truncated': report['task_list']['truncated']}
            path = f'/api/logistics/runs/{run_id}/tools/get_pending_tasks'
            assert client.post(path, json={'region_ids': ['6']}).status_code == 400
            app.state.data.db.execute('DELETE FROM logistics_grants WHERE account=?', (account,))
            assert client.post(path, json={}).status_code == 403
            if report_check:
                assert client.get(base+'/report/markdown').status_code == 403
                assert client.get(base+'/evidence/'+report['summary_evidence_id']).status_code == 403
                proof['report']['download_and_evidence_revocation_rejected'] = True
            proof.update(independent_metrics_match=True, independent_tasks_and_groups_match=True,
                         scope_override_rejected=True, live_revocation_rejected=True)
    with source_connection(env) as db:
        assert db.execute('SELECT batch_id,source_sha256,content_sha256 FROM lade.import_batches WHERE batch_id=%s', (batch['batch_id'],)).fetchone() == batch
    proof['stored_source_version_unchanged'] = True
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('x', encoding='utf-8') as target:
        json.dump(proof, target, ensure_ascii=False, indent=2)
    print(proof['task']+'真实源核验通过：' + str(output))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT/'test-results/logistics-t07/real-tools.json')
    parser.add_argument('--report', action='store_true', help='同时核验T08证据、报告与下载')
    args = parser.parse_args()
    verify(args.output, args.report)
