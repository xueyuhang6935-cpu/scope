"""Real source-data oracles plus runtime lifecycle tests in isolated workspaces.

Run with SCOPE_DATABASE_INTEGRATION=1. Never mutate supply_demo source tables.
"""
import copy
import os
import tempfile
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path

from dotenv import dotenv_values
from fastapi.testclient import TestClient

from .auth import now, password_hash
from .common import APIError
from .database_connection import connection
from .main import create_app
from .shortage import calculate, load_snapshot, timestamp


class InputTests(unittest.TestCase):
    def test_timestamp_requires_timezone(self):
        for invalid in (None, "", "2026-10-05T08:00", "tomorrow", {}, 1):
            with self.subTest(invalid=invalid), self.assertRaises(APIError):
                timestamp(invalid)
        self.assertEqual(timestamp("2026-10-05T08:00:00+08:00"), timestamp("2026-10-05T00:00:00Z"))


@unittest.skipUnless(os.getenv("SCOPE_DATABASE_INTEGRATION") == "1", "显式启用本机 PostgreSQL 集成测试")
class ShortageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = dotenv_values(Path(__file__).resolve().parents[1] / ".env")
        with connection(cls.env) as db:
            cls.answers = db.execute("SELECT scenario_id,as_of,expected FROM supply_demo.expected_results WHERE test_name='focus_shortage' ORDER BY scenario_id").fetchall()
            cls.snapshots = {a["scenario_id"]: load_snapshot(db, a["scenario_id"], a["as_of"]) for a in cls.answers}

    def item(self, scenario="supplier_delay", material="P001", factory="A", snapshot=None, days=3):
        result = calculate(snapshot or self.snapshots[scenario], days, factory, material)
        self.assertEqual(len(result["results"]), 1, result["missing"])
        return result["results"][0]

    def test_twelve_independent_scenario_answers(self):
        for answer in self.answers:
            with self.subTest(scenario=answer["scenario_id"]):
                expected = answer["expected"]
                item = self.item(answer["scenario_id"], expected["material"])
                self.assertEqual(item["computable"], expected["computable"])
                self.assertEqual(Decimal(item["totalDemand"]), expected["first_three_days_demand"])
                if expected["computable"]:
                    self.assertEqual(Decimal(item["shortage"]), expected["first_three_days_max_shortage"])
                    self.assertEqual(timestamp(item["firstShortageAt"]) if item["firstShortageAt"] else None,
                                     timestamp(expected["first_shortage_at"]) if expected["first_shortage_at"] else None)
                else:
                    self.assertIsNone(item["shortage"])
                    self.assertTrue(item["missing"])

    def test_reservations_safety_stock_and_multi_material_vehicle_union(self):
        p4 = self.item(material="P004")
        p5 = self.item(material="P005")
        self.assertEqual(Decimal(p4["opening"]), 100000)  # included demand not subtracted twice
        self.assertEqual(Decimal(p5["opening"]), 99970)  # external reservation deducted once
        snap = copy.deepcopy(self.snapshots["supplier_delay"])
        for stock in snap["tables"]["inventory_snapshots"]:
            if stock["factory_id"] == "A" and stock["material_id"] == "P006":
                stock["quantity"] = Decimal(800)
        result = calculate(snap, 3, "A")
        self.assertEqual(Decimal(result["affectedVehicles"]), 400)  # P001 and P006 affect same vehicles
        self.assertTrue(self.item()["safetyWarnings"])
        donor = self.item("partial_transfer", factory="B", days=14)
        self.assertEqual(Decimal(donor["timeline"][-1]["balance"]), 250)

    def test_missing_bom_stock_revision_and_unit_are_not_zero(self):
        for table in ("bom_headers", "inventory_snapshots", "material_revisions"):
            with self.subTest(table=table):
                snap = copy.deepcopy(self.snapshots["supplier_delay"])
                snap["tables"][table] = []
                result = calculate(snap, 3, "A", "P001")
                self.assertTrue(result["missing"] or all(not item["computable"] for item in result["results"]))
        self.assertFalse(self.item("missing_unit", "P002")["computable"])
        bom = self.item("bom_revision", "P003")
        self.assertEqual(Decimal(bom["totalDemand"]), 200)
        self.assertTrue(all(d["bomVersion"] == 2 for d in bom["demands"]))

    def test_equal_time_supply_first_and_transit_not_double_counted(self):
        snap = copy.deepcopy(self.snapshots["normal"])
        latest = next(c for c in snap["tables"]["supplier_commitments"] if c["id"] == "PO-FOCUS-V2")
        latest["available_at"] = timestamp("2026-10-07T16:00:00+08:00")
        snap["tables"]["shipment_lines"].append({"id": "SHIP-IN-TRANSIT", "purchase_line_id": "PO-FOCUS", "execution_id": None,
            "material_id": "P001", "quantity": Decimal(400), "actual_shipped_at": timestamp("2026-10-05T06:00:00+08:00"),
            "eta_at": timestamp("2026-10-07T12:00:00+08:00"), "status": "shipped", "ingested_at": snap["asOf"]})
        item = self.item(snapshot=snap)
        self.assertEqual(Decimal(item["shortage"]), 0)
        self.assertEqual(sum(Decimal(s["quantity"]) for s in item["supplies"]), 400)
        self.assertEqual(Decimal(item["timeline"][-1]["balance"]), 0)
        # A late ETA includes receiving, inspection and line delivery time.
        snap["tables"]["shipment_lines"][-1]["eta_at"] = timestamp("2026-10-07T15:00:00+08:00")
        self.assertEqual(Decimal(self.item(snapshot=snap)["shortage"]), 400)
        snap["tables"]["shipment_lines"][-1]["quantity"] = Decimal(100)
        self.assertEqual(Decimal(self.item(snapshot=snap)["shortage"]), 100)

    def test_known_quality_release_uses_business_time(self):
        snap = copy.deepcopy(self.snapshots["quality_hold"])
        snap["asOf"] = timestamp("2026-10-06T10:00:00+08:00")
        hold = snap["tables"]["quality_holds"][0]
        hold["released_at"] = timestamp("2026-10-06T08:00:00+08:00")
        hold["release_evidence"] = "QA-test"
        item = self.item(snapshot=snap)
        self.assertTrue(item["computable"])
        self.assertEqual(Decimal(item["shortage"]), 100)  # yesterday's shortage not erased
        hold["released_at"] = timestamp("2026-10-05T15:00:00+08:00")
        self.assertEqual(Decimal(self.item(snapshot=snap)["shortage"]), 0)

    def test_future_receipt_not_visible_and_unreleased_not_available(self):
        with connection(self.env) as db:
            before = load_snapshot(db, "closed_loop", "2026-10-06T12:30:00+08:00")
            after = load_snapshot(db, "closed_loop", "2026-10-06T14:00:00+08:00")
            held = load_snapshot(db, "unreleased_receipt", "2026-10-07T12:00:00+08:00")
        self.assertEqual(Decimal(self.item(snapshot=before)["shortage"]), 400)
        self.assertEqual(Decimal(self.item(snapshot=after)["shortage"]), 0)
        self.assertEqual(Decimal(self.item(snapshot=after, factory="B")["timeline"][-1]["balance"]), 1700)
        self.assertEqual(Decimal(self.item(snapshot=held)["shortage"]), 400)
        self.assertFalse(any(e["table"] in ("expected_results", "risk_events", "plan_versions", "closure_records")
                             for e in calculate(after, 3)["evidence"]))


