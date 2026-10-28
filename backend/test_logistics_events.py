"""T09：事务事件、游标补发、权限及真实HTTP SSE。"""
import json
import socket
import subprocess
import sys
import time
import unittest
from concurrent.futures import ThreadPoolExecutor

import httpx

from . import test_logistics_report as support
from .common import APIError
from .logistics_events import RunEvents


class EventsTests(unittest.TestCase):
    setUp = support.ReportTests.setUp
    tearDown = support.ReportTests.tearDown
    login = support.ReportTests.login
    create = support.ReportTests.create
    new_run = support.ReportTests.new_run
    call = support.ReportTests.call
    report = support.ReportTests.report

    def history(self, after=0):
        return self.client.get(f'/api/logistics/runs/{self.run}/events?after={after}')

    def test_transaction_order_duplicates_rollback_and_replay(self):
        self.assertEqual(self.history().json()['rows'][0]['kind'], 'run_created')
        service=self.app.state.logistics_runs
        claimed=service.claim()
        service.process(claimed)
        self.report()
        rows=self.history().json()['rows']
        self.assertEqual([r['seq'] for r in rows], list(range(1,len(rows)+1)))
        kinds=[r['kind'] for r in rows]
        self.assertIn('stage_changed',kinds)
        self.assertEqual(kinds.count('tool_started'),3)
        self.assertEqual(kinds.count('tool_finished'),3)
        self.assertEqual(kinds.count('report_ready'),1)
        self.report()
        self.assertEqual(self.history().json()['rows'],rows)
        self.assertEqual(self.history(3).json()['rows'],rows[3:])
        RunEvents(service)
        self.assertEqual(self.history().json()['rows'],rows)
        self.data.db.execute('BEGIN IMMEDIATE')
        self.data.db.execute("UPDATE logistics_runs SET status='failed' WHERE run_id=?",(self.run,))
        self.data.db.rollback()
        self.assertEqual(self.history().json()['rows'],rows)

    def test_concurrent_tools_sequences_cancel_failure_and_headers(self):
        with ThreadPoolExecutor(max_workers=3) as pool:
            list(pool.map(lambda _: self.service.call(self.user,self.run,'get_snapshot_summary',{}), range(6)))
        response=self.history()
        self.assertTrue(response.headers['X-Request-ID'])
        self.assertEqual(response.headers['X-Run-ID'],self.run)
        rows=response.json()['rows']
        self.assertEqual([r['seq'] for r in rows],list(range(1,14)))
        self.client.post(f'/api/logistics/runs/{self.run}/cancel',json={})
        self.assertEqual(self.history().json()['rows'][-1]['kind'],'run_cancelled')
        self.run=self.new_run('failed')
        claimed=self.app.state.logistics_runs.claim()
        self.app.state.logistics_runs.commit(self.run,claimed['claim_token'],'failed','validate',error='execution_failed')
        self.assertEqual(self.history().json()['rows'][-1]['kind'],'run_failed')
        self.assertEqual(self.call('get_snapshot_summary').status_code, 409)
        self.run=self.new_run('headers')
        result=self.call('get_snapshot_summary')
        self.assertEqual(result.status_code, 200, result.text)
        audit=self.data.one('SELECT request_id FROM logistics_tool_calls WHERE call_id=?',(result.json()['call_id'],))
        self.assertEqual(audit['request_id'],result.headers['X-Request-ID'])

    def test_list_and_events_permissions_and_parameters(self):
        self.assertEqual(self.client.get('/api/logistics/runs').json()['total_rows'],1)
        self.assertEqual(self.history(-1).status_code,400)
        self.assertEqual(self.client.get('/api/logistics/runs?status=bad').status_code,400)
        self.assertEqual(self.client.get(f'/api/logistics/runs/{self.run}/events?region_ids=6').status_code,400)
        self.data.db.execute("UPDATE logistics_grants SET regions='[\"6\"]' WHERE account='reader'")
        self.assertEqual(self.history().status_code,403)
        self.assertEqual(self.client.get('/api/logistics/runs').json()['total_rows'],0)
        with self.assertRaises(APIError):
            self.app.state.logistics_events.read(self.user,self.run)

    def test_real_sse_disconnect_reconnect_and_last_event_id(self):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
        code='''
import json,sys,uvicorn
from backend.main import create_app
uvicorn.run(create_app(json.loads(sys.argv[1])),host='127.0.0.1',port=int(sys.argv[2]),log_level='error')
'''
        process=subprocess.Popen([sys.executable,'-X','utf8','-c',code,json.dumps(self.env),str(port)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        try:
            with httpx.Client(base_url=f'http://127.0.0.1:{port}',timeout=10,trust_env=False) as client:
                for _ in range(100):
                    try:
                        if client.get('/api/health').status_code==200:break
                    except httpx.TransportError:time.sleep(.05)
                login=client.post('/api/auth/login',json={'account':'reader','password':'password123'})
                self.assertEqual(login.status_code,200)
                path=f'/api/logistics/runs/{self.run}/events?stream=1'
                def next_event(headers=None):
                    with client.stream('GET',path,headers=headers) as response:
                        self.assertEqual(response.status_code,200)
                        self.assertIn('text/event-stream',response.headers['content-type'])
                        for line in response.iter_lines():
                            if line.startswith('data: '):return json.loads(line[6:])
                first=next_event()
                self.assertEqual(first['seq'],1)
                result=client.post(f'/api/logistics/runs/{self.run}/tools/get_snapshot_summary',json={})
                self.assertEqual(result.status_code,200)
                resumed=next_event({'Last-Event-ID':str(first['seq'])})
                self.assertGreater(resumed['seq'],first['seq'])
                # 关闭SSE后创建运行；后台独立完成工具和报告，再重新读取落盘结果。
                snapshot=self.app.state.logistics_runs.saved(self.run)['snapshot_id']
                created=client.post('/api/logistics/runs',json={'snapshot_id':snapshot,'idempotency_key':'no-subscriber'})
                self.assertEqual(created.status_code,202)
                detached=created.json()['run_id']
                for _ in range(100):
                    value=client.get(f'/api/logistics/runs/{detached}').json()
                    if value['status'] in ('completed','failed'):break
                    time.sleep(.05)
                self.assertEqual(value['status'],'completed',value)
                report=client.get(f'/api/logistics/runs/{detached}/report')
                self.assertEqual(report.status_code,200,report.text)
                self.assertEqual(report.json()['summary']['metrics']['backlog']['value'],5)
                calls=client.get(f'/api/logistics/runs/{detached}/calls').json()['rows']
                self.assertEqual(len(calls),3)
                self.assertTrue(all(c['request_id']==created.headers['X-Request-ID'] for c in calls))
                history=client.get(f'/api/logistics/runs/{detached}/events').json()
                self.assertTrue(history['report_ready'])
                self.assertIn('report_ready',[e['kind'] for e in history['rows']])
                self.data.db.execute("UPDATE logistics_grants SET regions='[\"6\"]' WHERE account='reader'")
                with client.stream('GET',path) as response:
                    self.assertEqual(response.status_code,403)
        finally:
            if sys.platform == 'win32' and process.poll() is None:
                subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
            else:
                process.terminate()
            process.wait(timeout=10)

    def test_background_report_recovery_and_expired_claim(self):
        service=self.app.state.logistics_runs
        claimed=service.claim()
        self.data.db.execute('UPDATE logistics_runs SET lease_until=0 WHERE run_id=?',(self.run,))
        with self.assertRaises(APIError):
            self.app.state.logistics_reports.create(self.user,self.run,claim_token=claimed['claim_token'])
        service.process(service.claim())
        self.assertEqual(service.saved(self.run)['status'],'completed')
        self.assertTrue(self.history().json()['report_ready'])
        report=self.report()
        self.assertEqual(report['summary']['metrics']['backlog']['value'],5)
        self.assertEqual(len(self.client.get(f'/api/logistics/runs/{self.run}/calls').json()['rows']),3)


if __name__=='__main__':unittest.main()
