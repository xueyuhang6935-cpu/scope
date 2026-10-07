"""Authenticated, read-only previews of available PostgreSQL business data."""
from contextlib import contextmanager
from decimal import Decimal

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row
from fastapi import APIRouter, Request
from fastapi.encoders import jsonable_encoder

from .common import APIError, iso
from .data_import import require_user


SCHEMA = "supply_demo"


@contextmanager
def connection(env, readonly=True):
    dsn = env.get("ANALYST_DATABASE_URL")
    if not dsn:
        raise APIError(503, "服务器尚未配置 PostgreSQL 连接，请设置 ANALYST_DATABASE_URL。")
    try:
        with psycopg.connect(dsn, connect_timeout=3, row_factory=dict_row) as db:
            db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            db.execute("SET TRANSACTION READ ONLY" if readonly else "SET TRANSACTION READ WRITE")
            db.execute("SET LOCAL statement_timeout = '5s'")
            db.execute("SET LOCAL lock_timeout = '3s'")
            yield db
    except psycopg.Error as exc:
        raise APIError(503, "PostgreSQL 暂不可用，请检查服务、权限或稍后重试。") from exc


def routes(auth, data, env, monitor=None, access=None):
    router = APIRouter(prefix="/api/data/database")
    with data.lock:
        data.db.execute("""CREATE TABLE IF NOT EXISTS database_connections (
            user_id INTEGER PRIMARY KEY, scenario_id TEXT NOT NULL, saved_at TEXT NOT NULL
        )""")
        data.db.execute("""CREATE TABLE IF NOT EXISTS database_source_connections (
            workspace_id TEXT PRIMARY KEY, batch_id TEXT NOT NULL, saved_at TEXT NOT NULL
        )""")

    def inspect(db):
        if db.execute("SELECT to_regclass('lade.import_batches') AS relation").fetchone()['relation']:
            batches = db.execute("""SELECT batch_id AS id, row_count, source_sha256,
                actual_year, reference_year, year_status FROM lade.import_batches
                WHERE verified_at IS NOT NULL ORDER BY imported_at DESC,batch_id""").fetchall()
            if batches:
                tables = [r['table_name'] for r in db.execute("""SELECT table_name FROM information_schema.tables
                    WHERE table_schema='lade' AND table_name IN ('pickup_tasks','import_batches','quality_rules')
                    ORDER BY table_name""").fetchall()]
                if set(tables) != {'pickup_tasks','import_batches','quality_rules'}:
                    raise APIError(409, 'LaDe 导入表不完整，请检查数据库导入结果。')
                return {'schema':'lade', 'sourceType':'lade', 'defaultTable':'pickup_tasks',
                        'tables':tables, 'scenarios':[dict(b, name=f"上海揽收 · {b['row_count']:,} 条 · {b['id'][-8:]}") for b in batches]}
        tables = db.execute("""SELECT table_name FROM information_schema.tables
            WHERE table_schema=%s AND table_type='BASE TABLE' ORDER BY table_name""", (SCHEMA,)).fetchall()
        names = [row["table_name"] for row in tables]
        if "scenarios" not in names or "inventory_snapshots" not in names:
            raise APIError(409, "数据库中缺少 supply_demo 场景或库存表，请先准备供应链数据。")
        scenarios = db.execute("SELECT id,name FROM supply_demo.scenarios ORDER BY id").fetchall()
        return {"schema": SCHEMA, "sourceType": "supply_demo", "defaultTable": "inventory_snapshots",
                "tables": names, "scenarios": scenarios}

    def lade_selection(user, options):
        workspace = access.workspace(user)
        # 工厂角色不能自动获得物流区域权限；当前仅连接管理者可预览全库。
        access.require(user, workspace, 'connection', ['A','B'])
        saved = data.one('SELECT * FROM database_source_connections WHERE workspace_id=?', (workspace,))
        selected = saved['batch_id'] if saved else options['scenarios'][0]['id']
        validate_scenario(selected, options)
        return workspace, saved, selected

    def probe_lade(db, batch):
        if not db.execute('SELECT source_row FROM lade.pickup_tasks WHERE batch_id=%s LIMIT 1', (batch,)).fetchone():
            raise APIError(409, '导入批次没有可读取的任务，请核查导入结果。')

    def selection(user_id):
        from .business_access import current_scope
        scope=current_scope.get()
        if scope:
            c=data.one('SELECT * FROM risk_monitor_configs WHERE workspace_id=?',(scope['workspace'],))
            if c:
                return {'scenario_id':c['scenario_id'],'saved_at':c['as_of']}
            if scope.get('explicit'):
                return None
        return data.one("SELECT scenario_id,saved_at FROM database_connections WHERE user_id=?", (user_id,))

    def validate_scenario(value, options):
        if not isinstance(value, str) or value not in {item["id"] for item in options["scenarios"]}:
            raise APIError(400, "请选择有效的数据场景。")

    @router.get("")
    def status(request: Request):
        user = require_user(auth, request)
        saved = selection(user["id"])
        result = {"configured": bool(saved), "connected": False, "schema": SCHEMA,
                  "scenarioId": saved["scenario_id"] if saved else "normal",
                  "savedAt": saved["saved_at"] if saved else None}
        try:
            info = conninfo_to_dict(env.get("ANALYST_DATABASE_URL") or "")
            result.update(host=info.get("host", ""), port=info.get("port", "5432"), database=info.get("dbname", ""))
            with connection(env) as db:
                options = inspect(db)
                result.update(options)
                if options['sourceType'] == 'lade':
                    workspace, selected, batch = lade_selection(user, options)
                    probe_lade(db, batch)
                    result.update(configured=bool(selected), connected=True, scenarioId=batch,
                                  savedAt=selected['saved_at'] if selected else None, workspaceId=workspace)
                    return result
                if saved:
                    validate_scenario(saved["scenario_id"], options)
                    result["connected"] = True
        except (APIError, psycopg.Error) as exc:
            result["message"] = exc.body["message"] if isinstance(exc, APIError) else "服务器数据库连接格式无效。"
        return result

    @router.post("/test")
    def test(request: Request):
        user=require_user(auth, request)
        access.require(user,access.workspace(user),'connection',['A','B'])
        with connection(env) as db:
            options = inspect(db)
            validate_scenario(request.state.body.get("scenarioId"), options)
            if options['sourceType'] == 'lade':
                probe_lade(db, request.state.body['scenarioId'])
                return {'message':'连接测试成功，揽收任务和质量标记可读取。', **options}
            db.execute("SELECT id FROM supply_demo.inventory_snapshots WHERE scenario_id=%s LIMIT 1",
                       (request.state.body["scenarioId"],)).fetchall()
        return {"message": "连接测试成功，场景和库存数据可读取。", **options}

    @router.post("")
    def save(request: Request):
        user = require_user(auth, request)
        workspace=access.workspace(user)
        access.require(user,workspace,'connection',['A','B'])
        space=data.one('SELECT * FROM business_spaces WHERE id=?',(workspace,))
        config=data.one('SELECT * FROM risk_monitor_configs WHERE workspace_id=?',(workspace,))
        scenario = request.state.body.get("scenarioId")
        with connection(env) as db:
            options = inspect(db)
            if options['sourceType'] == 'lade':
                validate_scenario(scenario, options)
                probe_lade(db, scenario)
                with data.lock:
                    data.db.execute('''INSERT INTO database_source_connections VALUES(?,?,?)
                        ON CONFLICT(workspace_id) DO UPDATE SET batch_id=excluded.batch_id,saved_at=excluded.saved_at''',
                                    (workspace, scenario, iso()))
                return status(request)
        if config and scenario!=config['scenario_id'] and space['owner_id']!=user['id']:
            raise APIError(403,'共享成员不能替换工作空间场景，请联系空间所有者创建新场景。')
        with connection(env) as db:
            options = inspect(db)
            validate_scenario(scenario, options)
            db.execute("SELECT id FROM supply_demo.inventory_snapshots WHERE scenario_id=%s LIMIT 1", (scenario,)).fetchall()
        with data.lock:
            data.db.execute("""INSERT INTO database_connections VALUES(?,?,?)
                ON CONFLICT(user_id) DO UPDATE SET scenario_id=excluded.scenario_id,saved_at=excluded.saved_at""",
                            (space['owner_id'], scenario, iso()))
        if monitor:
            monitor.wake.set()
        from .business_access import current_scope
        token=current_scope.set(None)
        try:
            target=monitor.config(space['owner_id'])
            access.ensure_space(target['workspace_id'],space['owner_id'],scenario)
            selected=current_scope.set({'user':user,'workspace':target['workspace_id'],'explicit':True})
            try:
                return {**status(request),'workspaceId':target['workspace_id']}
            finally:
                current_scope.reset(selected)
        finally:
            current_scope.reset(token)

    @router.get("/preview")
    def preview(request: Request, table: str = "", page: int = 1, factory: str = "all", region: str = "", order_id: str = ""):
        user = require_user(auth, request)
        if factory not in ('all','A','B'):
            raise APIError(400,'工厂参数无效。')
        workspace=access.workspace(user)
        if page < 1 or page > 100000:
            raise APIError(400, '分页参数无效。')
        if env.get('ANALYST_DATABASE_URL'):
            with connection(env) as db:
                options = inspect(db)
                if options['sourceType'] == 'lade':
                    _, _, batch = lade_selection(user, options)
                    table = table or options['defaultTable']
                    if table not in options['tables']:
                        raise APIError(400, '请选择当前数据区域内的有效表。')
                    if (region or order_id) and table != 'pickup_tasks':
                        raise APIError(400, '区域和任务编号筛选仅适用于揽收任务表。')
                    columns = [r['column_name'] for r in db.execute('''SELECT column_name FROM information_schema.columns
                        WHERE table_schema='lade' AND table_name=%s ORDER BY ordinal_position''', (table,)).fetchall()]
                    clauses, params = [sql.SQL('batch_id=%s')], [batch]
                    for key, value in [('region_id',region), ('order_id',order_id)]:
                        if value:
                            clauses.append(sql.SQL('{}=%s').format(sql.Identifier(key)))
                            params.append(value)
                    where = sql.SQL(' WHERE ') + sql.SQL(' AND ').join(clauses)
                    target = sql.Identifier('lade',table)
                    total = db.execute(sql.SQL('SELECT count(*) AS n FROM {}{}').format(target,where),params).fetchone()['n']
                    order = 'source_row' if table=='pickup_tasks' else 'code' if table=='quality_rules' else 'batch_id'
                    # 批次元数据不向浏览器输出本机路径和巨大的内部审计快照。
                    if table=='import_batches':
                        columns = [c for c in columns if c not in ('source_file','evidence_sha256','field_mapping_markdown','audit_summary')]
                    rows = db.execute(sql.SQL('SELECT {} FROM {}{} ORDER BY {} LIMIT 20 OFFSET %s').format(
                        sql.SQL(',').join(map(sql.Identifier,columns)),target,where,sql.Identifier(order)),
                        [*params,(page-1)*20]).fetchall()
                    return jsonable_encoder({'table':table,'columns':columns,'rows':rows,'total':total,
                        'page':page,'pageSize':20,'scenarioId':batch,'schema':'lade','factoryScoped':False,
                        'region':region,'orderId':order_id},custom_encoder={Decimal:str})
        table = table or 'inventory_snapshots'
        domain = 'inventory' if table in ('inventory_snapshots','inventory_reservations','warehouses','materials','material_revisions') else 'production' if table in ('production_plan_lines','production_lines','products','bom_headers','bom_lines') else 'quality' if table in ('quality_holds','quality_releases') else 'purchase' if table in ('purchase_order_lines','supplier_commitments','shipment_lines','receipt_lines','line_deliveries','suppliers','expedite_options','transfer_routes') else 'rules'
        allowed=[f for f in ('A','B') if access.can(user,workspace,'data.read',[f],domain=domain)]
        if not allowed or (factory!='all' and factory not in allowed):
            raise APIError(403,'没有该数据域或工厂的读取权限。')
        saved = selection(user["id"])
        if not saved:
            raise APIError(409, "请先保存数据库配置。")
        if page < 1 or page > 100000 or factory not in ("all", "A", "B"):
            raise APIError(400, "分页或工厂参数无效。")
        with connection(env) as db:
            options = inspect(db)
            validate_scenario(saved["scenario_id"], options)
            if table not in options["tables"]:
                raise APIError(400, "请选择当前数据区域内的有效表。")
            columns = [row["column_name"] for row in db.execute("""SELECT column_name FROM information_schema.columns
                WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position""", (SCHEMA, table)).fetchall()]
            clauses, params = [], []
            if "scenario_id" in columns:
                clauses.append(sql.SQL("scenario_id=%s"))
                params.append(saved["scenario_id"])
            elif table == "scenarios":
                clauses.append(sql.SQL("id=%s"))
                params.append(saved["scenario_id"])
            elif table != "dataset_manifest":
                raise APIError(400, "该表没有场景字段，暂不支持读取。")
            factory_scoped = "factory_id" in columns
            if not factory_scoped and set(allowed)!={'A','B'}:
                raise APIError(403,'该表包含跨厂关联数据，须具备两个工厂的读取权限。')
            if factory_scoped:
                clauses.append(sql.SQL('factory_id=ANY(%s)'))
                params.append(allowed)
            if factory != "all" and factory_scoped:
                clauses.append(sql.SQL("factory_id=%s"))
                params.append(factory)
            where = sql.SQL(" WHERE ") + sql.SQL(" AND ").join(clauses) if clauses else sql.SQL("")
            target = sql.Identifier(SCHEMA, table)
            total = db.execute(sql.SQL("SELECT count(*) AS total FROM {}{}").format(target, where), params).fetchone()["total"]
            order = "id" if "id" in columns else columns[0]
            rows = db.execute(sql.SQL("SELECT * FROM {}{} ORDER BY {} LIMIT 20 OFFSET %s").format(
                target, where, sql.Identifier(order)), [*params, (page - 1) * 20]).fetchall()
        return jsonable_encoder({"table": table, "columns": columns, "rows": rows, "total": total,
                                 "page": page, "pageSize": 20, "scenarioId": saved["scenario_id"],
                                 "factoryScoped": factory_scoped}, custom_encoder={Decimal: str})

    return router
