"""物流原始数据管理预览；不是 Agent 快照，不开放给调查/审核角色。"""
from fastapi import APIRouter, Request
from fastapi.encoders import jsonable_encoder
from psycopg import sql

from .common import APIError, iso
from .database_connection import connection
from .logistics_access import WORKSPACE


def routes(access, data, env):
    router = APIRouter(prefix='/api/logistics/data', tags=['物流源数据管理'])

    def batches(db):
        return db.execute('''SELECT batch_id,row_count,source_sha256,reference_year,actual_year,year_status
            FROM lade.import_batches WHERE verified_at IS NOT NULL ORDER BY imported_at DESC,batch_id''').fetchall()

    def selected(db):
        saved = data.one('SELECT batch_id FROM database_source_connections WHERE workspace_id=?', (WORKSPACE,))
        values = batches(db)
        batch = saved['batch_id'] if saved else (values[0]['batch_id'] if values else '')
        if not batch or batch not in {b['batch_id'] for b in values}:
            raise APIError(409, '没有可用的已核验上海揽收批次，请联系数据管理员。')
        return batch, values, bool(saved)

    @router.get('/connection')
    def status(request: Request):
        user = access.user(request)
        access.require(user, 'source.read')
        with connection(env) as db:
            batch, values, saved = selected(db)
        # 不返回全库逐区域统计或行数给仅有局部区域权的成员。
        return {'connected': True, 'configured': saved, 'batchId': batch,
                'batches': [{'batch_id': b['batch_id'], 'reference_year': b['reference_year'], 'year_status': b['year_status']} for b in values],
                'regions': access.allowed_regions(user, 'source.read'), 'workspaceId': WORKSPACE}

    @router.post('/connection')
    def save(request: Request):
        user = access.user(request)
        # 批次变更影响全空间，须完整数据管理员授权。
        from .logistics_access import REGIONS
        with data.lock:
            access.require(user, 'connection', REGIONS)
            batch = request.state.body.get('batchId')
            with connection(env) as db:
                if not isinstance(batch, str) or batch not in {b['batch_id'] for b in batches(db)}:
                    raise APIError(400, '请选择已核验的导入批次。')
            old = data.one('SELECT * FROM database_source_connections WHERE workspace_id=?', (WORKSPACE,))
            import json
            data.db.execute('BEGIN IMMEDIATE')
            try:
                data.db.execute('INSERT INTO database_source_connections VALUES(?,?,?) ON CONFLICT(workspace_id) DO UPDATE SET batch_id=excluded.batch_id,saved_at=excluded.saved_at', (WORKSPACE, batch, iso()))
                data.db.execute('INSERT INTO business_audit(workspace_id,actor,account,before_json,after_json,created_at) VALUES(?,?,?,?,?,?)', (WORKSPACE,user['account'],'source',json.dumps(old),json.dumps({'batchId':batch}),iso()))
                data.db.execute('COMMIT')
            except Exception:
                data.db.execute('ROLLBACK')
                raise
        return status(request)

    @router.get('/preview')
    def preview(request: Request, region: str = '', order_id: str = '', page: int = 1):
        user = access.user(request)
        if page < 1 or page > 100000 or len(order_id) > 200:
            raise APIError(400, '分页或任务编号无效。')
        # 读取过程中锁定授权更新；每个请求重新检查，不能靠前端区域筛选。
        with data.lock:
            allowed = access.allowed_regions(user, 'source.read')
            if region:
                access.require(user, 'source.read', [region])
                allowed = [region]
            with connection(env) as db:
                # 全市 140 万行的权限过滤计数在本机可超过旧预览的 5 秒上限。
                db.execute("SET LOCAL statement_timeout = '10s'")
                batch, _, _ = selected(db)
                where = 'batch_id=%s AND city=%s AND region_id=ANY(%s)'
                params = [batch, 'Shanghai', allowed]
                if order_id:
                    where += ' AND order_id=%s'
                    params.append(order_id)
                total = db.execute('SELECT count(*) n FROM lade.pickup_tasks WHERE ' + where, params).fetchone()['n']
                columns = 'order_id region_id city courier_id accept_time time_window_start time_window_end lng lat aoi_id aoi_type pickup_time pickup_gps_time pickup_gps_lng pickup_gps_lat accept_gps_time accept_gps_lng accept_gps_lat ds source_file source_row quality_flags'.split()
                query = sql.SQL('SELECT {} FROM lade.pickup_tasks WHERE ' + where + ' ORDER BY source_row LIMIT 20 OFFSET %s').format(sql.SQL(',').join(map(sql.Identifier, columns)))
                rows = db.execute(query, [*params, (page - 1) * 20]).fetchall()
            access.require(user, 'source.read', allowed)
        return jsonable_encoder({'columns': columns, 'rows': rows, 'total': total, 'page': page, 'pageSize': 20,
                                  'batchId': batch, 'regions': allowed, 'mode': 'historical-source-preview'})

    return router
