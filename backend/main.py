import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import dotenv_values
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from . import auth as auth_module
from .auth import Auth
from .common import APIError, reply, request_id
from .data_store import DataStore
from .data_import import routes as data_routes
from .database_connection import routes as database_routes
from .supply import SupplyService
from .risk_monitor import RiskMonitor
from .supply_workflow import SupplyWorkflow
from .investigation_routes import routes as investigation_routes
from .business_access import BusinessAccess, current_scope
from .logistics_access import LogisticsAccess
from .logistics_data import routes as logistics_data_routes
from .logistics_snapshots import SnapshotService
from .logistics_metrics import LogisticsMetrics
from .logistics_runs import RunService
from .logistics_tools import LogisticsToolService
from .logistics_report import LogisticsReportService
from .logistics_events import RunEvents
from .logistics_model import LogisticsModelService
from .logistics_workflow import LogisticsWorkflow
from .logistics_collaboration import LogisticsCollaboration
from .logistics_chat import ChatService
from .logistics_knowledge import LogisticsKnowledge
import uuid

ROOT = Path(__file__).resolve().parents[1]


def create_app(env=None, *, _legacy_test=False):
    env = {**dotenv_values(ROOT / ".env"), **os.environ, **(env or {})}
    if _legacy_test and env.get('NODE_ENV') != 'test':
        raise RuntimeError('旧供应链组装只允许隔离测试使用。')
    auth = Auth(Path(env.get("AUTH_DB_PATH") or ROOT / "data/auth.sqlite"), env)
    data = DataStore(Path(env.get("DATA_DB_PATH") or ROOT / "data/supply.sqlite"))
    supply = monitor = workflow = None
    if _legacy_test:
        supply = SupplyService(auth, data, env)
        monitor = RiskMonitor(data, env, supply.user)
        supply.monitor = monitor
        workflow = SupplyWorkflow(auth, data, monitor, env)
        supply.workflow = workflow
        access = BusinessAccess(auth, data, monitor)
        workflow.permissions = monitor.permissions = supply.permissions = access
    else:
        access = LogisticsAccess(auth, data, env)

    @asynccontextmanager
    async def lifespan(app):
        if _legacy_test:
            monitor.start()
            workflow.recover()
        else:
            app.state.logistics_tools.evidence_store.interrupt_open()
            app.state.logistics_runs.start()
            app.state.logistics_chat.start()
        try:
            yield
        finally:
            if _legacy_test:
                monitor.stop()
                workflow.close()
                supply.close()
            else:
                app.state.logistics_chat.close()
                app.state.logistics_runs.close()
                app.state.logistics_workflow.close()
            auth.db.close()
            data.db.close()

    app = FastAPI(title="Scope 物流运营 API", version="1.0.0", lifespan=lifespan)
    app.state.auth = auth
    app.state.data = data
    app.state.supply = supply
    app.state.monitor = monitor
    app.state.workflow = workflow
    app.state.access = access

    @app.exception_handler(APIError)
    async def api_error(request, exc):
        return reply(exc.body, exc.status)

    @app.middleware("http")
    async def api_boundary(request: Request, call_next):
        request.state.request_id = uuid.uuid4().hex
        request_token = request_id.set(request.state.request_id)
        scope_token = None
        try:
            request.state.body = {}
            if request.method in ("POST", "PUT", "PATCH") and request.url.path != "/api/data/imports/preview":
                body = bytearray()
                async for chunk in request.stream():
                    body.extend(chunk)
                    if len(body) > 16384:
                        return reply({"message": "请求内容过大。"}, 413)
                if body:
                    try:
                        value = json.loads(body)
                        if not isinstance(value, dict):
                            raise ValueError()
                        request.state.body = value
                    except (ValueError, UnicodeError):
                        return reply({"message": "请求 JSON 格式错误。"}, 400)
            if _legacy_test and request.url.path.startswith(('/api/supply/', '/api/data/')):
                scope_token = current_scope.set(access.request_scope(request))
            response = await call_next(request)
        except APIError as exc:
            response = reply(exc.body, exc.status)
        except Exception:
            logging.getLogger(__name__).exception("API request failed")
            response = reply({"message": "服务暂不可用，请稍后重试。"}, 503)
        finally:
            request_id.reset(request_token)
            if scope_token is not None:
                current_scope.reset(scope_token)
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
            response.headers['X-Request-ID'] = request.state.request_id
            parts = request.url.path.split('/')
            if len(parts) > 4 and parts[3] == 'runs':
                response.headers['X-Run-ID'] = parts[4]
        return response

    if env.get("FRONTEND_ORIGIN"):
        app.add_middleware(CORSMiddleware, allow_origins=[env["FRONTEND_ORIGIN"]], allow_credentials=True, allow_methods=["GET", "POST", "OPTIONS"], allow_headers=["Content-Type", "X-Workspace-Id"])
    app.include_router(auth_module.routes(auth))
    if _legacy_test:
        app.include_router(data_routes(auth, data, access))
        app.include_router(database_routes(auth, data, env, monitor, access))
        app.include_router(supply.routes())
        app.include_router(monitor.routes())
        app.include_router(investigation_routes(workflow))
    else:
        app.include_router(logistics_data_routes(access, data, env))
        app.state.snapshots = SnapshotService(access, data, env)
        app.include_router(app.state.snapshots.routes())
        app.state.logistics_metrics = LogisticsMetrics(app.state.snapshots)
        app.include_router(app.state.logistics_metrics.routes())
        app.state.logistics_runs = RunService(app.state.snapshots)
        app.include_router(app.state.logistics_runs.routes())
        app.state.logistics_tools = LogisticsToolService(app.state.logistics_runs)
        app.state.logistics_model = LogisticsModelService(app.state.logistics_tools, env)
        app.include_router(app.state.logistics_tools.routes())
        app.include_router(app.state.logistics_tools.evidence_store.routes())
        app.state.logistics_reports = LogisticsReportService(app.state.logistics_tools)
        app.state.logistics_workflow = LogisticsWorkflow(app.state.logistics_model, app.state.logistics_reports)
        app.state.logistics_runs.workflow = app.state.logistics_workflow
        app.include_router(app.state.logistics_workflow.routes())
        app.state.logistics_runs.report_builder = lambda row: app.state.logistics_reports.create(
            {'id': row['creator_id'], 'account': row['creator_account']}, row['run_id'], claim_token=row['claim_token'])
        app.include_router(app.state.logistics_reports.routes())
        app.state.logistics_events = RunEvents(app.state.logistics_runs)
        app.include_router(app.state.logistics_events.routes())
        app.state.logistics_collaboration = LogisticsCollaboration(app.state.logistics_reports)
        app.include_router(app.state.logistics_collaboration.routes())
        app.include_router(app.state.logistics_collaboration.approval_routes())
        app.state.logistics_chat = ChatService(app.state.logistics_collaboration, env)
        app.include_router(app.state.logistics_chat.routes())
        app.state.logistics_knowledge = LogisticsKnowledge(app.state.logistics_chat)
        app.include_router(app.state.logistics_knowledge.routes())
    app.include_router(access.routes())

    @app.get("/api/health")
    def health():
        auth.query("SELECT 1")
        return {"status": "ok", "backend": "fastapi", "database": "sqlite", "workspace": "supply-chain" if _legacy_test else "shanghai-logistics"}

    @app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"], include_in_schema=False)
    def missing_api(path: str):
        raise APIError(404, "接口不存在。")

    @app.get("/{path:path}", include_in_schema=False)
    def frontend(path: str):
        dist = (ROOT / "dist").resolve()
        target = (dist / path).resolve()
        if not target.is_relative_to(dist) or any(p.startswith(".") for p in Path(path).parts):
            raise APIError(404, "文件不存在。")
        if target.is_file():
            return FileResponse(target)
        if (dist / "index.html").is_file():
            return FileResponse(dist / "index.html")
        raise APIError(404, "前端尚未构建，请运行 npm run build 或使用开发服务器。")

    return app


def create_legacy_test_app(env):
    """保留可复用核心的旧集成测试；正式 CLI 不挂载旧接口。"""
    return create_app(env, _legacy_test=True)
