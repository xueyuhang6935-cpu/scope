"""T04：仅从授权快照可见事件计算指标，不访问历史源表。"""
from collections import defaultdict
from datetime import timedelta
import json

from fastapi import APIRouter, Request

from .common import APIError
from .logistics_repository import reference_time

METRIC_VERSION = 'logistics-metrics-v1'
HORIZON_MINUTES = 30
RECENT_MINUTES = 60
NAMES = {'backlog': '样本内积压', 'overdue': '逾期未完成', 'due_soon': '临期未完成',
         'recent_accepted': '近期接单', 'recent_completed': '近期完成'}


def metric(key, value, denominator, *, missing=0, empty=False):
    result = {'name': NAMES[key], 'value': None if missing else value, 'unit': 'task',
              'denominator': denominator, 'missing_window_count': missing,
              'status': 'empty' if empty else 'unavailable' if missing else 'ok'}
    if missing:
        result['known_lower_bound'] = value
    return result


def calculate(rows, business_as_of):
    """输入只允许 T03 掩码行；分类使用精确时间差，不提前取整。"""
    moment = reference_time(business_as_of, 'business_as_of', business=True)
    recent_start = moment - timedelta(minutes=RECENT_MINUTES)
    horizon = moment + timedelta(minutes=HORIZON_MINUTES)
    pending, recent_accepted, recent_completed, completed_count = [], 0, 0, 0
    visible_count = 0
    for source in rows:
        accepted = reference_time(source['accept_time_reference'], 'accept_time_reference', business=True)
        if accepted > moment or type(source.get('completion_observed')) is not bool:
            raise APIError(409, '快照事件或完成状态不符合时点契约。')
        visible_count += 1
        recent_accepted += recent_start < accepted <= moment
        if source['completion_observed']:
            completed = reference_time(source.get('pickup_time_reference'), 'pickup_time_reference', business=True)
            if not accepted <= completed <= moment:
                raise APIError(409, '快照可见完成事件不符合时点契约。')
            completed_count += 1
            recent_completed += recent_start < completed <= moment
        else:
            row = dict(source)
            start = reference_time(row.get('time_window_start_reference'), 'time_window_start_reference', business=True, optional=True)
            end = reference_time(row.get('time_window_end_reference'), 'time_window_end_reference', business=True, optional=True)
            valid_window = start is not None and end is not None and start <= end
            row['current_wait_minutes'] = (moment - accepted).total_seconds() / 60
            row['current_overdue_minutes'] = max(0, (moment - end).total_seconds() / 60) if valid_window else None
            row['remaining_minutes'] = (end - moment).total_seconds() / 60 if valid_window else None
            row['risk_state'] = ('window_unavailable' if not valid_window else 'overdue' if end < moment
                                 else 'due_soon' if end <= horizon else 'normal')
            pending.append(row)
    missing = sum(r['risk_state'] == 'window_unavailable' for r in pending)
    overdue = sum(r['risk_state'] == 'overdue' for r in pending)
    due_soon = sum(r['risk_state'] == 'due_soon' for r in pending)
    metrics = {'backlog': metric('backlog', len(pending), visible_count, empty=not visible_count),
               'overdue': metric('overdue', overdue, len(pending), missing=missing, empty=not visible_count),
               'due_soon': metric('due_soon', due_soon, len(pending), missing=missing, empty=not visible_count),
               'recent_accepted': metric('recent_accepted', recent_accepted, visible_count, empty=not visible_count),
               'recent_completed': metric('recent_completed', recent_completed, visible_count, empty=not visible_count)}

    def priority(row):
        state = row['risk_state']
        rank = {'overdue': 0, 'due_soon': 1, 'normal': 2, 'window_unavailable': 3}[state]
        score = (-row['current_overdue_minutes'] if state == 'overdue' else row['remaining_minutes']
                 if state == 'due_soon' else row['time_window_end_reference'] if state == 'normal' else 0)
        return rank, score, row['city'], row['region_id'], row['order_id'], row['batch_id'], row['source_row']

    pending.sort(key=priority)
    return {'metrics': metrics, 'tasks': pending, 'completed_tasks': completed_count,
            'visible_tasks': visible_count, 'missing_window_count': missing,
            'denominator': {'visible_tasks': visible_count, 'backlog': len(pending),
                            'overdue': None if missing else overdue, 'due_soon': None if missing else due_soon},
            'status': 'empty' if not visible_count else 'unavailable' if missing else 'ok'}


