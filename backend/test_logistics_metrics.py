"""T04 手算、独立复算、精确边界、分母和受控接口。"""
from contextlib import contextmanager
from copy import deepcopy
import json
from unittest.mock import patch
import unittest

from .logistics_metrics import calculate, group_tasks, METRIC_VERSION
from .logistics_repository import LogisticsRepository, reference_time
from .logistics_snapshots import mask_row
from . import test_logistics_snapshots as snapshot_test_support
from scripts.reference_logistics_metrics import fixture_records, reference, FIXTURE


def visible(rows, time):
    moment = reference_time(time,'T',business=True)
    return [masked for row in rows if (masked:=mask_row(row,moment)) is not None]


class MetricTests(unittest.TestCase):
    def test_hand_calculation_and_independent_reference(self):
        rows = fixture_records()
        expected = json.loads(FIXTURE.read_text(encoding='utf-8'))['expected']
        for time, answer in expected.items():
            with self.subTest(time=time):
                result = calculate(visible(rows,time),time)
                direct = reference(rows,time)
                actual = {k:v['value'] for k,v in result['metrics'].items()}
                actual.update(visible_tasks=result['visible_tasks'], completed_tasks=result['completed_tasks'],
                              order=[r['order_id'] for r in result['tasks']])
                self.assertEqual(actual,answer)
                self.assertEqual({k:direct[k] for k in answer},answer)
                for row, independent in zip(result['tasks'],direct['tasks']):
                    self.assertEqual({k:row[k] for k in independent},independent)
                    self.assertFalse({'pickup_time','ds','completed_duration_minutes','is_late'} & row.keys())
                for dimension in ('courier_id','aoi_id'):
                    groups = group_tasks(result,dimension)
                    normalized = [{dimension:g[dimension],**{k:v['value'] for k,v in g['metrics'].items()}} for g in groups]
                    self.assertEqual(normalized,direct['groups'][dimension])
        self.assertEqual(calculate(visible(rows,'2000-07-02 10:00:00'),'2000-07-02 10:00:00')['tasks'][0]['current_overdue_minutes'],630)

    def test_precise_boundaries_recent_left_open_and_zero_duration(self):
        rows = fixture_records()
        rows[0].update(time_window_end='07-02 09:59:59')
        rows[1].update(time_window_end='07-02 10:30:00')
        rows[2].update(time_window_start='07-02 09:30:00',time_window_end='07-02 10:30:01')
        result = calculate(visible(rows,'2000-07-02 10:00:00'),'2000-07-02 10:00:00')
        tasks = {r['order_id']:r for r in result['tasks']}
        self.assertEqual(tasks['A']['current_overdue_minutes'],1/60)
        self.assertEqual(tasks['B']['risk_state'],'due_soon')
        self.assertEqual(tasks['C']['risk_state'],'normal')
        self.assertEqual(tasks['H']['risk_state'],'due_soon')
        self.assertEqual(result['metrics']['recent_accepted']['value'],4)  # A 恰好 T-W，排除。
        self.assertEqual(result['metrics']['recent_completed']['value'],1)  # D 恰好 T-W，F 零时长等于 T。

    def test_missing_window_is_not_zero_and_empty_is_distinct(self):
        rows = fixture_records()
        rows[0]['time_window_end']=''
        result = calculate(visible(rows,'2000-07-02 10:00:00'),'2000-07-02 10:00:00')
        self.assertEqual(result['metrics']['backlog']['value'],5)
        self.assertIsNone(result['metrics']['overdue']['value'])
        self.assertEqual(result['metrics']['overdue']['known_lower_bound'],1)
        self.assertEqual(result['status'],'unavailable')
        self.assertEqual(result['tasks'][-1]['risk_state'],'window_unavailable')
        self.assertIsNone(result['tasks'][-1]['current_overdue_minutes'])
        self.assertEqual(result['tasks'][-1]['current_wait_minutes'],60)
        groups=group_tasks(result,'courier_id')
        self.assertTrue(all(g['metrics']['overdue']['share'] is None for g in groups))
        empty=calculate([],'2000-07-02 10:00:00')
        self.assertEqual(empty['status'],'empty')
        self.assertEqual(empty['metrics']['overdue']['value'],0)
        completed_only=calculate(visible([fixture_records()[3]],'2000-07-02 10:00:00'),'2000-07-02 10:00:00')
        self.assertEqual(completed_only['status'],'ok')
        self.assertEqual(completed_only['metrics']['overdue']['value'],0)

    def test_future_invariance_and_stable_string_ties(self):
        rows=fixture_records()
        time='2000-07-02 10:00:00'
        baseline=calculate(visible(rows,time),time)
        changed=deepcopy(rows)
        for row in changed:
            if row['pickup_time']>'07-02 10:00:00':
                row.update(pickup_time='07-03 18:00:00',ds='0703',quality_flags=['future'],is_late=True)
        self.assertEqual(baseline,calculate(visible(changed,time),time))
        rows=[{**fixture_records()[0],'order_id':identifier} for identifier in ('2','10','01')]
        self.assertEqual([r['order_id'] for r in calculate(visible(rows,time),time)['tasks']],['01','10','2'])


