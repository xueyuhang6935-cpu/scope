"""报告先持久化结构化结果；读取与Markdown下载共享实时授权和证据校验。"""
import json
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import Response

from project.agents.logistics_report import build_report, render_markdown, render_html
from .common import APIError, iso
from .logistics_access import WORKSPACE
from .logistics_snapshots import canonical, content_hash


SCHEMA = '''CREATE TABLE IF NOT EXISTS logistics_reports (
 report_id TEXT PRIMARY KEY, run_id TEXT NOT NULL UNIQUE REFERENCES logistics_runs(run_id),
 workspace_id TEXT NOT NULL, snapshot_id TEXT NOT NULL, result_json TEXT NOT NULL,
 result_fingerprint TEXT NOT NULL, created_at TEXT NOT NULL
);'''


class LogisticsReportService:
    def __init__(self, tools):
        self.tools, self.runs, self.data = tools, tools.runs, tools.data
        with self.data.lock:
            self.data.db.executescript(SCHEMA)

    def create(self, user, run_id, *, claim_token=None, results=None):
        with self.data.lock:
            run = self.runs.saved(run_id)
            metadata = self.runs.authorize(user, run, owner=True)
            access = self.runs.access
            if claim_token is not None and not self.runs.owned(run_id, claim_token):
                raise APIError(409, '运行领取凭证已失效。')
            if not set(metadata['region_ids']).issubset(access.allowed_regions(user, 'investigate')):
                raise APIError(403, '报告生成缺少调查区域权限。')
            if self.data.one('SELECT report_id FROM logistics_reports WHERE run_id=?', (run_id,)):
                return self.read(user, run_id)
            if run['status'] in ('cancelled', 'failed'):
                raise APIError(409, '已取消或失败运行不能生成新报告。')
            summary = results['get_snapshot_summary'] if results else self.tools.call(user, run_id, 'get_snapshot_summary', {})
            tasks = results['get_pending_tasks'] if results else self.tools.call(user, run_id, 'get_pending_tasks', {'page_size': 20})
            quality = results['get_quality_summary'] if results else self.tools.call(user, run_id, 'get_quality_summary', {})
            try:
                report = build_report(run_id, summary, tasks, quality)
            except ValueError:
                raise APIError(409, '报告所需证据不可用或范围不一致。') from None
            report.update(report_id='LREPORT-'+uuid.uuid4().hex, created_at=iso())
            self.runs.authorize(user, run, owner=True)
            self.validate_references(user, run_id, report)
            with self.runs.transaction():
                if claim_token is not None and not self.runs.owned(run_id, claim_token):
                    raise APIError(409, '运行领取凭证已失效。')
                self.data.db.execute('INSERT INTO logistics_reports VALUES(?,?,?,?,?,?,?)',
                    (report['report_id'], run_id, WORKSPACE, run['snapshot_id'], canonical(report), content_hash(report), report['created_at']))
            return self.read(user, run_id)

    def validate_references(self, user, run_id, report):
        results = {}
        for key in ('summary_evidence_id', 'tasks_evidence_id', 'quality_evidence_id'):
            evidence = self.tools.evidence_store.read(user, run_id, report[key])
            results[key] = evidence['record']
            if evidence['source_type'] != 'tool_result' or any(evidence['record'][key] != value
                    for key, value in report['scope'].items()):
                raise APIError(409, '报告与证据范围或口径不一致。')
        if (results['summary_evidence_id']['tool'] != 'get_snapshot_summary'
                or results['tasks_evidence_id']['tool'] != 'get_pending_tasks'
                or results['quality_evidence_id']['tool'] != 'get_quality_summary'):
            raise APIError(409, '报告引用工具类型不一致。')
        rebuilt = build_report(run_id, {**results['summary_evidence_id'], 'result_evidence_id': report['summary_evidence_id']},
                               {**results['tasks_evidence_id'], 'result_evidence_id': report['tasks_evidence_id']},
                               {**results['quality_evidence_id'], 'result_evidence_id': report['quality_evidence_id']})
        if any(report[key] != value for key, value in rebuilt.items()):
            raise APIError(409, '报告数值或内容与已保存证据不一致。')
        for task in report['focus_tasks']:
            evidence = self.tools.evidence_store.read(user, run_id, task['evidence_id'])
            if any(task[key] != evidence['record'].get(key) for key in task if key != 'evidence_id'):
                raise APIError(409, '报告重点任务与证据不一致。')

    def read(self, user, run_id):
        with self.data.lock:
            run = self.runs.saved(run_id)
            metadata = self.runs.authorize(user, run)
            saved = self.data.one('SELECT * FROM logistics_reports WHERE run_id=?', (run_id,))
            if not saved:
                raise APIError(404, '报告尚未生成。')
            report = json.loads(saved['result_json'])
            if (saved['workspace_id'] != WORKSPACE or saved['snapshot_id'] != run['snapshot_id']
                    or report['scope']['fingerprint'] != metadata['snapshot_fingerprint']):
                raise APIError(403, '报告不属于当前调查快照。')
            if content_hash(report) != saved['result_fingerprint']:
                raise APIError(409, '报告内容校验失败。')
            self.validate_references(user, run_id, report)
            return report

    def routes(self):
        router = APIRouter(prefix='/api/logistics/runs', tags=['物流确定性报告'])

        def user(request):
            if set(request.query_params) - {'workspaceId'}:
                raise APIError(400, '报告不接受范围或文件路径参数。')
            return self.runs.access.user(request)

        @router.post('/{run_id}/report')
        def create(request: Request, run_id: str):
            actor = user(request)
            if request.state.body:
                raise APIError(400, '报告由已保存证据生成，不接受客户端报告内容。')
            return self.create(actor, run_id)

        @router.get('/{run_id}/report')
        def read(request: Request, run_id: str):
            return self.read(user(request), run_id)

        @router.get('/{run_id}/report/markdown')
        def download(request: Request, run_id: str):
            with self.data.lock:
                report = self.read(user(request), run_id)
                return Response(render_markdown(report), media_type='text/markdown; charset=utf-8',
                    headers={'Content-Disposition': f'attachment; filename="{report["report_id"]}.md"',
                             'X-Content-Type-Options': 'nosniff'})

        @router.get('/{run_id}/report/view')
        def view(request: Request, run_id: str):
            with self.data.lock:
                report = self.read(user(request), run_id)
                return Response(render_html(report), media_type='text/html; charset=utf-8',
                                headers={'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'",
                                         'X-Content-Type-Options': 'nosniff'})

        return router
