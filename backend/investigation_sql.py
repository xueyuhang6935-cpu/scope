"""Model SQL boundary: parsed SELECTs, dedicated login and scoped security-barrier views."""
import json
import time
import uuid
from datetime import datetime, timezone

import psycopg
from psycopg.rows import dict_row
from sqlglot import parse, exp
from sqlglot.errors import SqlglotError
from sqlglot.optimizer.scope import traverse_scope

SCHEMA = 'agent_read'
ROLE = 'scope_agent_reader'
# table: (business domain, description, ownership). A parent reference is (column, table).
TABLES = {
    'factories': ('reference', '工厂主数据', 'id'),
    'materials': ('reference', '物料主数据；base_unit 为基准单位', None),
    'material_revisions': ('reference', '物料版本及有效期', None),
    'warehouses': ('inventory', '仓库所属工厂', 'factory_id'),
    'production_lines': ('production', '产线所属工厂', 'factory_id'),
    'bom_headers': ('production', 'BOM 版本、审批状态及有效期', 'factory_id'),
    'bom_lines': ('production', '单位产品用量；关联 bom_headers.id', ('bom_id', 'bom_headers')),
    'production_plan_lines': ('production', '生产需求；need_at 为需求时点，须辨别版本和状态', 'factory_id'),
    'inventory_snapshots': ('inventory', '库存快照；不是可用库存，不可跨快照累加', 'factory_id'),
    'inventory_reservations': ('inventory', '库存预留；included_demand 不可重复扣减', ('snapshot_id', 'inventory_snapshots')),
    'quality_holds': ('quality', '质量冻结；须检查开始和解除时间', ('snapshot_id', 'inventory_snapshots')),
    'unit_conversions': ('rules', '经确认的单位换算及有效期', 'factory_id'),
    'purchase_order_lines': ('purchase', '采购行；数量、已交及取消数量', 'factory_id'),
    'supplier_commitments': ('purchase', '原始供应承诺历史；需结合 commitment_changes，反馈不等于已证实原因', ('purchase_line_id', 'purchase_order_lines')),
    'execution_orders': ('purchase', '执行单据不等于实际到货', 'factory_id'),
    'shipment_lines': ('purchase', '采购或执行单对应的发运', 'shipment'),
    'receipt_lines': ('purchase', '实际收货；尚不代表放行或到线', ('shipment_id', 'shipment_lines')),
    'quality_releases': ('quality', '质检放行', ('receipt_id', 'receipt_lines')),
    'line_deliveries': ('production', '产线配送；expected_at 与 actual_at 区分计划和实际', ('release_id', 'quality_releases')),
    'transfer_routes': ('purchase', '调拨线路；两端工厂均须授权', 'route'),
    'expedite_options': ('purchase', '采购行加急报价与有效期', ('purchase_line_id', 'purchase_order_lines')),
    'supply_rules': ('rules', '供应规则及有效期', 'factory_id'),
    'approval_policies': ('rules', '场景审批政策；不替代工作空间审批校验', None),
    'commitment_changes': ('purchase', '当前工作空间注入的承诺变更，按 known_at/created_at/id 顺序覆盖原承诺', ('purchase_line_id', 'purchase_order_lines')),
}

FUNCTIONS = {'SUM', 'COUNT', 'MIN', 'MAX', 'AVG', 'COALESCE', 'NULLIF', 'ABS', 'ROUND',
             'LOWER', 'UPPER', 'LENGTH', 'DATE_TRUNC', 'TIMESTAMP_TRUNC', 'EXTRACT',
             'CAST', 'CASE', 'IF', 'GREATEST', 'LEAST', 'ROW_NUMBER', 'RANK', 'DENSE_RANK'}
NODES = set('Select Union Intersect Except Subquery With CTE From Join Table TableAlias Identifier '
            'Column Star Alias Literal Null Boolean Placeholder Where Having Group Order Ordered '
            'Limit Offset Distinct Paren And Or Not EQ NEQ GT GTE LT LTE Is In Between Like ILike '
            'Add Sub Mul Div Mod Neg Exists Tuple Window WindowSpec Partition Var DataType '
            'DataTypeParam Interval'.split())
CAST_TYPES = {'TEXT', 'VARCHAR', 'CHAR', 'INT', 'BIGINT', 'SMALLINT', 'DECIMAL', 'DOUBLE',
              'FLOAT', 'BOOLEAN', 'DATE', 'TIMESTAMP', 'TIMESTAMPTZ', 'INTERVAL'}


