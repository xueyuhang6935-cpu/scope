"""Account-scoped persistent conversations and single-process read-only tasks."""
import json
import logging
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

from fastapi import APIRouter, Request

from .common import APIError, iso
from .business_access import current_scope
from .data_import import require_user
from .supply_analysis import INTENTS, analyze, propose, validate_plan, request_questions, plan_questions, action_questions
from .supply_model import configured, propose_with_model
from .supply_context import compact_task, focus_context, followup, grounded_answer, requested_action, ACTIONS

SCHEMA = """
CREATE TABLE IF NOT EXISTS supply_threads (
 id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, factory TEXT NOT NULL, title TEXT NOT NULL,
 request_id TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(user_id,request_id)
);
CREATE TABLE IF NOT EXISTS supply_tasks (
 id TEXT PRIMARY KEY, thread_id TEXT NOT NULL REFERENCES supply_threads(id), request_id TEXT NOT NULL,
 text TEXT NOT NULL, input_json TEXT NOT NULL, plan_json TEXT NOT NULL, status TEXT NOT NULL,
 result_json TEXT, error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(thread_id,request_id)
);
CREATE INDEX IF NOT EXISTS supply_threads_user ON supply_threads(user_id,created_at);
"""
ACTIVE = ("planning", "queued", "running", "awaiting_confirmation")


def identifier(value):
    if not isinstance(value, str) or not 16 <= len(value) <= 80:
        raise APIError(400, "请提供 16 至 80 字的唯一请求编号。")
    return value


def serialize(row):
    return {**{k: v for k, v in row.items() if k not in ("input_json", "plan_json", "result_json", "planning_json", "source_json")},
            "source": json.loads(row.get("source_json") or 'null'),
            "planning": {k: v for k, v in json.loads(row.get("planning_json") or '{}').items() if k not in ('context', 'answer', 'effectiveScope', 'queryText')},
            "plan": json.loads(row["plan_json"]), "result": json.loads(row["result_json"]) if row["result_json"] else None}


