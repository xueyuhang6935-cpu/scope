"""T04 真实数据抽查：只读源库，临时业务库，独立复算及增量守恒。"""
import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

from dotenv import dotenv_values
from fastapi.testclient import TestClient

from backend.auth import now, password_hash
from backend.logistics_access import WORKSPACE
from backend.logistics_repository import source_connection
from backend.main import create_app
from scripts.reference_logistics_metrics import reference

TIMES = ('2000-07-02 00:00:00','2000-07-02 10:00:00','2000-07-02 10:30:00')


def verify(output):
    env = {**dotenv_values(ROOT/'.env'),**os.environ}
    proof = {'checked_at':datetime.now(timezone(timedelta(hours=8))).isoformat(), 'scope':'T04', 'points':[], 'conservation':[]}
    with source_connection(env) as db:
        batch = db.execute('SELECT batch_id,source_sha256,content_sha256 FROM lade.import_batches WHERE verified_at IS NOT NULL ORDER BY imported_at DESC LIMIT 1').fetchone()
        assert batch, '没有已核验批次'
    proof['source_version'] = dict(batch)
    with tempfile.TemporaryDirectory() as temp:
        app_env = {**env,'AUTH_DB_PATH':str(Path(temp)/'auth.sqlite'),'DATA_DB_PATH':str(Path(temp)/'business.sqlite'),'NODE_ENV':'test'}
        app = create_app(app_env)
        with TestClient(app) as client:
            app.state.auth.query("INSERT INTO users(account,type,name,password_hash,created_at) VALUES('t04-reader','email','T04',?,?)",(password_hash('temporary-test-password'),now()))
            app.state.access.sync()
            data = app.state.data
            data.db.execute('INSERT INTO logistics_grants VALUES(?,?,?,?,?)',(WORKSPACE,'t04-reader','investigator','Shanghai','["5","6"]'))
            data.db.execute('INSERT INTO database_source_connections VALUES(?,?,?)',(WORKSPACE,batch['batch_id'],'verification'))
            assert client.post('/api/auth/login',json={'account':'t04-reader','password':'temporary-test-password'}).status_code==200
            for region in ('5','6'):
                # 独立参考输入来自只读源；不调用被测 Repository.read、快照解析或指标函数。
                with source_connection(env) as db:
                    raw = db.execute('''SELECT batch_id,source_row,order_id,city,region_id,courier_id,aoi_id,
                        accept_time,pickup_time,time_window_start,time_window_end
                        FROM lade.pickup_tasks WHERE batch_id=%s AND city='Shanghai' AND region_id=%s
                        AND ('2000-'||accept_time)::timestamp<=%s::timestamp''',
                        (batch['batch_id'],region,TIMES[-1])).fetchall()
                references = []
                for time in TIMES:
                    independent = reference(raw,time)
                    references.append(independent)
                    response = client.post('/api/logistics/snapshots',json={'batch_id':batch['batch_id'],'city':'Shanghai','region_ids':[region],'business_as_of':time})
                    assert response.status_code==200,response.text
                    metadata=response.json()
                    base='/api/logistics/snapshots/'+metadata['snapshot_id']
                    summary_response=client.get(base+'/summary')
                    assert summary_response.status_code==200,summary_response.text
                    summary=summary_response.json()
                    assert summary['visible_tasks']==independent['visible_tasks']
                    assert summary['completed_tasks']==independent['completed_tasks']
                    for key, metric in summary['metrics'].items():
                        assert metric['value']==independent[key],key
                    tasks_response=client.get(base+'/tasks?page_size=100')
                    assert tasks_response.status_code==200,tasks_response.text
                    tasks=tasks_response.json()
                    assert tasks['total_rows']==independent['backlog']
                    assert [r['order_id'] for r in tasks['rows']]==independent['order'][:100]
                    for row,expected in zip(tasks['rows'],independent['tasks']):
                        assert {k:row[k] for k in expected}==expected
                        assert not {'pickup_time','ds','quality_flags','is_late','duration_minutes'} & row.keys()
                    for dimension in ('courier_id','aoi_id'):
                        group_response=client.get(base+'/groups',params={'dimension':dimension,'page_size':100})
                        assert group_response.status_code==200,group_response.text
                        groups=group_response.json()
                        expected=independent['groups'][dimension]
                        assert groups['total_rows']==len(expected)
                        normalized=[{dimension:g[dimension],**{k:v['value'] for k,v in g['metrics'].items()}} for g in groups['rows']]
                        assert normalized==expected[:100]
                        for group in groups['rows']:
                            for key,metric in group['metrics'].items():
                                assert metric['denominator']==independent[key]
                                assert metric['share']==(metric['value']/independent[key] if independent[key] else None)
                    cross_day=sum('2000-'+r['accept_time'][:5]<time[:10] and '2000-'+r['pickup_time']>time for r in raw)
                    proof['points'].append({'region_id':region,'business_as_of':time,
                                            'metrics':{k:v['value'] for k,v in summary['metrics'].items()},
                                            'visible_tasks':summary['visible_tasks'],'completed_tasks':summary['completed_tasks'],
                                            'cross_day_pending':cross_day,'task_order_and_minutes_match':True,
                                            'group_counts_shares_and_denominators_match':True,
                                            'fingerprint':summary['fingerprint']})
                    print('指标独立复核通过：区域 '+region+' / '+time,flush=True)
                for index in (0,1):
                    t1,t2=TIMES[index:index+2]
                    accepted=sum(t1<'2000-'+r['accept_time']<=t2 for r in raw)
                    completed=sum(t1<'2000-'+r['pickup_time']<=t2 for r in raw)
                    before,after=references[index]['backlog'],references[index+1]['backlog']
                    assert after==before+accepted-completed
                    proof['conservation'].append({'region_id':region,'from':t1,'to':t2,'backlog_before':before,
                                                  'accepted':accepted,'completed':completed,'backlog_after':after,'passed':True})
            assert any(p['cross_day_pending']>0 for p in proof['points'])
            data.db.execute("DELETE FROM logistics_grants WHERE account='t04-reader'")
            for endpoint in ('summary','tasks','groups'):
                assert client.get(base+'/'+endpoint).status_code==403
            proof['all_metric_endpoints_reject_revocation']=True
    with source_connection(env) as db:
        assert db.execute('SELECT batch_id,source_sha256,content_sha256 FROM lade.import_batches WHERE batch_id=%s',(batch['batch_id'],)).fetchone()==batch
    proof['stored_source_version_unchanged']=True
    output.parent.mkdir(parents=True,exist_ok=True)
    with output.open('x',encoding='utf-8') as target:
        json.dump(proof,target,ensure_ascii=False,indent=2)
    print('T04 真实源核验完成：'+str(output))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=ROOT/'test-results/logistics-t04'/datetime.now(timezone(timedelta(hours=8))).strftime('%Y%m%d-%H%M%S')/'metric-verification.json')
    verify(parser.parse_args().output)
