"""T07：真实HTTP、宿主直调、手算复用、证据范围与未来不变性。"""
from copy import deepcopy
import io
import json
import logging
import unittest
from unittest.mock import patch

from project.tools.logistics import REGISTRY, OUTPUT_SCHEMAS, validate_arguments
from . import test_logistics_snapshots as support
from .common import APIError
from .logistics_repository import LogisticsRepository
from .logistics_runs import RunService
from scripts.reference_logistics_metrics import fixture_records, reference


class LogisticsToolsTests(unittest.TestCase):
    login = support.SnapshotTests.login
    create = support.SnapshotTests.create
    tearDown = support.SnapshotTests.tearDown

    def setUp(self):
        with patch.object(RunService, 'start'):
            support.SnapshotTests.setUp(self)
        self.records[:] = fixture_records()
        for row in self.records:
            row['batch_id'] = 'batch'
        self.user = self.app.state.auth.query("SELECT * FROM users WHERE account='reader'")
        self.service = self.app.state.logistics_tools
        self.run = self.new_run()

    def new_run(self, key='request'):
        snapshot = self.create().json()['snapshot_id']
        response = self.client.post('/api/logistics/runs', json={'snapshot_id': snapshot, 'idempotency_key': key})
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()['run_id']

    def call(self, name, args=None, run=None):
        return self.client.post(f'/api/logistics/runs/{run or self.run}/tools/{name}', json=args or {})

    def result(self, name, args=None):
        response = self.call(name, args)
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        schema = OUTPUT_SCHEMAS[name]
        self.assertTrue(set(schema['required']).issubset(result))
        self.assertEqual(result['returned_rows'], len(result['rows']))
        self.assertTrue(set(schema['properties']['data']['required']).issubset(result['data']))
        required = schema['properties']['rows']['items'].get('required', [])
        self.assertTrue(all(set(required).issubset(row) for row in result['rows']))
        return result

    def test_registry_and_all_tools_match_hand_and_independent_calculation(self):
        catalog = self.client.get('/api/logistics/tools')
        self.assertEqual(catalog.status_code, 200)
        self.assertEqual({t['function']['name'] for t in catalog.json()['tools']}, set(REGISTRY))
        self.assertEqual(set(catalog.json()['output_schemas']), set(REGISTRY))
        summary = self.result('get_snapshot_summary')
        expected = reference(self.records, '2000-07-02 10:00:00')
        self.assertEqual(summary['data']['visible_tasks'], 7)
        for name in ('backlog', 'overdue', 'due_soon', 'recent_accepted', 'recent_completed'):
            self.assertEqual(summary['data']['metrics'][name]['value'], expected[name])
        self.assertEqual(summary['data']['metrics']['backlog']['value'], 5)
        tasks = self.result('get_pending_tasks')
        self.assertEqual([row['order_id'] for row in tasks['rows']], ['G', 'A', 'H', 'B', 'C'])
        self.assertEqual([row['order_id'] for row in tasks['rows']], expected['order'])
        for dimension in ('courier_id', 'aoi_id'):
            distribution = self.result('get_risk_distribution', {'dimension': dimension})
            values = [{dimension: r[dimension], **{k: v['value'] for k, v in r['metrics'].items()}} for r in distribution['rows']]
            self.assertEqual(values, expected['groups'][dimension])
        workload = self.result('get_courier_workload')
        self.assertEqual(sum(row['metrics']['backlog']['value'] for row in workload['rows']), 5)
        self.assertEqual(sum(row['metrics']['recent_accepted']['value'] for row in workload['rows']), expected['recent_accepted'])
        quality = self.result('get_quality_summary')
        pickup = quality['data']['quality']['pickup_gps']
        self.assertEqual(pickup, {'available': 0, 'unavailable': 2, 'denominator': 2, 'not_applicable': 5, 'available_share': 0})
        evidence = self.result('get_evidence', {'evidence_id': tasks['rows'][0]['evidence_id']})
        self.assertEqual(evidence['rows'][0]['record']['order_id'], 'G')
        self.assertNotIn('pickup_time', evidence['rows'][0]['record'])
        self.assertEqual(evidence['fingerprint'], tasks['fingerprint'])
        self.assertEqual(summary['horizon_minutes'], validate_arguments('get_snapshot_summary', {})['horizon_minutes'])

    def test_pagination_sorting_filtering_and_full_denominators(self):
        pages = [self.result('get_pending_tasks', {'page': p, 'page_size': 2}) for p in (1, 2, 3)]
        self.assertEqual([r['order_id'] for page in pages for r in page['rows']], ['G', 'A', 'H', 'B', 'C'])
        self.assertTrue(all(page['total_rows'] == 5 and page['denominator']['backlog'] == 5 for page in pages))
        self.assertEqual([page['truncated'] for page in pages], [True, True, False])
        risk = self.result('get_pending_tasks', {'risk_state': 'overdue', 'page_size': 1})
        self.assertEqual(risk['total_rows'], 2)
        self.assertEqual(risk['denominator']['overdue'], 2)
        group = self.result('get_risk_distribution', {'dimension': 'aoi_id', 'sort': 'overdue_desc', 'page_size': 1})
        self.assertEqual(group['total_rows'], 3)
        self.assertEqual(group['rows'][0]['metrics']['overdue']['share'], 1)
        self.assertEqual(group['rows'][0]['metrics']['overdue']['denominator'], 2)
        for order, field, reverse in (('wait_minutes_desc', 'current_wait_minutes', True),
                                      ('overdue_minutes_desc', 'current_overdue_minutes', True),
                                      ('remaining_minutes_asc', 'remaining_minutes', False)):
            rows = self.result('get_pending_tasks', {'sort': order})['rows']
            self.assertEqual([r[field] for r in rows], sorted((r[field] for r in rows), reverse=reverse))
        courier = pages[0]['rows'][0]['courier_id']
        filtered = self.result('get_pending_tasks', {'courier_id': courier})
        self.assertTrue(all(row['courier_id'] == courier for row in filtered['rows']))
        workload = self.result('get_courier_workload', {'courier_id': courier, 'sort': 'backlog_desc', 'page_size': 1})
        self.assertEqual(workload['total_rows'], 1)
        self.assertEqual(workload['rows'][0]['courier_id'], courier)
        empty = self.result('get_pending_tasks', {'page': 100})
        self.assertEqual(empty['status'], 'empty')
        self.assertEqual(empty['total_rows'], 5)

    def test_http_and_direct_calls_cannot_expand_scope_or_accept_illegal_parameters(self):
        for name in REGISTRY:
            for field in ('snapshot_id', 'region_ids', 'business_as_of', 'sql'):
                self.assertEqual(self.call(name, {field: 'override'}).status_code, 400)
                with self.assertRaises(APIError) as error:
                    self.service.call(self.user, self.run, name, {field: 'override'})
                self.assertEqual(error.exception.status, 400)
                with self.assertRaises(APIError):
                    self.service.execute(self.user, self.run, name, {field: 'override'})
        for args in ({'page': True}, {'page_size': 101}, {'page': 0}, {'sort': 'DROP TABLE'}, {'risk_state': 'other'}, {'horizon_minutes': 31}, {'recent_minutes': 59}):
            self.assertEqual(self.call('get_pending_tasks', args).status_code, 400)
        self.assertEqual(self.call('execute_sql', {'sql': 'SELECT 1'}).status_code, 400)
        self.assertEqual(self.call('get_risk_distribution', {'dimension': 'raw_json'}).status_code, 400)
        path = f'/api/logistics/runs/{self.run}/tools/get_snapshot_summary'
        self.assertEqual(self.client.post(path+'?region_ids=6', json={}).status_code, 400)
        self.assertEqual(self.client.post(path, json={}, headers={'X-Workspace-Id': 'other'}).status_code, 403)
        with patch.object(LogisticsRepository, 'read', side_effect=AssertionError('不允许读取源表')):
            for name in REGISTRY:
                args = {'evidence_id': self.result('get_pending_tasks')['rows'][0]['evidence_id']} if name == 'get_evidence' else {}
                self.result(name, args)

    def test_evidence_bound_to_run_and_snapshot_and_survives_reconstruction(self):
        tasks = self.result('get_pending_tasks')
        identifier = tasks['rows'][0]['evidence_id']
        second = self.new_run('second')
        self.assertEqual(self.call('get_evidence', {'evidence_id': identifier}, second).status_code, 400)
        for suffix in ('-1', '01', '99999999999', "0 OR 1=1", '０'):
            self.assertEqual(self.call('get_evidence', {'evidence_id': f'LTASK:{self.run}:{suffix}'}).status_code, 400)
        self.assertEqual(self.call('get_evidence', {'evidence_id': f'LTASK:{self.run}:9999'}).status_code, 404)
        fresh = type(self.service)(self.app.state.logistics_runs)
        def business(result):
            return {k: v for k, v in result.items() if k not in ('call_id', 'result_evidence_id', 'result_fingerprint')}
        self.assertEqual(business(fresh.call(self.user, self.run, 'get_evidence', {'evidence_id': identifier})),
                         business(self.result('get_evidence', {'evidence_id': identifier})))

    def test_live_permissions_foreign_snapshot_disabled_deleted_batch_and_cached_dispatcher(self):
        bound = self.service.for_run(self.user, self.run)
        snapshot = self.app.state.logistics_runs.saved(self.run)['snapshot_id']
        self.login('reviewer')
        self.assertEqual(self.call('get_snapshot_summary').status_code, 200)
        self.login('admin')
        self.assertEqual(self.call('get_snapshot_summary').status_code, 403)
        self.login('reader')
        for sql, undo in (
                ("UPDATE logistics_grants SET regions='[\"6\"]' WHERE account='reader'", "UPDATE logistics_grants SET regions='[\"5\"]' WHERE account='reader'"),
                ("UPDATE business_members SET enabled=0 WHERE account='reader'", "UPDATE business_members SET enabled=1 WHERE account='reader'"),
                ("UPDATE logistics_snapshots SET workspace_id='foreign'", "UPDATE logistics_snapshots SET workspace_id='shanghai-logistics'"),
                ("UPDATE logistics_runs SET workspace_id='foreign'", "UPDATE logistics_runs SET workspace_id='shanghai-logistics'")):
            self.data.db.execute(sql)
            for name in REGISTRY:
                args = {'evidence_id': f'LTASK:{self.run}:0'} if name == 'get_evidence' else {}
                self.assertEqual(self.call(name, args).status_code, 403)
                with self.assertRaises(APIError):
                    bound.call(name, args)
            self.data.db.execute(undo)
        self.data.db.execute("UPDATE database_source_connections SET batch_id='other'")
        self.assertEqual(self.call('get_snapshot_summary').status_code, 409)
        self.data.db.execute("UPDATE database_source_connections SET batch_id='batch'")
        self.data.db.execute('UPDATE logistics_snapshots SET status=? WHERE snapshot_id=?', ('failed', snapshot))
        self.assertEqual(self.call('get_snapshot_summary').status_code, 409)
        self.data.db.execute('UPDATE logistics_snapshots SET status=? WHERE snapshot_id=?', ('ready', snapshot))
        self.app.state.auth.query("DELETE FROM users WHERE account='reader'")
        self.assertEqual(self.call('get_snapshot_summary').status_code, 401)
        with self.assertRaises(APIError):
            bound.call('get_snapshot_summary', {})

    def test_empty_unavailable_error_and_zero_risk_are_distinct(self):
        self.records.clear()
        self.run = self.new_run('empty')
        for name in REGISTRY:
            if name != 'get_evidence':
                result = self.result(name)
                self.assertEqual(result['status'], 'empty')
                self.assertEqual(result['total_rows'], 0)
        self.records[:] = [{**fixture_records()[3], 'batch_id': 'batch'}]  # 已完成，当前无积压。
        self.run = self.new_run('completed')
        summary = self.result('get_snapshot_summary')
        self.assertEqual(summary['status'], 'ok')
        self.assertEqual(summary['data']['metrics']['overdue']['value'], 0)
        workload = self.result('get_courier_workload')
        self.assertEqual(workload['rows'][0]['metrics']['backlog']['value'], 0)
        self.records[:] = [{**fixture_records()[0], 'batch_id': 'batch', 'time_window_end': ''}]
        self.run = self.new_run('missing')
        summary = self.result('get_snapshot_summary')
        self.assertEqual(summary['status'], 'unavailable')
        self.assertIsNone(summary['data']['metrics']['overdue']['value'])
        for name in ('get_pending_tasks', 'get_risk_distribution', 'get_courier_workload', 'get_quality_summary'):
            self.assertEqual(self.result(name)['status'], 'unavailable')
        for order in ('overdue_minutes_desc', 'remaining_minutes_asc'):
            self.assertIsNone(self.result('get_pending_tasks', {'sort': order})['rows'][0]['remaining_minutes'])
        with patch('backend.logistics_tools.calculate', side_effect=RuntimeError('SECRET_CONNECTION')):
            result = self.call('get_snapshot_summary').json()
            self.assertEqual(result['status'], 'error')
            self.assertIsNone(result['total_rows'])
            self.assertNotIn('SECRET_CONNECTION', json.dumps(result))

    def test_future_invariance_for_all_tools_and_visible_quality(self):
        def outputs():
            result = {name: self.result(name) for name in REGISTRY if name != 'get_evidence'}
            evidence = result['get_pending_tasks']['rows'][0]['evidence_id']
            result['get_evidence'] = self.result('get_evidence', {'evidence_id': evidence})
            for value in result.values():
                for key in ('call_id', 'result_evidence_id', 'result_fingerprint'):
                    value.pop(key, None)
            text = json.dumps(result, sort_keys=True).replace(self.run, 'RUN')
            snapshot = self.app.state.logistics_runs.saved(self.run)['snapshot_id']
            return text.replace(snapshot, 'SNAPSHOT')
        baseline = outputs()
        self.records.append({**deepcopy(self.records[-1]), 'order_id': 'FUTURE_SECRET', 'source_row': 100,
                             'accept_time': '07-03 10:00:00', 'pickup_time': '07-04 18:00:00'})
        for row in self.records:
            row['quality_flags'] = ['FUTURE_SECRET']
            row['duration_minutes'] = 999
            if row['pickup_time'] > '07-02 10:00:00':
                row.update(pickup_time='07-04 18:00:00', ds='0704', pickup_gps_time='07-04 18:01:00',
                           pickup_gps_lng='FUTURE_SECRET', pickup_gps_lat='FUTURE_SECRET')
            # 当前无GPS的任务：未来采样与截至T无可用采样必须不可区分。
            row.update(accept_gps_time='07-04 00:00:00', accept_gps_lng='121.4', accept_gps_lat='31.2')
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        logging.getLogger().addHandler(handler)
        try:
            self.run = self.new_run('future')
            future = outputs()
            self.assertEqual(baseline, future)
            self.assertNotIn('FUTURE_SECRET', future)
            self.assertNotIn('FUTURE_SECRET', stream.getvalue())
        finally:
            logging.getLogger().removeHandler(handler)

    def test_quality_known_zero_missing_and_unsupported_metric_version(self):
        self.records[:] = [{**fixture_records()[0], 'batch_id': 'batch',
                           'accept_gps_time': '07-02 09:00:00', 'accept_gps_lng': '121.4', 'accept_gps_lat': '31.2'}]
        self.run = self.new_run('quality-known')
        quality = self.result('get_quality_summary')
        self.assertEqual(quality['status'], 'ok')
        self.assertEqual(quality['data']['quality']['pickup_gps']['denominator'], 0)
        self.assertEqual(quality['data']['quality']['pickup_gps']['not_applicable'], 1)
        self.assertIsNone(quality['data']['quality']['pickup_gps']['available_share'])
        snapshot = self.app.state.logistics_runs.saved(self.run)['snapshot_id']
        saved = self.data.one('SELECT metadata_json FROM logistics_snapshots WHERE snapshot_id=?', (snapshot,))
        metadata = json.loads(saved['metadata_json'])
        metadata['metric_version'] = 'unknown'
        self.data.db.execute('UPDATE logistics_snapshots SET metadata_json=? WHERE snapshot_id=?', (json.dumps(metadata), snapshot))
        for name in REGISTRY:
            result = self.call(name, {'evidence_id': f'LTASK:{self.run}:0'} if name == 'get_evidence' else {}).json()
            self.assertEqual(result['status'], 'unavailable')
            self.assertIsNone(result['total_rows'])
        metadata['metric_version'] = None
        self.data.db.execute('UPDATE logistics_snapshots SET metadata_json=? WHERE snapshot_id=?', (json.dumps(metadata), snapshot))
        self.assertEqual(self.result('get_snapshot_summary')['metric_version'], 'logistics-metrics-v1')


if __name__ == '__main__':
    unittest.main()