def group_tasks(result, dimension):
    grouped = defaultdict(list)
    for row in result['tasks']:
        grouped[row[dimension]].append(row)
    groups = []
    for identity, rows in sorted(grouped.items()):
        missing = sum(r['risk_state'] == 'window_unavailable' for r in rows)
        values = {'backlog': len(rows), 'overdue': sum(r['risk_state'] == 'overdue' for r in rows),
                  'due_soon': sum(r['risk_state'] == 'due_soon' for r in rows)}
        metrics = {}
        for key, value in values.items():
            denominator = result['denominator'][key]
            entry = metric(key, value, denominator, missing=missing if key != 'backlog' else 0)
            entry['share'] = value / denominator if denominator and entry['status'] == 'ok' else None
            metrics[key] = entry
        groups.append({dimension: identity, 'metrics': metrics, 'missing_window_count': missing})
    return groups


def quality_summary(rows):
    """页面和调查工具共用，只统计快照时点可见字段。"""
    quality = {}
    for field, completed_only in (('window', False), ('stop_coordinates', False), ('accept_gps', False), ('pickup_gps', True)):
        eligible = [row for row in rows if not completed_only or row['completion_observed']]
        available = sum(row['visible_quality'][field] == 'available' for row in eligible)
        quality[field] = {'available': available, 'unavailable': len(eligible)-available,
                          'denominator': len(eligible), 'not_applicable': len(rows)-len(eligible),
                          'available_share': available/len(eligible) if eligible else None}
    return quality


