"""T03：固定时点、白名单掩码和实时授权的持久化快照。"""
import hashlib
import json
import math
from pathlib import PurePosixPath, PureWindowsPath
import uuid

from fastapi import APIRouter, Request

from .common import APIError, iso
from .logistics_access import WORKSPACE, REGIONS
from .logistics_repository import LogisticsRepository, reference_time
from .logistics_metrics import METRIC_VERSION

CONTRACT = 'logistics-snapshot-v1'
SCHEMA = '''
CREATE TABLE IF NOT EXISTS logistics_snapshots(
 snapshot_id TEXT PRIMARY KEY,workspace_id TEXT NOT NULL,batch_id TEXT NOT NULL,
 city TEXT NOT NULL,regions_json TEXT NOT NULL,business_as_of TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('building','ready','failed')),
 metadata_json TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS logistics_snapshot_rows(
 snapshot_id TEXT NOT NULL REFERENCES logistics_snapshots(snapshot_id),
 row_index INTEGER NOT NULL,row_json TEXT NOT NULL,PRIMARY KEY(snapshot_id,row_index));
'''
LIMITATIONS = ['历史已完成任务样本，积压不代表生产全量未完成或取消任务。',
              '2000 年为参考时间，真实年份未知；静态属性和窗口假设接单时已知。',
              'GPS 仅作粗粒度辅助，坐标系未认证。']


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def content_hash(value):
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


def coordinates(lng, lat):
    try:
        pair = float(lng), float(lat)
        return pair if all(math.isfinite(v) for v in pair) and -180 <= pair[0] <= 180 and -90 <= pair[1] <= 90 else None
    except (ValueError, TypeError):
        return None


def mask_row(raw, moment):
    accepted = reference_time(raw.get('accept_time'), 'accept_time')
    completed = reference_time(raw.get('pickup_time'), 'pickup_time')
    if completed < accepted:
        raise APIError(409, '任务完成早于接单，批次数据无效。')
    if accepted > moment:
        return None
    for key in ('order_id', 'region_id', 'city', 'courier_id', 'aoi_id', 'aoi_type'):
        if not isinstance(raw.get(key), str) or not raw[key]:
            raise APIError(409, '任务身份或静态字段缺失。')
    source = raw.get('source_file')
    if (not isinstance(source, str) or not source or PureWindowsPath(source).drive
            or PureWindowsPath(source).is_absolute() or PurePosixPath(source).is_absolute()
            or '..' in PurePosixPath(source.replace('\\', '/')).parts):
        raise APIError(409, '任务来源必须为逻辑相对名称。')
    if type(raw.get('source_row')) is not int or raw['source_row'] < 2:
        raise APIError(409, '任务来源行无效。')
    row = {k: raw[k] for k in ('batch_id', 'source_file', 'source_row', 'order_id', 'city', 'region_id', 'courier_id', 'aoi_id', 'aoi_type', 'accept_time')}
    row['task_key'] = {'dataset': 'LaDe-P', 'city': row['city'], 'order_id': row['order_id']}
    row['accept_time_reference'] = accepted.isoformat(sep=' ')
    window = {}
    for key in ('time_window_start', 'time_window_end'):
        window[key] = reference_time(raw.get(key), key, optional=True)
        row[key] = raw.get(key) or None
        row[key + '_reference'] = window[key].isoformat(sep=' ') if window[key] else None
    start, end = window.values()
    if start and end and end < start:
        raise APIError(409, '要求窗口终点早于起点。')
    quality = {'window': 'available' if start and end else 'unavailable'}
    pair = coordinates(raw.get('lng'), raw.get('lat'))
    quality['stop_coordinates'] = 'available' if pair else 'unavailable'
    if pair:
        row.update(lng=pair[0], lat=pair[1])
    observed = completed <= moment
    row['completion_observed'] = observed
    if observed:
        if raw.get('ds') != completed.strftime('%m%d'):
            raise APIError(409, '可见完成日期与完成事件不一致。')
        row.update(pickup_time=raw['pickup_time'], pickup_time_reference=completed.isoformat(sep=' '), ds=raw['ds'])
    for prefix, event_visible in (('accept', True), ('pickup', observed)):
        time_key = prefix + '_gps_time'
        quality[prefix + '_gps'] = '截至 T 无可用采样'
        # 完成未发生时不解析/诊断其 GPS；不可见采样与原始缺失输出相同。
        if not event_visible:
            continue
        sampled = reference_time(raw.get(time_key), time_key, optional=True)
        pair = coordinates(raw.get(prefix + '_gps_lng'), raw.get(prefix + '_gps_lat'))
        if sampled and sampled <= moment and pair:
            row.update({time_key: raw[time_key], time_key + '_reference': sampled.isoformat(sep=' '),
                        prefix + '_gps_lng': pair[0], prefix + '_gps_lat': pair[1]})
            quality[prefix + '_gps'] = 'available'
    row['visible_quality'] = quality
    # 聚合指标及风险排序属于 T04；本阶段只交付可见事件与字段。
    return row


