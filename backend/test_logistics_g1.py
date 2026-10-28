"""G1：完整 HTTP 数据边界、未来不变性与权限攻击入口。"""
from copy import deepcopy
import io
import json
import logging
import unittest

from . import test_logistics_snapshots as support
from scripts.reference_logistics_metrics import fixture_records


class G1Tests(unittest.TestCase):
    setUp = support.SnapshotTests.setUp
    tearDown = support.SnapshotTests.tearDown
    login = support.SnapshotTests.login
    create = support.SnapshotTests.create

    def outputs(self):
        response=self.create()
        self.assertEqual(response.status_code,200,response.text)
        metadata=response.json()
        base='/api/logistics/snapshots/'+metadata['snapshot_id']
        results={}
        for path in ('','/summary','/tasks?page_size=2','/tasks?page=2&page_size=2','/groups?dimension=aoi_id'):
            response=self.client.get(base+path)
            self.assertEqual(response.status_code,200,response.text)
            self.assertEqual(response.headers['Cache-Control'],'no-store')
            result=response.json()
            if path=='':
                result['snapshot']={k:v for k,v in result['snapshot'].items() if k not in ('snapshot_id','created_at')}
            result.pop('snapshot_id',None)
            results[path]=result
        return metadata,results

    def test_future_changes_do_not_affect_browser_content_sorting_or_export_serialization(self):
        self.records[:]=fixture_records()
        for row in self.records: row['batch_id']='batch'
        stream=io.StringIO(); handler=logging.StreamHandler(stream)
        logger=logging.getLogger(); logger.addHandler(handler)
        try:
            before,outputs=self.outputs()
            for row in self.records:
                row['quality_flags']=['FUTURE_SECRET']
                row['eligible_lifecycle']=False
                if row['pickup_time']>'07-02 10:00:00':
                    row.update(pickup_time='07-03 18:00:00',ds='0703',pickup_gps_time='07-04 00:00:00',
                               pickup_gps_lng='FUTURE_SECRET',pickup_gps_lat='FUTURE_SECRET',is_late=True,duration_minutes=999)
                row.update(accept_gps_time='07-04 00:00:00',accept_gps_lng='121.4',accept_gps_lat='31.2')
            self.records.append({**deepcopy(self.records[-1]),'order_id':'FUTURE_SECRET',
                                 'source_row':100,'accept_time':'07-03 10:00:00','pickup_time':'07-04 18:00:00'})
            after,changed=self.outputs()
            self.assertEqual(outputs,changed)
            self.assertEqual(before['visible_content_hash'],after['visible_content_hash'])
            # 可下载/模型可见的业务 JSON 输入即受控响应，尚未存在的报告/工具不伪造验收。
            body=json.dumps(changed,ensure_ascii=False)
            self.assertNotIn('FUTURE_SECRET',body)
            self.assertNotIn('FUTURE_SECRET',stream.getvalue())
        finally:
            logger.removeHandler(handler)

    def test_actual_foreign_snapshot_disabled_deleted_and_scope_shrink(self):
        identifier=self.create().json()['snapshot_id']
        paths=['/api/logistics/snapshots/'+identifier+suffix for suffix in ('','/summary','/tasks','/groups')]
        self.data.db.execute('UPDATE logistics_snapshots SET workspace_id=? WHERE snapshot_id=?',('foreign-space',identifier))
        for path in paths: self.assertEqual(self.client.get(path).status_code,403)
        self.data.db.execute('UPDATE logistics_snapshots SET workspace_id=? WHERE snapshot_id=?',('shanghai-logistics',identifier))
        self.data.db.execute("UPDATE logistics_grants SET regions='[\"6\"]' WHERE account='reader'")
        for path in paths: self.assertEqual(self.client.get(path).status_code,403)
        self.assertEqual(self.create(region_ids=['5','6']).status_code,403)
        self.data.db.execute("UPDATE logistics_grants SET regions='[\"5\"]' WHERE account='reader'")
        self.data.db.execute("UPDATE business_members SET enabled=0 WHERE account='reader'")
        for path in paths: self.assertEqual(self.client.get(path).status_code,403)
        self.data.db.execute("UPDATE business_members SET enabled=1 WHERE account='reader'")
        user=self.app.state.auth.query("SELECT * FROM users WHERE account='reader'")
        self.app.state.auth.query("DELETE FROM users WHERE account='reader'")
        self.assertEqual(self.client.get(paths[0]).status_code,401)
        with self.assertRaises(support.APIError): self.app.state.snapshots.read_all(user,identifier)

    def test_config_only_returns_authorized_selection_not_source(self):
        result=self.client.get('/api/logistics/snapshots/config')
        self.assertEqual(result.status_code,200)
        self.assertEqual(result.json(),{'batch_id':'batch','city':'Shanghai','region_ids':['5'],
                                        'reference_year':2000,'actual_year':None,'year_status':'undisclosed','configured':True})
        self.assertEqual(self.client.get('/api/logistics/data/preview').status_code,403)
        self.login('admin')
        self.assertEqual(self.client.get('/api/logistics/snapshots/config').status_code,403)


if __name__=='__main__': unittest.main()
