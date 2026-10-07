"""Scenario-backed calculations and persistent, idempotent risk monitoring."""
import json
import logging
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Request
from psycopg.types.json import Jsonb

from .common import APIError, iso
from .database_connection import connection
from .shortage import calculate, load_snapshot, timestamp, fingerprint, plain
from .business_access import current_scope


class RiskMonitor:
    def __init__(self, data, env, authorize):
        self.data, self.env, self.authorize = data, env, authorize
        self.wake, self.stopping = threading.Event(), threading.Event()
        self.thread = None
        self.schema_ready = False
        self.schema_lock = threading.Lock()
        # ponytail: one scheduler thread for the bounded demo; PostgreSQL advisory
        # locks/unique constraints also protect scans from concurrent API callers.
        with data.lock:
            data.db.executescript("""
                CREATE TABLE IF NOT EXISTS database_connections (
                    user_id INTEGER PRIMARY KEY, scenario_id TEXT NOT NULL, saved_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS risk_monitor_configs (
                    user_id INTEGER NOT NULL, scenario_id TEXT NOT NULL, workspace_id TEXT NOT NULL UNIQUE,
                    as_of TEXT NOT NULL, horizon_days INTEGER NOT NULL DEFAULT 7,
                    enabled INTEGER NOT NULL DEFAULT 1, interval_seconds INTEGER NOT NULL DEFAULT 60,
                    last_attempt TEXT, error TEXT, PRIMARY KEY(user_id,scenario_id));
            """)

    def ensure_schema(self):
        with self.schema_lock:
            if self.schema_ready:
                return
            with connection(self.env, readonly=False) as db:
                db.execute("SELECT pg_advisory_xact_lock(hashtext('scope-risk-schema-v1'))")
                db.execute(Path(__file__).with_name("risk_schema.sql").read_text(encoding="utf-8"))
            self.schema_ready = True

    def config(self, user_id):
        scope = current_scope.get()
        if scope and scope['user']['id']==user_id:
            row = self.data.one('SELECT * FROM risk_monitor_configs WHERE workspace_id=?',(scope['workspace'],))
            if row:
                return row
            if scope.get('explicit'):
                raise APIError(409,'当前工作空间未接入模拟场景，请切换场景空间。')
        saved = self.data.one("SELECT * FROM database_connections WHERE user_id=?", (user_id,))
        if not saved:
            raise APIError(409, "请先在数据接入中选择场景并保存数据库配置。")
        row = self.data.one("SELECT * FROM risk_monitor_configs WHERE user_id=? AND scenario_id=?", (user_id, saved["scenario_id"]))
        if not row:
            with connection(self.env) as db:
                scenario = db.execute("SELECT initial_as_of FROM supply_demo.scenarios WHERE id=%s", (saved["scenario_id"],)).fetchone()
            if not scenario:
                raise APIError(409, "已保存的场景不存在，请重新配置。")
            with self.data.lock:
                self.data.db.execute("INSERT OR IGNORE INTO risk_monitor_configs(user_id,scenario_id,workspace_id,as_of) VALUES(?,?,?,?)",
                                     (user_id, saved["scenario_id"], uuid.uuid4().hex, iso(scenario["initial_as_of"])))
            row = self.data.one("SELECT * FROM risk_monitor_configs WHERE user_id=? AND scenario_id=?", (user_id, saved["scenario_id"]))
        return row

    def snapshot(self, db, config, as_of=None):
        snapshot = load_snapshot(db, config["scenario_id"], as_of or config["as_of"])
        changes = db.execute("SELECT * FROM sc.commitment_changes WHERE workspace_id=%s AND known_at<=%s ORDER BY known_at,created_at,id",
                             (config["workspace_id"], snapshot["asOf"])).fetchall()
        commitments = snapshot["tables"]["supplier_commitments"]
        for change in changes:
            candidates = [r for r in commitments if r["purchase_line_id"] == change["purchase_line_id"] and r["installment"] == change["installment"]]
            if not candidates:
                continue
            previous = max(candidates, key=lambda r: (r["version"], r["known_at"]))
            if change["known_at"] < previous["known_at"]:
                continue
            commitments.append({**previous, "id": change["id"], "version": previous["version"] + 1,
                                "available_at": change["available_at"], "known_at": change["known_at"],
                                "changed_at": change["known_at"], "ingested_at": change["known_at"],
                                "source_system": "SIMULATED_EVENT_INJECTION", "source_record_id": change["id"],
                                "feedback": change["note"], "evidence_ref": change["id"]})
        return snapshot

    def compute(self, user_id, *, days=None, factory="all", material="", as_of=None, scenario_id=None):
        config = self.config(user_id)
        if scenario_id and config["scenario_id"] != scenario_id:
            raise APIError(409, "数据库场景已改变，请重新确认范围。")
        self.ensure_schema()
        with connection(self.env) as db:
            snapshot = self.snapshot(db, config, as_of)
            return calculate(snapshot, config["horizon_days"] if days is None else days, factory, material)

    def scan(self, user_id, trigger="manual"):
        config = self.config(user_id)
        self.ensure_schema()
        try:
            with connection(self.env, readonly=False) as db:
                # Transaction lock spans snapshot, computation and event updates.
                locked = db.execute("SELECT pg_try_advisory_xact_lock(hashtext(%s)) AS locked", (config["workspace_id"],)).fetchone()["locked"]
                if not locked:
                    raise APIError(409, "当前场景正在扫描，请稍后刷新。")
                snapshot = self.snapshot(db, config)
                result = calculate(snapshot, config["horizon_days"])
                latest = db.execute("SELECT * FROM sc.risk_scans WHERE workspace_id=%s ORDER BY created_at DESC,id DESC LIMIT 1",
                                    (config["workspace_id"],)).fetchone()
                if latest and fingerprint(latest["result"]) == fingerprint(result):
                    scan_id = latest["id"]
                else:
                    scan_id = "SCAN-" + uuid.uuid4().hex
                    db.execute("INSERT INTO sc.risk_scans(id,workspace_id,scenario_id,as_of,trigger,snapshot_hash,result) VALUES(%s,%s,%s,%s,%s,%s,%s)",
                               (scan_id, config["workspace_id"], config["scenario_id"], result["asOf"], trigger, result["snapshotHash"], Jsonb(result)))
                    self.reconcile(db, config, scan_id, result, trigger)
            with self.data.lock:
                self.data.db.execute("UPDATE risk_monitor_configs SET last_attempt=?,error=NULL WHERE workspace_id=?",
                                     (iso(), config["workspace_id"]))
            return {"scanId": scan_id, **result}
        except Exception as exc:
            with self.data.lock:
                self.data.db.execute("UPDATE risk_monitor_configs SET last_attempt=?,error=? WHERE workspace_id=?",
                                     (iso(), exc.body["message"] if isinstance(exc, APIError) else "风险扫描失败，请重试。", config["workspace_id"]))
            raise

    def reconcile(self, db, config, scan_id, result, trigger, selected_only=False):
        existing = db.execute("SELECT * FROM sc.risk_events WHERE workspace_id=%s ORDER BY created_at DESC,id DESC",
                              (config["workspace_id"],)).fetchall()
        active = {(e["factory"], e["material"], e["revision"]): e for e in existing if e["status"] != "covered"}
        seen = set()
        for item in result["results"]:
            key = (item["factory"], item["material"], item["revision"])
            seen.add(key)
            old = active.get(key)
            status = "data_required" if not item["computable"] else "open" if float(item["shortage"]) > 0 else "covered"
            if not old and status == "covered":
                continue
            if old:
                if status == "open" and old["status"] == "acknowledged":
                    status = "acknowledged"
                if old["result"] == item and old["status"] == status:
                    continue
                version = old["version"] + 1
                db.execute("UPDATE sc.risk_events SET status=%s,version=%s,scan_id=%s,result=%s,updated_at=now() WHERE id=%s",
                           (status, version, scan_id, Jsonb(item), old["id"]))
                self.history(db, old["id"], version, trigger, {"status": status, "scanId": scan_id, "result": item})
            else:
                previous = next((e for e in existing if (e["factory"], e["material"], e["revision"]) == key), None)
                event_id = "RISK-" + uuid.uuid4().hex
                db.execute("""INSERT INTO sc.risk_events(id,workspace_id,scenario_id,factory,material,revision,status,owner,scan_id,result,recurrence_of)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                           (event_id, config["workspace_id"], config["scenario_id"], *key, status, item["owner"], scan_id, Jsonb(item), previous["id"] if previous else None))
                self.history(db, event_id, 1, "detected", {"status": status, "scanId": scan_id, "result": item})
        for key, old in active.items():
            if selected_only:
                continue
            if key in seen:
                continue
            # A removed plan or a narrower window is not evidence of fulfillment.
            item = {**old["result"], "computable": False, "shortage": None,
                    "missing": result["missing"] or ["当前计算窗口没有该事件的有效需求，需人工核对计划或窗口变化。"]}
            if item != old["result"] or old["status"] != "data_required":
                version = old["version"] + 1
                db.execute("UPDATE sc.risk_events SET status='data_required',version=%s,scan_id=%s,result=%s,updated_at=now() WHERE id=%s",
                           (version, scan_id, Jsonb(item), old["id"]))
                self.history(db, old["id"], version, trigger, {"status": "data_required", "scanId": scan_id, "result": item})

    @staticmethod
    def history(db, event_id, version, kind, details):
        db.execute("INSERT INTO sc.risk_history(event_id,version,kind,details) VALUES(%s,%s,%s,%s)", (event_id, version, kind, Jsonb(details)))

    def start(self):
        if self.env.get("SUPPLY_MONITOR_AUTOSTART", "0" if self.env.get("NODE_ENV") == "test" else "1") == "0":
            return
        self.thread = threading.Thread(target=self.loop, name="risk-monitor", daemon=True)
        self.thread.start()

    def stop(self):
        self.stopping.set()
        self.wake.set()
        if self.thread:
            self.thread.join()

    def tick(self):
        for selected in self.data.all("SELECT user_id FROM database_connections"):
            if self.stopping.is_set():
                return
            try:
                config = self.config(selected["user_id"])
                due = not config["last_attempt"] or datetime.now(timezone.utc) - timestamp(config["last_attempt"]) >= timedelta(seconds=config["interval_seconds"])
                if config["enabled"] and due:
                    self.scan(selected["user_id"], "scheduled")
            except Exception:
                logging.getLogger(__name__).warning("Risk monitor scan unavailable for an account; will retry.")

    def loop(self):
        while not self.stopping.is_set():
            self.wake.clear()
            self.tick()
            self.wake.wait(5)

    def routes(self):
        router = APIRouter(prefix="/api/supply", tags=["缺料计算与风险监测"])

        @router.get("/monitor")
        def state(request: Request):
            user = self.authorize(request)
            workspace = self.permissions.workspace(user)
            if not self.data.one('SELECT 1 FROM risk_monitor_configs WHERE workspace_id=?',(workspace,)):
                return {"configured": False}
            config = self.config(user["id"])
            return {"configured": True, **{k: v for k, v in config.items() if k not in ("user_id", "workspace_id")}}

        @router.post("/monitor")
        def configure(request: Request):
            user = self.authorize(request)
            config = self.config(user["id"])
            self.permissions.require(user,config['workspace_id'],'monitor',['A','B'])
            body = request.state.body
            as_of = timestamp(body.get("asOf", config["as_of"]))
            days, interval, enabled = body.get("days", config["horizon_days"]), body.get("intervalSeconds", config["interval_seconds"]), body.get("enabled", bool(config["enabled"]))
            if type(days) is not int or not 1 <= days <= 14 or type(interval) is not int or not 10 <= interval <= 3600 or type(enabled) is not bool:
                raise APIError(400, "窗口须为 1–14 天，扫描间隔须为 10–3600 秒，启用值须为布尔值。")
            with connection(self.env) as db:
                load_snapshot(db, config["scenario_id"], as_of)
            with self.data.lock:
                self.data.db.execute("UPDATE risk_monitor_configs SET as_of=?,horizon_days=?,enabled=?,interval_seconds=?,last_attempt=NULL,error=NULL WHERE workspace_id=?",
                                     (iso(as_of), days, int(enabled), interval, config["workspace_id"]))
            self.wake.set()
            return state(request)

        @router.post("/shortage")
        def shortage(request: Request):
            user = self.authorize(request)
            b = request.state.body
            if b.get('factory','all') not in ('all','A','B'):
                raise APIError(400,'工厂参数无效。')
            self.permissions.require(user,self.permissions.workspace(user),'view',['A','B'] if b.get('factory','all')=='all' else [b['factory']])
            material = b.get("material", "")
            if not isinstance(material, str) or len(material) > 120:
                raise APIError(400, "物料编码无效。")
            return self.compute(user["id"], days=b.get("days"), factory=b.get("factory", "all"), material=material, as_of=b.get("asOf"))

        @router.post("/monitor/scan")
        def scan(request: Request):
            user=self.authorize(request)
            self.permissions.require(user,self.permissions.workspace(user),'scan',['A','B'])
            return self.scan(user['id'])

        @router.get("/risks")
        def events(request: Request, factory: str = "all"):
            user = self.authorize(request)
            if factory not in ("all", "A", "B"):
                raise APIError(400, "工厂参数无效。")
            config = self.config(user["id"])
            self.ensure_schema()
            with connection(self.env) as db:
                rows = db.execute("SELECT * FROM sc.risk_events WHERE workspace_id=%s AND (%s='all' OR factory=%s) ORDER BY updated_at DESC,id",
                                  (config["workspace_id"], factory, factory)).fetchall()
                latest = db.execute("SELECT id,created_at,as_of,trigger,result FROM sc.risk_scans WHERE workspace_id=%s ORDER BY created_at DESC,id DESC LIMIT 1", (config["workspace_id"],)).fetchone()
            allowed = [f for f in ('A','B') if self.permissions.can(user,config['workspace_id'],'view',[f])]
            rows = [r for r in rows if r['factory'] in allowed]
            if latest and set(allowed)!={'A','B'}:
                # Full scan evidence/aggregates can contain the other factory.
                latest = None
            return plain({"events": rows, "latestScan": latest})

        def owned(db, user_id, event_id, factory="all"):
            config = self.config(user_id)
            row = db.execute("SELECT * FROM sc.risk_events WHERE id=%s AND workspace_id=%s AND (%s='all' OR factory=%s)",
                             (event_id, config["workspace_id"], factory, factory)).fetchone()
            if not row:
                raise APIError(404, "事件不存在或不在当前场景和工厂范围。")
            user=self.permissions.auth.query('SELECT id,account FROM users WHERE id=?',(user_id,))
            self.permissions.require(user,config['workspace_id'],'view',[row['factory']])
            return row

        @router.get("/risks/{event_id}")
        def detail(request: Request, event_id: str, factory: str = "all"):
            user = self.authorize(request)
            self.ensure_schema()
            with connection(self.env) as db:
                row = owned(db, user["id"], event_id, factory)
                history = db.execute("SELECT * FROM sc.risk_history WHERE event_id=%s ORDER BY version", (event_id,)).fetchall()
                scan = db.execute("SELECT result FROM sc.risk_scans WHERE id=%s", (row["scan_id"],)).fetchone()["result"]
            ids = set(row["result"]["evidenceIds"])
            return plain({**row, "history": history, "evidence": [e for e in scan["evidence"] if e["id"] in ids],
                          "asOf": scan["asOf"], "windowStart": scan["windowStart"], "windowEnd": scan["windowEnd"], "snapshotHash": scan["snapshotHash"]})

        @router.post("/risks/{event_id}")
        def update(request: Request, event_id: str):
            user = self.authorize(request)
            self.ensure_schema()
            b = request.state.body
            with connection(self.env, readonly=False) as db:
                row = owned(db, user["id"], event_id)
                if not self.permissions.can(user,row['workspace_id'],'risk.update',[row['factory']]):
                    self.permissions.require(user,row['workspace_id'],'event.update',[row['factory']],event=row)
                    if b.get('owner',row['owner']) != row['owner']:
                        raise APIError(403,'采购／物流人员不能转派事件。')
                if b.get("version") != row["version"]:
                    raise APIError(409, "事件已更新，请刷新后重试。")
                owner = b.get("owner", row["owner"])
                status = b.get("status", row["status"])
                if not isinstance(owner, str) or not 1 <= len(owner.strip()) <= 100:
                    raise APIError(400, "负责人须为 1 至 100 字。")
                if status != row["status"] and not (row["status"] in ("open", "acknowledged") and status in ("open", "acknowledged")):
                    raise APIError(400, "只能确认/恢复处理风险；覆盖与数据不足状态由扫描计算，不能手动关闭。")
                changed = db.execute("UPDATE sc.risk_events SET owner=%s,status=%s,version=version+1,updated_at=now() WHERE id=%s AND version=%s RETURNING version",
                                     (owner.strip(), status, event_id, row["version"])).fetchone()
                if not changed:
                    raise APIError(409, "事件已更新，请刷新后重试。")
                self.history(db, event_id, changed["version"], "manual_update", {"owner": owner.strip(), "status": status})
            return detail(request, event_id)

        @router.post("/monitor/commitment")
        def inject(request: Request):
            user = self.authorize(request)
            config = self.config(user["id"])
            self.ensure_schema()
            b = request.state.body
            key, purchase_id, installment, note = b.get("requestId"), b.get("purchaseLineId"), b.get("installment", "01"), b.get("note", "模拟交期变更")
            if not all(isinstance(v, str) and 1 <= len(v) <= 200 for v in (key, purchase_id, installment, note)):
                raise APIError(400, "请提供有效的请求编号、采购行、批次和变更说明。")
            available = timestamp(b.get("availableAt"))
            known = timestamp(b.get("knownAt", config["as_of"]))
            with connection(self.env, readonly=False) as db:
                db.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (config["workspace_id"],))
                snapshot = load_snapshot(db, config["scenario_id"], known)
                purchase=next((p for p in snapshot['tables']['purchase_order_lines'] if p['id']==purchase_id),None)
                if not purchase:
                    raise APIError(400,'采购行不存在。')
                if not self.permissions.can(user,config['workspace_id'],'monitor',['A','B']):
                    event=db.execute('SELECT * FROM sc.risk_events WHERE workspace_id=%s AND factory=%s AND material=%s AND owner=%s ORDER BY updated_at DESC LIMIT 1',(config['workspace_id'],purchase['factory_id'],purchase['material_id'],user['account'])).fetchone()
                    self.permissions.require(user,config['workspace_id'],'event.update',[purchase['factory_id']],event=event)
                if known > timestamp(config["as_of"]) or not known <= available <= snapshot["scenario"]["window_end"]:
                    raise APIError(400, "变更获知时间不得晚于回放时点，到线承诺须介于获知时间与场景结束之间。")
                if not any(c["purchase_line_id"] == purchase_id and c["installment"] == installment for c in snapshot["tables"]["supplier_commitments"]):
                    raise APIError(400, "当时没有该采购行的承诺，不能注入变更。")
                old = db.execute("SELECT * FROM sc.commitment_changes WHERE workspace_id=%s AND request_id=%s", (config["workspace_id"], key)).fetchone()
                values = (purchase_id, installment, available, known, note)
                if old:
                    if tuple(old[k] for k in ("purchase_line_id", "installment", "available_at", "known_at", "note")) != values:
                        raise APIError(409, "请求编号已用于不同的交期变更。")
                else:
                    db.execute("INSERT INTO sc.commitment_changes(id,workspace_id,scenario_id,request_id,purchase_line_id,installment,available_at,known_at,note) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                               ("CHANGE-" + uuid.uuid4().hex, config["workspace_id"], config["scenario_id"], key, *values))
            # A committed injection is retryable by the same request ID if scanning fails.
            result=self.scan(user["id"], "commitment_changed")
            # A scoped logistics update must not return the full two-factory scan.
            return result if self.permissions.can(user,config['workspace_id'],'view',['A','B']) else {'scanId':result['scanId'],'updated':True}

        return router