class SnapshotService:
    def __init__(self, access, data, env):
        self.access, self.data = access, data
        self.repository = LogisticsRepository(env)
        with data.lock:
            data.db.executescript(SCHEMA)

    def authorize(self, user, batch, regions, city):
        current = self.access.auth.query('SELECT id FROM users WHERE account=?', (user.get('account'),))
        if not current or current['id'] != user.get('id'):
            raise APIError(403, '快照请求账号已失效。')
        self.access.require(user, 'view', city=city)
        if not set(regions).issubset(self.access.allowed_regions(user, 'view')):
            raise APIError(403, '请求包含未授权物流区域。')
        saved = self.data.one('SELECT batch_id FROM database_source_connections WHERE workspace_id=?', (WORKSPACE,))
        if not saved or saved['batch_id'] != batch:
            raise APIError(409, '批次与空间已保存的数据批次不一致。')
        return self.access.member(user['account'])['version']

    def create(self, user, body, *, operation_id=None):
        if set(body) - {'batch_id', 'city', 'region_ids', 'business_as_of'}:
            raise APIError(400, '快照参数包含不允许的字段。')
        batch, city, regions = body.get('batch_id'), body.get('city'), body.get('region_ids')
        if (not isinstance(batch, str) or not batch or len(batch) > 200 or city != 'Shanghai'
                or not isinstance(regions, list) or not regions
                or any(not isinstance(r, str) or r not in REGIONS for r in regions)):
            raise APIError(400, '批次、城市或区域参数无效。')
        regions = sorted(set(regions))
        moment = reference_time(body.get('business_as_of'), 'business_as_of', business=True)
        with self.data.lock:
            version = self.authorize(user, batch, regions, city)
            identifier = 'SNP-'+hashlib.sha256(f'{user["id"]}:{operation_id}'.encode()).hexdigest()[:32] if operation_id else 'SNP-'+uuid.uuid4().hex
            if operation_id:
                saved = self.data.one('SELECT * FROM logistics_snapshots WHERE snapshot_id=?', (identifier,))
                if saved:
                    if (saved['batch_id'], saved['city'], json.loads(saved['regions_json']), saved['business_as_of']) != (batch, city, regions, body['business_as_of']):
                        raise APIError(409, '快照操作标识已用于不同内容。')
                    return self.ready_metadata(user, identifier)
            metadata = dict(snapshot_id=identifier, workspace_id=WORKSPACE,
                            dataset='LaDe-P', batch_id=batch, city=city, region_ids=regions,
                            business_as_of=body['business_as_of'], reference_year=2000,
                            actual_year=None, year_status='undisclosed', contract_version=CONTRACT,
                            metric_version=METRIC_VERSION, permission_version=version, status='building', created_at=iso())
            self.data.db.execute('INSERT INTO logistics_snapshots VALUES(?,?,?,?,?,?,?,?,?)',
                                 (metadata['snapshot_id'], WORKSPACE, batch, city, canonical(regions),
                                  metadata['business_as_of'], 'building', canonical(metadata), metadata['created_at']))
        try:
            # ponytail: 首版可见行在内存组装；全市快照超过内存预算时改为临时表排序和流式摘要。
            rows, keys = [], set()
            with self.repository.read(batch, city, regions) as (source, records):
                for raw in records:
                    if raw.get('batch_id') != batch or raw.get('city') != city or raw.get('region_id') not in regions:
                        raise APIError(409, '源记录与授权范围不一致。')
                    identity = (raw.get('city'), raw.get('order_id'))
                    if not isinstance(identity[1], str) or not identity[1] or identity in keys:
                        raise APIError(409, '批次任务编号缺失或冲突。')
                    keys.add(identity)
                    row = mask_row(raw, moment)
                    if row is not None:
                        rows.append(row)
                metadata['data_version'] = content_hash({k: source[k] for k in ('source_sha256', 'content_sha256')})
            rows.sort(key=lambda r: (r['city'], r['order_id'], r['batch_id'], r['source_row']))
            metadata.update(visible_content_hash=content_hash(rows),
                            coverage={'visible_tasks': len(rows), 'scope': 'sample'}, limitations=LIMITATIONS)
            metadata['snapshot_fingerprint'] = content_hash({k: metadata[k] for k in (
                'workspace_id', 'data_version', 'batch_id', 'city', 'region_ids', 'business_as_of',
                'contract_version', 'metric_version', 'visible_content_hash')})
            with self.data.lock:
                if self.authorize(user, batch, regions, city) != version:
                    raise APIError(409, '构建期间成员授权版本已改变，请重新创建快照。')
                metadata['status'] = 'ready'
                self.data.db.execute('BEGIN IMMEDIATE')
                try:
                    self.data.db.executemany('INSERT INTO logistics_snapshot_rows VALUES(?,?,?)',
                                             ((metadata['snapshot_id'], i, canonical(r)) for i, r in enumerate(rows)))
                    self.data.db.execute('UPDATE logistics_snapshots SET status=?,metadata_json=? WHERE snapshot_id=?',
                                         ('ready', canonical(metadata), metadata['snapshot_id']))
                    self.data.db.execute('COMMIT')
                except Exception:
                    self.data.db.execute('ROLLBACK')
                    raise
            return metadata
        except Exception:
            with self.data.lock:
                metadata['status'] = 'failed'
                self.data.db.execute('UPDATE logistics_snapshots SET status=?,metadata_json=? WHERE snapshot_id=?',
                                     ('failed', canonical(metadata), metadata['snapshot_id']))
            raise

    def read(self, user, snapshot_id, *, page=1, page_size=20):
        if type(page) is not int or type(page_size) is not int or not 1 <= page <= 100000 or not 1 <= page_size <= 100:
            raise APIError(400, '快照分页参数无效。')
        with self.data.lock:
            metadata = self.ready_metadata(user, snapshot_id)
            rows = self.data.all('SELECT row_json FROM logistics_snapshot_rows WHERE snapshot_id=? ORDER BY row_index LIMIT ? OFFSET ?',
                                 (snapshot_id, page_size, (page - 1) * page_size))
            total = metadata['coverage']['visible_tasks']
            return {'snapshot': metadata, 'rows': [json.loads(r['row_json']) for r in rows],
                    'page': page, 'page_size': page_size, 'total_rows': total, 'returned_rows': len(rows),
                    'truncated': (page - 1) * page_size + len(rows) < total,
                    'status': 'ok' if total else 'empty'}

    def ready_metadata(self, user, snapshot_id):
        """调用方持有 data.lock；所有读取共用同一授权及状态门禁。"""
        saved = self.data.one('SELECT * FROM logistics_snapshots WHERE snapshot_id=?', (snapshot_id,))
        if not saved:
            raise APIError(404, '快照不存在。')
        if saved['workspace_id'] != WORKSPACE:
            raise APIError(403, '快照不属于当前空间。')
        self.authorize(user, saved['batch_id'], json.loads(saved['regions_json']), saved['city'])
        if saved['status'] != 'ready':
            raise APIError(409, '快照尚未就绪或构建失败。')
        return json.loads(saved['metadata_json'])

    def read_all(self, user, snapshot_id):
        """仅宿主指标复算用；HTTP/模型不提供全量行导出入口。"""
        with self.data.lock:
            metadata = self.ready_metadata(user, snapshot_id)
            rows = self.data.all('SELECT row_json FROM logistics_snapshot_rows WHERE snapshot_id=? ORDER BY row_index', (snapshot_id,))
            return metadata, [json.loads(r['row_json']) for r in rows]

    def routes(self):
        router = APIRouter(prefix='/api/logistics/snapshots', tags=['物流时点快照'])

        @router.get('/config')
        def config(request: Request):
            user = self.access.user(request)
            with self.data.lock:
                regions = self.access.allowed_regions(user, 'view')
                saved = self.data.one('SELECT batch_id FROM database_source_connections WHERE workspace_id=?', (WORKSPACE,))
                return {'batch_id': saved['batch_id'] if saved else None, 'city': 'Shanghai',
                        'region_ids': regions, 'reference_year': 2000, 'actual_year': None,
                        'year_status': 'undisclosed', 'configured': bool(saved)}

        @router.post('')
        def create(request: Request):
            return self.create(self.access.user(request), request.state.body)

        @router.get('/{snapshot_id}')
        def read(request: Request, snapshot_id: str, page: int = 1, page_size: int = 20):
            if set(request.query_params) - {'page', 'page_size', 'workspaceId'}:
                raise APIError(400, '快照读取不能覆盖业务时刻、批次或区域。')
            return self.read(self.access.user(request), snapshot_id, page=page, page_size=page_size)

        return router
