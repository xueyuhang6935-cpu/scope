"""Demo 人工协同：版本化建议、跨账号审批、内部回执与同源新快照复核。"""
import json
import uuid

from fastapi import APIRouter, Request

from .common import APIError, iso
from .logistics_metrics import calculate
from .logistics_snapshots import canonical, content_hash

SCHEMA = '''CREATE TABLE IF NOT EXISTS logistics_collaboration (
 run_id TEXT PRIMARY KEY REFERENCES logistics_runs(run_id),
 state_json TEXT NOT NULL, state_hash TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS logistics_collaboration_operations (
 run_id TEXT NOT NULL, actor_id INTEGER NOT NULL, operation_id TEXT NOT NULL,
 request_hash TEXT NOT NULL, result_json TEXT NOT NULL,
 PRIMARY KEY(run_id,actor_id,operation_id)
);'''
STRATEGIES = {'contact_courier': '联系快递员核查优先任务', 'verify_window': '核对窗口与现场约束'}


def text(value, name, limit=1000):
    if type(value) is not str or not 0 < len(value.strip()) <= limit:
        raise APIError(400, f'{name}须为1—{limit}字。')
    return value.strip()


class LogisticsCollaboration:
    def __init__(self, reports):
        self.reports, self.runs = reports, reports.runs
        self.data, self.access, self.snapshots = self.runs.data, self.runs.access, self.runs.snapshots
        with self.data.lock:
            self.data.db.executescript(SCHEMA)

    def state(self, run_id):
        saved = self.data.one('SELECT * FROM logistics_collaboration WHERE run_id=?', (run_id,))
        if not saved:
            return {'version': 0, 'phase': 'investigation', 'task_limit': 5, 'feedback': [],
                    'proposals': [], 'drafts': [], 'followup': None, 'review': None}
        value = json.loads(saved['state_json'])
        if content_hash(value) != saved['state_hash']:
            raise APIError(409, '协同记录校验失败。')
        value.setdefault('drafts', [])
        return value

    def permitted(self, user, row, metadata, action):
        try:
            self.access.require(user, action, metadata['region_ids'], metadata['city'],
                                requested_by=row['creator_account'] if action == 'approve' else None)
            return True
        except APIError:
            return False

    def read(self, user, run_id):
        with self.data.lock:
            row = self.runs.saved(run_id)
            metadata = self.runs.authorize(user, row)
            value = self.state(run_id)
            if value['review']:
                current = self.snapshots.ready_metadata(user, value['review']['snapshot_id'])
                if current['snapshot_fingerprint'] != value['review']['snapshot_fingerprint']:
                    raise APIError(409, '复核快照指纹已变化。')
            if row['creator_id'] != user['id']:
                value.pop('drafts', None)
            return {**value, 'scope': {key: metadata[key] for key in (
                'snapshot_id', 'batch_id', 'city', 'region_ids', 'business_as_of', 'snapshot_fingerprint')},
                'can_edit': row['creator_id'] == user['id'] and self.permitted(user, row, metadata, 'investigate'),
                'can_approve': self.permitted(user, row, metadata, 'approve')}

    def mutate(self, user, run_id, action, body, *, operation_id=None):
        fields = {
            'feedback': {'kind', 'text', 'task_limit', 'simulated'},
            'propose': {'strategy', 'note'},
            'decide': {'proposal_version', 'proposal_hash', 'decision', 'reason'},
            'followup': set(),
            'receipt': {'result', 'simulated', 'note', 'reference'},
            'review': {'snapshot_id'},
            'draft': {'strategy', 'note', 'task_ids'},
            'submit': {'draft_version', 'draft_hash'},
            'chat_feedback': {'kind', 'text', 'task_ids', 'occurred_at', 'source', 'constraint_key', 'active'},
        }
        if action not in fields or type(body) is not dict or set(body) != fields[action] | {'version'}:
            raise APIError(400, '协同操作或参数无效，不能覆盖运行范围。')
        if type(body['version']) is not int or body['version'] < 0:
            raise APIError(400, '协同版本无效。')
        with self.runs.transaction():
            row = self.runs.saved(run_id)
            metadata = self.runs.authorize(user, row, owner=action != 'decide')
            self.access.require(user, 'approve' if action == 'decide' else 'investigate',
                                metadata['region_ids'], metadata['city'],
                                requested_by=row['creator_account'] if action == 'decide' else None)
            state = self.state(run_id)
            request_hash = content_hash([user['account'], action, body])
            if operation_id is not None:
                if type(operation_id) is not str or not 0 < len(operation_id) <= 128:
                    raise APIError(400, '操作标识无效。')
                previous = self.data.one('SELECT * FROM logistics_collaboration_operations WHERE run_id=? AND actor_id=? AND operation_id=?',
                                         (run_id, user['id'], operation_id))
                if previous:
                    if previous['request_hash'] != request_hash:
                        raise APIError(409, '操作标识已用于不同内容。')
                    return json.loads(previous['result_json'])
            # 相同最后操作在响应丢失后可重读；旧版本的不同操作必须重新核对。
            if state.get('last_request') == request_hash:
                return self.read(user, run_id)
            if state['version'] != body['version']:
                raise APIError(409, '协同状态已变化，请刷新后核对最新版本。')
            if row['status'] not in ('completed', 'waiting'):
                raise APIError(409, '调查须先完成或保存部分结果，才能进入人工协同。')
            getattr(self, action)(user, row, metadata, state, body)
            state['version'] += 1
            state['last_request'] = request_hash
            stamp = iso()
            self.data.db.execute('INSERT INTO logistics_collaboration VALUES(?,?,?,?) '
                'ON CONFLICT(run_id) DO UPDATE SET state_json=excluded.state_json,state_hash=excluded.state_hash,updated_at=excluded.updated_at',
                (run_id, canonical(state), content_hash(state), stamp))
            self.data.db.execute('INSERT INTO logistics_events SELECT ?,coalesce(max(seq),0)+1,?,?,? '
                'FROM logistics_events WHERE run_id=?',
                (run_id, 'collaboration_'+action, canonical({'version': state['version'], 'phase': state['phase'],
                 'actor': user['account']}), stamp, run_id))
            result = self.read(user, run_id)
            if operation_id is not None:
                self.data.db.execute('INSERT INTO logistics_collaboration_operations VALUES(?,?,?,?,?)',
                                     (run_id, user['id'], operation_id, request_hash, canonical(result)))
            return result

    def chat_feedback(self, user, row, metadata, state, body):
        if state['followup'] or any(p['status'] == 'approved' for p in state['proposals']):
            raise APIError(409, '已批准版本的变更须建立新的调查与方案。')
        if body['kind'] not in ('reported', 'constraint') or type(body['active']) is not bool:
            raise APIError(400, '反馈类型无效。')
        if body['constraint_key'] not in ('instruction', 'contact_courier'):
            raise APIError(400, '约束字段无效。')
        report = self.reports.read(user, row['run_id'])
        tasks = {task['order_id'] for task in report['focus_tasks']}
        ids = body['task_ids']
        if type(ids) is not list or len(ids) > 20 or any(type(t) is not str or t not in tasks for t in ids) or len(set(ids)) != len(ids):
            raise APIError(400, '反馈对象不属于当前报告。')
        if type(body['source']) is not str or not 0 < len(body['source']) <= 200:
            raise APIError(400, '来源无效。')
        if body['occurred_at'] is not None:
            from .logistics_repository import reference_time
            reference_time(body['occurred_at'], 'occurred_at', business=True)
        if len(state['feedback']) >= 50:
            raise APIError(409, '反馈已达50条，请建立新的调查。')
        if body['kind'] == 'constraint':
            for old in state['feedback']:
                if old.get('constraint_key') == body['constraint_key'] and old.get('task_ids', []) == ids:
                    old['active'] = False
        state['feedback'].append({'feedback_id': 'LFB-'+uuid.uuid4().hex, 'kind': body['kind'],
            'text': text(body['text'], '反馈'), 'simulated': False, 'verification': 'unverified' if body['kind'] == 'reported' else 'analysis_constraint',
            'task_ids': ids, 'occurred_at': body['occurred_at'], 'source': body['source'],
            'constraint_key': body['constraint_key'], 'active': body['active'],
            'actor': user['account'], 'created_at': iso()})
        for proposal in state['proposals']:
            if proposal['status'] == 'pending':
                proposal['status'] = 'invalidated'
        state['phase'] = 'changes_requested'

    def draft(self, user, row, metadata, state, body):
        if state['followup'] or any(p['status'] == 'approved' for p in state['proposals']):
            raise APIError(409, '批准后的变更须建立新的调查与方案。')
        if type(body['strategy']) is not str or body['strategy'] not in STRATEGIES:
            raise APIError(400, '核查方式无效。')
        report = self.reports.read(user, row['run_id'])
        available = {task['order_id']: task for task in report['focus_tasks']}
        ids = body['task_ids']
        if type(ids) is not list or not 1 <= len(ids) <= 20 or any(type(t) is not str or t not in available for t in ids) or len(set(ids)) != len(ids):
            raise APIError(400, '草稿须引用当前报告中的具体任务。')
        constraints = [f for f in state['feedback'] if f['kind'] == 'constraint' and f.get('active', True)]
        if body['strategy'] == 'contact_courier' and any(f.get('constraint_key') == 'contact_courier' and
                (not f.get('task_ids') or set(ids) & set(f['task_ids'])) for f in constraints):
            raise APIError(409, '当前约束禁止联系所选任务的快递员，请先核查窗口或明确解除约束。')
        drafts = state.setdefault('drafts', [])
        if len(drafts) >= 50:
            raise APIError(409, '草稿版本已达50版，请建立新的调查。')
        previous = state['proposals'][-1] if state['proposals'] else None
        content = {'title': STRATEGIES[body['strategy']], 'strategy': body['strategy'], 'note': text(body['note'], '草稿说明'),
            'tasks': [available[t] for t in ids], 'task_limit': len(ids),
            'actions': [{'order_id': t, 'type': body['strategy'], 'sequence': i+1} for i, t in enumerate(ids)],
            'feedback': state['feedback'], 'snapshot_id': row['snapshot_id'], 'snapshot_fingerprint': metadata['snapshot_fingerprint'],
            'report_id': report['report_id'], 'business_as_of': metadata['business_as_of'],
            'limitations': ['仅为内部核查草稿，不代表派单或执行。', '人工陈述未经验证，不能证明风险原因。']}
        for proposal in state['proposals']:
            if proposal['status'] == 'pending':
                proposal['status'] = 'invalidated'
        drafts.append({'draft_version': len(drafts)+1, 'draft_hash': content_hash(content), 'content': content,
                       'status': 'draft', 'created_at': iso(), 'change_note': body['note'],
                       'previous_rejection': previous.get('decision') if previous and previous['status'] == 'rejected' else None})
        state['phase'] = 'draft'

    def submit(self, user, row, metadata, state, body):
        drafts = state.get('drafts', [])
        if not drafts or type(body['draft_version']) is not int:
            raise APIError(409, '没有可提交的草稿。')
        draft = drafts[-1]
        if draft['status'] != 'draft' or draft['draft_version'] != body['draft_version'] or draft['draft_hash'] != body['draft_hash']:
            raise APIError(409, '草稿已变化，请核对最新内容。')
        if content_hash(draft['content']) != draft['draft_hash'] or draft['content']['feedback'] != state['feedback']:
            raise APIError(409, '草稿内容或约束已变化，请生成新版本。')
        if state['followup'] or any(p['status'] in ('approved', 'pending') for p in state['proposals']):
            raise APIError(409, '已有待审或批准版本。')
        self.reports.read(user, row['run_id'])
        if len(state['proposals']) >= 20:
            raise APIError(409, '提交版本已达20版。')
        previous = state['proposals'][-1] if state['proposals'] else None
        if previous and previous['proposal_hash'] == draft['draft_hash']:
            raise APIError(409, '不能无变化重交原方案。')
        state['proposals'].append({'proposal_version': len(state['proposals'])+1, 'proposal_hash': draft['draft_hash'],
            'draft_version': draft['draft_version'], 'content': draft['content'], 'status': 'pending',
            'requested_by': user['account'], 'created_at': iso(), 'previous_rejection': draft['previous_rejection']})
        draft['status'] = 'submitted'
        state['phase'] = 'pending_approval'

    def feedback(self, user, row, metadata, state, body):
        if state['followup'] or any(p['status'] == 'approved' for p in state['proposals']):
            raise APIError(409, '已批准建议不能被追加反馈改写，请建立新的调查。')
        if body['kind'] not in ('reported', 'constraint') or type(body['simulated']) is not bool:
            raise APIError(400, '反馈类型或演示标记无效。')
        if type(body['task_limit']) is not int or not 1 <= body['task_limit'] <= 20:
            raise APIError(400, '优先核查任务数须为1—20。')
        if len(state['feedback']) >= 50:
            raise APIError(409, '本次调查反馈已达50条，请建立新的调查。')
        state['feedback'].append({'feedback_id': 'LFB-'+uuid.uuid4().hex, 'kind': body['kind'],
            'text': text(body['text'], '反馈'), 'simulated': body['simulated'],
            'verification': 'unverified' if body['kind'] == 'reported' else 'analysis_constraint',
            'task_limit': body['task_limit'] if body['kind'] == 'constraint' else None,
            'actor': user['account'], 'created_at': iso()})
        if body['kind'] == 'constraint':
            state['task_limit'] = body['task_limit']
        for proposal in state['proposals']:
            if proposal['status'] == 'pending':
                proposal['status'] = 'invalidated'
        state['phase'] = 'changes_requested'

    def propose(self, user, row, metadata, state, body):
        if state['followup'] or any(p['status'] in ('approved', 'pending') for p in state['proposals']):
            raise APIError(409, '先审核或通过反馈使当前待审版本失效。')
        if type(body['strategy']) is not str or body['strategy'] not in STRATEGIES:
            raise APIError(400, '建议方式无效。')
        if len(state['proposals']) >= 20:
            raise APIError(409, '建议版本已达20版，请建立新的调查。')
        report = self.reports.read(user, row['run_id'])
        tasks = report['focus_tasks'][:state['task_limit']]
        if not tasks:
            raise APIError(409, '报告没有待核查任务，不能生成处置建议。')
        content = {'strategy': body['strategy'], 'title': STRATEGIES[body['strategy']],
            'note': text(body['note'], '建议说明'), 'task_limit': state['task_limit'],
            'tasks': tasks, 'feedback': state['feedback'], 'snapshot_id': row['snapshot_id'],
            'snapshot_fingerprint': metadata['snapshot_fingerprint'], 'report_id': report['report_id'],
            'business_as_of': metadata['business_as_of'],
            'limitations': ['程序生成内部核查建议，不是最优派单或外部执行指令。',
                           '人工陈述未经验证；合成反馈只用于演示，不改变快照事实。']}
        proposal_hash = content_hash(content)
        if state['proposals'] and state['proposals'][-1]['proposal_hash'] == proposal_hash:
            raise APIError(409, '新建议必须修改任务范围、方式、说明或人工约束，不能重交被驳回的同一内容。')
        previous = state['proposals'][-1] if state['proposals'] else None
        state['proposals'].append({'proposal_version': len(state['proposals'])+1, 'proposal_hash': proposal_hash,
            'content': content, 'status': 'pending', 'requested_by': user['account'], 'created_at': iso(),
            'previous_rejection': previous.get('decision') if previous and previous['status'] == 'rejected' else None})
        state['phase'] = 'pending_approval'

    def decide(self, user, row, metadata, state, body):
        if not state['proposals'] or type(body['proposal_version']) is not int:
            raise APIError(409, '没有可审核的建议版本。')
        proposal = state['proposals'][-1]
        if (proposal['status'] != 'pending' or body['proposal_version'] != proposal['proposal_version']
                or body['proposal_hash'] != proposal['proposal_hash']
                or content_hash(proposal['content']) != proposal['proposal_hash']
                or proposal['content']['snapshot_fingerprint'] != metadata['snapshot_fingerprint']):
            raise APIError(409, '建议已失效或版本、摘要不一致，请重新核对。')
        if body['decision'] not in ('approved', 'rejected'):
            raise APIError(400, '审核决定无效。')
        self.reports.read(user, row['run_id'])
        creator = self.access.auth.query('SELECT * FROM users WHERE account=?', (row['creator_account'],))
        if not creator or not self.permitted(creator, row, metadata, 'investigate'):
            raise APIError(403, '发起人调查权限已失效，建议不能继续审核。')
        proposal['decision'] = {'result': body['decision'], 'reason': text(body['reason'], '审核理由'),
                                'actor': user['account'], 'created_at': iso()}
        proposal['status'] = body['decision']
        state['phase'] = 'approved' if body['decision'] == 'approved' else 'changes_requested'

    def followup(self, user, row, metadata, state, body):
        if state['followup'] or not state['proposals'] or state['proposals'][-1]['status'] != 'approved':
            raise APIError(409, '只有最新批准版本可创建一张内部跟进单。')
        proposal = state['proposals'][-1]
        reviewer = self.access.auth.query('SELECT * FROM users WHERE account=?', (proposal['decision']['actor'],))
        if not reviewer or not self.permitted(reviewer, row, metadata, 'approve'):
            raise APIError(403, '审核人权限已失效，请重新调查并审核。')
        state['followup'] = {'followup_id': 'LFOLLOW-'+uuid.uuid4().hex,
            'proposal_version': proposal['proposal_version'], 'proposal_hash': proposal['proposal_hash'],
            'status': 'open', 'external_dispatch': False, 'created_at': iso(), 'receipt': None}
        state['phase'] = 'followup'

    def receipt(self, user, row, metadata, state, body):
        if not state['followup'] or state['followup']['receipt']:
            raise APIError(409, '须有内部跟进单且尚未记录回执。')
        if body['result'] not in ('recorded', 'not_executed') or type(body['simulated']) is not bool:
            raise APIError(400, '回执结果或模拟标记无效。')
        if type(body['reference']) is not str or len(body['reference']) > 200:
            raise APIError(400, '回执来源编号无效。')
        if not body['simulated'] and not body['reference'].strip():
            raise APIError(400, '人工记录真实回执须提供来源编号，仍需外部核验。')
        status = 'not_executed' if body['result'] == 'not_executed' else 'simulated' if body['simulated'] else 'reported_executed'
        state['followup'].update(status=status, receipt={'receipt_id': 'LRECEIPT-'+uuid.uuid4().hex,
            'result': body['result'], 'simulated': body['simulated'], 'note': text(body['note'], '回执说明'),
            'reference': body['reference'].strip(), 'actor': user['account'], 'created_at': iso(),
            'verification': 'simulation' if body['simulated'] else 'unverified_external_report'})
        state['phase'] = 'receipt'

    def review(self, user, row, metadata, state, body):
        if not state['followup'] or not state['followup']['receipt'] or state['review']:
            raise APIError(409, '记录回执后才能进行一次新快照复核。')
        snapshot_id = text(body['snapshot_id'], '新快照编号', 100)
        new_meta, new_rows = self.snapshots.read_all(user, snapshot_id)
        for key in ('batch_id', 'city', 'region_ids', 'data_version', 'contract_version', 'metric_version'):
            if new_meta.get(key) != metadata.get(key):
                raise APIError(409, '复核须使用同一批次、范围、源版本和指标口径。')
        if new_meta['business_as_of'] <= metadata['business_as_of']:
            raise APIError(409, '复核时点必须晚于原快照。')
        self.access.require(user, 'investigate', new_meta['region_ids'], new_meta['city'])
        _, old_rows = self.snapshots.read_all(user, row['snapshot_id'])
        old = calculate(old_rows, metadata['business_as_of'])
        new = calculate(new_rows, new_meta['business_as_of'])
        key = lambda task: (task['city'], task['order_id'])
        visible = {key(task): task for task in new_rows}
        if not {key(task) for task in old_rows}.issubset(visible):
            raise APIError(409, '新快照缺少原可见任务，不能声称风险解除。')
        pending = {key(task): task for task in new['tasks']}
        risk = [task for task in old['tasks'] if task['risk_state'] != 'normal']
        comparisons = [{'order_id': task['order_id'], 'region_id': task['region_id'],
            'before': task['risk_state'], 'after': pending[key(task)]['risk_state'] if key(task) in pending else 'completed',
            'completion_observed': visible[key(task)]['completion_observed']} for task in risk]
        inflow = [task for task in new_rows if metadata['business_as_of'] < task['accept_time_reference'] <= new_meta['business_as_of']]
        new_flow = calculate(inflow, new_meta['business_as_of'])
        state['review'] = {'review_id': 'LREVIEW-'+uuid.uuid4().hex, 'snapshot_id': snapshot_id,
            'snapshot_fingerprint': new_meta['snapshot_fingerprint'], 'business_as_of': new_meta['business_as_of'],
            'original_snapshot_id': row['snapshot_id'], 'original_business_as_of': metadata['business_as_of'],
            'old_risk_queue': {'total': len(risk), 'completed': sum(t['completion_observed'] for t in comparisons),
                'still_pending': sum(not t['completion_observed'] for t in comparisons),
                'overdue': sum(t['after'] == 'overdue' for t in comparisons),
                'due_soon': sum(t['after'] == 'due_soon' for t in comparisons),
                'window_unavailable': sum(t['after'] == 'window_unavailable' for t in comparisons),
                'rows': comparisons[:20], 'truncated': len(comparisons) > 20},
            'new_inflow': {'total': len(inflow), 'metrics': new_flow['metrics']},
            'before_metrics': old['metrics'], 'after_metrics': new['metrics'], 'created_at': iso(),
            'attribution_supported': False,
            'limitations': ['仅复核公开历史样本；历史任务完成不能归因于内部建议或模拟回执。',
                '批准、内部跟进、回执和样本风险变化是不同状态；不代表真实派单或业务正式关闭。']}
        state['phase'] = 'reviewed'

    def routes(self):
        router = APIRouter(prefix='/api/logistics/runs', tags=['物流 Demo 人工协同'])

        def actor(request):
            if set(request.query_params) - {'workspaceId'}:
                raise APIError(400, '协同接口不能覆盖范围。')
            return self.access.user(request)

        @router.get('/{run_id}/collaboration')
        def read(request: Request, run_id: str):
            return self.read(actor(request), run_id)

        @router.post('/{run_id}/collaboration/{action}')
        def mutate(request: Request, run_id: str, action: str):
            return self.mutate(actor(request), run_id, action, request.state.body)

        return router

    def approval_routes(self):
        router = APIRouter(prefix='/api/logistics/approvals', tags=['物流 Demo 审核列表'])

        @router.get('/todos')
        def todos(request: Request, kind: str = '', page: int = 1, page_size: int = 5):
            user = self.access.user(request)
            kinds = {'my_pending': '我发起的待审建议', 'to_review': '我可审核的建议', 'needs_changes': '需修改的方案',
                     'awaiting_receipt': '待回执', 'awaiting_review': '待复核'}
            if (set(request.query_params) - {'workspaceId', 'kind', 'page', 'page_size'} or kind not in ('', *kinds)
                    or not 1 <= page <= 10000 or not 1 <= page_size <= 100):
                raise APIError(400, '待办筛选或分页无效。')
            with self.data.lock:
                self.access.require(user, 'view')
                member = self.access.member(user['account'])
                roles = {grant['role'] for grant in member['roles']}
                visible_kinds = [key for key in kinds if ('approver' in roles if key == 'to_review' else 'investigator' in roles)]
                values = {key: [] for key in visible_kinds}
                # ponytail: 沿用协同列表的逐项授权；记录量增长后复用授权索引查询。
                for saved in self.data.all('SELECT run_id FROM logistics_collaboration ORDER BY updated_at DESC,run_id DESC'):
                    try:
                        state = self.read(user, saved['run_id'])
                    except APIError as exc:
                        if exc.status == 403:
                            continue
                        raise
                    proposal = state['proposals'][-1] if state['proposals'] else None
                    item = {'run_id': saved['run_id'], 'scope': state['scope'],
                            'title': proposal['content']['title'] if proposal else '需修改的调查方案',
                            'proposal_version': proposal['proposal_version'] if proposal else None,
                            'status': proposal['status'] if proposal else state['phase']}
                    link = self.data.one('''SELECT c.conversation_id FROM logistics_conversation_runs r
                        JOIN logistics_conversations c USING(conversation_id)
                        WHERE r.run_id=? AND c.creator_id=? AND c.creator_account=? ORDER BY c.updated_at DESC LIMIT 1''',
                        (saved['run_id'], user['id'], user['account']))
                    item['conversation_id'] = link['conversation_id'] if link else None
                    if proposal and proposal['status'] == 'pending':
                        if state['can_edit'] and 'my_pending' in values:
                            values['my_pending'].append(item)
                        if state['can_approve'] and 'to_review' in values:
                            values['to_review'].append(item)
                    if (state['can_edit'] and 'needs_changes' in values and not state['followup']
                            and (state['phase'] == 'changes_requested' or proposal and proposal['status'] in ('rejected', 'invalidated'))):
                        values['needs_changes'].append(item)
                    if state['can_edit'] and state['followup']:
                        if not state['followup']['receipt'] and 'awaiting_receipt' in values:
                            values['awaiting_receipt'].append({**item, 'status': 'followup'})
                        elif state['followup']['receipt'] and not state['review'] and 'awaiting_review' in values:
                            values['awaiting_review'].append({**item, 'status': 'receipt'})
                selected = kind if kind in values else visible_kinds[0] if visible_kinds and not kind else kind
                rows = values.get(selected, [])
                offset = (page-1)*page_size
                return {'categories': [{'kind': key, 'name': kinds[key], 'count': len(values[key])} for key in visible_kinds],
                        'kind': selected, 'rows': rows[offset:offset+page_size], 'total_rows': len(rows),
                        'page': page, 'page_size': page_size, 'scope_label': '当前账号全部授权记录，不按当前快照筛选'}

        @router.get('')
        def approvals(request: Request, page: int = 1, status: str = '', search: str = '', view: str = ''):
            user = self.access.user(request)
            if (set(request.query_params) - {'workspaceId', 'page', 'status', 'search', 'view'} or not 1 <= page <= 10000
                    or status not in ('', 'pending', 'approved', 'rejected', 'invalidated') or len(search) > 200
                    or view not in ('', 'pending', 'history')):
                raise APIError(400, '审核列表参数无效。')
            with self.data.lock:
                self.access.require(user, 'view')
                values = []
                # ponytail: Demo 逐项复核授权；规模增长后按授权区域建立索引查询。
                for saved in self.data.all('SELECT run_id FROM logistics_collaboration ORDER BY updated_at DESC'):
                    try:
                        state = self.read(user, saved['run_id'])
                    except APIError as exc:
                        if exc.status == 403:
                            continue
                        raise
                    proposals = state['proposals'] if view == 'history' else state['proposals'][-1:]
                    for proposal in proposals:
                        if view == 'pending' and (proposal['status'] != 'pending' or not state['can_approve']):
                            continue
                        if view == 'history' and proposal['status'] == 'pending':
                            continue
                        if status and proposal['status'] != status:
                            continue
                        if search.strip() not in ' '.join((saved['run_id'], proposal['content']['title'], proposal['requested_by'])):
                            continue
                        values.append({'run_id': saved['run_id'], 'scope': state['scope'],
                            'proposal_version': proposal['proposal_version'], 'status': proposal['status'],
                            'title': proposal['content']['title'], 'requested_by': proposal['requested_by'],
                            'created_at': proposal['created_at'], 'decision': proposal.get('decision')})
                values.sort(key=lambda item: (item['created_at'], item['run_id'], item['proposal_version']), reverse=True)
                return {'rows': values[(page-1)*20:page*20], 'total_rows': len(values)}

        return router
