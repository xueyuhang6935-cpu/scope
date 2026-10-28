"""物流只读工具契约；运行绑定、授权和实际数据访问由宿主注入。"""
from copy import deepcopy


PAGE = {'type': 'integer', 'minimum': 1, 'maximum': 100000, 'default': 1}
PAGE_SIZE = {'type': 'integer', 'minimum': 1, 'maximum': 100, 'default': 20}
IDENTITY = {'type': 'string', 'minLength': 1, 'maxLength': 128}
WINDOWS = {'horizon_minutes': {'type': 'integer', 'enum': [30], 'default': 30},
           'recent_minutes': {'type': 'integer', 'enum': [60], 'default': 60}}
PAGING = {'page': PAGE, 'page_size': PAGE_SIZE}


def choice(values, default):
    return {'type': 'string', 'enum': values, 'default': default}


TOOL_SPECS = {
    'get_snapshot_summary': ('获取当前运行绑定快照的三核心指标、近期流量与样本限制。', WINDOWS),
    'get_risk_distribution': ('按快递员或AOI查看当前风险分布，占比分母来自完整快照。',
        {**WINDOWS, **PAGING,
         'dimension': choice(['courier_id', 'aoi_id'], 'courier_id'),
         'sort': choice(['identity_asc', 'backlog_desc', 'overdue_desc', 'due_soon_desc'], 'identity_asc')}),
    'get_pending_tasks': ('分页获取当前待揽收任务；缺失窗口不等于无风险。',
        {**WINDOWS, **PAGING,
         'risk_state': choice(['all', 'overdue', 'due_soon', 'normal', 'window_unavailable'], 'all'),
         'sort': choice(['risk_priority', 'wait_minutes_desc', 'overdue_minutes_desc', 'remaining_minutes_asc'], 'risk_priority'),
         'courier_id': IDENTITY, 'aoi_id': IDENTITY}),
    'get_courier_workload': ('获取快递员积压、风险和近期接单/完成；样本负荷不代表真实容量。',
        {**WINDOWS, **PAGING, 'courier_id': IDENTITY,
         'sort': choice(['identity_asc', 'backlog_desc'], 'identity_asc')}),
    'get_quality_summary': ('仅统计当前可见字段质量；不可见完成GPS不当作缺失。', {}),
    'get_evidence': ('展开当前运行的快照任务证据，禁止跨运行引用。',
        {'evidence_id': {'type': 'string', 'minLength': 1, 'maxLength': 200}}),
}
REGISTRY = {name: {'type': 'function', 'function': {
    'name': name, 'description': description,
    'parameters': {'type': 'object', 'properties': deepcopy(properties),
                   'required': ['evidence_id'] if name == 'get_evidence' else [],
                   'additionalProperties': False}}}
    for name, (description, properties) in TOOL_SPECS.items()}

RESULT_SCHEMA = {
    'type': 'object',
    'required': ['tool', 'status', 'run_id', 'snapshot_id', 'business_as_of', 'metric_version',
                 'rows', 'data', 'total_rows', 'returned_rows', 'truncated', 'limitations'],
    'properties': {
        **{key: {'type': 'string'} for key in ('tool', 'run_id', 'snapshot_id', 'business_as_of', 'metric_version',
                                             'fingerprint', 'visible_content_hash', 'city')},
        'region_ids': {'type': 'array', 'items': {'type': 'string'}},
        'limitations': {'type': 'array', 'items': {'type': 'string'}},
        'call_id': {'type': 'string'}, 'result_evidence_id': {'type': 'string'},
        'result_fingerprint': {'type': 'string'},
        'status': {'type': 'string', 'enum': ['ok', 'empty', 'unavailable', 'error']},
        'rows': {'type': 'array', 'items': {'type': 'object'}},
        'data': {'type': ['object', 'null']},
        'total_rows': {'type': ['integer', 'null'], 'minimum': 0},
        'returned_rows': {'type': 'integer', 'minimum': 0},
        'truncated': {'type': 'boolean'},
    },
}

COUNT = {'type': 'integer', 'minimum': 0}
METRIC = {'type': 'object', 'required': ['value', 'status', 'denominator', 'unit'], 'properties': {
    'value': {'type': ['integer', 'null']}, 'denominator': {'type': ['integer', 'null']},
    'status': {'type': 'string', 'enum': ['ok', 'empty', 'unavailable']}, 'unit': {'const': 'task'},
    'known_lower_bound': COUNT, 'missing_window_count': COUNT,
    'share': {'type': ['number', 'null'], 'minimum': 0, 'maximum': 1}}}