class MetricsAPITests(unittest.TestCase):
    # 复用 T03 的隔离账号/业务库生命周期；避免继承并重复执行 T03 测试。
    def setUp(self):
        snapshot_test_support.SnapshotTests.setUp(self)
        self.records[:] = fixture_records()
        for row in self.records: row['batch_id']='batch'

    tearDown = snapshot_test_support.SnapshotTests.tearDown
    login = snapshot_test_support.SnapshotTests.login
    create = snapshot_test_support.SnapshotTests.create

    def test_summary_tasks_groups_and_pagination(self):
        metadata=self.create().json()
        base='/api/logistics/snapshots/'+metadata['snapshot_id']
        summary=self.client.get(base+'/summary').json()
        self.assertEqual(summary['metric_version'],METRIC_VERSION)
        self.assertEqual(summary['metrics']['backlog']['value'],5)
        self.assertEqual(summary['metrics']['overdue']['value'],2)
        self.assertEqual(summary['metrics']['due_soon']['value'],2)
        pages=[self.client.get(base+f'/tasks?page={p}&page_size=2').json() for p in (1,2,3)]
        self.assertEqual([r['order_id'] for p in pages for r in p['rows']],['G','A','H','B','C'])
        self.assertTrue(pages[0]['truncated'])
        self.assertFalse(pages[-1]['truncated'])
        self.assertTrue(all(p['denominator']['backlog']==5 and p['total_rows']==5 for p in pages))
        filtered=self.client.get(base+'/tasks?risk_state=overdue&page_size=1').json()
        self.assertEqual(filtered['total_rows'],2)
        self.assertEqual(filtered['denominator']['overdue'],2)
        groups=self.client.get(base+'/groups?dimension=aoi_id&page_size=1').json()
        self.assertEqual(groups['total_rows'],3)
        self.assertEqual(groups['rows'][0]['metrics']['overdue']['share'],1)
        self.assertEqual(groups['rows'][0]['metrics']['overdue']['denominator'],2)
        self.assertEqual(summary['fingerprint'],metadata['snapshot_fingerprint'])
        with patch.object(LogisticsRepository,'read',side_effect=AssertionError('不得读源')):
            self.assertEqual(self.client.get(base+'/summary').status_code,200)

    def test_all_metric_endpoints_enforce_authorization_and_parameters(self):
        identifier=self.create().json()['snapshot_id']
        base='/api/logistics/snapshots/'+identifier
        for path in ('summary','tasks','groups'):
            self.assertEqual(self.client.get(base+'/'+path+'?business_as_of=anything').status_code,400)
            self.assertEqual(self.client.get(base+'/'+path,headers={'X-Workspace-Id':'other'}).status_code,403)
        self.assertEqual(self.client.get(base+'/tasks?risk_state=wrong').status_code,400)
        self.assertEqual(self.client.get(base+'/groups?dimension=city').status_code,400)
        self.assertEqual(self.client.get(base+'/tasks?page_size=101').status_code,400)
        self.data.db.execute("DELETE FROM logistics_grants WHERE account='reader'")
        for path in ('summary','tasks','groups'):
            self.assertEqual(self.client.get(base+'/'+path).status_code,403)

    def test_empty_missing_window_and_version_compatibility(self):
        self.records[0]['time_window_end']=''
        metadata=self.create().json()
        base='/api/logistics/snapshots/'+metadata['snapshot_id']
        result=self.client.get(base+'/summary').json()
        self.assertEqual(result['status'],'unavailable')
        self.assertEqual(result['metrics']['overdue']['known_lower_bound'],1)
        self.assertIsNone(result['metrics']['overdue']['value'])
        # 历史 T03 null 口径快照可按显式 v1 复算，不改写其内容或指纹。
        metadata['metric_version']=None
        self.data.db.execute('UPDATE logistics_snapshots SET metadata_json=? WHERE snapshot_id=?',
                             (json.dumps(metadata),metadata['snapshot_id']))
        self.assertEqual(self.client.get(base+'/summary').json()['metric_version'],METRIC_VERSION)
        metadata['metric_version']='unknown-version'
        self.data.db.execute('UPDATE logistics_snapshots SET metadata_json=? WHERE snapshot_id=?',
                             (json.dumps(metadata),metadata['snapshot_id']))
        self.assertEqual(self.client.get(base+'/summary').status_code,409)
        self.records.clear()
        metadata=self.create().json()
        base='/api/logistics/snapshots/'+metadata['snapshot_id']
        for path in ('summary','tasks','groups'):
            self.assertEqual(self.client.get(base+'/'+path).json()['status'],'empty')

    def test_risk_search_before_pagination_and_authorized_task_evidence(self):
        self.records[2]['time_window_end'] = ''
        metadata = self.create().json()
        base = '/api/logistics/snapshots/' + metadata['snapshot_id']
        risks = self.client.get(base+'/tasks?risk_state=risk&page_size=1').json()
        self.assertEqual(risks['total_rows'], 4)
        self.assertTrue(all(r['risk_state'] in ('overdue', 'due_soon') for r in risks['rows']))
        second = self.client.get(base+'/tasks?risk_state=risk&page_size=1&page=2').json()['rows'][0]
        found = self.client.get(base+'/tasks', params={'risk_state': 'risk', 'search': second['order_id'], 'page_size': 1}).json()
        self.assertEqual(found['total_rows'], 1)
        self.assertEqual(found['rows'][0], second)
        self.assertEqual(self.client.get(base+'/tasks?risk_state=risk&search=C').json()['total_rows'], 0)
        missing = self.client.get(base+'/tasks?risk_state=window_unavailable').json()
        self.assertEqual(missing['total_rows'], 1)
        evidence_path = base+'/task-evidence/'+str(second['row_index'])
        evidence = self.client.get(evidence_path).json()
        self.assertEqual(evidence['record']['order_id'], second['order_id'])
        self.assertEqual(evidence['snapshot_id'], metadata['snapshot_id'])
        self.assertNotIn('pickup_time_reference', evidence['record'])
        self.assertEqual(self.client.get(base+'/tasks', params={'search': 'x'*201}).status_code, 400)
        self.assertEqual(self.client.get(base+'/task-evidence/999999').status_code, 404)
        self.assertEqual(self.client.get(evidence_path+'?business_as_of=anything').status_code, 400)
        self.assertEqual(self.client.get(evidence_path, headers={'X-Workspace-Id': 'other'}).status_code, 403)
        self.data.db.execute("DELETE FROM logistics_grants WHERE account='reader'")
        for path in (evidence_path, base+'/tasks?risk_state=risk&search=A'):
            self.assertEqual(self.client.get(path).status_code, 403)

    def test_recent_details_quality_and_future_invariance(self):
        metadata = self.create().json()
        base = '/api/logistics/snapshots/' + metadata['snapshot_id']
        for flow in ('recent_accepted', 'recent_completed'):
            result = self.client.get(base+'/recent', params={'flow': flow, 'page_size': 1}).json()
            self.assertEqual(result['total_rows'], self.client.get(base+'/summary').json()['metrics'][flow]['value'])
            rows = [row for page in range(1, result['total_rows']+1)
                    for row in self.client.get(base+'/recent', params={'flow': flow, 'page_size': 1, 'page': page}).json()['rows']]
            self.assertEqual(len(rows), result['total_rows'])
            if flow == 'recent_completed':
                self.assertTrue(all(row['completion_observed'] for row in rows))
                self.assertEqual([row['order_id'] for row in rows], ['F'])
            else:
                self.assertNotIn('A', [row['order_id'] for row in rows])  # T-W 左开
            for row in rows:
                if not row['completion_observed']:
                    self.assertNotIn('pickup_time_reference', row)
        quality = self.client.get(base+'/quality').json()
        self.assertEqual(quality['quality']['pickup_gps']['not_applicable'], 5)
        self.assertEqual(quality['quality']['pickup_gps']['denominator'], 2)
        for path in ('quality', 'recent', 'task-evidence/0'):
            self.assertEqual(self.client.get(base+'/'+path+'?region_ids=6').status_code, 400)
        baseline = {path: self.client.get(base+'/'+path).json() for path in ('quality', 'recent?flow=recent_completed')}
        for row in self.records:
            if row['pickup_time'] > '07-02 10:00:00':
                row.update(pickup_time='07-03 18:00:00', ds='0703', pickup_gps_time='07-04 00:00:00')
        changed_base = '/api/logistics/snapshots/' + self.create().json()['snapshot_id']
        for path, before in baseline.items():
            after = self.client.get(changed_base+'/'+path).json()
            self.assertEqual(before.get('quality'), after.get('quality'))
            self.assertEqual(before.get('rows'), after.get('rows'))
        self.data.db.execute("DELETE FROM logistics_grants WHERE account='reader'")
        for path in ('quality', 'recent', 'groups'):
            self.assertEqual(self.client.get(base+'/'+path).status_code, 403)


if __name__=='__main__': unittest.main()
