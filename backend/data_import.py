import csv
import io
import json
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from itertools import islice
from urllib.parse import quote
from zipfile import BadZipFile

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import Response
from openpyxl import load_workbook

from .common import APIError
from .data_access import DataGuard, workspace, migrate
from .business_access import current_scope


MAX_FILE = 10 * 1024 * 1024
MAX_ROWS = 10000
DOMAINS = {"inventory": "库存", "purchase": "采购"}
REQUIRED = ("material", "quantity", "unit")


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def require_user(auth, request):
    user = auth.user(request)
    if not user:
        raise APIError(401, "请先登录。")
    return user


def parse_file(filename, content, sheet):
    if not content or len(content) > MAX_FILE:
        raise APIError(400, "文件须为非空且不超过 10 MB。")
    try:
        if filename.lower().endswith(".csv"):
            rows = list(islice(csv.reader(io.StringIO(content.decode("utf-8-sig"), newline="")), MAX_ROWS + 2))
            sheets = []
        elif filename.lower().endswith(".xlsx"):
            book = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
            sheets = book.sheetnames
            if sheet and sheet not in sheets:
                raise APIError(400, "工作表不存在。")
            selected = sheet or sheets[0]
            rows = list(book[selected].iter_rows(min_row=1, max_row=MAX_ROWS + 2, values_only=True))
            sheet = selected
            book.close()
        else:
            raise APIError(400, "仅支持 CSV 或 XLSX 文件。")
    except APIError:
        raise
    except (UnicodeError, csv.Error, ValueError, OSError, KeyError, BadZipFile) as exc:
        raise APIError(400, "文件无法解析，请检查编码和格式。") from exc
    if len(rows) < 2 or len(rows) > MAX_ROWS + 1:
        raise APIError(400, f"文件应包含 1 至 {MAX_ROWS} 条数据记录。")
    columns = [str(value).strip() if value is not None else "" for value in rows[0]]
    if not all(columns) or len(set(columns)) != len(columns):
        raise APIError(400, "表头不能为空或重复。")
    records = [{column: "" if index >= len(row) or row[index] is None else row[index].date().isoformat() if isinstance(row[index], datetime) else row[index].isoformat() if isinstance(row[index], date) else str(row[index]).strip()
                for index, column in enumerate(columns)} for row in rows[1:] if any(value is not None and str(value).strip() for value in row)]
    if not records:
        raise APIError(400, "文件没有有效记录。")
    return columns, records, sheets, sheet or ""


def latest_rule(data, material, factory, unit, effective_date):
    return data.one("SELECT * FROM unit_rules WHERE material=? AND factory=? AND from_unit=? AND to_unit='件' AND valid_from<=? AND workspace_id=? ORDER BY version DESC LIMIT 1",
                    (material, factory, unit, effective_date, workspace()))


def standard_material(data, material, factory, effective_date):
    rule = data.one("SELECT standard_code FROM material_rules WHERE source_code=? AND factory=? AND valid_from<=? AND workspace_id=? ORDER BY version DESC LIMIT 1",
                    (material, factory, effective_date, workspace()))
    return rule["standard_code"] if rule else material