@unittest.skipUnless(os.getenv("SCOPE_DATABASE_INTEGRATION") == "1", "显式启用本机 PostgreSQL 集成测试")
class RiskLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = {"AUTH_DB_PATH": str(Path(self.temp.name) / "auth.sqlite"), "DATA_DB_PATH": str(Path(self.temp.name) / "supply.sqlite"),
                    "NODE_ENV": "test", "SUPPLY_PLANNER": "rules", "SUPPLY_MONITOR_AUTOSTART": "0"}
        self.app = create_app(self.env)
        self.client = TestClient(self.app).__enter__()
        for account in ("risk-one@test.invalid", "risk-two@test.invalid"):
            self.app.state.auth.query("INSERT INTO users(account,type,name,password_hash,created_at) VALUES(?,'email',?,?,?)", (account, account, password_hash("password123"), now()))
        self.login()

    def tearDown(self):
        workspaces = self.app.state.data.all("SELECT workspace_id FROM risk_monitor_configs")
        try:
            if self.app.state.monitor.schema_ready:
                with connection(self.app.state.monitor.env, readonly=False) as db:
                    for row in workspaces:
                        key = row["workspace_id"]
                        db.execute("DELETE FROM sc.risk_history WHERE event_id IN (SELECT id FROM sc.risk_events WHERE workspace_id=%s)", (key,))
                        db.execute("DELETE FROM sc.risk_events WHERE workspace_id=%s", (key,))
                        db.execute("DELETE FROM sc.risk_scans WHERE workspace_id=%s", (key,))
                        db.execute("DELETE FROM sc.commitment_changes WHERE workspace_id=%s", (key,))
        finally:
            self.client.__exit__(None, None, None)
            self.temp.cleanup()

    def login(self, account="risk-one@test.invalid"):
        self.assertEqual(self.client.post("/api/auth/login", json={"account": account, "password": "password123"}).status_code, 200)

    def post(self, path, body=None, expected=200):
        response = self.client.post(path, json=body or {})
        self.assertEqual(response.status_code, expected, response.text)
        return response.json()

    def select(self, scenario="supplier_delay"):
        self.post("/api/data/database", {"scenarioId": scenario})
        self.post("/api/supply/monitor", {"days": 3, "enabled": True})

    def events(self):
        response = self.client.get("/api/supply/risks?factory=A")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["events"]

    def inject(self, arrival, key=None):
        return self.post("/api/supply/monitor/commitment", {"requestId": key or str(uuid.uuid4()), "purchaseLineId": "PO-FOCUS", "availableAt": arrival})

    def test_scan_merge_change_coverage_recurrence_and_account_isolation(self):
        self.select()
        first = self.post("/api/supply/monitor/scan")
        again = self.post("/api/supply/monitor/scan")
        self.assertEqual(first["scanId"], again["scanId"])
        event = self.events()[0]
        self.assertEqual(len(self.events()), 1)
        self.assertEqual(Decimal(event["result"]["shortage"]), 400)
        self.assertEqual(self.client.get(f"/api/supply/risks/{event['id']}?factory=B").status_code, 404)
        changed = self.post(f"/api/supply/risks/{event['id']}", {"version": event["version"], "status": "acknowledged", "owner": "测试负责人"})
        self.assertEqual(changed["owner"], "测试负责人")
        self.post(f"/api/supply/risks/{event['id']}", {"version": event["version"], "owner": "旧版本"}, 409)
        self.inject("2026-10-07T18:00:00+08:00")
        self.assertEqual(self.events()[0]["id"], event["id"])
        self.assertEqual(self.events()[0]["status"], "acknowledged")
        self.assertEqual(self.events()[0]["owner"], "测试负责人")
        key = str(uuid.uuid4())
        restored = self.inject("2026-10-06T08:00:00+08:00", key)
        repeated = self.inject("2026-10-06T08:00:00+08:00", key)
        self.assertEqual(restored["scanId"], repeated["scanId"])
        self.assertEqual(self.events()[0]["status"], "covered")
        self.post("/api/supply/monitor/commitment", {"requestId": key, "purchaseLineId": "PO-FOCUS", "availableAt": "2026-10-08T08:00:00+08:00"}, 409)
        self.inject("2026-10-08T08:00:00+08:00")
        events = self.events()
        self.assertEqual(len(events), 2)
        recurrence = next(e for e in events if e["status"] == "open")
        self.assertEqual(recurrence["recurrence_of"], event["id"])
        self.login("risk-two@test.invalid")
        self.assertFalse(self.client.get("/api/supply/monitor").json()["configured"])
        self.select()
        self.assertEqual(self.events(), [])
        self.assertEqual(self.client.get(f"/api/supply/risks/{event['id']}").status_code, 404)

    def test_scheduler_restart_concurrent_scan_and_incomplete_data(self):
        self.select()
        self.app.state.monitor.tick()
        event_id = self.events()[0]["id"]
        # Concurrent manual deliveries may return busy, but cannot duplicate events.
        def run(_):
            try:
                return self.app.state.monitor.scan(1)["scanId"]
            except APIError as exc:
                self.assertIn(exc.status, (409, 503))
                return None
        with ThreadPoolExecutor(max_workers=3) as executor:
            list(executor.map(run, range(3)))
        self.assertEqual(len(self.events()), 1)
        self.client.__exit__(None, None, None)
        self.app = create_app(self.env)
        self.client = TestClient(self.app).__enter__()
        self.login()
        self.assertEqual(self.events()[0]["id"], event_id)
        # Before source ingestion, missing plans cannot clear an existing event.
        self.post("/api/supply/monitor", {"asOf": "2026-10-05T00:00:00+08:00"})
        self.post("/api/supply/monitor/scan")
        self.assertEqual(self.events()[0]["status"], "data_required")
        self.assertIsNone(self.events()[0]["result"]["shortage"])

    def test_assistant_uses_database_and_preserves_evidence(self):
        self.select()
        thread = self.post("/api/supply/threads", {"factory": "A", "requestId": str(uuid.uuid4())})
        task = self.post(f"/api/supply/threads/{thread['id']}/messages", {"text": "检查 P001 未来三天缺料", "requestId": str(uuid.uuid4()), "scope": {}})
        self.assertEqual(task["source"]["scenarioId"], "supplier_delay")
        self.assertEqual(task['status'], 'queued')
        for _ in range(200):
            done = self.client.get(f"/api/supply/tasks/{task['id']}").json()
            if done["status"] not in ("queued", "running"):
                break
            time.sleep(.02)
        self.assertEqual(done["status"], "completed", done)
        result = done["result"]["calculation"]
        self.assertEqual(Decimal(result["results"][0]["shortage"]), 400)
        self.inject("2026-10-06T08:00:00+08:00")
        self.assertEqual(self.client.get(f"/api/supply/tasks/{task['id']}").json()["result"]["calculation"], result)

    def test_input_auth_and_no_source_fallback(self):
        self.post("/api/supply/shortage", {}, 409)
        self.select()
        for body in ({"days": 30}, {"days": True}, {"factory": "C"}, {"material": "unknown"}, {"asOf": "2026-10-05T08:00"}):
            self.post("/api/supply/shortage", body, 400)
        response = self.client.post("/api/supply/monitor/scan", json={}, headers={"origin": "https://untrusted.invalid"})
        self.assertEqual(response.status_code, 403)
        self.client.cookies.clear()
        self.post("/api/supply/monitor/scan", {}, 401)
        self.assertEqual(self.client.get("/api/supply/risks").status_code, 401)


if __name__ == "__main__":
    unittest.main()