class LogisticsMetrics:
    def __init__(self, snapshots):
        self.snapshots = snapshots

    def query(self, user, snapshot_id, *, view='summary', risk_state='all', dimension='courier_id', page=1, page_size=20, search=''):
        if view not in ('summary', 'tasks', 'groups', 'quality', 'recent_accepted', 'recent_completed') or risk_state not in ('all', 'risk', 'overdue', 'due_soon', 'normal', 'window_unavailable'):
            raise APIError(400, '指标视图或风险类型无效。')
        if dimension not in ('courier_id', 'aoi_id'):
            raise APIError(400, '仅支持快递员或 AOI 分组。')
        if type(page) is not int or type(page_size) is not int or not 1 <= page <= 100000 or not 1 <= page_size <= 100:
            raise APIError(400, '指标分页参数无效。')
        if type(search) is not str or len(search) > 200:
            raise APIError(400, '任务搜索最多200字。')
        search = search.strip().casefold()
        # 与授权修改共享单进程锁，计算和返回期间不允许成员授权变化。
        with self.snapshots.data.lock:
            metadata, rows = self.snapshots.read_all(user, snapshot_id)
            if metadata.get('metric_version') not in (None, METRIC_VERSION):
                raise APIError(409, '快照指标口径版本不受当前服务支持。')
            # ponytail: 首版每次从持久化掩码行复算；吞吐成为瓶颈时再加带版本的受控结果缓存。
            result = calculate([{**row, 'row_index': index} for index, row in enumerate(rows)], metadata['business_as_of'])
            response = {'snapshot_id': snapshot_id, 'fingerprint': metadata['snapshot_fingerprint'],
                        'visible_content_hash': metadata['visible_content_hash'], 'metric_version': METRIC_VERSION,
                        'business_as_of': metadata['business_as_of'], 'city': metadata['city'], 'region_ids': metadata['region_ids'],
                        'horizon_minutes': HORIZON_MINUTES, 'recent_minutes': RECENT_MINUTES,
                        'denominator': result['denominator'], 'unit': {'count': 'task', 'duration': 'minute', 'share': 'ratio'},
                        'limitations': [*metadata['limitations'], '快递员/AOI 风险集中不等于责任或因果。'],
                        'status': result['status']}
            if result['missing_window_count']:
                response['limitations'].append('存在窗口缺失任务，完整逾期/临期数不可计算；已知数量仅为下界。')
            if view == 'summary':
                response.update(metrics=result['metrics'], visible_tasks=result['visible_tasks'],
                                completed_tasks=result['completed_tasks'], missing_window_count=result['missing_window_count'],
                                total_rows=result['visible_tasks'], returned_rows=0, truncated=False)
            elif view == 'quality':
                quality = quality_summary(rows)
                response.update(quality=quality, visible_tasks=len(rows), total_rows=len(rows), returned_rows=0, truncated=False,
                                status='empty' if not rows else 'unavailable' if any(q['unavailable'] for q in quality.values()) else 'ok')
                response['limitations'].append('截至T无可用GPS不区分原始缺失与未来采样；未发生的完成GPS不适用。')
            else:
                values = group_tasks(result, dimension) if view == 'groups' else [
                    r for r in result['tasks'] if (risk_state == 'all' or r['risk_state'] == risk_state
                        or risk_state == 'risk' and r['risk_state'] in ('overdue', 'due_soon'))
                    and (not search or any(search in r[key].casefold() for key in ('order_id', 'courier_id', 'aoi_id')))]
                if view in ('recent_accepted', 'recent_completed'):
                    moment = reference_time(metadata['business_as_of'], 'business_as_of', business=True)
                    start = moment - timedelta(minutes=RECENT_MINUTES)
                    field = 'accept_time_reference' if view == 'recent_accepted' else 'pickup_time_reference'
                    pending = {row['row_index']: row for row in result['tasks']}
                    values = [{**pending.get(index, row), 'row_index': index} for index, row in enumerate(rows)
                              if (view != 'recent_completed' or row['completion_observed'])
                              and start < reference_time(row[field], field, business=True) <= moment
                              and (not search or any(search in row[key].casefold() for key in ('order_id', 'courier_id', 'aoi_id')))]
                    values.sort(key=lambda row: (row[field], row['order_id'], row['row_index']), reverse=True)
                    response['flow'] = view
                offset = (page - 1) * page_size
                selected = values[offset:offset + page_size]
                response.update(rows=selected, total_rows=len(values), returned_rows=len(selected),
                                page=page, page_size=page_size, truncated=offset + len(selected) < len(values))
                if view == 'groups':
                    response['dimension'] = dimension
                else:
                    response['risk_state'] = risk_state
                    response['search'] = search
                if not values and result['status'] == 'ok':
                    response['status'] = 'empty'
            return response

    def task_evidence(self, user, snapshot_id, row_index):
        if type(row_index) is not int or row_index < 0:
            raise APIError(400, '任务定位无效。')
        with self.snapshots.data.lock:
            metadata = self.snapshots.ready_metadata(user, snapshot_id)
            if metadata.get('metric_version') not in (None, METRIC_VERSION):
                raise APIError(409, '快照指标口径版本不受当前服务支持。')
            saved = self.snapshots.data.one('SELECT row_json FROM logistics_snapshot_rows WHERE snapshot_id=? AND row_index=?', (snapshot_id, row_index))
            if not saved:
                raise APIError(404, '当前快照任务不存在。')
            row = json.loads(saved['row_json'])
            result = calculate([row], metadata['business_as_of'])
            return {'snapshot_id': snapshot_id, 'row_index': row_index, 'metric_version': METRIC_VERSION,
                    'business_as_of': metadata['business_as_of'], 'region_ids': metadata['region_ids'],
                    'fingerprint': metadata['snapshot_fingerprint'], 'source_type': 'snapshot_task',
                    'record': result['tasks'][0] if result['tasks'] else row,
                    'limitations': metadata['limitations']}

    def routes(self):
        router = APIRouter(prefix='/api/logistics/snapshots', tags=['物流确定性指标'])

        def user(request, allowed):
            user = self.snapshots.access.user(request)
            if set(request.query_params) - {*allowed, 'workspaceId'}:
                raise APIError(400, '指标查询包含不允许的参数，不能覆盖业务时点或授权范围。')
            return user

        @router.get('/{snapshot_id}/summary')
        def summary(request: Request, snapshot_id: str):
            return self.query(user(request, ()), snapshot_id)

        @router.get('/{snapshot_id}/tasks')
        def tasks(request: Request, snapshot_id: str, risk_state: str = 'all', page: int = 1, page_size: int = 20, search: str = ''):
            return self.query(user(request, ('risk_state', 'page', 'page_size', 'search')), snapshot_id,
                              view='tasks', risk_state=risk_state, page=page, page_size=page_size, search=search)

        @router.get('/{snapshot_id}/task-evidence/{row_index}')
        def task_evidence(request: Request, snapshot_id: str, row_index: int):
            return self.task_evidence(user(request, ()), snapshot_id, row_index)

        @router.get('/{snapshot_id}/groups')
        def groups(request: Request, snapshot_id: str, dimension: str = 'courier_id', page: int = 1, page_size: int = 20):
            return self.query(user(request, ('dimension', 'page', 'page_size')), snapshot_id,
                              view='groups', dimension=dimension, page=page, page_size=page_size)

        @router.get('/{snapshot_id}/quality')
        def quality(request: Request, snapshot_id: str):
            return self.query(user(request, ()), snapshot_id, view='quality')

        @router.get('/{snapshot_id}/recent')
        def recent(request: Request, snapshot_id: str, flow: str = 'recent_completed', page: int = 1, page_size: int = 20, search: str = ''):
            if flow not in ('recent_accepted', 'recent_completed'):
                raise APIError(400, '近期明细类型无效。')
            return self.query(user(request, ('flow', 'page', 'page_size', 'search')), snapshot_id,
                              view=flow, page=page, page_size=page_size, search=search)

        return router
