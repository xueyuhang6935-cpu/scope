"""聊天动作协议；不读取配置、数据库或执行业务动作。"""
import re

from project.agents.logistics_protocol import obj, enum, array, check, ProtocolError


ACTIONS = ('clarify', 'reply', 'investigate', 'followup', 'fact', 'constraint', 'draft', 'submit', 'approval_status')
TEXT = {'type': 'string', 'maxLength': 2000}
REF = {'type': 'string', 'minLength': 1, 'maxLength': 200}
PARAMS = {'type': 'object', 'additionalProperties': False, 'properties': {
    'region_ids': {**array(REF, 30), 'uniqueItems': True}, 'business_as_of': TEXT,
    'task_ids': {**array(REF, 20), 'uniqueItems': True}, 'strategy': enum('contact_courier', 'verify_window'),
    'note': {'type': 'string', 'maxLength': 1000}, 'occurred_at': TEXT,
    'source': {'type': 'string', 'maxLength': 200},
    'constraint_key': enum('instruction', 'contact_courier'), 'active': enum('yes', 'no'),
    'draft_version': {'type': 'integer', 'minimum': 1},
}}
SCHEMA = obj(action=enum(*ACTIONS), params=PARAMS, reply=TEXT, evidence_ids=array(REF, 20))
FIELDS = {
    'clarify': {'region_ids', 'business_as_of'}, 'reply': set(),
    'investigate': {'region_ids', 'business_as_of'}, 'followup': set(),
    'fact': {'task_ids', 'note', 'source', 'occurred_at'},
    'constraint': {'task_ids', 'note', 'constraint_key', 'active'},
    'draft': {'task_ids', 'strategy', 'note'}, 'submit': {'draft_version'}, 'approval_status': set(),
}


def validate(value, evidence_ids=()):
    check(value, SCHEMA)
    if set(value['params']) - FIELDS[value['action']]:
        raise ProtocolError('chat_action_parameters')
    if not set(value['evidence_ids']).issubset(evidence_ids):
        raise ProtocolError('chat_evidence_reference')
    return value


def explicit_submit(text):
    # 只接受明确命令；引用、条件、否定和询问不能触发写入待审状态。
    if any(word in text for word in ('不', '别', '不要', '如果', '假如', '是否', '怎么', '如何', '能否', '?', '？', '“', '”', '"', '撤回')):
        return False
    subject = r'(?:这个|当前|这份|该|方案|草稿|它|的|\s|v\d+)*'
    return bool(re.fullmatch(r'\s*(?:请|现在|帮我|把|将|\s)*'+subject+r'提交(?:'+subject+r')?(?:审批|审核)?[。！!]?\s*', text))


def constraint_parameters(text, params):
    """明确的受支持限制由程序核对，防止模型把禁止解析成解除。"""
    result = dict(params)
    for clause in re.split(r'[，,。；;]', text):
        clause = clause.strip()
        if re.fullmatch(r'(?:请|现在|暂时|先|目前|这次|此次|本次|我要求|\s)*(?:不要|别|禁止|不得|不允许|暂不)联系快递员[！!]?\s*', clause):
            result.update(constraint_key='contact_courier', active='yes')
        elif re.fullmatch(r'(?:请|现在|我要求|\s)*(?:解除|取消)(?:暂时)?(?:不要|禁止|不允许)?联系快递员(?:的)?(?:限制|约束)[！!]?\s*', clause):
            result.update(constraint_key='contact_courier', active='no')
    return result
