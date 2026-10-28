"""T03：精确字段、未来不变性、持久化及后端权限边界。"""
from contextlib import contextmanager
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from .auth import now, password_hash
from .common import APIError
from .logistics_access import WORKSPACE
from .logistics_repository import LogisticsRepository, reference_time, source_connection
from .logistics_snapshots import SnapshotService, mask_row, content_hash
from .main import create_app


def task(order='A', **changes):
    row = dict(batch_id='batch', source_file='data/pickup_sh.csv', source_row=2,
               order_id=order, city='Shanghai', region_id='5', courier_id='001',
               aoi_id='001', aoi_type='0', accept_time='07-02 09:40:00',
               pickup_time='07-02 10:20:00', time_window_start='07-02 09:00:00',
               time_window_end='07-02 10:10:00', ds='0702', lng='121.4', lat='31.2',
               accept_gps_time='07-02 09:40:01', accept_gps_lng='121.4', accept_gps_lat='31.2',
               pickup_gps_time='07-02 10:20:01', pickup_gps_lng='121.4', pickup_gps_lat='31.2',
               quality_flags=['final-late'], duration_minutes=40, is_late=True,
               eligible_duration=False, raw_json={'future': 'secret'})
    row.update(changes)
    return row


class MaskTests(unittest.TestCase):
    def setUp(self):
        self.t = reference_time('2000-07-02 10:00:00', 'T', business=True)

    def test_exact_pending_fields_and_future_invariance(self):
        source = task(accept_gps_time='07-02 10:10:00')
        row = mask_row(source, self.t)
        expected = {'batch_id','source_file','source_row','order_id','city','region_id','courier_id',
                    'aoi_id','aoi_type','accept_time','task_key','accept_time_reference',
                    'time_window_start','time_window_start_reference','time_window_end',
                    'time_window_end_reference','completion_observed','lng','lat','visible_quality'}
        self.assertEqual(set(row), expected)
        changed = task(pickup_time='07-03 18:00:00', ds='0703', quality_flags=['other'],
                       accept_gps_time='07-04 00:00:00', pickup_gps_time='07-05 00:00:00',
                       pickup_gps_lng='not-a-number', duration_minutes=999)
        self.assertEqual(row, mask_row(changed, self.t))
        self.assertEqual(content_hash([row]), content_hash([mask_row(changed, self.t)]))
        missing = deepcopy(source)
        missing.update(accept_gps_time='', accept_gps_lng='', accept_gps_lat='')
        self.assertEqual(row, mask_row(missing, self.t))
        self.assertEqual(row['courier_id'], '001')
        self.assertFalse(row['completion_observed'])

    def test_event_boundaries_cross_day_and_gps_gate(self):
        self.assertIsNone(mask_row(task(accept_time='07-02 10:00:01'), self.t))
        self.assertIsNotNone(mask_row(task(accept_time='07-02 10:00:00'), self.t))
        self.assertIsNotNone(mask_row(task(accept_time='07-01 23:00:00'), self.t))
        row = mask_row(task(pickup_time='07-02 10:00:00', pickup_gps_time='07-02 10:00:01'), self.t)
        self.assertTrue(row['completion_observed'])
        self.assertNotIn('pickup_gps_time', row)
        row = mask_row(task(pickup_time='07-02 10:00:00', pickup_gps_time='07-02 10:00:00'), self.t)
        self.assertIn('pickup_gps_time', row)
        self.assertNotIn('pickup_gps_time', mask_row(task(pickup_gps_time='07-02 09:50:00'), self.t))
        row = mask_row(task(time_window_start='', time_window_end='', lng='NaN'), self.t)
        self.assertIsNone(row['time_window_end_reference'])
        self.assertEqual(row['visible_quality']['window'], 'unavailable')
        self.assertNotIn('lng', row)

    def test_data_errors_do_not_echo_source(self):
        for changes in ({'accept_time':'07-32 00:00:00'}, {'pickup_time':'07-01 00:00:00'},
                        {'order_id':''}, {'source_file':'C:\\secret.csv'},
                        {'time_window_end':'07-01 00:00:00'}, {'pickup_time':'FUTURE_SECRET'}):
            with self.subTest(changes=changes), self.assertRaises(APIError) as error:
                mask_row(task(**changes), self.t)
            self.assertNotIn('FUTURE_SECRET', json.dumps(error.exception.body))
        for value in ('07-02 10:00:00','2026-07-02 10:00:00','2000-02-30 10:00:00','2000-07-02T10:00:00','2000-07-02 10:00:00+08:00'):
            with self.assertRaises(APIError): reference_time(value, 'T', business=True)

    def test_no_application_connection_fallback(self):
        with patch('backend.logistics_repository.psycopg.connect') as connect:
            with self.assertRaises(APIError):
                with source_connection({'ANALYST_DATABASE_URL':'secret'}): pass
            connect.assert_not_called()


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = {'AUTH_DB_PATH':str(Path(self.temp.name)/'auth.sqlite'),
                    'DATA_DB_PATH':str(Path(self.temp.name)/'data.sqlite'), 'NODE_ENV':'test',
                    'LOGISTICS_SOURCE_DATABASE_URL':''}
        self.app = create_app(self.env)
        self.client = TestClient(self.app).__enter__()
        self.data = self.app.state.data
        self.access = self.app.state.access
        for account, role in [('reader','investigator'),('reviewer','approver'),('admin','system_admin'),('pending',None)]:
            self.app.state.auth.query("INSERT INTO users(account,type,name,password_hash,created_at) VALUES(?,'email',?,?,?)", (account,account,password_hash('password123'),now()))
            self.access.sync()
            if role:
                self.data.db.execute('INSERT INTO logistics_grants VALUES(?,?,?,?,?)',
                                     (WORKSPACE,account,role,'Shanghai',json.dumps(['5'] if role != 'system_admin' else [])))
        self.data.db.execute('INSERT INTO database_source_connections VALUES(?,?,?)',(WORKSPACE,'batch','now'))
        self.records = [task(),task('B',source_row=3,accept_time='07-01 23:00:00'),
                        task('F',source_row=4,accept_time='07-02 10:00:01')]
        records = self.records
        @contextmanager
        def read(repository, batch, city, regions):
            yield {'source_sha256':'a'*64,'content_sha256':'b'*64}, records
        self.patch = patch.object(LogisticsRepository,'read',read)
        self.patch.start()
        self.body = dict(batch_id='batch',city='Shanghai',region_ids=['5'],business_as_of='2000-07-02 10:00:00')
        self.login('reader')

    def tearDown(self):
        self.patch.stop()
        self.client.__exit__(None,None,None)
        self.temp.cleanup()

    def login(self, account):
        self.assertEqual(self.client.post('/api/auth/login',json={'account':account,'password':'password123'}).status_code,200)

    def create(self, **changes):
        return self.client.post('/api/logistics/snapshots',json={**self.body,**changes})

    def test_create_persist_read_pagination_and_future_hash(self):
        result = self.create()
        self.assertEqual(result.status_code,200,result.text)
        metadata = result.json()
        self.assertEqual(metadata['coverage']['visible_tasks'],2)
        identifier = metadata['snapshot_id']
        SnapshotService(self.access,self.data,self.env)  # 幂等迁移，保存内容仍可读取。
        response = self.client.get(f'/api/logistics/snapshots/{identifier}?page_size=1').json()
        self.assertEqual(response['rows'][0]['order_id'],'A')
        self.assertTrue(response['truncated'])
        for forbidden in ('quality_flags','duration_minutes','pickup_time','raw_json'):
            self.assertNotIn(forbidden,response['rows'][0])
        self.records[0].update(pickup_time='07-03 10:00:00',ds='0703',quality_flags=['secret'])
        self.records.append(task('NEW',source_row=5,accept_time='07-02 11:00:00',pickup_time='07-02 12:00:00'))
        new = self.create().json()
        self.assertEqual(metadata['visible_content_hash'],new['visible_content_hash'])
        self.assertEqual(metadata['snapshot_fingerprint'],new['snapshot_fingerprint'])
        self.assertNotEqual(metadata['snapshot_id'],new['snapshot_id'])
        with patch.object(LogisticsRepository,'read',side_effect=AssertionError('read must use persisted mask')):
            self.assertEqual(self.client.get(f'/api/logistics/snapshots/{identifier}').status_code,200)
        self.assertEqual(self.client.get(f'/api/logistics/snapshots/{identifier}?business_as_of=anything').status_code,400)
        self.assertEqual(self.create(snapshot_id=identifier).status_code,400)

    def test_authorization_every_read_no_admin_bypass(self):
        identifier = self.create().json()['snapshot_id']
        for account in ('pending','admin'):
            self.login(account)
            self.assertEqual(self.create().status_code,403)
            self.assertEqual(self.client.get(f'/api/logistics/snapshots/{identifier}').status_code,403)
        self.login('reviewer')
        self.assertEqual(self.client.get(f'/api/logistics/snapshots/{identifier}').status_code,200)
        self.assertEqual(self.create(region_ids=['5','6']).status_code,403)
        self.assertEqual(self.create(city='Beijing').status_code,400)
        self.assertEqual(self.create(batch_id='other').status_code,409)
        self.assertEqual(self.client.get(f'/api/logistics/snapshots/{identifier}',headers={'X-Workspace-Id':'other'}).status_code,403)
        self.data.db.execute("DELETE FROM logistics_grants WHERE account='reviewer'")
        self.assertEqual(self.client.get(f'/api/logistics/snapshots/{identifier}').status_code,403)
        self.login('reader')
        self.data.db.execute("UPDATE business_members SET enabled=0 WHERE account='reader'")
        self.assertEqual(self.client.get(f'/api/logistics/snapshots/{identifier}').status_code,403)

    def test_build_failure_has_no_rows_and_is_not_readable(self):
        self.records.append(task('A',source_row=8))
        result = self.create()
        self.assertEqual(result.status_code,409)
        saved = self.data.one('SELECT * FROM logistics_snapshots')
        self.assertEqual(saved['status'],'failed')
        self.assertEqual(self.data.all('SELECT * FROM logistics_snapshot_rows'),[])
        self.assertEqual(self.client.get('/api/logistics/snapshots/'+saved['snapshot_id']).status_code,409)

    def test_permission_revoked_during_build_and_batch_changed(self):
        @contextmanager
        def revoke(*args):
            self.data.db.execute("DELETE FROM logistics_grants WHERE account='reader'")
            yield {'source_sha256':'a'*64,'content_sha256':'b'*64}, self.records
        with patch.object(LogisticsRepository,'read',revoke):
            self.assertEqual(self.create().status_code,403)
        self.assertEqual(self.data.all('SELECT * FROM logistics_snapshot_rows'),[])

    def test_combined_view_grants_and_mid_build_version_change(self):
        self.data.db.execute('INSERT INTO logistics_grants VALUES(?,?,?,?,?)',
                             (WORKSPACE,'reader','approver','Shanghai','["6"]'))
        self.assertEqual(self.create(region_ids=['5','6']).status_code,200)
        @contextmanager
        def change(*args):
            self.data.db.execute("UPDATE business_members SET version=version+1 WHERE account='reader'")
            yield {'source_sha256':'a'*64,'content_sha256':'b'*64}, self.records
        with patch.object(LogisticsRepository,'read',change):
            self.assertEqual(self.create().status_code,409)

    def test_empty_snapshot_and_batch_change(self):
        self.records.clear()
        metadata = self.create().json()
        url = '/api/logistics/snapshots/'+metadata['snapshot_id']
        self.assertEqual(self.client.get(url).json()['status'],'empty')
        self.data.db.execute("UPDATE database_source_connections SET batch_id='new'")
        self.assertEqual(self.client.get(url).status_code,409)


if __name__ == '__main__':
    unittest.main()
