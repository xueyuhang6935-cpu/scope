"""调查成果索引；正文与证据仍读取原保存结果，归档不扩大访问权。"""
import json

from fastapi import APIRouter, Request
from fastapi.responses import Response

from project.agents.logistics_report import render_markdown
from project.agents.logistics_protocol import validate_output, ProtocolError
from .common import APIError
from .logistics_snapshots import content_hash
from .logistics_chat import chat_time

SCHEMA = '''CREATE TABLE IF NOT EXISTS logistics_knowledge (
 knowledge_id TEXT PRIMARY KEY, run_id TEXT NOT NULL UNIQUE REFERENCES logistics_runs(run_id),
 snapshot_id TEXT NOT NULL, title TEXT NOT NULL, result_kind TEXT NOT NULL,
 result_status TEXT NOT NULL, reference_fingerprint TEXT NOT NULL, archived_at TEXT NOT NULL
);'''


class LogisticsKnowledge:
    def __init__(self, chat):
        self.chat, self.runs, self.data = chat, chat.runs, chat.data
        self.reports, self.access = chat.reports, chat.access
        with self.data.lock:
            self.data.db.executescript(SCHEMA)
        chat.knowledge = self

    def source(self, run):
        result = json.loads(run['result_json']) if run['result_json'] else {}
        saved = self.data.one('SELECT * FROM logistics_reports WHERE run_id=?', (run['run_id'],))
        template = json.loads(saved['result_json']) if saved else None
        model = result.get('report') if run['mode'] == 'model' else None
        return {'template': template, 'model': model}

    def refresh_index(self):
        # ponytail: 索引只补录已保存成果；数据量增长后按更新时间增量扫描。
        with self.data.lock:
            for run in self.data.all("SELECT * FROM logistics_runs WHERE status IN ('completed','waiting','failed','cancelled') ORDER BY updated_at"):
                source = self.source(run)
                if not source['template'] and not source['model']:
                    continue
                snapshot = self.data.one('SELECT * FROM logistics_snapshots WHERE snapshot_id=?', (run['snapshot_id'],))
                if not snapshot:
                    continue
                partial = run['status'] != 'completed' or (source['model'] or {}).get('value', {}).get('status') == 'partial'
                kind = 'model' if source['model'] else 'deterministic'
                title = run['question'] or '区域 '+ '、'.join(json.loads(snapshot['regions_json']))+' 揽收风险调查'
                self.data.db.execute('INSERT OR IGNORE INTO logistics_knowledge VALUES(?,?,?,?,?,?,?,?)',
                    ('LKNOW-'+run['run_id'], run['run_id'], run['snapshot_id'], title, kind,
                     'partial' if partial else 'complete', content_hash(source['model'] or source['template']), run['updated_at']))

    def read(self, user, identifier):
        with self.data.lock:
            self.access.require(user, 'view')
            saved = self.data.one('SELECT * FROM logistics_knowledge WHERE knowledge_id=?', (identifier,))
            if not saved:
                raise APIError(404, '调查成果不存在。')
            run = self.runs.saved(saved['run_id'])
            metadata = self.runs.authorize(user, run)
            source = self.source(run)
            original = source['model'] if saved['result_kind'] == 'model' else source['template']
            if saved['snapshot_id'] != run['snapshot_id'] or content_hash(original) != saved['reference_fingerprint']:
                raise APIError(409, '归档成果与原保存结果不一致。')
            template = self.reports.read(user, run['run_id']) if source['template'] else None
            model = source['model']
            if model:
                scope = {key: metadata[key] for key in ('snapshot_id', 'business_as_of', 'city', 'region_ids', 'metric_version')}
                scope.update(run_id=run['run_id'], workspace_id=run['workspace_id'], fingerprint=metadata['snapshot_fingerprint'])
                records = {ref: self.runs.workflow.tools.evidence_store.read(user, run['run_id'], ref)
                           for ref in model['value'].get('evidence_ids', [])}
                try:
                    validate_output('report', model['value'], scope=scope, evidence=records)
                except ProtocolError:
                    raise APIError(409, '归档模型成果与原证据不一致。') from None
            evidence_ids = list(dict.fromkeys((model or {}).get('value', {}).get('evidence_ids', [])+
                [template[key] for key in ('summary_evidence_id', 'tasks_evidence_id', 'quality_evidence_id') if template and template.get(key)]+
                [task['evidence_id'] for task in (template or {}).get('focus_tasks', [])]))
            for ref in evidence_ids:
                self.runs.workflow.tools.evidence_store.read(user, run['run_id'], ref)
            collaboration = self.chat.collaboration.read(user, run['run_id'])
            scope = {key: metadata[key] for key in ('snapshot_id', 'batch_id', 'city', 'region_ids', 'business_as_of')}
            value = (model or {}).get('value', {})
            limitations = list(dict.fromkeys((template or {}).get('limitations', [])+value.get('limitations', [])))
            if saved['result_status'] == 'partial' and not value.get('unresolved_questions'):
                unresolved = ['本次调查未完整完成，请核对已有证据及停止原因。']
            else:
                unresolved = value.get('unresolved_questions', [])
            return {**dict(saved), 'scope': scope, 'report': template, 'model_report': model,
                    'evidence_ids': evidence_ids, 'limitations': limitations, 'unresolved_questions': unresolved,
                    'stop_reason': (json.loads(run['result_json']) if run['result_json'] else {}).get('stop_reason') or run['error_code'],
                    'processing': {'phase': collaboration['phase'], 'proposals': collaboration['proposals'],
                                   'followup': collaboration['followup'], 'review': collaboration['review']}}

    def brief(self, value):
        claims = (value['model_report'] or {}).get('value', {}).get('claims', [])
        if claims:
            abstract = '；'.join(claim['statement'] for claim in claims[:2])
        elif value['report']:
            abstract = '；'.join(f"{metric['name']}：{metric['value'] if metric['value'] is not None else '不可计算'}"
                                for metric in value['report']['summary']['metrics'].values())
        else:
            abstract = '已保存部分调查结果，仍有待核查事项。'
        return {key: value[key] for key in ('knowledge_id', 'run_id', 'title', 'scope', 'result_kind',
                'result_status', 'archived_at', 'limitations')} | {'abstract': abstract}

    def destination(self, user, run_id):
        with self.data.lock:
            run = self.runs.saved(run_id)
            self.runs.authorize(user, run)
            state = self.chat.collaboration.read(user, run_id)
            link = self.chat.conversation_for_run(user, run_id)
            if link:
                return {'path': f"/assistant/{link}?run={run_id}"}
            if run['creator_id'] == user['id'] and (run['status'] in ('queued', 'running') or
                    state['phase'] in ('changes_requested', 'draft', 'pending_approval', 'approved', 'followup', 'receipt')):
                self.access.require(user, 'investigate', state['scope']['region_ids'])
                return {'requires_association': True}
            if state['proposals']:
                proposal = state['proposals'][-1]
                return {'path': f"/approvals/{run_id}?version={proposal['proposal_version']}"}
            self.refresh_index()
            entry = self.data.one('SELECT knowledge_id FROM logistics_knowledge WHERE run_id=?', (run_id,))
            if entry:
                self.read(user, entry['knowledge_id'])
                return {'path': '/knowledge/'+entry['knowledge_id']}
            raise APIError(404, '当前调查尚无可展示成果或可处理对象。')

    def routes(self):
        router = APIRouter(prefix='/api/logistics', tags=['物流调查知识库'])

        def actor(request, allowed=()):
            if set(request.query_params) - {'workspaceId', *allowed}:
                raise APIError(400, '成果读取参数无效。')
            return self.access.user(request)

        @router.get('/knowledge')
        def listing(request: Request, page: int = 1, search: str = '', region: str = '', status: str = '', sort: str = 'archived', reference_from: str = '', reference_to: str = ''):
            user = actor(request, ('page', 'search', 'region', 'status', 'sort', 'reference_from', 'reference_to'))
            self.access.require(user, 'view')
            if not 1 <= page <= 10000 or len(search) > 200 or status not in ('', 'complete', 'partial') or sort not in ('archived', 'reference'):
                raise APIError(400, '成果筛选或分页无效。')
            if region and region not in self.access.allowed_regions(user, 'view'):
                raise APIError(403, '成果区域无权访问。')
            start, end = chat_time(reference_from) if reference_from else '', chat_time(reference_to) if reference_to else ''
            if start and end and start > end:
                raise APIError(400, '参考时点起止顺序无效。')
            self.refresh_index()
            values = []
            with self.data.lock:
                for row in self.data.all('SELECT knowledge_id FROM logistics_knowledge'):
                    try:
                        value = self.read(user, row['knowledge_id'])
                    except APIError as exc:
                        if exc.status in (403, 404):
                            continue
                        raise
                    if region and region not in value['scope']['region_ids'] or status and value['result_status'] != status:
                        continue
                    if start and value['scope']['business_as_of'] < start or end and value['scope']['business_as_of'] > end:
                        continue
                    terms = value['title']+' '+value['scope']['business_as_of']+' '+ ' '.join(
                        task['order_id'] for task in (value['report'] or {}).get('focus_tasks', []))
                    if search.strip() in terms:
                        values.append(self.brief(value))
            values.sort(key=lambda item: (item['scope']['business_as_of'] if sort == 'reference' else item['archived_at'], item['knowledge_id']), reverse=True)
            return {'rows': values[(page-1)*20:page*20], 'total_rows': len(values), 'page': page, 'page_size': 20}

        @router.get('/knowledge/{identifier}')
        def detail(request: Request, identifier: str):
            user = actor(request)
            self.access.require(user, 'view')
            self.refresh_index()
            return self.read(user, identifier)

        @router.get('/knowledge/{identifier}/download')
        def download(request: Request, identifier: str):
            user = actor(request)
            self.access.require(user, 'view')
            self.refresh_index()
            value = self.read(user, identifier)
            lines = ['# '+value['title'], '参考时点：'+value['scope']['business_as_of'],
                     '成果状态：'+('部分结果' if value['result_status'] == 'partial' else '完整报告')]
            model = (value['model_report'] or {}).get('value', {})
            lines += [claim['statement'] for claim in model.get('claims', [])]
            lines += value['limitations']+value['unresolved_questions']
            if value['report']:
                lines += ['\n## 原保存的确定性报告\n', render_markdown(value['report'])]
            return Response('\n\n'.join(lines), media_type='text/markdown; charset=utf-8', headers={
                'Content-Disposition': f'attachment; filename="{identifier}.md"', 'Cache-Control': 'no-store'})

        @router.get('/runs/{run_id}/destination')
        def destination(request: Request, run_id: str):
            return self.destination(actor(request), run_id)

        return router