def validate_sql(sql, parameters, tables, *, referenced_tables=None):
    if not isinstance(sql, str) or not 1 <= len(sql) <= 12000 or not isinstance(parameters, dict):
        raise ValueError('sql 必须为 1–12000 字符，parameters 必须为对象。')
    if len(json.dumps(parameters, ensure_ascii=False)) > 8000 or any(
            not isinstance(k, str) or not k.isascii() or not k.isidentifier() or
            (v is not None and type(v) not in (str, int, float, bool)) for k, v in parameters.items()):
        raise ValueError('参数仅支持具名标量；名称使用 ASCII 标识符。')
    try:
        statements = parse(sql, read='postgres')
        if len(statements) != 1 or not isinstance(statements[0], (exp.Select, exp.Union, exp.Intersect, exp.Except)):
            raise ValueError('只允许单条 SELECT，可使用只读 CTE、关联和聚合。')
        tree = statements[0]
        for node in tree.walk():
            if isinstance(node, exp.Func):
                if isinstance(node, exp.Anonymous) or node.sql_name() not in FUNCTIONS:
                    raise ValueError('函数不在授权聚合、日期和文本函数列表内。')
            elif type(node).__name__ not in NODES:
                raise ValueError('不支持此 SQL 结构：' + type(node).__name__)
            if isinstance(node, exp.DataType) and node.this.value not in CAST_TYPES:
                raise ValueError('不允许此类型转换。')
            if isinstance(node, exp.With) and node.args.get('recursive'):
                raise ValueError('暂不支持递归 CTE。')
            if isinstance(node, exp.Table):
                if not isinstance(node.this, exp.Identifier) or node.catalog or node.db not in ('', SCHEMA):
                    raise ValueError('只能查询 get_database_schema 列出的授权视图。')
        # Resolve CTE scopes: a same-named CTE must not disguise a catalog/base-table read.
        for scope in traverse_scope(tree):
            for source in scope.sources.values():
                if isinstance(source, exp.Table):
                    if source.name not in tables:
                        raise ValueError('表不在本次数据域授权范围：' + source.name)
                    source.set('db', exp.to_identifier(SCHEMA))
                    if referenced_tables is not None:
                        referenced_tables.add(source.name)
        def parameter_name(p):
            return p.this.name if isinstance(p.this, exp.Identifier) else str(p.this)
        names = {parameter_name(p) for p in tree.find_all(exp.Placeholder)}
        if names != set(parameters):
            raise ValueError('SQL 的 %(name)s 占位符必须与 parameters 完全对应。')
        # Execute only the serialized, checked AST; never the original text.
        markers = {}
        for placeholder in list(tree.find_all(exp.Placeholder)):
            marker = 'agentparam' + uuid.uuid4().hex
            markers[marker] = '%(' + parameter_name(placeholder) + ')s'
            placeholder.replace(exp.Var(this=marker))
        rendered = tree.sql(dialect='postgres')
        if parameters:
            rendered = rendered.replace('%', '%%')
        for marker, binding in markers.items():
            rendered = rendered.replace(marker, binding)
        return rendered
    except SqlglotError:
        raise ValueError('SQL 语法无法解析，请使用 PostgreSQL SELECT。') from None