def check_rows(data, draft, mapping):
    columns = json.loads(draft["columns_json"])
    if not isinstance(mapping, dict) or any(mapping.get(key) not in columns for key in REQUIRED):
        raise APIError(400, "请完成物料编码、数量和单位的字段映射。")
    selected = [mapping[key] for key in REQUIRED]
    if len(set(selected)) != len(selected):
        raise APIError(400, "必填字段不能重复映射。")
    if mapping.get("date") and mapping["date"] not in columns:
        raise APIError(400, "日期字段映射无效。")
    if mapping.get("date") and mapping.get("dateFormat") not in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d"):
        raise APIError(400, "请选择日期格式。")
    problems = []
    normalized = []
    seen = set()
    for index, row in enumerate(json.loads(draft["rows_json"]), start=2):
        raw_material, raw_quantity, unit = (row[mapping[key]].strip() for key in REQUIRED)
        effective_date = datetime.now(timezone.utc).date().isoformat()
        if mapping.get("date"):
            try:
                effective_date = datetime.strptime(row[mapping["date"]], mapping["dateFormat"]).date().isoformat()
            except ValueError:
                pass
        material = standard_material(data, raw_material, draft["factory"], effective_date)
        issue = None
        try:
            quantity = Decimal(raw_quantity)
            if not quantity.is_finite() or quantity < 0:
                raise InvalidOperation()
        except InvalidOperation:
            quantity = None
        if not raw_material or not unit or quantity is None:
            issue = {"row": index, "severity": "blocking", "rule": "required_value", "originalValue": f"{raw_material} / {raw_quantity} / {unit}",
                     "reason": "物料编码、非负数量和单位均为必填且须有效。", "impact": "不能导入该记录。"}
        elif material in seen:
            issue = {"row": index, "severity": "blocking", "rule": "duplicate_key", "originalValue": material,
                     "reason": "同一文件中物料编码重复。", "impact": "无法确定该物料的有效记录。"}
        elif mapping.get("date"):
            try:
                datetime.strptime(row[mapping["date"]], mapping["dateFormat"])
            except ValueError:
                issue = {"row": index, "severity": "blocking", "rule": "date_format", "originalValue": row[mapping["date"]],
                         "reason": "日期不符合所选格式。", "impact": "不能导入该记录。"}
        seen.add(material)
        if not issue and draft["domain"] == "inventory" and unit != "件":
            rule = latest_rule(data, material, draft["factory"], unit, effective_date)
            if not rule:
                issue = {"row": index, "severity": "warning", "rule": "unit_conversion", "originalValue": f"{raw_quantity} {unit}",
                         "reason": f"{material} 缺少 {unit}/件换算关系。", "impact": "标准化数量尚不可计算；记录须隔离。"}
            else:
                quantity *= Decimal(rule["factor"])
        normalized.append({"row": index, "material": material, "quantity": raw_quantity, "unit": unit,
                           "normalizedQuantity": str(quantity) if quantity is not None and not issue else None,
                           "status": "blocked" if issue and issue["severity"] == "blocking" else "isolated" if issue else "valid"})
        if issue:
            problems.append(issue)
    return {"rows": normalized, "issues": problems, "blocking": sum(p["severity"] == "blocking" for p in problems),
            "isolated": sum(p["severity"] == "warning" for p in problems)}


