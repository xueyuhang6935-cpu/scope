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
from .common import APIError, reply
from .data_store import DataStore
from .data_import import routes as data_routes
from .database_connection import routes as database_routes
from .supply import SupplyService
from .risk_monitor import RiskMonitor
from .supply_workflow import SupplyWorkflow
from .investigation_routes import routes as investigation_routes
from .business_access import BusinessAccess, current_scope

ROOT = Path(__file__).resolve().parents[1]


def create_app(env=None):
    env = {**dotenv_values(ROOT / ".env"), **os.environ, **(env or {})}
    auth = Auth(Path(env.get("AUTH_DB_PATH") or ROOT / "data/auth.sqlite"), env)
    data = DataStore(Path(env.get("DATA_DB_PATH") or ROOT / "data/supply.sqlite"))
    supply = SupplyService(auth, data, env)
    monitor = RiskMonitor(data, env, supply.user)
    supply.monitor = monitor
    workflow = SupplyWorkflow(auth, data, monitor, env)
    supply.workflow = workflow
    access = BusinessAccess(auth, data, monitor)
    workflow.permissions = monitor.permissions = supply.permissions = access

    @asynccontextmanager
    async def lifespan(app):
        monitor.start()
        workflow.recover()
        try:
            yield
        finally:
            monitor.stop()
            workflow.close()
            supply.close()
            auth.db.close()
            data.db.close()

    app = FastAPI(title="SupplyPilot API", version="1.0.0", lifespan=lifespan)
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
            if request.url.path.startswith(('/api/supply/', '/api/data/')):
                scope_token = current_scope.set(access.request_scope(request))
            response = await call_next(request)
        except APIError as exc:
            response = reply(exc.body, exc.status)
        except Exception:
            logging.getLogger(__name__).exception("API request failed")
            response = reply({"message": "服务暂不可用，请稍后重试。"}, 503)
        finally:
            if scope_token is not None:
                current_scope.reset(scope_token)
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    if env.get("FRONTEND_ORIGIN"):
        app.add_middleware(CORSMiddleware, allow_origins=[env["FRONTEND_ORIGIN"]], allow_credentials=True, allow_methods=["GET", "POST", "OPTIONS"], allow_headers=["Content-Type", "X-Workspace-Id"])
    app.include_router(auth_module.routes(auth))
    app.include_router(data_routes(auth, data, access))
    app.include_router(database_routes(auth, data, env, monitor, access))
    app.include_router(supply.routes())
    app.include_router(monitor.routes())
    app.include_router(investigation_routes(workflow))
    app.include_router(access.routes())

    @app.get("/api/health")
    def health():
        auth.query("SELECT 1")
        return {"status": "ok", "backend": "fastapi", "database": "sqlite", "workspace": "supply-chain"}

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