class InvestigationSQL:
    def __init__(self, env, authorize):
        self.env, self.authorize = env, authorize
        self.schema_tables_seen = set()

    def call(self, name, args):
        started = time.monotonic()
        audit = {'tool': name, 'status': 'error'}
        try:
            if not isinstance(args, dict):
                raise ValueError('工具参数必须为对象。')
            # Recheck membership, per-domain factory grants and config on EVERY call.
            scope = self.authorize()
            audit['scope'] = scope
            tables = {t for t, (domain, _, _) in TABLES.items() if scope['domains'].get(domain)}
            if name == 'get_database_schema':
                requested = args.get('tables', [])
                if set(args) - {'tables'} or not isinstance(requested, list) or len(requested) > 6 or any(not isinstance(t, str) or t not in tables for t in requested):
                    raise ValueError('tables 可选，最多列出 6 个已授权表名；省略时返回表目录。')
            elif name == 'execute_sql':
                if set(args) != {'sql', 'parameters'}:
                    raise ValueError('execute_sql 需要 sql 和 parameters 两个字段。')
                audit.update(sql=args['sql'], parameters=args['parameters'])
                referenced = set()
                statement = validate_sql(args['sql'], args['parameters'], tables, referenced_tables=referenced)
                unread = referenced - self.schema_tables_seen
                if unread:
                    raise ValueError('执行SQL前请用 get_database_schema 读取这些表的字段：' + '、'.join(sorted(unread)))
            else:
                raise ValueError('未知查询工具。')
            dsn = self.env.get('SUPPLY_AGENT_DATABASE_URL')
            if not dsn:
                raise ValueError('尚未配置专用只读查询连接 SUPPLY_AGENT_DATABASE_URL。')
            with psycopg.connect(dsn, connect_timeout=3, row_factory=dict_row) as db:
                db.execute('SET TRANSACTION READ ONLY')
                db.execute("SET LOCAL statement_timeout = '3s'")
                db.execute("SET LOCAL lock_timeout = '1s'")
                db.execute("SET LOCAL search_path = pg_catalog, agent_read")
                role = db.execute('SELECT current_user AS name, rolsuper, rolcreaterole, rolcreatedb, rolbypassrls FROM pg_roles WHERE rolname=current_user').fetchone()
                if role['name'] != ROLE or any(role[k] for k in ('rolsuper', 'rolcreaterole', 'rolcreatedb', 'rolbypassrls')):
                    raise ValueError('查询连接必须使用受限的 scope_agent_reader 角色。')
                db.execute("SELECT set_config('scope.agent_context', %s, true)", (json.dumps(scope),))
                if name == 'get_database_schema':
                    columns = db.execute("SELECT table_name,column_name,data_type FROM information_schema.columns WHERE table_schema=%s ORDER BY table_name,ordinal_position", (SCHEMA,)).fetchall()
                    result = {'scope': scope, 'dialect': 'PostgreSQL', 'parameters': '%(name)s',
                              'tables': {t: {'description': TABLES[t][1], 'domain': TABLES[t][0],
                                            **({'columns': [{'name': c['column_name'], 'type': c['data_type']} for c in columns if c['table_name'] == t]} if t in requested else {}),
                                            'parent': TABLES[t][2]} for t in sorted(tables)},
                              'schemaHelp': '指定 tables 数组获取最多6张表的完整字段；不传时仅返回目录。',
                              'rules': ['所有视图自动限定场景、授权工厂、数据域和 ingested_at <= asOf。',
                                        '同一场景内 id 为记录键；跨表按 *_id 关联，并保留物料 revision_id。',
                                        '数量不可跨单位直接相加；快照、预留、冻结不可重复扣减。',
                                        '承诺查最新版本并结合 commitment_changes；未来到货承诺不等于实际收货。',
                                        'SQL 用于补充证据，缺料数量、费用和可行性以确定性计算为准。'],
                              'functions': sorted(FUNCTIONS), 'maxRows': 100, 'maxResultBytes': 16000}
                    if tables - {c['table_name'] for c in columns}:
                        raise ValueError('授权视图尚未安装完整，请运行只读查询安装脚本。')
                    result['fieldSemantics'] = {'supplier_commitments.feedback': {
                        'sourceType': 'supplier_statement', 'verified': False,
                        'description': '只证明供应商如此报告；不得将反馈原因视为独立证实。'}}
                    self.schema_tables_seen.update(requested)
                else:
                    # Bound each row in PostgreSQL before it reaches Python, then bound the whole result.
                    query = 'SELECT left(row_to_json(q)::text, 16001) AS payload FROM (' + statement + ') q LIMIT 101'
                    with db.cursor(name='agent_query') as cursor:
                        cursor.execute(query, args['parameters'] or None)
                        rows, size, truncated = [], 0, False
                        columns = []
                        for record in cursor:
                            raw = record['payload']
                            if len(rows) == 100 or len(raw.encode('utf-8')) + size > 16000:
                                truncated = True
                                break
                            pairs = json.loads(raw, object_pairs_hook=list)
                            # Duplicate result names cannot be cited unambiguously.
                            names = [k for k, _ in pairs]
                            if len(names) != len(set(names)):
                                raise ValueError('结果列名重复，请使用 AS 设置唯一列名。')
                            columns = names
                            rows.append(json.loads(raw, parse_float=str))
                            size += len(raw.encode('utf-8'))
                    result = {'id': 'sql:' + uuid.uuid4().hex, 'table': 'sql_query',
                              'provenance': {'sourceType': 'database_query', 'tables': sorted(referenced),
                                             'truncated': truncated,
                                             'containsSupplierStatements': 'supplier_commitments' in referenced},
                              'record': {'columns': columns, 'rows': rows, 'truncated': truncated,
                                         'sql': args['sql'], 'parameters': args['parameters'], 'scope': scope},
                              'knownAt': datetime.now(timezone.utc).isoformat(), 'sourceRecordId': 'sql_query'}
                    audit.update(evidenceId=result['id'], items=len(rows), truncated=truncated)
            audit['status'] = 'completed'
        except ValueError as exc:
            result = {'error': {'code': 'invalid_query', 'message': str(exc), 'retryable': True}}
        except psycopg.Error as exc:
            messages = {'42703': '列不存在，请查看结构并修正列名。', '42P01': '表或别名不存在。',
                        '42883': '函数或运算符类型不匹配，请检查字段类型。', '42601': 'SQL 语法错误。',
                        '57014': '查询超时，请增加筛选或简化关联。', '42501': '数据库拒绝访问该对象。',
                        '22012': '除数为零，请使用 NULLIF。', '22P02': '参数或类型转换格式不正确。'}
            result = {'error': {'code': exc.sqlstate or 'database_unavailable',
                                'message': messages.get(exc.sqlstate, '查询失败，请检查结构或数据库配置。'), 'retryable': True}}
        except Exception:
            # Never expose connection strings, unrelated data or permission internals to the model.
            result = {'error': {'code': 'scope_unavailable', 'message': '调查授权或配置已失效，停止查询并转人工。', 'retryable': False}}
        audit['elapsedMs'] = round((time.monotonic() - started) * 1000)
        if 'error' in result:
            audit['error'] = result['error']
        return result, audit