class SupplyService:
    def __init__(self, auth, data, env):
        self.auth, self.data, self.env = auth, data, env
        with data.lock:
            data.db.executescript(SCHEMA)
            if 'focus_task_id' not in {r['name'] for r in data.all('PRAGMA table_info(supply_threads)')}:
                data.db.execute('ALTER TABLE supply_threads ADD COLUMN focus_task_id TEXT')
            if 'planning_json' not in {r['name'] for r in data.all('PRAGMA table_info(supply_tasks)')}:
                data.db.execute('ALTER TABLE supply_tasks ADD COLUMN planning_json TEXT')
            if 'source_json' not in {r['name'] for r in data.all('PRAGMA table_info(supply_tasks)')}:
                data.db.execute('ALTER TABLE supply_tasks ADD COLUMN source_json TEXT')
            for table in ('supply_threads','supply_tasks'):
                if 'workspace_id' not in {r['name'] for r in data.all(f'PRAGMA table_info({table})')}:
                    data.db.execute(f"ALTER TABLE {table} ADD COLUMN workspace_id TEXT NOT NULL DEFAULT ''")
            data.db.execute("UPDATE supply_threads SET workspace_id='local-' || user_id WHERE workspace_id=''")
            data.db.execute("UPDATE supply_tasks SET workspace_id=(SELECT workspace_id FROM supply_threads WHERE id=thread_id) WHERE workspace_id=''")
            # ponytail: one application process; use an external worker/lease before multiple workers.
            data.db.execute("UPDATE supply_tasks SET status='failed',error=?,updated_at=? WHERE status IN ('planning','queued','running')",
                            ("服务重启中断了任务，请检查后重新发起。", iso()))
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="supply")

    def close(self):
        self.executor.shutdown(wait=True, cancel_futures=True)

    def user(self, request):
        user = require_user(self.auth, request)
        if request.method == "POST":
            origin = request.headers.get("origin")
            allowed = {str(request.base_url).rstrip("/"), self.env.get("FRONTEND_ORIGIN", "")}
            if origin and origin not in allowed:
                parsed = urlsplit(origin)
                local_dev = self.env.get("NODE_ENV") != "production" and parsed.hostname in {"localhost", "127.0.0.1", "::1"} and parsed.scheme in {"http", "https"}
                if not local_dev:
                    raise APIError(403, "请求来源不被允许。")
        return user

    def thread(self, thread_id, user_id):
        row = self.data.one("SELECT * FROM supply_threads WHERE id=? AND user_id=?", (thread_id, user_id))
        if not row:
            raise APIError(404, "会话不存在或无权访问。")
        user=self.auth.query('SELECT id,account FROM users WHERE id=?',(user_id,))
        self.permissions.require(user,row['workspace_id'],'view', ['A','B'] if row['factory']=='all' else [row['factory']])
        return row

    def task(self, task_id, user_id):
        row = self.data.one("SELECT t.* FROM supply_tasks t JOIN supply_threads h ON h.id=t.thread_id WHERE t.id=? AND h.user_id=?", (task_id, user_id))
        if not row:
            raise APIError(404, "任务不存在或无权访问。")
        user=self.auth.query('SELECT id,account FROM users WHERE id=?',(user_id,))
        self.check_plan(user,row['workspace_id'],json.loads(row['plan_json']))
        return row

    def check_plan(self,user,workspace,plan):
        factories=['A','B'] if plan['factory']=='all' else [plan['factory']]
        action='view' if plan['intent'] in ('readiness','') else 'data.read'
        self.permissions.require(user,workspace,action,factories)
        if plan['intent'] in ('inventory','quality','readiness'):
            for domain in (['inventory'] if plan['intent']=='inventory' else ['inventory','purchase']):
                self.permissions.require(user,workspace,'data.read',factories,domain=domain)

    def conversation_context(self, thread, user, source, preserve_previous=False):
        recent = []
        previous = None
        rows = self.data.all("SELECT * FROM supply_tasks WHERE thread_id=? ORDER BY rowid DESC LIMIT 100", (thread['id'],))
        for row in rows:
            # Stop at a changed data snapshot; never revive older, unrelated evidence.
            if not preserve_previous and json.loads(row.get('source_json') or 'null') != source:
                break
            self.check_plan(user, row['workspace_id'], json.loads(row['plan_json']))
            item = compact_task(row)
            if len(recent) < 6:
                recent.append(item)
            if previous is None and row['status'] in ('completed', 'no_data', 'insufficient_data', 'failed') and not (item['result'] or {}).get('answer'):
                previous = item
            if previous and len(recent) >= 6:
                break
        if thread.get('focus_task_id'):
            focused = self.task(thread['focus_task_id'], user['id'])
            if focused['thread_id'] == thread['id'] and focused['workspace_id'] == thread['workspace_id']:
                item = compact_task(focused)
                if (preserve_previous or item['source'] == source) and focused['status'] in ('completed', 'no_data', 'insufficient_data', 'failed') and not (item['result'] or {}).get('answer'):
                    previous = item
        return focus_context({'recent': list(reversed(recent)), 'previous': previous})

    def enrich_context(self, context, user, workspace):
        previous = context.get('previous')
        link = ((previous or {}).get('result') or {}).get('workflow')
        run = None
        if link:
            # read_run validates workspace and all captured data-domain permissions.
            run = self.workflow.read_run(user, link['runId'], workspace)
            # A linked replan is bounded by the existing maximum (five).
            for _ in range(5):
                if not run.get('nextRunId'):
                    break
                run = self.workflow.read_run(user, run['nextRunId'], workspace)
        return focus_context(context, run)

    def answer(self, row, plan, metadata, answer):
        context = metadata.get('context', {})
        previous = context.get('previous')
        result = {'answer': answer, 'summary': answer, 'scope': plan,
                  'basedOnTaskId': previous['id'] if previous else None,
                  'evidenceCapturedAt': (previous.get('result') or {}).get('capturedAt') if previous else None,
                  'evidenceSource': previous.get('source') if previous else None,
                  'capturedAt': iso(), 'ruleVersion': 'supply-followup-v1',
                  'records': [], 'missing': [], 'limitations': [], 'recommendations': [], 'truncated': False}
        self.data.db.execute("UPDATE supply_tasks SET plan_json=?,planning_json=?,result_json=?,status='completed',updated_at=? WHERE id=? AND status IN ('planning','awaiting_confirmation')",
                             (json.dumps(plan, ensure_ascii=False), json.dumps({**metadata, 'execution': 'answer', 'questions': [], 'notice': '基于会话已有信息回答，未重新查询。'}, ensure_ascii=False), json.dumps(result, ensure_ascii=False), iso(), row['id']))

    def check_context(self, row, user_id, context):
        user = self.auth.query('SELECT id,account FROM users WHERE id=?', (user_id,))
        for item in context.get('recent', []) + ([context['previous']] if context.get('previous') else []):
            self.check_plan(user, row['workspace_id'], item['plan'])
        focus = context.get('focus') or {}
        if focus.get('runId'):
            self.workflow.read_run(user, focus['runId'], row['workspace_id'])

    def action_context(self, row, user):
        metadata = json.loads(row.get('planning_json') or '{}')
        previous = metadata.get('context', {}).get('previous')
        if not previous:
            raise APIError(400, '请先查询缺料风险，再选择需要调查的对象。')
        origin = self.task(previous['id'], user['id'])
        if origin['thread_id'] != row['thread_id'] or origin['workspace_id'] != row['workspace_id']:
            raise APIError(403, '依据任务不在当前会话和工作空间。')
        return origin, json.loads(origin['result_json'] or '{}')

    def start_action(self, row, user, metadata, selection=None):
        action = metadata['action']
        if action == 'unsupported_execution':
            self.answer(row, json.loads(row['plan_json']), metadata, '尚未接入 ERP/WMS 执行，未下发调拨、采购或发货单据。方案仍须核对并由有权限的另一位用户审批。')
            return
        if metadata.get('questions') and not metadata.get('riskOptions'):
            return
        try:
            origin, result = self.action_context(row, user)
        except APIError as exc:
            if exc.status >= 403:
                raise
            self.answer(row, json.loads(row['plan_json']), metadata, '你想处理哪个问题？请先完成缺料查询，再选择风险。')
            return
        if action == 'approval_request':
            link = result.get('workflow')
            if not link:
                self.answer(row, json.loads(row['plan_json']), metadata, '尚未关联候选方案，请先调查并试算。批准必须核对具体方案版本并填写审批意见；本条消息未批准或执行任何方案。')
                return
            self.workflow.read_run(user, link['runId'], row['workspace_id'])
            self.save_workflow(row, metadata, link, '请在下方核对具体方案后审批。本条消息未批准或执行方案。')
            return
        # Resolve an existing discussion back to the original calculation.
        if result.get('workflow'):
            base = self.task(result['workflow']['basedOnTaskId'], user['id'])
            if base['thread_id'] != row['thread_id'] or base['workspace_id'] != row['workspace_id']:
                raise APIError(403, '原计算不在当前会话。')
            origin, result = base, json.loads(base['result_json'] or '{}')
        calculation = result.get('calculation')
        if origin['status'] not in ('completed', 'insufficient_data') or not calculation:
            self.answer(row, json.loads(row['plan_json']), metadata, '上一轮没有可用于处置的缺料计算结果。请先补齐数据并查询缺料风险。')
            return
        plan = json.loads(row['plan_json'])
        options = [r for r in calculation['results'] if r['computable'] and float(r['shortage']) > 0
                   and (plan['factory'] == 'all' or r['factory'] == plan['factory'])
                   and (not plan['material'] or r['material'] == plan['material'])]
        public = [{k: r[k] for k in ('factory', 'material', 'revision', 'shortage')} for r in options]
        if selection is not None:
            options = [r for r in options if all(r[k] == selection.get(k) for k in ('factory', 'material', 'revision'))]
            if len(options) != 1:
                raise APIError(400, '所选风险不在本轮保存的计算结果中。')
        if len(options) != 1:
            metadata.update(riskOptions=public, questions=['请选择要调查的具体风险。' if options else '当前结果中没有可处置的缺料风险，请重新查询。'])
            self.data.db.execute("UPDATE supply_tasks SET status='awaiting_confirmation',planning_json=? WHERE id=?", (json.dumps(metadata, ensure_ascii=False), row['id']))
            return
        source = json.loads(origin['source_json'])
        origin_plan = json.loads(origin['plan_json'])
        days = {'未来三天': 3, '未来一周': 7}.get(origin_plan['period'], source['days'])
        link = self.workflow.from_calculation(user, row['workspace_id'], calculation, options[0], 'chat:' + row['id'], days, question=row['text'])
        link.update(basedOnTaskId=origin['id'], windowDays=days, selected={k: options[0][k] for k in ('factory', 'material', 'revision')})
        self.save_workflow(row, metadata, link, '已关联现有调查和方案。' if link['reused'] else '已发起调查并试算，以下进度来自后台实际运行。')

    def save_workflow(self, row, metadata, link, summary):
        result = {'summary': summary, 'scope': json.loads(row['plan_json']), 'capturedAt': iso(), 'ruleVersion': 'supply-actions-v1',
                  'workflow': link, 'records': [], 'missing': [], 'limitations': ['审批不自动下发业务单据。'], 'recommendations': [], 'truncated': False}
        metadata.update(questions=[], execution='workflow')
        self.data.db.execute("UPDATE supply_tasks SET status='completed',result_json=?,planning_json=?,updated_at=? WHERE id=?", (json.dumps(result, ensure_ascii=False), json.dumps(metadata, ensure_ascii=False), iso(), row['id']))

    def run(self, task_id, user_id):
        started = time.monotonic()
        data = self.data
        token=None
        try:
            with data.lock:
                row = self.task(task_id, user_id)
                if row["status"] != "queued":
                    return
                data.db.execute("UPDATE supply_tasks SET status='running',updated_at=? WHERE id=?", (iso(), task_id))
                plan = json.loads(row["plan_json"])
                source = json.loads(row.get("source_json") or 'null')
                actor=self.auth.query('SELECT id,account FROM users WHERE id=?',(user_id,))
                token=current_scope.set({'user':actor,'workspace':row['workspace_id'],'explicit':True})
            if plan["intent"] == "readiness" and source:
                config = self.monitor.config(user_id)
                if config["scenario_id"] != source["scenarioId"]:
                    raise APIError(409, "数据库场景已改变，请重新发起并确认范围。")
                if plan["supplier"]:
                    raise APIError(400, "缺料计算不支持限定供应商，请清空后重试。")
                days = {"": source["days"], "未来三天": 3, "未来一周": 7, "未来一个月": 30}[plan["period"]]
                calculation = self.monitor.compute(user_id, days=days, factory=plan["factory"], material=plan["material"], as_of=source["asOf"], scenario_id=source["scenarioId"])
                missing = sorted(set(calculation["missing"] + [m for r in calculation["results"] for m in r["missing"]]))
                risks = [r for r in calculation["results"] if r["computable"] and float(r["shortage"]) > 0]
                status = "insufficient_data" if missing else "completed" if calculation["results"] else "no_data"
                result = {"summary": f"场景 {calculation['scenarioName']}：检测到 {len(risks)} 项缺料风险。" + ("部分物料数据不足，不能判断为无风险。" if missing else ""),
                          "scope": plan, "capturedAt": iso(), "ruleVersion": calculation["ruleVersion"], "records": [],
                          "truncated": False, "missing": missing, "limitations": calculation["limitations"],
                          "recommendations": ["在风险工作台扫描并查看首次缺料、受影响计划和来源证据。"], "calculation": calculation}
            else:
                # Conversations stay private; source data follows workspace grants.
                with data.lock:
                    if self.task(task_id, user_id)["status"] != "running":
                        return
                    status, result = analyze(data, user_id, plan, row["workspace_id"])
            if time.monotonic() - started > 30:
                raise TimeoutError()
            with data.lock:
                self.check_plan(actor,row["workspace_id"],plan)
                metadata = json.loads(row.get('planning_json') or '{}')
                if metadata.get('action') == 'refresh':
                    previous = metadata.get('context', {}).get('previous')
                    result['basedOnTaskId'] = previous['id'] if previous else None
                    old_calc = ((previous or {}).get('result') or {}).get('calculation') or {}
                    new_calc = result.get('calculation') or {}
                    result['refreshNotice'] = ('已重新计算，数据截止时点未变化；不代表取得实时数据。' if new_calc and old_calc.get('asOf') == new_calc.get('asOf') else '已使用当前配置重新查询计算；请核对本次数据时点。')
                data.db.execute("UPDATE supply_tasks SET status=?,result_json=?,updated_at=? WHERE id=? AND status='running'",
                                (status, json.dumps(result, ensure_ascii=False), iso(), task_id))
        except Exception as exc:
            logging.getLogger(__name__).warning("Supply task failed: %s", task_id)
            with data.lock:
                data.db.execute("UPDATE supply_tasks SET status='failed',error=?,updated_at=? WHERE id=? AND status IN ('queued','running')",
                                (exc.body["message"] if isinstance(exc, APIError) else "分析失败或超时，未生成结论。可重新发起只读分析。", iso(), task_id))
        finally:
            if token is not None:
                current_scope.reset(token)

    def plan(self, task_id, user_id):
        with self.data.lock:
            try:
                row = self.task(task_id, user_id)
                saved = json.loads(row.get('planning_json') or '{}')
                context = saved.get('context', {})
                self.check_context(row, user_id, context)
            except APIError as exc:
                self.data.db.execute("UPDATE supply_tasks SET status='failed',error=?,updated_at=? WHERE id=? AND status='planning'", (exc.body['message'], iso(), task_id))
                return
            if row['status'] != 'planning':
                return
            scope = saved.get('effectiveScope', json.loads(row['input_json'])['scope'])
        plan, metadata = propose_with_model(self.env, row['text'], scope, context)
        metadata = {**saved, **metadata}
        with self.data.lock:
            try:
                current = self.task(task_id, user_id)
                self.check_context(current, user_id, context)
            except APIError as exc:
                self.data.db.execute("UPDATE supply_tasks SET status='failed',error=?,updated_at=? WHERE id=? AND status='planning'", (exc.body['message'], iso(), task_id))
                return
            if current['status'] != 'planning':
                return
            try:
                plan = validate_plan(plan or json.loads(row['plan_json']), self.thread(row['thread_id'], user_id)['factory'])
                self.check_plan(self.auth.query('SELECT id,account FROM users WHERE id=?', (user_id,)), row['workspace_id'], plan)
            except APIError:
                plan = json.loads(row['plan_json'])
                metadata['understood'] = False
            action = saved.get('action', 'query')
            proposed_action = metadata.get('action', 'query')
            if proposed_action == 'answer':
                proposed_action = 'explain'
            if action == 'query' and proposed_action in ACTIONS and metadata.get('understood') is True:
                action = proposed_action
            metadata['action'] = action
            if action in ('investigate', 'simulate', 'approval_request', 'unsupported_execution') and metadata.get('understood') is True:
                # Model proposes routing only. Objects are resolved from saved host results.
                questions = action_questions(saved.get('queryText', row['text']), scope, plan, json.loads(row.get('source_json') or 'null'), True) if action in ('investigate', 'simulate') else []
                metadata['questions'] = questions
                self.data.db.execute("UPDATE supply_tasks SET plan_json=?,planning_json=?,status='awaiting_confirmation' WHERE id=?", (json.dumps(plan, ensure_ascii=False), json.dumps(metadata, ensure_ascii=False), task_id))
                if not questions:
                    try:
                        self.start_action(self.task(task_id, user_id), self.auth.query('SELECT id,account FROM users WHERE id=?', (user_id,)), metadata)
                    except Exception as exc:
                        self.data.db.execute("UPDATE supply_tasks SET status='failed',error=?,updated_at=? WHERE id=?", (exc.body['message'] if isinstance(exc, APIError) else '调查关联失败，请重新核对后重试。', iso(), task_id))
                return
            if saved.get('explanation') or action == 'explain':
                # An answer cannot change the scope or claim a new query was executed.
                answer_plan = json.loads(row['plan_json'])
                answer = metadata.get('answer') if metadata.get('understood') is True and context.get('previous') else None
                self.answer(current, answer_plan, metadata, answer or grounded_answer(context))
                return
            questions = request_questions(saved.get('queryText', row['text']), scope, plan, json.loads(row.get('source_json') or 'null'), metadata.get('understood') is True)
            metadata['questions'] = list(dict.fromkeys(questions + metadata.get('questions', [])))
            if not metadata['questions'] and metadata.get('understood') is True:
                try:
                    self.queue(current, self.auth.query('SELECT id,account FROM users WHERE id=?', (user_id,)), plan, {**metadata, 'execution': 'automatic'})
                except APIError as exc:
                    self.data.db.execute("UPDATE supply_tasks SET status='failed',error=?,updated_at=? WHERE id=?", (exc.body['message'], iso(), task_id))
                return
            if not metadata['questions']:
                metadata['questions'] = ['问题包含尚未可靠识别的条件，请核对模型建议的实际范围。']
            self.data.db.execute("UPDATE supply_tasks SET plan_json=?,planning_json=?,status=CASE WHEN status='planning' THEN 'awaiting_confirmation' ELSE status END,updated_at=? WHERE id=?",
                                 (json.dumps(plan or json.loads(row['plan_json']), ensure_ascii=False), json.dumps(metadata, ensure_ascii=False), iso(), task_id))

    def queue(self, row, user, plan, metadata):
        """Caller holds data.lock; auto and clarified requests share the same checks."""
        self.check_plan(user, row['workspace_id'], plan)
        questions = plan_questions(plan, json.loads(row.get('source_json') or 'null'))
        if questions:
            raise APIError(400, '；'.join(questions))
        if self.data.one("SELECT COUNT(*) AS n FROM supply_tasks WHERE status IN ('planning','queued','running') AND id<>?", (row['id'],))["n"] >= 16:
            raise APIError(429, '分析服务繁忙，请稍后重试。')
        self.data.db.execute("UPDATE supply_tasks SET plan_json=?,planning_json=?,status='queued',updated_at=? WHERE id=?",
                             (json.dumps(plan, ensure_ascii=False), json.dumps(metadata, ensure_ascii=False), iso(), row['id']))
        try:
            self.executor.submit(self.run, row['id'], user['id'])
        except RuntimeError:
            self.data.db.execute("UPDATE supply_tasks SET status='failed',error=? WHERE id=?", ('服务正在关闭，请稍后重试。', row['id']))

    def routes(self):
        router = APIRouter(prefix="/api/supply", tags=["供应链分析"])
        data = self.data

        @router.get("/capabilities")
        def capabilities(request: Request):
            self.user(request)
            return {"available": True, "intents": INTENTS, "parser": "model" if configured(self.env) else "rules_v2", "dataAccess": "authorized_workspace",
                    "limitations": ["库存和质量查询使用授权工作空间数据；缺料计算使用已接入的模拟数据库", "明确的只读查询直接执行；范围不明确、冲突或暂不支持时追问", "不支持供应商筛选；模拟场景计算窗口最长14天"]}

        @router.post("/threads")
        def create_thread(request: Request):
            user = self.user(request)
            body = request.state.body
            key, factory = identifier(body.get("requestId")), body.get("factory")
            if factory not in ("A", "B", "all"):
                raise APIError(400, "工厂范围无效。")
            self.permissions.require(user,self.permissions.workspace(user),'view',['A','B'] if factory=='all' else [factory])
            with data.lock:
                row = data.one("SELECT * FROM supply_threads WHERE user_id=? AND request_id=?", (user["id"], key))
                if row:
                    self.thread(row['id'],user['id'])
                    if row["factory"] != factory or row['workspace_id']!=self.permissions.workspace(user):
                        raise APIError(409, "请求编号已用于其他工厂。")
                    return row
                thread_id = "CHAT-" + uuid.uuid4().hex
                data.db.execute("INSERT INTO supply_threads(id,user_id,factory,title,request_id,created_at,workspace_id) VALUES(?,?,?,?,?,?,?)", (thread_id, user["id"], factory, "新对话", key, iso(), self.permissions.workspace(user)))
                return self.thread(thread_id, user["id"])

        @router.get("/threads")
        def threads(request: Request, factory: str = "all"):
            user = self.user(request)
            if factory not in ("A", "B", "all"):
                raise APIError(400, "工厂范围无效。")
            with data.lock:
                rows=data.all("SELECT * FROM supply_threads WHERE user_id=? AND workspace_id=? AND (?='all' OR factory=?) ORDER BY created_at DESC LIMIT 100", (user["id"], self.permissions.workspace(user), factory, factory))
                return [r for r in rows if self.permissions.can(user,r['workspace_id'],'view',['A','B'] if r['factory']=='all' else [r['factory']])]

        @router.get("/threads/{thread_id}")
        def read_thread(request: Request, thread_id: str):
            user = self.user(request)
            with data.lock:
                row = self.thread(thread_id, user["id"])
                return {**row, "tasks": [serialize(self.task(t["id"],user["id"])) for t in data.all("SELECT * FROM supply_tasks WHERE thread_id=? ORDER BY created_at,id", (thread_id,))]}

        @router.post("/threads/{thread_id}/messages")
        def message(request: Request, thread_id: str):
            user = self.user(request)
            body = request.state.body
            key, text = identifier(body.get("requestId")), body.get("text")
            if not isinstance(text, str) or not 1 <= len(text.strip()) <= 2000:
                raise APIError(400, "问题须为 1 至 2000 字。")
            text = text.strip()
            source = None
            if data.one("SELECT 1 FROM risk_monitor_configs WHERE workspace_id=?", (self.permissions.workspace(user),)):
                config = self.monitor.config(user["id"])
                source = {"scenarioId": config["scenario_id"], "asOf": config["as_of"], "days": config["horizon_days"]}
            with data.lock:
                thread = self.thread(thread_id, user["id"])
                if thread['workspace_id']!=self.permissions.workspace(user):
                    raise APIError(409,'请切换到会话所属工作空间后继续。')
                scope = validate_plan(body.get("scope", {}), thread["factory"])
                self.check_plan(user,thread["workspace_id"],scope)
                signature = json.dumps({"text": text, "scope": scope, **({'basedOnTaskId': body['basedOnTaskId']} if body.get('basedOnTaskId') else {})}, sort_keys=True, ensure_ascii=False)
                old = data.one("SELECT * FROM supply_tasks WHERE thread_id=? AND request_id=?", (thread_id, key))
                if old:
                    if old["input_json"] != signature:
                        raise APIError(409, "请求编号对应的内容已改变，请使用新编号。")
                    return serialize(old)
                if data.one("SELECT id FROM supply_tasks WHERE thread_id=? AND status IN ('planning','queued','running','awaiting_confirmation')", (thread_id,)):
                    raise APIError(409, "请先完成或停止当前任务。")
                if data.one("SELECT COUNT(*) AS n FROM supply_tasks WHERE thread_id=?", (thread_id,))["n"] >= 100:
                    raise APIError(409, "会话已达 100 轮，请新建对话。")
                if data.one("SELECT COUNT(*) AS n FROM supply_tasks WHERE status IN ('planning','queued','running')")["n"] >= 16:
                    raise APIError(429, "分析服务繁忙，请稍后重试同一请求。")
                task_id, now = "TASK-" + uuid.uuid4().hex, iso()
                action = requested_action(text)
                context = self.conversation_context(thread, user, source, preserve_previous=action != 'query')
                based_on = body.get('basedOnTaskId')
                if based_on:
                    if not isinstance(based_on, str) or len(based_on) > 80:
                        raise APIError(400, '依据任务编号格式无效。')
                    base = self.task(based_on, user['id'])
                    if base['thread_id'] != thread_id or base['workspace_id'] != thread['workspace_id']:
                        raise APIError(403, '依据任务不在当前会话。')
                    context['previous'] = compact_task(base)
                if action == 'explain' and context.get('previous'):
                    source = context['previous']['source']
                    context['recent'] = [item for item in context['recent'] if item['source'] == source]
                    previous_plan = context['previous']['plan']
                    explicit = propose(text, {'intent': '', 'factory': 'all', 'material': '', 'supplier': '', 'period': ''})
                    changed = any(explicit[k] and explicit[k] != previous_plan[k] for k in ('intent', 'material', 'period'))
                    factories = {f.upper() for f in re.findall(r'([AB])\s*(?:工厂|厂)', text, re.I)}
                    if changed or (factories and factories != {previous_plan['factory']}):
                        raise APIError(400, '解释只能使用所选结果的原范围；如需改变物料、工厂或时间，请重新查询。')
                context['manualScope'] = scope
                self.enrich_context(context, user, thread['workspace_id'])
                explanation, effective_scope, query_text = followup(text, scope, context)
                plan = propose(query_text, effective_scope)
                if action == 'explain':
                    explanation = True
                if action == 'refresh':
                    explanation = False
                    query_text = re.sub(r'重新查询|重新查一下|重新查|重新计算|刷新|现在还是缺吗[？?]?|现在还缺吗[？?]?', '', query_text)
                questions = request_questions(query_text, effective_scope, plan, source)
                workflow_action = action in ('investigate', 'simulate', 'approval_request', 'unsupported_execution')
                if workflow_action:
                    questions = action_questions(query_text, effective_scope, plan, source) if action in ('investigate', 'simulate') and context.get('previous') else []
                model_needed = not workflow_action and (bool(questions) or explanation) and configured(self.env)
                metadata = {'mode': 'rules', 'questions': questions, 'execution': 'clarification' if questions else 'automatic',
                            'context': context, 'effectiveScope': effective_scope, 'queryText': query_text, 'explanation': explanation, 'action': action}
                self.check_plan(user, thread['workspace_id'], plan)
                data.db.execute("BEGIN IMMEDIATE")
                try:
                    data.db.execute("INSERT INTO supply_tasks(id,thread_id,request_id,text,input_json,plan_json,status,result_json,error,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (task_id, thread_id, key, text, signature, json.dumps(plan, ensure_ascii=False), "planning" if model_needed else "awaiting_confirmation", None, None, now, now))
                    data.db.execute("UPDATE supply_tasks SET workspace_id=? WHERE id=?",(thread["workspace_id"],task_id))
                    data.db.execute("UPDATE supply_tasks SET source_json=? WHERE id=?", (json.dumps(source, ensure_ascii=False) if source else None, task_id))
                    data.db.execute("UPDATE supply_tasks SET planning_json=? WHERE id=?", (json.dumps(metadata, ensure_ascii=False), task_id))
                    data.db.execute("UPDATE supply_threads SET title=? WHERE id=? AND title='新对话'", (text[:40], thread_id))
                    if not explanation:
                        data.db.execute('UPDATE supply_threads SET focus_task_id=? WHERE id=?', (task_id, thread_id))
                    data.db.execute("COMMIT")
                except Exception:
                    data.db.execute("ROLLBACK")
                    raise
                if workflow_action:
                    try:
                        self.start_action(self.task(task_id, user['id']), user, metadata)
                    except Exception as exc:
                        data.db.execute("UPDATE supply_tasks SET status='failed',error=?,updated_at=? WHERE id=?", (exc.body['message'] if isinstance(exc, APIError) else '调查关联失败，请检查服务后重试；未批准或执行任何方案。', iso(), task_id))
                elif explanation and not model_needed:
                    self.answer(self.task(task_id, user['id']), plan, metadata, grounded_answer(context))
                elif not questions and not model_needed:
                    self.queue(self.task(task_id, user['id']), user, plan, metadata)
                elif model_needed:
                    self.executor.submit(self.plan, task_id, user['id'])
                return serialize(self.task(task_id, user["id"]))

        @router.get("/tasks/{task_id}")
        def read_task(request: Request, task_id: str):
            user = self.user(request)
            with data.lock:
                return serialize(self.task(task_id, user["id"]))

        @router.post('/tasks/{task_id}/select-risk')
        def select_risk(request: Request, task_id: str):
            user = self.user(request)
            with data.lock:
                row = self.task(task_id, user['id'])
                if row['workspace_id'] != self.permissions.workspace(user):
                    raise APIError(403, '请切换到会话所属工作空间。')
                metadata = json.loads(row['planning_json'] or '{}')
                if row['status'] == 'completed':
                    selected = (json.loads(row['result_json'] or '{}').get('workflow') or {}).get('selected', {})
                    if any(selected.get(k) != request.state.body.get(k) for k in ('factory', 'material', 'revision')):
                        raise APIError(409, '该任务已经选择其他风险，请新建调查请求。')
                    return serialize(row)
                if row['status'] != 'awaiting_confirmation' or metadata.get('action') not in ('investigate', 'simulate'):
                    raise APIError(409, '当前任务不等待选择风险。')
                self.start_action(row, user, metadata, request.state.body)
                return serialize(self.task(task_id, user['id']))

        @router.post('/tasks/{task_id}/select-plan')
        def select_plan(request: Request, task_id: str):
            user = self.user(request)
            with data.lock:
                row = self.task(task_id, user['id'])
                if row['workspace_id'] != self.permissions.workspace(user):
                    raise APIError(403, '请切换到会话所属工作空间。')
                result = json.loads(row['result_json'] or '{}')
                link = result.get('workflow')
                if not link:
                    raise APIError(409, '本条消息没有关联调查。')
                run = self.workflow.read_run(user, link['runId'], row['workspace_id'])
                # Follow only server-recorded replan links, never a client-provided run.
                for _ in range(6):
                    if not run.get('nextRunId'):
                        break
                    run = self.workflow.read_run(user, run['nextRunId'], row['workspace_id'])
                selected = next((p for p in run['plans'] if p['id'] == request.state.body.get('planId')), None)
                if not selected:
                    raise APIError(400, '方案不属于本条消息关联的当前调查。')
                link.update(planId=selected['id'], runId=run['id'])
                data.db.execute('UPDATE supply_tasks SET result_json=?,updated_at=? WHERE id=?', (json.dumps(result, ensure_ascii=False), iso(), task_id))
                data.db.execute('UPDATE supply_threads SET focus_task_id=? WHERE id=?', (task_id, row['thread_id']))
                return serialize(self.task(task_id, user['id']))

        @router.post("/tasks/{task_id}/confirm")
        def confirm(request: Request, task_id: str):
            user = self.user(request)
            with data.lock:
                row = self.task(task_id, user["id"])
                if json.loads(row.get('planning_json') or '{}').get('action') in ('investigate', 'simulate', 'approval_request', 'unsupported_execution'):
                    raise APIError(400, '请在聊天中选择风险，不能将调查改为普通查询。')
                thread = self.thread(row["thread_id"], user["id"])
                plan = validate_plan(request.state.body.get("plan"), thread["factory"])
                self.check_plan(user,row["workspace_id"],plan)
                if not plan["intent"] or request.state.body.get("confirmed") is not True:
                    raise APIError(400, "请选择分析类型并确认实际执行范围。")
                if row["status"] != "awaiting_confirmation":
                    if json.loads(row["plan_json"]) == plan:
                        return serialize(row)
                    raise APIError(409, "任务已确认，不能修改当次范围。")
                metadata = {**json.loads(row.get('planning_json') or '{}'), 'questions': [], 'execution': 'clarified', 'notice': '按澄清后的范围执行只读查询。'}
                self.queue(row, user, plan, metadata)
                return serialize(self.task(task_id, user["id"]))

        @router.post("/tasks/{task_id}/cancel")
        def cancel(request: Request, task_id: str):
            user = self.user(request)
            with data.lock:
                row = self.task(task_id, user["id"])
                if row["status"] in ACTIVE:
                    data.db.execute("UPDATE supply_tasks SET status='cancelled',updated_at=? WHERE id=?", (iso(), task_id))
                return serialize(self.task(task_id, user["id"]))

        return router