def routes(auth, data, access):
    migrate(data,access)
    guard=DataGuard(data,access)
    router = APIRouter(prefix="/api/data", dependencies=[Depends(guard.route)])

    def draft_for(request, draft_id):
        user = require_user(auth, request)
        draft = data.one("SELECT * FROM import_drafts WHERE id=? AND workspace_id=?", (draft_id, workspace()))
        if not draft:
            raise APIError(404, "导入草稿不存在。")
        guard.check(draft,True)
        return user, draft

    @router.post("/imports/preview")
    async def preview(request: Request, file: UploadFile = File(...), domain: str = Form(...), factory: str = Form(...),
                      owner: str = Form(...), sheet: str = Form("")):
        user = require_user(auth, request)
        if domain not in DOMAINS or factory not in ("A", "B") or not owner.strip():
            raise APIError(400, "请选择数据域、工厂并填写负责人。")
        access.require(user,workspace(),"data.write",[factory],domain=domain)
        content = await file.read(MAX_FILE + 1)
        columns, records, sheets, selected = parse_file(file.filename or "", content, sheet)
        draft_id = uuid.uuid4().hex
        with data.lock:
            data.db.execute("INSERT INTO import_drafts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (draft_id, user["id"], file.filename, domain, factory, owner.strip(), selected,
                             json.dumps(columns, ensure_ascii=False), json.dumps(records, ensure_ascii=False), None, None, timestamp(), workspace()))
            data.db.execute("INSERT INTO draft_files VALUES(?,?)", (draft_id, content))
        return {"draftId": draft_id, "filename": file.filename, "domain": domain, "factory": factory,
                "columns": columns, "sample": records[:10], "total": len(records), "sheets": sheets, "sheet": selected}

    @router.post("/imports/{draft_id}/check")
    def check(request: Request, draft_id: str):
        _, draft = draft_for(request, draft_id)
        mapping = request.state.body.get("mapping")
        with data.lock:
            result = check_rows(data, draft, mapping)
            data.db.execute("UPDATE import_drafts SET mapping_json=?,check_json=? WHERE id=?",
                            (json.dumps(mapping, ensure_ascii=False), json.dumps(result, ensure_ascii=False), draft_id))
        return result

    @router.post("/imports/{draft_id}/commit")
    def commit(request: Request, draft_id: str):
        return commit_draft(request, draft_id, request.state.body)

    def commit_draft(request, draft_id, body):
        user, draft = draft_for(request, draft_id)
        strategy, business_key, key = body.get("strategy"), body.get("businessKey"), body.get("idempotencyKey")
        if strategy not in ("insert", "upsert") or business_key != "material" or not isinstance(key, str) or len(key) < 16:
            raise APIError(400, "请确认导入策略、业务键和提交标识。")
        if not draft["mapping_json"] or not draft["check_json"]:
            raise APIError(409, "请先完成质量校验。")
        with data.lock:
            old = data.one("SELECT * FROM import_batches WHERE idempotency_key=?", (key,))
            if old:
                if old["draft_id"] != draft_id or old["workspace_id"] != workspace():
                    raise APIError(409, "提交标识已用于其他批次。")
                return old
            if data.one("SELECT id FROM import_batches WHERE draft_id=?", (draft_id,)):
                raise APIError(409, "该草稿已经提交。")
            result = check_rows(data, draft, json.loads(draft["mapping_json"]))
            if result["blocking"]:
                raise APIError(409, "存在阻断问题，不能提交。")
            if result["isolated"] and body.get("isolateWarnings") is not True:
                raise APIError(409, "请确认隔离尚不可计算的记录。")
            batch_id = "BATCH-" + uuid.uuid4().hex[:10].upper()
            rows = json.loads(draft["rows_json"])
            issues = {issue["row"]: issue for issue in result["issues"]}
            mapping = json.loads(draft["mapping_json"])
            now = timestamp()
            try:
                data.db.execute("SAVEPOINT import_file")
                data.db.execute("INSERT INTO import_batches VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                (batch_id, draft_id, user["id"], draft["filename"], draft["domain"], draft["factory"], draft["owner"],
                                 draft["sheet"], draft["mapping_json"], strategy, business_key, len(rows), len(rows)-result["isolated"],
                                 result["isolated"], now, key, workspace()))
                for entry in result["rows"]:
                    raw = rows[entry["row"] - 2]
                    cursor = data.db.execute("INSERT INTO source_records(batch_id,row_number,raw_json,material,quantity,unit,normalized_quantity,status) VALUES(?,?,?,?,?,?,?,?)",
                                             (batch_id, entry["row"], json.dumps(raw, ensure_ascii=False), entry["material"], entry["quantity"],
                                              entry["unit"], entry["normalizedQuantity"], entry["status"]))
                    if entry["status"] == "valid":
                        existing = data.one("SELECT source_record_id FROM current_records WHERE domain=? AND factory=? AND business_key=? AND workspace_id=?",
                                            (draft["domain"], draft["factory"], entry["material"], workspace()))
                        if existing and strategy == "insert":
                            raise APIError(409, f"物料 {entry['material']} 已存在，请选择更新策略。")
                        data.db.execute("INSERT INTO current_records VALUES(?,?,?,?,?) ON CONFLICT(workspace_id,domain,factory,business_key) DO UPDATE SET source_record_id=excluded.source_record_id",
                                        (draft["domain"], draft["factory"], entry["material"], cursor.lastrowid, workspace()))
                    if entry["row"] in issues:
                        issue = issues[entry["row"]]
                        data.db.execute("INSERT INTO quality_issues VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                                        ("ISS-" + uuid.uuid4().hex[:10].upper(), batch_id, cursor.lastrowid, issue["rule"], issue["severity"],
                                         "open", issue["originalValue"], issue["reason"], issue["impact"], None, None, now))
                data.db.execute("INSERT INTO change_events(batch_id,issue_id,kind,details,actor_id,created_at) VALUES(?,NULL,'import',?,?,?)",
                                (batch_id, f"导入 {len(rows)} 条，隔离 {result['isolated']} 条。", user["id"], now))
                data.db.execute("RELEASE SAVEPOINT import_file")
            except Exception:
                data.db.execute("ROLLBACK TO SAVEPOINT import_file")
                data.db.execute("RELEASE SAVEPOINT import_file")
                raise
            return data.one("SELECT * FROM import_batches WHERE id=?", (batch_id,))

    @router.post("/imports/bulk-commit")
    def bulk_commit(request: Request):
        user = require_user(auth, request)
        body = request.state.body
        items, key, strategy = body.get("files"), body.get("idempotencyKey"), body.get("strategy")
        if (not isinstance(items, list) or not 1 <= len(items) <= 20
                or not isinstance(key, str) or not 16 <= len(key) <= 128
                or strategy not in ("insert", "upsert")):
            raise APIError(400, "请选择 1 至 20 个文件并确认导入策略和提交标识。")
        if any(not isinstance(item, dict) or not isinstance(item.get("draftId"), str)
               or not isinstance(item.get("isolateWarnings"), bool) for item in items):
            raise APIError(400, "文件草稿和隔离确认无效。")
        ids = [item["draftId"] for item in items]
        if len(set(ids)) != len(ids):
            raise APIError(400, "同一草稿不能重复提交。")
        payload = json.dumps({"files": items, "strategy": strategy}, sort_keys=True)
        with data.lock:
            previous = data.one("SELECT * FROM import_groups WHERE idempotency_key=?", (key,))
            if previous:
                if previous["user_id"] != user["id"] or previous["payload"] != payload:
                    raise APIError(409, "提交标识已用于其他导入内容。")
                for item in items:
                    draft_for(request,item["draftId"])
                return json.loads(previous["result_json"])
            try:
                data.db.execute("BEGIN IMMEDIATE")
                seen, scope = set(), None
                # All drafts must belong to this user and one common source scope.
                for item in items:
                    _, draft = draft_for(request, item["draftId"])
                    current_scope = (draft["domain"], draft["factory"], draft["owner"])
                    if scope is not None and scope != current_scope:
                        raise APIError(409, "同批文件须使用相同的数据域、工厂和负责人。")
                    scope = current_scope
                    if data.one("SELECT id FROM import_batches WHERE draft_id=?", (draft["id"],)):
                        raise APIError(409, f"{draft['filename']} 已提交，请重新选择文件。")
                    if not draft["mapping_json"] or not draft["check_json"]:
                        raise APIError(409, f"{draft['filename']} 尚未完成质量校验。")
                    result = check_rows(data, draft, json.loads(draft["mapping_json"]))
                    if result["blocking"] or (result["isolated"] and not item["isolateWarnings"]):
                        raise APIError(409, f"{draft['filename']} 存在阻断问题或未确认隔离；整批未保存。")
                    for row in result["rows"]:
                        if row["material"] in seen:
                            raise APIError(409, f"{draft['filename']} 的物料 {row['material']} 与本批其他文件重复；整批未保存。")
                        seen.add(row["material"])
                batches = []
                for item in items:
                    try:
                        batches.append(commit_draft(request, item["draftId"], {
                            "strategy": strategy, "businessKey": "material", "isolateWarnings": item["isolateWarnings"],
                            "idempotencyKey": f"bulk:{key}:{item['draftId']}"}))
                    except APIError as exc:
                        _, draft = draft_for(request, item["draftId"])
                        raise APIError(409, f"{draft['filename']} 导入失败；整批未保存。{exc.body['message']}") from exc
                data.db.execute("INSERT INTO import_groups VALUES(?,?,?,?)",
                                (key, user["id"], payload, json.dumps(batches, ensure_ascii=False)))
                data.db.execute("COMMIT")
                return batches
            except Exception:
                data.db.execute("ROLLBACK")
                raise

    @router.get("/imports")
    def batches(request: Request, factory: str = "all"):
        require_user(auth, request)
        if factory not in ("all", "A", "B"):
            raise APIError(400, "工厂范围无效。")
        return guard.visible(data.all("SELECT * FROM import_batches WHERE (?='all' OR factory=?) ORDER BY created_at DESC", (factory, factory)))

    @router.get("/imports/{batch_id}")
    def batch(request: Request, batch_id: str):
        require_user(auth, request)
        item = data.one("SELECT * FROM import_batches WHERE id=?", (batch_id,))
        if not item:
            raise APIError(404, "批次不存在。")
        return {**item, "records": data.all("SELECT * FROM source_records WHERE batch_id=? ORDER BY row_number", (batch_id,)),
                "issues": data.all("SELECT * FROM quality_issues WHERE batch_id=? ORDER BY id", (batch_id,))}

    @router.get("/imports/{batch_id}/file")
    def source_file(request: Request, batch_id: str):
        require_user(auth, request)
        item = data.one("SELECT b.filename,f.content FROM import_batches b JOIN draft_files f ON f.draft_id=b.draft_id WHERE b.id=?", (batch_id,))
        if not item:
            raise APIError(404, "来源文件不存在。")
        filename = item["filename"].replace("\r", "").replace("\n", "")
        return Response(item["content"], media_type="application/octet-stream",
                        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename, safe='')}"})

    @router.get("/records")
    def records(request: Request, domain: str = "inventory", factory: str = "all"):
        require_user(auth, request)
        if domain not in DOMAINS or factory not in ("all", "A", "B"):
            raise APIError(400, "数据域或工厂范围无效。")
        return guard.visible(data.all("SELECT r.*,b.filename,b.factory,b.created_at,b.domain,b.workspace_id FROM current_records c JOIN source_records r ON r.id=c.source_record_id JOIN import_batches b ON b.id=r.batch_id WHERE c.domain=? AND (?='all' OR c.factory=?) ORDER BY b.created_at DESC,r.row_number",
                        (domain, factory, factory)))

    @router.post("/imports/{batch_id}/recheck")
    def recheck_batch(request: Request, batch_id: str):
        user = require_user(auth, request)
        with data.lock:
            batch = data.one("SELECT * FROM import_batches WHERE id=?", (batch_id,))
            if not batch:
                raise APIError(404, "批次不存在。")
            draft = data.one("SELECT * FROM import_drafts WHERE id=?", (batch["draft_id"],))
            result = check_rows(data, draft, json.loads(batch["mapping_json"]))
            if result["blocking"]:
                raise APIError(409, "原始记录存在阻断问题，无法复检。")
            try:
                data.db.execute("BEGIN IMMEDIATE")
                for entry in result["rows"]:
                    record = data.one("SELECT * FROM source_records WHERE batch_id=? AND row_number=?", (batch_id, entry["row"]))
                    if record["material"] != entry["material"]:
                        data.db.execute("DELETE FROM current_records WHERE source_record_id=?", (record["id"],))
                        data.db.execute("UPDATE source_records SET material=? WHERE id=?", (entry["material"], record["id"]))
                    if record["status"] == "isolated" and entry["status"] == "valid":
                        data.db.execute("UPDATE source_records SET normalized_quantity=?,status='valid' WHERE id=?", (entry["normalizedQuantity"], record["id"]))
                        data.db.execute("UPDATE quality_issues SET status='resolved' WHERE record_id=? AND rule='unit_conversion'", (record["id"],))
                    if entry["status"] == "valid":
                        data.db.execute("UPDATE source_records SET normalized_quantity=? WHERE id=?", (entry["normalizedQuantity"], record["id"]))
                        other = data.one("SELECT source_record_id FROM current_records WHERE domain=? AND factory=? AND business_key=? AND workspace_id=?",
                                         (batch["domain"], batch["factory"], entry["material"], workspace()))
                        if other and other["source_record_id"] != record["id"] and record["material"] != entry["material"]:
                            raise APIError(409, "标准物料编码与现有记录冲突，请先处理重复业务键。")
                        if not other or other["source_record_id"] == record["id"]:
                            data.db.execute("INSERT INTO current_records VALUES(?,?,?,?,?) ON CONFLICT(workspace_id,domain,factory,business_key) DO UPDATE SET source_record_id=excluded.source_record_id",
                                            (batch["domain"], batch["factory"], entry["material"], record["id"], workspace()))
                data.db.execute("UPDATE import_batches SET imported=(SELECT COUNT(*) FROM source_records WHERE batch_id=? AND status='valid'),isolated=(SELECT COUNT(*) FROM source_records WHERE batch_id=? AND status='isolated') WHERE id=?",
                                (batch_id, batch_id, batch_id))
                data.db.execute("INSERT INTO change_events(batch_id,issue_id,kind,details,actor_id,created_at) VALUES(?,NULL,'recheck',?,?,?)",
                                (batch_id, f"复检完成，仍隔离 {result['isolated']} 条。", user["id"], timestamp()))
                data.db.execute("COMMIT")
            except Exception:
                data.db.execute("ROLLBACK")
                raise
            return result

    @router.get("/governance/issues")
    def issues(request: Request, factory: str = "all", status: str = "all"):
        require_user(auth, request)
        if factory not in ("all", "A", "B") or status not in ("all", "open", "resolved"):
            raise APIError(400, "筛选条件无效。")
        return guard.visible(data.all("SELECT i.*,b.factory,b.filename,b.domain,b.workspace_id,r.material,r.quantity,r.unit,r.normalized_quantity,r.row_number FROM quality_issues i JOIN import_batches b ON b.id=i.batch_id JOIN source_records r ON r.id=i.record_id WHERE (?='all' OR b.factory=?) AND (?='all' OR i.status=?) ORDER BY i.created_at DESC",
                        (factory, factory, status, status)))

    @router.get("/governance/issues/{issue_id}")
    def issue(request: Request, issue_id: str):
        require_user(auth, request)
        item = data.one("SELECT i.*,b.factory,b.filename,b.domain,b.workspace_id,r.material,r.quantity,r.unit,r.normalized_quantity,r.row_number FROM quality_issues i JOIN import_batches b ON b.id=i.batch_id JOIN source_records r ON r.id=i.record_id WHERE i.id=?", (issue_id,))
        if not item:
            raise APIError(404, "问题不存在。")
        return item

    @router.post("/governance/issues/{issue_id}/assign")
    def assign(request: Request, issue_id: str):
        user = require_user(auth, request)
        assignee, note = (str(request.state.body.get(key) or "").strip() for key in ("assignee", "note"))
        if not assignee or not note:
            raise APIError(400, "请填写负责人和确认说明。")
        with data.lock:
            item = data.one("SELECT * FROM quality_issues WHERE id=?", (issue_id,))
            if not item or item["status"] != "open":
                raise APIError(409, "问题不存在或已解决。")
            data.db.execute("UPDATE quality_issues SET assignee=?,note=? WHERE id=?", (assignee, note, issue_id))
            data.db.execute("INSERT INTO change_events(batch_id,issue_id,kind,details,actor_id,created_at) VALUES(?,?,'assign',?,?,?)",
                            (item["batch_id"], issue_id, f"分配给 {assignee}：{note}", user["id"], timestamp()))
        return {"assignee": assignee, "note": note}

    @router.get("/governance/rules")
    def rules(request: Request, factory: str = "all"):
        require_user(auth, request)
        return guard.visible(data.all("SELECT * FROM unit_rules WHERE (?='all' OR factory=?) ORDER BY created_at DESC,version DESC", (factory, factory)))

    @router.get("/governance/material-rules")
    def material_rules(request: Request, factory: str = "all"):
        require_user(auth, request)
        if factory not in ("all", "A", "B"):
            raise APIError(400, "工厂范围无效。")
        return guard.visible(data.all("SELECT * FROM material_rules WHERE (?='all' OR factory=?) ORDER BY created_at DESC,version DESC", (factory, factory)))

    @router.post("/governance/material-rules")
    def save_material_rule(request: Request):
        require_user(auth, request)
        body = request.state.body
        source, standard, factory, evidence, confirmed_by, valid_from = (str(body.get(key) or "").strip() for key in
                                                                         ("sourceCode", "standardCode", "factory", "evidence", "confirmedBy", "validFrom"))
        try:
            datetime.strptime(valid_from, "%Y-%m-%d")
        except ValueError as exc:
            raise APIError(400, "生效日期无效。") from exc
        valid_from = datetime.strptime(valid_from, "%Y-%m-%d").date().isoformat()
        if not source or not standard or source == standard or factory not in ("A", "B") or not evidence or not confirmed_by or not body.get("confirmed"):
            raise APIError(400, "请填写原始编码、标准编码、适用工厂及确认依据。")
        with data.lock:
            current = data.one("SELECT MAX(version) AS version FROM material_rules WHERE source_code=? AND factory=?", (source, factory))
            version = (current["version"] or 0) + 1
            data.db.execute("INSERT INTO material_rules(source_code,standard_code,factory,evidence,confirmed_by,valid_from,version,created_at,workspace_id) VALUES(?,?,?,?,?,?,?,?,?)",
                            (source, standard, factory, evidence, confirmed_by, valid_from, version, timestamp(), workspace()))
        return {"sourceCode": source, "standardCode": standard, "factory": factory, "version": version}

    @router.post("/governance/rules")
    def save_rule(request: Request):
        require_user(auth, request)
        body = request.state.body
        material, factory, from_unit, evidence, confirmed_by, valid_from = (str(body.get(key) or "").strip() for key in
                                                                            ("material", "factory", "fromUnit", "evidence", "confirmedBy", "validFrom"))
        try:
            factor = Decimal(str(body.get("factor")))
            valid_from = datetime.strptime(valid_from, "%Y-%m-%d").date().isoformat()
        except (InvalidOperation, ValueError) as exc:
            raise APIError(400, "换算系数或生效日期无效。") from exc
        if not material or factory not in ("A", "B") or not from_unit or from_unit == "件" or not evidence or not confirmed_by or not body.get("confirmed") or not factor.is_finite() or factor <= 0 or factor > 1000000:
            raise APIError(400, "请填写适用范围、换算依据和确认信息。")
        with data.lock:
            current = data.one("SELECT MAX(version) AS version FROM unit_rules WHERE material=? AND factory=? AND from_unit=? AND to_unit='件'",
                               (material, factory, from_unit))
            version = (current["version"] or 0) + 1
            data.db.execute("INSERT INTO unit_rules(material,factory,from_unit,to_unit,factor,evidence,confirmed_by,valid_from,version,created_at,workspace_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                            (material, factory, from_unit, "件", str(factor), evidence, confirmed_by, valid_from, version, timestamp(), workspace()))
            return {"material": material, "factory": factory, "fromUnit": from_unit, "version": version}

    @router.post("/governance/issues/{issue_id}/repair")
    def repair(request: Request, issue_id: str):
        user = require_user(auth, request)
        body = request.state.body
        evidence, confirmed_by, valid_from = (str(body.get(key) or "").strip() for key in ("evidence", "confirmedBy", "validFrom"))
        try:
            factor = Decimal(str(body.get("factor")))
        except InvalidOperation as exc:
            raise APIError(400, "换算系数无效。") from exc
        if not factor.is_finite() or factor <= 0 or factor > 1000000 or not evidence or not confirmed_by or not valid_from or not body.get("confirmed"):
            raise APIError(400, "请填写有效换算系数、依据、生效日期，并确认负责人已核对。")
        try:
            valid_from = datetime.strptime(valid_from, "%Y-%m-%d").date().isoformat()
        except ValueError as exc:
            raise APIError(400, "生效日期无效。") from exc
        with data.lock:
            item = data.one("SELECT i.*,b.factory,r.material,r.quantity,r.unit FROM quality_issues i JOIN import_batches b ON b.id=i.batch_id JOIN source_records r ON r.id=i.record_id WHERE i.id=?", (issue_id,))
            if not item or item["status"] != "open" or item["rule"] != "unit_conversion" or not item["assignee"]:
                raise APIError(409, "问题尚未分配、已解决或不支持单位换算修复。")
            batch = data.one("SELECT * FROM import_batches WHERE id=?", (item["batch_id"],))
            mapping = json.loads(batch["mapping_json"])
            raw = data.one("SELECT raw_json FROM source_records WHERE id=?", (item["record_id"],))
            effective_date = datetime.strptime(json.loads(raw["raw_json"])[mapping["date"]], mapping["dateFormat"]).date().isoformat() if mapping.get("date") else datetime.now(timezone.utc).date().isoformat()
            if valid_from > effective_date:
                raise APIError(409, "生效日期晚于原始记录日期，不能完成当前问题复检。")
            maximum = data.one("SELECT MAX(version) AS version FROM unit_rules WHERE material=? AND factory=? AND from_unit=? AND to_unit='件'",
                               (item["material"], item["factory"], item["unit"]))
            version = (maximum["version"] or 0) + 1
            normalized = str(Decimal(item["quantity"]) * factor)
            now = timestamp()
            try:
                data.db.execute("BEGIN IMMEDIATE")
                data.db.execute("INSERT INTO unit_rules(material,factory,from_unit,to_unit,factor,evidence,confirmed_by,valid_from,version,created_at,workspace_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                                (item["material"], item["factory"], item["unit"], "件", str(factor), evidence, confirmed_by, valid_from, version, now, workspace()))
                data.db.execute("UPDATE source_records SET normalized_quantity=?,status='valid' WHERE id=?", (normalized, item["record_id"]))
                data.db.execute("UPDATE quality_issues SET status='resolved' WHERE id=?", (issue_id,))
                data.db.execute("UPDATE import_batches SET imported=imported+1,isolated=isolated-1 WHERE id=?", (item["batch_id"],))
                data.db.execute("INSERT INTO current_records VALUES(?,?,?,?,?) ON CONFLICT(workspace_id,domain,factory,business_key) DO UPDATE SET source_record_id=excluded.source_record_id",
                                (batch["domain"], item["factory"], item["material"], item["record_id"], workspace()))
                data.db.execute("INSERT INTO change_events(batch_id,issue_id,kind,details,actor_id,created_at) VALUES(?,?,'repair',?,?,?)",
                                (item["batch_id"], issue_id, f"标准化 v{version}：{item['quantity']} {item['unit']} = {normalized} 件；依据：{evidence}", user["id"], now))
                data.db.execute("COMMIT")
            except Exception:
                data.db.execute("ROLLBACK")
                raise
        return {"issueId": issue_id, "status": "resolved", "version": version, "normalizedQuantity": normalized, "unit": "件"}

    @router.get("/governance/changes")
    def changes(request: Request, factory: str = "all"):
        require_user(auth, request)
        return guard.visible(data.all("SELECT e.*,b.filename,b.factory,b.domain,b.workspace_id FROM change_events e JOIN import_batches b ON b.id=e.batch_id WHERE (?='all' OR b.factory=?) ORDER BY e.id DESC", (factory, factory)))

    return router
