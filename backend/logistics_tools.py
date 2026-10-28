"""T07 宿主：每次调用绑定真实run、实时授权和持久化掩码快照。"""
from collections import defaultdict
import json
import logging
import time

from fastapi import APIRouter, Request

from project.tools.logistics import LogisticsTools, RESULT_SCHEMA, OUTPUT_SCHEMAS, validate_arguments
from .common import APIError
from .logistics_metrics import calculate, group_tasks, quality_summary, METRIC_VERSION, HORIZON_MINUTES, RECENT_MINUTES
from .logistics_evidence import EvidenceStore
from project.agents.logistics_protocol import ProtocolError


class LogisticsToolService:
    def __init__(self, runs):
        self.runs, self.snapshots, self.data = runs, runs.snapshots, runs.data
        self.evidence_store = EvidenceStore(runs)

    def for_run(self, user, run_id):
        # 不缓存授权；宿主提供身份和run，模型参数中没有snapshot或范围。
        return LogisticsTools(lambda name, args: self.execute(user, run_id, name, args))

    def call(self, user, run_id, name, arguments):
        try:
            return self.for_run(user, run_id).call(name, arguments)
        except ValueError:
            raise APIError(400, '工具未注册或参数无效；不能覆盖快照、时点、区域或执行任意SQL。') from None

    @staticmethod
    def evidence_id(run_id, row_index):
        return f'LTASK:{run_id}:{row_index}'

    @staticmethod
    def envelope(run_id, name, metadata):
        return {'tool': name, 'run_id': run_id, 'snapshot_id': metadata['snapshot_id'],
                'fingerprint': metadata['snapshot_fingerprint'], 'visible_content_hash': metadata['visible_content_hash'],
                'business_as_of': metadata['business_as_of'], 'city': metadata['city'], 'region_ids': metadata['region_ids'],
                'metric_version': METRIC_VERSION, 'horizon_minutes': HORIZON_MINUTES, 'recent_minutes': RECENT_MINUTES,
                'status': 'ok', 'data': {}, 'rows': [], 'total_rows': 0, 'returned_rows': 0, 'truncated': False,
                'limitations': [*metadata['limitations'], '空结果不证明生产零风险；当前快照仅为样本。']}

    @staticmethod
    def page(response, values, args, status):
        offset = (args['page'] - 1) * args['page_size']
        selected = values[offset:offset + args['page_size']]
        response.update(rows=selected, total_rows=len(values), returned_rows=len(selected),
                        truncated=offset + len(selected) < len(values), page=args['page'], page_size=args['page_size'],
                        status=status if status == 'unavailable' else 'empty' if not selected else 'ok')

    @staticmethod
    def sort_groups(groups, dimension, order):
        if order == 'identity_asc':
            return sorted(groups, key=lambda r: r[dimension])
        metric = order.removesuffix('_desc')
        return sorted(groups, key=lambda r: (r['metrics'][metric]['value'] is None,
                      -(r['metrics'][metric]['value'] or r['metrics'][metric].get('known_lower_bound', 0)), r[dimension]))

    def execute(self, user, run_id, name, args):
        try:
            args = validate_arguments(name, args)
        except ValueError:
            raise APIError(400, '工具未注册或参数无效。') from None
        with self.data.lock:
            run = self.runs.saved(run_id)
            metadata = self.runs.authorize(user, run)
            if run['status'] in ('cancelled', 'failed'):
                raise APIError(409, '已取消或失败的运行不能继续调用工具。')
            if run['mode'] == 'model' and run['status'] not in ('queued', 'running'):
                raise APIError(409, '模型调查已停止，只能读取保存证据。')
            try:
                self.runs.budgets.reserve(run_id, 'tool_calls')
            except ProtocolError as exc:
                raise APIError(409, '工具调用已达到任务家族限制：'+exc.code) from None
            started = time.monotonic()
            ticket = self.evidence_store.start(run_id, metadata, name, args)
            try:
                result = self.compute(user, run_id, name, args)
                self.runs.authorize(user, run)
                return self.evidence_store.finish(ticket, run_id, metadata, result)
            except APIError as exc:
                self.evidence_store.reject(ticket, exc.status)
                raise
            except Exception:
                self.evidence_store.reject(ticket, 503)
                raise APIError(503, '工具证据未能持久化，请稍后重试。') from None
            finally:
                if run['status'] != 'running':
                    with self.runs.transaction():
                        budget = json.loads(self.runs.saved(run_id)['budget_json'])
                        budget['standalone_seconds'] = budget.get('standalone_seconds', 0)+time.monotonic()-started
                        self.data.db.execute('UPDATE logistics_runs SET budget_json=? WHERE run_id=?', (json.dumps(budget), run_id))

    def compute(self, user, run_id, name, args):
        # 授权修改、读取和结果交付共享既有单进程锁。
        with self.data.lock:
            run = self.runs.saved(run_id)
            metadata = self.runs.authorize(user, run)
            response = self.envelope(run_id, name, metadata)
            if metadata.get('metric_version') not in (None, METRIC_VERSION):
                response.update(status='unavailable', data=None, total_rows=None,
                                error={'code': 'metric_version_unavailable', 'message': '快照口径版本暂不支持。'})
                return response
            try:
                if name == 'get_evidence':
                    if args['evidence_id'].startswith('LEV-'):
                        saved = self.evidence_store.read(user, run_id, args['evidence_id'])
                        response.update(rows=[{'evidence_id': saved['evidence_id'], 'source_type': saved['source_type'],
                                               'record': saved['record']}], total_rows=1, returned_rows=1,
                                        status=saved['record']['status'])
                        return response
                    return self.evidence(response, args['evidence_id'], metadata)
                # ponytail: 复用T04全量快照复算；吞吐成为瓶颈后再加带版本缓存或等价SQL聚合。
                _, rows = self.snapshots.read_all(user, run['snapshot_id'])
                result = calculate(rows, metadata['business_as_of'])
                response['denominator'] = result['denominator']
                if result['missing_window_count']:
                    response['limitations'].append('存在窗口缺失，完整逾期/临期数量不可计算；已知数仅为下界。')
                if name == 'get_snapshot_summary':
                    response.update(status=result['status'], total_rows=len(rows),
                                    data={k: result[k] for k in ('metrics', 'visible_tasks', 'completed_tasks', 'missing_window_count')})
                elif name == 'get_risk_distribution':
                    values = self.sort_groups(group_tasks(result, args['dimension']), args['dimension'], args['sort'])
                    response['data'] = {'dimension': args['dimension'], 'sort': args['sort']}
                    response['limitations'].append('风险集中不等于责任或因果。')
                    self.page(response, values, args, result['status'])
                elif name == 'get_pending_tasks':
                    indices = {row['order_id']: index for index, row in enumerate(rows)}
                    values = [{**row, 'evidence_id': self.evidence_id(run_id, indices[row['order_id']])}
                              for row in result['tasks'] if (args['risk_state'] == 'all' or row['risk_state'] == args['risk_state'])
                              and all(row[key] == args[key] for key in ('courier_id', 'aoi_id') if key in args)]
                    if args['sort'] != 'risk_priority':
                        field, descending = {'wait_minutes_desc': ('current_wait_minutes', True),
                                             'overdue_minutes_desc': ('current_overdue_minutes', True),
                                             'remaining_minutes_asc': ('remaining_minutes', False)}[args['sort']]
                        values.sort(key=lambda r: (r[field] is None, (-1 if descending else 1) * (r[field] or 0),
                                                  r['city'], r['region_id'], r['order_id'], r['batch_id'], r['source_row']))
                    response['data'] = {key: args[key] for key in ('risk_state', 'sort', 'courier_id', 'aoi_id') if key in args}
                    self.page(response, values, args, result['status'])
                elif name == 'get_courier_workload':
                    grouped = defaultdict(list)
                    for row in rows:
                        grouped[row['courier_id']].append(row)
                    risks = {g['courier_id']: g for g in group_tasks(result, 'courier_id')}
                    values = []
                    for identity, tasks in sorted(grouped.items()):
                        if 'courier_id' in args and identity != args['courier_id']:
                            continue
                        local = calculate(tasks, metadata['business_as_of'])
                        values.append({'courier_id': identity, 'visible_tasks': len(tasks),
                            'completed_tasks': local['completed_tasks'], 'metrics': local['metrics'],
                            'risk_distribution': risks.get(identity), 'status': local['status']})
                    values = self.sort_groups(values, 'courier_id', args['sort'])
                    response['limitations'].append('样本任务负荷不代表人员容量；不能据此归因。')
                    response['data'] = {key: args[key] for key in ('sort', 'courier_id') if key in args}
                    self.page(response, values, args, result['status'])
                elif name == 'get_quality_summary':
                    quality = quality_summary(rows)
                    response.update(status='empty' if not rows else 'unavailable' if any(q['unavailable'] for q in quality.values()) else 'ok',
                                    total_rows=len(rows), data={'visible_tasks': len(rows), 'quality': quality})
                    response['limitations'].append('截至T无可用GPS不区分原始缺失与未来采样；未发生的完成GPS不适用。')
                return response
            except APIError as exc:
                if exc.status in (400, 403, 404):
                    raise
                response.update(status='error', data=None, rows=[], total_rows=None, returned_rows=0, truncated=False,
                                error={'code': 'snapshot_data_error', 'message': '当前快照数据不满足工具计算契约。'})
            except Exception:
                logging.getLogger(__name__).warning('Logistics tool failed: %s (%s)', run_id, name)
                response.update(status='error', data=None, rows=[], total_rows=None, returned_rows=0, truncated=False,
                                error={'code': 'tool_execution_failed', 'message': '工具执行失败，请稍后重试。'})
            return response

    def evidence(self, response, identifier, metadata):
        prefix = f"LTASK:{response['run_id']}:"
        index = identifier.removeprefix(prefix)
        if not identifier.startswith(prefix) or not index.isascii() or not index.isdigit() or len(index) > 10 or str(int(index)) != index:
            raise APIError(400, '证据必须属于当前运行且使用合法标识。')
        saved = self.data.one('SELECT row_json FROM logistics_snapshot_rows WHERE snapshot_id=? AND row_index=?',
                              (response['snapshot_id'], int(index)))
        if not saved:
            raise APIError(404, '当前运行的任务证据不存在。')
        row = json.loads(saved['row_json'])
        computed = calculate([row], metadata['business_as_of'])
        visible = computed['tasks'][0] if computed['tasks'] else row
        response.update(rows=[{'evidence_id': identifier, 'source_type': 'snapshot_task', 'record': visible}],
                        total_rows=1, returned_rows=1, status=computed['status'])
        return response

    def routes(self):
        router = APIRouter(prefix='/api/logistics', tags=['物流参数化工具'])

        @router.get('/tools')
        def registry(request: Request):
            user = self.snapshots.access.user(request)
            self.snapshots.access.require(user, 'view')
            if set(request.query_params) - {'workspaceId'}:
                raise APIError(400, '工具目录不接受额外参数。')
            return {'tools': LogisticsTools(None).declarations(), 'result_schema': RESULT_SCHEMA,
                    'output_schemas': OUTPUT_SCHEMAS}

        @router.post('/runs/{run_id}/tools/{name}')
        def call(request: Request, run_id: str, name: str):
            if set(request.query_params) - {'workspaceId'}:
                raise APIError(400, '工具调用不接受额外查询参数。')
            return self.call(self.snapshots.access.user(request), run_id, name, request.state.body)

        return router