METRICS = {'type': 'object', 'required': ['backlog', 'overdue', 'due_soon'],
           'properties': {key: METRIC for key in ('backlog', 'overdue', 'due_soon', 'recent_accepted', 'recent_completed')}}
TASK = {'type': 'object', 'required': ['order_id', 'task_key', 'completion_observed', 'accept_time_reference', 'visible_quality']}
QUALITY = {'type': 'object', 'required': ['available', 'unavailable', 'denominator', 'not_applicable', 'available_share'],
           'properties': {**{key: COUNT for key in ('available', 'unavailable', 'denominator', 'not_applicable')},
                          'available_share': {'type': ['number', 'null'], 'minimum': 0, 'maximum': 1}}}

OUTPUT_DETAILS = {
    'get_snapshot_summary': ({'metrics': METRICS, 'visible_tasks': COUNT, 'completed_tasks': COUNT, 'missing_window_count': COUNT}, None),
    'get_risk_distribution': ({'dimension': {'enum': ['courier_id', 'aoi_id']}, 'sort': {'type': 'string'}},
                              {'type': 'object', 'required': ['metrics', 'missing_window_count'], 'properties': {'metrics': METRICS}}),
    'get_pending_tasks': ({'risk_state': {'type': 'string'}, 'sort': {'type': 'string'}},
                          {**TASK, 'required': [*TASK['required'], 'evidence_id', 'risk_state', 'current_wait_minutes',
                                               'current_overdue_minutes', 'remaining_minutes']}),
    'get_courier_workload': ({'sort': {'type': 'string'}}, {'type': 'object',
                            'required': ['courier_id', 'visible_tasks', 'completed_tasks', 'metrics', 'risk_distribution', 'status'],
                            'properties': {'metrics': METRICS}}),
    'get_quality_summary': ({'visible_tasks': COUNT, 'quality': {'type': 'object',
                            'required': ['window', 'stop_coordinates', 'accept_gps', 'pickup_gps'],
                            'properties': {key: QUALITY for key in ('window', 'stop_coordinates', 'accept_gps', 'pickup_gps')}}}, None),
    'get_evidence': ({}, {'type': 'object', 'required': ['evidence_id', 'source_type', 'record'],
                         'properties': {'source_type': {'enum': ['snapshot_task', 'tool_result']},
                                        'record': {'type': 'object'}}}),
}
OUTPUT_SCHEMAS = {}
for _name, (_data, _row) in OUTPUT_DETAILS.items():
    _schema = deepcopy(RESULT_SCHEMA)
    # 失败/不可计算结果允许data为null；成功结果的数据字段由各工具契约定义。
    _schema['properties']['data'] = {'type': ['object', 'null'], 'properties': deepcopy(_data), 'required': list(_data)}
    if _row:
        _schema['properties']['rows']['items'] = deepcopy(_row)
    OUTPUT_SCHEMAS[_name] = _schema


def validate_arguments(name, arguments):
    if not isinstance(name, str) or name not in REGISTRY:
        raise ValueError('工具未注册。')
    schema = REGISTRY[name]['function']['parameters']
    properties = schema['properties']
    if not isinstance(arguments, dict) or set(arguments) - properties.keys():
        raise ValueError('工具参数包含不允许的字段；快照、时点和区域由服务器绑定。')
    if any(key not in arguments for key in schema['required']):
        raise ValueError('缺少必要的工具参数。')
    values = {}
    for key, rule in properties.items():
        if key not in arguments and 'default' not in rule:
            continue
        value = arguments.get(key, rule.get('default'))
        valid = (type(value) is int if rule['type'] == 'integer' else isinstance(value, str))
        if valid and 'enum' in rule:
            valid = value in rule['enum']
        if valid and rule['type'] == 'integer':
            valid = rule.get('minimum', value) <= value <= rule.get('maximum', value)
        if valid and rule['type'] == 'string':
            valid = bool(value.strip()) and rule.get('minLength', 0) <= len(value) <= rule.get('maxLength', len(value))
        if not valid:
            raise ValueError('工具参数类型或范围无效。')
        values[key] = value
    return values


class LogisticsTools:
    """Agent 可直接调用的分发器，不持有连接或自行裁定权限。"""
    def __init__(self, execute):
        self.execute = execute

    def declarations(self):
        return deepcopy(list(REGISTRY.values()))

    def call(self, name, arguments):
        return self.execute(name, validate_arguments(name, arguments))
