"""真实文件导入、隔离和换算复检的接口契约。"""
import io
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from openpyxl import Workbook

from .auth import now, password_hash
from .main import create_legacy_test_app as create_app


class DataImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.client = TestClient(create_app({"AUTH_DB_PATH": str(root / "auth.sqlite"), "DATA_DB_PATH": str(root / "supply.sqlite"), "NODE_ENV": "test"}))
        self.client.__enter__()
        auth = self.client.app.state.auth
        auth.query("INSERT INTO users(account,type,name,password_hash,created_at) VALUES(?,'email',?,?,?)",
                   ("data@test.invalid", "数据负责人", password_hash("password123"), now()))
        self.client.post("/api/auth/login", json={"account": "data@test.invalid", "password": "password123"})

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temp.cleanup()

    def preview(self, content, name="inventory.csv"):
        response = self.client.post("/api/data/imports/preview", data={"domain": "inventory", "factory": "B", "owner": "张工"},
                                    files={"file": (name, content)})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["draftId"]

    def test_import_warning_isolation_and_repair(self):
        draft = self.preview("material,quantity,unit\nP012,20,box\nP013,10,件".encode())
        mapping = {"material": "material", "quantity": "quantity", "unit": "unit"}
        checked = self.client.post(f"/api/data/imports/{draft}/check", json={"mapping": mapping})
        self.assertEqual(checked.status_code, 200)
        self.assertEqual(checked.json()["isolated"], 1)
        self.assertIsNone(checked.json()["rows"][0]["normalizedQuantity"])
        body = {"strategy": "insert", "businessKey": "material", "idempotencyKey": "test-import-unique-001", "isolateWarnings": True}
        self.assertEqual(self.client.post(f"/api/data/imports/{draft}/commit", json={**body, "isolateWarnings": False}).status_code, 409)
        committed = self.client.post(f"/api/data/imports/{draft}/commit", json=body)
        self.assertEqual(committed.status_code, 200, committed.text)
        batch = committed.json()["id"]
        self.assertEqual(self.client.get(f"/api/data/imports/{batch}/file").content, "material,quantity,unit\nP012,20,box\nP013,10,件".encode())
        self.assertEqual(self.client.post(f"/api/data/imports/{draft}/commit", json=body).json()["id"], batch)
        issues = self.client.get("/api/data/governance/issues?factory=B").json()
        self.assertEqual(len(issues), 1)
        issue_id = issues[0]["id"]
        self.assertEqual(issues[0]["material"], "P012")
        repair_body = {"factor": 24, "evidence": "规格单 SPEC-P012", "confirmedBy": "张工", "validFrom": "2026-10-02", "confirmed": True}
        self.assertEqual(self.client.post(f"/api/data/governance/issues/{issue_id}/repair", json={"factor": 24}).status_code, 400)
        self.assertEqual(self.client.post(f"/api/data/governance/issues/{issue_id}/repair", json=repair_body).status_code, 409)
        self.assertEqual(self.client.post(f"/api/data/governance/issues/{issue_id}/assign", json={"assignee": "张工", "note": "核对包装规格"}).status_code, 200)
        repaired = self.client.post(f"/api/data/governance/issues/{issue_id}/repair", json=repair_body)
        self.assertEqual(repaired.status_code, 200, repaired.text)
        self.assertEqual(repaired.json()["normalizedQuantity"], "480")
        self.assertEqual(self.client.get(f"/api/data/imports/{batch}").json()["records"][0]["normalized_quantity"], "480")
        self.assertEqual(self.client.get(f"/api/data/governance/issues/{issue_id}").json()["status"], "resolved")
        self.assertEqual(len(self.client.get("/api/data/governance/changes?factory=B").json()), 3)

    def test_blocking_rows_and_xlsx(self):
        book = Workbook()
        sheet = book.active
        sheet.title = "库存"
        sheet.append(["料号", "数量", "单位"])
        sheet.append(["P012", "bad", "箱"])
        stream = io.BytesIO()
        book.save(stream)
        draft = self.preview(stream.getvalue(), "inventory.xlsx")
        checked = self.client.post(f"/api/data/imports/{draft}/check", json={"mapping": {"material": "料号", "quantity": "数量", "unit": "单位"}})
        self.assertEqual(checked.json()["blocking"], 1)
        self.assertEqual(self.client.post(f"/api/data/imports/{draft}/commit", json={"strategy": "insert", "businessKey": "material", "idempotencyKey": "test-blocked-unique-001"}).status_code, 409)

    def test_material_code_rule_applies_on_recheck(self):
        draft = self.preview("material,quantity,unit\nP-012,20,件".encode())
        self.assertEqual(self.client.post(f"/api/data/imports/{draft}/check", json={"mapping": {
            "material": "material", "quantity": "quantity", "unit": "unit"}}).status_code, 200)
        batch = self.client.post(f"/api/data/imports/{draft}/commit", json={
            "strategy": "insert", "businessKey": "material", "idempotencyKey": "test-material-unique-001"}).json()["id"]
        rule = self.client.post("/api/data/governance/material-rules", json={
            "sourceCode": "P-012", "standardCode": "P012", "factory": "B", "evidence": "物料主数据单",
            "confirmedBy": "张工", "validFrom": "2020-01-01", "confirmed": True})
        self.assertEqual(rule.status_code, 200, rule.text)
        self.assertEqual(self.client.post(f"/api/data/imports/{batch}/recheck", json={}).status_code, 200)
        records = self.client.get("/api/data/records?factory=B").json()
        self.assertEqual(records[0]["material"], "P012")
        self.assertIn("P-012", records[0]["raw_json"])
        self.assertEqual(self.client.get("/api/data/governance/material-rules?factory=B").json()[0]["version"], 1)

    def test_requires_login(self):
        self.client.post("/api/auth/logout")
        self.assertEqual(self.client.get("/api/data/imports").status_code, 401)
        self.assertEqual(self.client.post("/api/data/imports/preview", data={"domain": "inventory", "factory": "B", "owner": "张工"}, files={"file": ("a.csv", b"a\nb")}).status_code, 401)

    def checked_draft(self, material, quantity="10"):
        draft = self.preview(f"material,quantity,unit\n{material},{quantity},件".encode(), f"{material}.csv")
        response = self.client.post(f"/api/data/imports/{draft}/check", json={"mapping": {
            "material": "material", "quantity": "quantity", "unit": "unit"}})
        self.assertEqual(response.status_code, 200)
        return draft

    def bulk_body(self, drafts, strategy="insert", key="test-bulk-atomic-001"):
        return {"files": [{"draftId": draft, "isolateWarnings": False} for draft in drafts],
                "strategy": strategy, "idempotencyKey": key}

    def test_bulk_success_idempotency_and_validation(self):
        drafts = [self.checked_draft("P001"), self.checked_draft("P002")]
        body = self.bulk_body(drafts)
        response = self.client.post("/api/data/imports/bulk-commit", json=body)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(response.json()), 2)
        self.assertEqual(self.client.post("/api/data/imports/bulk-commit", json=body).json(), response.json())
        self.assertEqual(len(self.client.get("/api/data/imports").json()), 2)
        self.assertEqual(self.client.post("/api/data/imports/bulk-commit", json={**body, "strategy": "upsert"}).status_code, 409)
        for bad in [[], [{}], [{"draftId": drafts[0], "isolateWarnings": "false"}], body["files"] * 11]:
            self.assertEqual(self.client.post("/api/data/imports/bulk-commit", json={**body, "files": bad}).status_code, 400)
        self.client.post("/api/auth/logout")
        self.assertEqual(self.client.post("/api/data/imports/bulk-commit", json=body).status_code, 401)

    def test_bulk_late_failure_rolls_back_and_restores_updates(self):
        original = self.checked_draft("P001", "7")
        self.assertEqual(self.client.post(f"/api/data/imports/{original}/commit", json={
            "strategy": "insert", "businessKey": "material", "idempotencyKey": "original-single-001"}).status_code, 200)
        store = self.client.app.state.data
        tables = ["import_batches", "source_records", "current_records", "quality_issues", "change_events", "import_groups"]
        before = {table: store.all(f"SELECT * FROM {table}") for table in tables}
        drafts = [self.checked_draft("P001", "99"), self.checked_draft("P002")]
        # Fail only after the first file has overwritten the current record.
        store.db.execute("CREATE TRIGGER fail_second BEFORE INSERT ON source_records WHEN NEW.material='P002' BEGIN SELECT RAISE(ABORT, 'test failure'); END")
        body = self.bulk_body(drafts, "upsert")
        self.assertEqual(self.client.post("/api/data/imports/bulk-commit", json=body).status_code, 503)
        for table in tables:
            self.assertEqual(store.all(f"SELECT * FROM {table}"), before[table], table)
        store.db.execute("DROP TRIGGER fail_second")
        self.assertEqual(self.client.post("/api/data/imports/bulk-commit", json=body).status_code, 200)
        records = self.client.get("/api/data/records?factory=B").json()
        self.assertEqual({row["material"]: row["quantity"] for row in records}, {"P001": "99", "P002": "10"})

    def test_bulk_rejects_cross_file_duplicates_and_blocking(self):
        first = self.checked_draft("P001")
        for second in [self.checked_draft("P001"), self.checked_draft("P002", "bad")]:
            response = self.client.post("/api/data/imports/bulk-commit", json=self.bulk_body([first, second]))
            self.assertEqual(response.status_code, 409, response.text)
            self.assertEqual(self.client.get("/api/data/imports").json(), [])
            self.assertEqual(self.client.get("/api/data/records").json(), [])

    def test_bulk_rejects_foreign_drafts_and_scope_mismatch(self):
        first, second = self.checked_draft("P001"), self.checked_draft("P002")
        store = self.client.app.state.data
        store.db.execute("UPDATE import_drafts SET factory='A' WHERE id=?", (second,))
        body = self.bulk_body([first, second])
        self.assertEqual(self.client.post("/api/data/imports/bulk-commit", json=body).status_code, 409)
        store.db.execute("UPDATE import_drafts SET workspace_id='foreign-workspace' WHERE id=?", (second,))
        self.assertEqual(self.client.post("/api/data/imports/bulk-commit", json=body).status_code, 404)
        self.assertEqual(self.client.get("/api/data/imports").json(), [])


if __name__ == "__main__":
    unittest.main()
