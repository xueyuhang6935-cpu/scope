"""真实源库 T03 验证：临时业务库，不调用模型，不写源任务。"""
import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import dotenv_values
from fastapi.testclient import TestClient
import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row

from backend.auth import now, password_hash
from backend.logistics_access import WORKSPACE
from backend.logistics_repository import source_connection
from backend.main import create_app


def verify(output):
    env = {**dotenv_values(ROOT / '.env'), **os.environ}
    proof = {'checked_at':datetime.now(timezone(timedelta(hours=8))).isoformat(), 'scope':'T03', 'points':[]}
    with source_connection(env) as db:
        batch = db.execute('SELECT batch_id,source_sha256,content_sha256 FROM lade.import_batches WHERE verified_at IS NOT NULL ORDER BY imported_at DESC LIMIT 1').fetchone()
        assert batch, '缺少核验批次'
        proof['source_version'] = dict(batch)
    # 明确关闭默认只读事务，再用无匹配写语句核验真实权限；所有探测最终回滚。
    with psycopg.connect(env['LOGISTICS_SOURCE_DATABASE_URL'],row_factory=dict_row) as db:
        db.execute('SET TRANSACTION READ WRITE')
        app_role = conninfo_to_dict(env['ANALYST_DATABASE_URL'])['user']
        attempts = {
            'update_denied':sql.SQL('UPDATE lade.pickup_tasks SET order_id=order_id WHERE false'),
            'ddl_denied':sql.SQL('CREATE TABLE lade.t03_forbidden_probe(id int)'),
            'set_role_denied':sql.SQL('SET ROLE {}').format(sql.Identifier(app_role)),
        }
        for key, query in attempts.items():
            try:
                with db.transaction(): db.execute(query)
            except psycopg.errors.InsufficientPrivilege:
                proof[key] = True
            else:
                raise AssertionError(key + ' 未被拒绝')
        db.rollback()
    with psycopg.connect(env['SUPPLY_AGENT_DATABASE_URL']) as db:
        try:
            with db.transaction(): db.execute('SELECT order_id FROM lade.pickup_tasks LIMIT 1')
        except psycopg.errors.InsufficientPrivilege:
            proof['agent_direct_source_denied'] = True
        else:
            raise AssertionError('Agent 可直接访问源表')
    with tempfile.TemporaryDirectory() as temp:
        app = create_app({**env,'AUTH_DB_PATH':str(Path(temp)/'auth.sqlite'), 'DATA_DB_PATH':str(Path(temp)/'business.sqlite'), 'NODE_ENV':'test'})
        with TestClient(app) as client:
            app.state.auth.query("INSERT INTO users(account,type,name,password_hash,created_at) VALUES('t03-reader','email','T03',?,?)",(password_hash('temporary-test-password'),now()))
            app.state.access.sync()
            data = app.state.data
            data.db.execute('INSERT INTO logistics_grants VALUES(?,?,?,?,?)',(WORKSPACE,'t03-reader','investigator','Shanghai','["5"]'))
            data.db.execute('INSERT INTO database_source_connections VALUES(?,?,?)',(WORKSPACE,batch['batch_id'],'verification'))
            assert client.post('/api/auth/login',json={'account':'t03-reader','password':'temporary-test-password'}).status_code == 200
            for time in ('2000-07-02 00:00:00','2000-07-02 10:00:00','2000-07-02 10:30:00'):
                response = client.post('/api/logistics/snapshots',json={'batch_id':batch['batch_id'],'city':'Shanghai','region_ids':['5'],'business_as_of':time})
                assert response.status_code == 200, response.text
                metadata = response.json()
                saved = [json.loads(r['row_json']) for r in data.all('SELECT row_json FROM logistics_snapshot_rows WHERE snapshot_id=? ORDER BY row_index',(metadata['snapshot_id'],))]
                # 独立 SQL 复核，不调用被测解析/掩码/指标函数。
                with source_connection(env) as db:
                    counts = dict(db.execute('''SELECT count(*) AS visible,
                        count(*) FILTER (WHERE ('2000-'||pickup_time)::timestamp<=%s::timestamp) AS completed,
                        count(*) FILTER (WHERE ('2000-'||pickup_time)::timestamp>%s::timestamp
                            AND ('2000-'||accept_time)::timestamp::date<%s::timestamp::date) AS cross_day_pending
                        FROM lade.pickup_tasks WHERE batch_id=%s AND city='Shanghai' AND region_id='5'
                        AND ('2000-'||accept_time)::timestamp<=%s::timestamp''',
                        (time,time,time,batch['batch_id'],time)).fetchone())
                assert len(saved) == counts['visible']
                assert sum(r['completion_observed'] for r in saved) == counts['completed']
                for row in saved:
                    assert row['accept_time_reference'] <= time
                    assert not {'quality_flags','duration_minutes','late_minutes','is_late','raw_json'} & row.keys()
                    if not row['completion_observed']:
                        assert not {'pickup_time','pickup_time_reference','ds','pickup_gps_time','pickup_gps_lng','pickup_gps_lat'} & row.keys()
                    for event in ('accept','pickup'):
                        if event+'_gps_time_reference' in row:
                            assert row[event+'_gps_time_reference'] <= time
                result = client.get('/api/logistics/snapshots/'+metadata['snapshot_id'])
                assert result.status_code == 200
                proof['points'].append({'business_as_of':time, **counts, 'pending':counts['visible']-counts['completed'],
                                        'visible_content_hash':metadata['visible_content_hash'], 'pagination_total':result.json()['total_rows']})
                print('真实时点核验通过：'+time,flush=True)
            assert proof['points'][0]['cross_day_pending'] > 0
            data.db.execute("DELETE FROM logistics_grants WHERE account='t03-reader'")
            assert client.get('/api/logistics/snapshots/'+metadata['snapshot_id']).status_code == 403
            proof['real_source_snapshot_revocation_denied'] = True
        restarted = create_app({**env,'AUTH_DB_PATH':str(Path(temp)/'auth.sqlite'), 'DATA_DB_PATH':str(Path(temp)/'business.sqlite'), 'NODE_ENV':'test'})
        with TestClient(restarted) as client:
            assert client.post('/api/auth/login',json={'account':'t03-reader','password':'temporary-test-password'}).status_code == 200
            url = '/api/logistics/snapshots/'+metadata['snapshot_id']
            assert client.get(url).status_code == 403
            restarted.state.data.db.execute('INSERT INTO logistics_grants VALUES(?,?,?,?,?)',(WORKSPACE,'t03-reader','investigator','Shanghai','["5"]'))
            restored = client.get(url)
            assert restored.status_code == 200
            assert restored.json()['snapshot']['visible_content_hash'] == metadata['visible_content_hash']
            proof['disk_restart_snapshot_and_revocation_preserved'] = True
    with source_connection(env) as db:
        after = db.execute('SELECT batch_id,source_sha256,content_sha256 FROM lade.import_batches WHERE batch_id=%s',(batch['batch_id'],)).fetchone()
        assert after == batch
    proof['stored_source_version_unchanged'] = True
    output.parent.mkdir(parents=True,exist_ok=True)
    with output.open('x',encoding='utf-8') as target:
        json.dump(proof,target,ensure_ascii=False,indent=2)
    print(json.dumps(proof,ensure_ascii=False,indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,default=ROOT/'test-results'/'logistics-t03'/datetime.now(timezone(timedelta(hours=8))).strftime('%Y%m%d-%H%M%S')/'snapshot-verification.json')
    verify(parser.parse_args().output)
