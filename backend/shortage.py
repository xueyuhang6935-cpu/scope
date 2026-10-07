"""Deterministic, time-aware calculation over supply_demo source records.

No expected_results, fixture risk_events, plans, or closure records are read.
Quantities stay Decimal until the JSON boundary. All supply precedes demand
at an equal timestamp; demand is allocated by (need_at, plan_no, line_no).
"""
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING
import hashlib
import json

from psycopg import sql

from .common import APIError

RULE_VERSION = "supply-shortage-v1"
TABLES = (
    "factories", "materials", "material_revisions", "warehouses", "production_lines",
    "bom_headers", "bom_lines", "production_plan_lines", "inventory_snapshots",
    "inventory_reservations", "quality_holds", "unit_conversions", "purchase_order_lines",
    "supplier_commitments", "shipment_lines", "receipt_lines", "quality_releases",
    "line_deliveries", "execution_orders", "supply_rules", "responsibility_rules", "people", "dataset_status",
)


def timestamp(value):
    try:
        result = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError()
        return result.astimezone(timezone.utc)
    except (TypeError, ValueError, AttributeError):
        raise APIError(400, "时间必须为包含时区的 ISO 日期时间。") from None


def encode(value):
    if isinstance(value, datetime):
        return timestamp(value).isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    raise TypeError(type(value).__name__)


def plain(value):
    return json.loads(json.dumps(value, default=encode, ensure_ascii=False))


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, default=encode, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def load_snapshot(db, scenario_id, as_of):
    scenario = db.execute("SELECT * FROM supply_demo.scenarios WHERE id=%s", (scenario_id,)).fetchone()
    if not scenario or not scenario["is_simulated"]:
        raise APIError(400, "请选择已接入的模拟场景。")
    as_of = timestamp(as_of or scenario["initial_as_of"])
    if not scenario["window_start"] <= as_of <= scenario["window_end"]:
        raise APIError(400, "回放时点必须位于场景时间窗口内。")
    tables = {}
    for table in TABLES:
        tables[table] = db.execute(sql.SQL(
            "SELECT * FROM supply_demo.{} WHERE scenario_id=%s AND ingested_at<=%s ORDER BY id"
        ).format(sql.Identifier(table)), (scenario_id, as_of)).fetchall()
    tables["supplier_commitments"] = [r for r in tables["supplier_commitments"]
                                      if r["known_at"] <= as_of and r["changed_at"] <= as_of]
    return {"scenario": scenario, "asOf": as_of, "tables": tables}


def calculate(snapshot, days=7, factory="all", material=""):
    if type(days) is not int or not 1 <= days <= 14 or factory not in ("all", "A", "B"):
        raise APIError(400, "计算窗口支持 1 至 14 天，工厂须为 A、B 或 all。")
    scenario, as_of, tables = snapshot["scenario"], snapshot["asOf"], snapshot["tables"]
    start = scenario["window_start"]
    end = min(start + timedelta(days=days), scenario["window_end"])
    materials = {r["id"]: r for r in tables["materials"]}
    if material and material not in materials:
        raise APIError(400, "当前场景不存在该物料，不能将缺失物料视为零库存。")
    revisions = {r["id"]: r for r in tables["material_revisions"]}
    evidence = {}
    global_missing = []
    buckets = {}

    def ref(table, row):
        key = f"{table}:{row['id']}"
        evidence[key] = {"id": key, "table": table, "sourceId": row["id"],
                         "sourceRecordId": row.get("source_record_id", key),
                         "knownAt": row.get("ingested_at", as_of), "record": row}
        return key

    def bucket(f, m, revision_id):
        key = (f, m, revision_id)
        if key not in buckets:
            buckets[key] = {"factory": f, "material": m, "name": materials.get(m, {}).get("name", m),
                            "revision": revision_id, "unit": materials.get(m, {}).get("base_unit", ""),
                            "opening": Decimal(0), "demands": [], "supplies": [], "missing": [], "evidenceIds": []}
        return buckets[key]

    def quantity(row, field, unit, f, m, at, b):
        value = Decimal(str(row[field]))
        if not value.is_finite() or value < 0:
            b["missing"].append(f"{row['id']} 数量无效")
            return Decimal(0)
        if unit == materials.get(m, {}).get("base_unit"):
            return value
        rules = [r for r in tables["unit_conversions"] if r["factory_id"] == f and r["material_id"] == m
                 and r["from_unit"] == unit and r["to_unit"] == materials.get(m, {}).get("base_unit")
                 and r["valid_from"] <= at < r["valid_to"]]
        if len(rules) != 1:
            b["missing"].append(f"{m}/{f} 缺少唯一有效的 {unit} 换算规则（{row['id']}）")
            return Decimal(0)
        b["evidenceIds"].append(ref("unit_conversions", rules[0]))
        return value * Decimal(str(rules[0]["factor"]))

    def applicable_rules(f, m, kind, at):
        return [r for r in tables["supply_rules"] if r["factory_id"] == f and r["material_id"] in (None, m)
                and r["rule_type"] == kind and r["valid_from"] <= at < r["valid_to"]]

    status = {r["table_name"]: r["availability"] for r in tables["dataset_status"]}
    for table in ("inventory_snapshots", "production_plan_lines", "bom_headers", "bom_lines", "purchase_order_lines"):
        if status.get(table) not in ("provided", "confirmed_empty"):
            global_missing.append(f"{table} 数据完整性未确认")

    # Select the latest known effective version BEFORE checking its active status.
    plans = {}
    for p in tables["production_plan_lines"]:
        if p["effective_at"] > as_of or (factory != "all" and p["factory_id"] != factory):
            continue
        key = (p["factory_id"], p["plan_no"], p["line_no"])
        if key not in plans or p["version"] > plans[key]["version"]:
            plans[key] = p
    for p in sorted(plans.values(), key=lambda r: (r["need_at"], r["plan_no"], r["line_no"])):
        if p["status"] != "active" or not start <= p["need_at"] < end or not p["quantity"]:
            continue
        headers = [h for h in tables["bom_headers"] if h["factory_id"] == p["factory_id"]
                   and h["product_id"] == p["product_id"] and h["approval_status"] == "approved"
                   and h["valid_from"] <= p["need_at"] < h["valid_to"]]
        if len(headers) != 1:
            global_missing.append(f"计划 {p['id']} 缺少唯一有效且已批准的 BOM")
            continue
        h = headers[0]
        lines = [l for l in tables["bom_lines"] if l["bom_id"] == h["id"]]
        if not lines:
            global_missing.append(f"BOM {h['id']} 缺少明细")
        for line in lines:
            m, f = line["material_id"], p["factory_id"]
            if material and m != material:
                continue
            b = bucket(f, m, line["revision_id"])
            rev = revisions.get(line["revision_id"])
            if not rev or rev["material_id"] != m or not rev["valid_from"] <= p["need_at"] < rev["valid_to"]:
                b["missing"].append(f"BOM {h['id']} 物料版本在需求时点不适用")
            per = quantity(line, "quantity_per", line["unit"], f, m, p["need_at"], b)
            ids = [ref("production_plan_lines", p), ref("bom_headers", h), ref("bom_lines", line)]
            b["evidenceIds"].extend(ids)
            b["demands"].append({"planId": p["id"], "planNo": p["plan_no"], "planVersion": p["version"],
                                 "product": p["product_id"], "at": p["need_at"], "quantity": per * p["quantity"],
                                 "vehicles": p["quantity"], "perVehicle": per, "bomVersion": h["version"], "evidenceIds": ids})

    # Use a complete opening snapshot at/before window start. Later snapshots are
    # not additive stock and must never be mixed with already counted receipts.
    opening_lots = set()
    for (f, m, rev), b in buckets.items():
        stocks = [s for s in tables["inventory_snapshots"] if s["factory_id"] == f and s["material_id"] == m
                  and s["revision_id"] == rev and s["snapshot_at"] <= start]
        if not stocks:
            b["missing"].append(f"{f}/{m}/{rev} 缺少期初库存快照（零库存也需明确记录）")
            continue
        latest_at = max(s["snapshot_at"] for s in stocks)
        if latest_at != start:
            b["missing"].append(f"{f}/{m} 期初快照与场景起点不一致，缺少期间库存流水")
        for stock in (s for s in stocks if s["snapshot_at"] == latest_at):
            opening_lots.add((stock["warehouse_id"], stock["lot_no"]))
            b["evidenceIds"].append(ref("inventory_snapshots", stock))
            q = quantity(stock, "quantity", stock["unit"], f, m, start, b)
            holds = [r for r in tables["quality_holds"] if r["snapshot_id"] == stock["id"] and r["started_at"] <= as_of]
            reservations = [r for r in tables["inventory_reservations"] if r["snapshot_id"] == stock["id"]
                            and r["effective_at"] <= as_of]
            b["evidenceIds"].extend(ref("inventory_reservations", r) for r in reservations)
            if stock["quality_status"] in ("held", "blocked", "frozen"):
                q = Decimal(0)
            elif stock["quality_status"] not in ("released", "partially_held"):
                b["missing"].append(f"{stock['id']} 质量状态不明确")
            else:
                if stock["quality_status"] == "partially_held" and not holds:
                    b["missing"].append(f"{stock['id']} 缺少冻结数量依据")
                rule = applicable_rules(f, m, "reservation", start)
                if holds and reservations and (not rule or rule[-1]["value"].get("holds_overlap") is not False):
                    b["missing"].append(f"{stock['id']} 冻结与占用重叠口径未确认")
                for hold in holds:
                    eid = ref("quality_holds", hold)
                    b["evidenceIds"].append(eid)
                    held = quantity(hold, "quantity", stock["unit"], f, m, start, b)
                    if hold["released_at"] and hold["released_at"] < start:
                        continue
                    if hold["started_at"] <= start:
                        q -= held
                    else:
                        b["supplies"].append({"at": hold["started_at"], "quantity": -held, "kind": "quality_hold", "evidenceIds": [eid]})
                    if hold["released_at"] and hold["released_at"] <= as_of:
                        if not hold["release_evidence"]:
                            b["missing"].append(f"{hold['id']} 缺少质量解冻依据")
                        else:
                            b["supplies"].append({"at": hold["released_at"], "quantity": held, "kind": "quality_release", "evidenceIds": [eid]})
                for r in reservations:
                    if r["deduction_scope"] == "external_demand":
                        reserved = quantity(r, "quantity", stock["unit"], f, m, start, b)
                        if r["released_at"] and r["released_at"] < start:
                            continue
                        # Reservations are allocations against opening stock;
                        # already-included production demand is never deducted.
                        q -= reserved
                        if r["released_at"] and r["released_at"] <= as_of:
                            b["supplies"].append({"at": max(start, r["released_at"]), "quantity": reserved,
                                                  "kind": "reservation_released", "evidenceIds": [ref("inventory_reservations", r)]})
                if q < 0:
                    b["missing"].append(f"{stock['id']} 冻结与外部占用超过库存")
            b["opening"] += max(q, Decimal(0))

    purchases = {p["id"]: p for p in tables["purchase_order_lines"]}
    shipments = {s["id"]: s for s in tables["shipment_lines"]}
    receipts = {r["id"]: r for r in tables["receipt_lines"] if r["received_at"] <= as_of}
    releases = {r["id"]: r for r in tables["quality_releases"] if r["released_at"] <= as_of and r["status"] == "released"}
    executions = {r["id"]: r for r in tables["execution_orders"]}
    lines = {r["id"]: r for r in tables["production_lines"]}
    warehouses = {r["id"]: r for r in tables["warehouses"]}
    for shipment in shipments.values():
        if not shipment["execution_id"] or not shipment["actual_shipped_at"] or shipment["actual_shipped_at"] > as_of:
            continue
        origin = executions.get(shipment["execution_id"])
        if not origin:
            targets = {warehouses[r["warehouse_id"]]["factory_id"] for r in receipts.values()
                       if r["shipment_id"] == shipment["id"] and r["warehouse_id"] in warehouses}
            for b in buckets.values():
                if b["material"] == shipment["material_id"] and b["factory"] not in targets:
                    b["missing"].append(f"调拨发运 {shipment['id']} 缺少当时已知的调出工厂记录")
            continue
        b = buckets.get((origin["from_factory"], origin["material_id"], origin["revision_id"]))
        if b and start <= shipment["actual_shipped_at"] < end:
            ids = [ref("shipment_lines", shipment), ref("execution_orders", origin)]
            b["evidenceIds"].extend(ids)
            q = quantity(shipment, "quantity", origin["unit"], b["factory"], b["material"], shipment["actual_shipped_at"], b)
            b["supplies"].append({"at": shipment["actual_shipped_at"], "quantity": -q, "kind": "transfer_out", "evidenceIds": ids})
    received = defaultdict(Decimal)
    for r in receipts.values():
        s = shipments.get(r["shipment_id"])
        if s and s["purchase_line_id"]:
            received[s["purchase_line_id"]] += r["quantity"]
    # Actual supply requires the entire receipt -> released -> line chain.
    delivered_per_release = defaultdict(Decimal)
    for delivery in tables["line_deliveries"]:
        if not delivery["actual_at"] or delivery["actual_at"] > as_of or delivery["status"] != "delivered":
            continue
        release = releases.get(delivery["release_id"])
        receipt = receipts.get(release["receipt_id"]) if release else None
        shipment = shipments.get(receipt["shipment_id"]) if receipt else None
        origin = (purchases.get(shipment["purchase_line_id"]) or executions.get(shipment["execution_id"])) if shipment else None
        target_line = lines.get(delivery["line_id"])
        if not all((release, receipt, shipment, origin, target_line)):
            global_missing.append(f"到线记录 {delivery['id']} 缺少完整的采购/调拨、收货及放行关联")
            continue
        f, m, rev = target_line["factory_id"], shipment["material_id"], origin["revision_id"]
        b = buckets.get((f, m, rev))
        if not b or (receipt["warehouse_id"], receipt["lot_no"]) in opening_lots:
            continue
        delivered_per_release[release["id"]] += delivery["quantity"]
        if delivered_per_release[release["id"]] > release["quantity"] or release["quantity"] > receipt["quantity"] or not receipt["received_at"] <= release["released_at"] <= delivery["actual_at"]:
            b["missing"].append(f"到线记录 {delivery['id']} 的数量或时间关系无效")
            continue
        q = quantity(delivery, "quantity", origin["unit"], f, m, delivery["actual_at"], b)
        ids = [ref("line_deliveries", delivery), ref("quality_releases", release), ref("receipt_lines", receipt), ref("shipment_lines", shipment)]
        b["evidenceIds"].extend(ids)
        b["supplies"].append({"at": delivery["actual_at"], "quantity": q, "kind": "actual_line_delivery", "evidenceIds": ids})

    commitments = {}
    for c in tables["supplier_commitments"]:
        key = (c["purchase_line_id"], c["installment"])
        if key not in commitments or (c["version"], c["known_at"]) > (commitments[key]["version"], commitments[key]["known_at"]):
            commitments[key] = c
    for p in purchases.values():
        f, m, rev = p["factory_id"], p["material_id"], p["revision_id"]
        b = buckets.get((f, m, rev))
        if not b:
            continue
        cs = sorted([c for c in commitments.values() if c["purchase_line_id"] == p["id"]], key=lambda c: (c["available_at"], c["installment"]))
        # delivered_quantity and receipts describe the SAME quantity, not two deductions.
        delivered = max(p["delivered_quantity"], received[p["id"]])
        if delivered + p["cancelled_quantity"] > p["quantity"]:
            b["missing"].append(f"采购 {p['id']} 已收货及取消数量超过订单")
        remaining = max(Decimal(0), p["quantity"] - delivered - p["cancelled_quantity"])
        if remaining and not cs:
            b["missing"].append(f"采购 {p['id']} 未交部分缺少交期承诺")
        consumed = delivered + p["cancelled_quantity"]
        if sum((c["quantity"] for c in cs), Decimal(0)) < p["quantity"] - p["cancelled_quantity"] and remaining:
            b["missing"].append(f"采购 {p['id']} 分批承诺未覆盖订单数量")
        pending_shipments = []
        for s in sorted(shipments.values(), key=lambda r: (r["eta_at"], r["id"])):
            if s["purchase_line_id"] != p["id"] or not s["actual_shipped_at"] or s["actual_shipped_at"] > as_of:
                continue
            in_transit = s["quantity"] - sum((r["quantity"] for r in receipts.values() if r["shipment_id"] == s["id"]), Decimal(0))
            if in_transit <= 0:
                continue
            handling, ids = Decimal(0), [ref("shipment_lines", s)]
            for kind in ("receiving", "inspection", "line_delivery"):
                rules = applicable_rules(f, m, kind, s["eta_at"])
                if not rules:
                    b["missing"].append(f"{p['id']} 缺少 {kind} 到线时长规则")
                else:
                    handling += Decimal(str(rules[-1]["value"]["hours"]))
                    ids.append(ref("supply_rules", rules[-1]))
            pending_shipments.append({"quantity": in_transit, "at": s["eta_at"] + timedelta(seconds=float(handling * 3600)), "evidenceIds": ids})
        if sum((s["quantity"] for s in pending_shipments), Decimal(0)) > remaining:
            b["missing"].append(f"采购 {p['id']} 在途数量超过未交量")
        for c in cs:
            used = min(consumed, c["quantity"])
            consumed -= used
            q = min(remaining, c["quantity"] - used)
            remaining -= q
            if q <= 0:
                continue
            ids = [ref("purchase_order_lines", p), ref("supplier_commitments", c)]
            b["evidenceIds"].extend(ids)
            chunks = []
            # FIFO maps shipments to installments when no explicit installment
            # relation exists. Only the shipped portion gets the transit ETA.
            for s in pending_shipments:
                part = min(q, s["quantity"])
                if part:
                    chunks.append((max(c["available_at"], s["at"]), part, ids + s["evidenceIds"]))
                    s["quantity"] -= part
                    q -= part
            if q:
                chunks.append((c["available_at"], q, ids))
            for at, amount, source_ids in chunks:
                b["evidenceIds"].extend(source_ids)
                if at >= end:
                    continue
                if at < as_of:
                    # Past promises without actual receipt evidence are not actual stock.
                    b["missing"].append(f"采购 {p['id']} 承诺已过期但缺少到线证据或新承诺")
                    continue
                b["supplies"].append({"at": at, "quantity": quantity({**c, "quantity": amount}, "quantity", p["unit"], f, m, at, b),
                                      "kind": "confirmed_commitment", "evidenceIds": source_ids})

    results = []
    affected_union = {}
    for b in buckets.values():
        b["missing"] = sorted(set(b["missing"] + global_missing))
        b["evidenceIds"] = sorted(set(b["evidenceIds"]))
        owners = [r for r in tables["responsibility_rules"] if r["factory_id"] == b["factory"] and r["material_id"] == b["material"]]
        b["owner"] = owners[0]["owner_id"] if owners else "待分配"
        b["computable"] = not b["missing"]
        b.update(firstShortageAt=None, shortage=Decimal(0), totalDemand=sum((d["quantity"] for d in b["demands"]), Decimal(0)),
                 timeline=[], affectedPlans=[], windows=[], safetyWarnings=[])
        if b["missing"]:
            b.update(opening=None, shortage=None)
            results.append(b)
            continue
        balance = b["opening"]
        events = [(s["at"], 0, i, s) for i, s in enumerate(b["supplies"]) if start <= s["at"] < end]
        events += [(d["at"], 1, i, d) for i, d in enumerate(b["demands"])]
        active_window = None
        for at, kind, _, item in sorted(events):
            before = balance
            balance += item["quantity"] if kind == 0 else -item["quantity"]
            b["timeline"].append({"at": at, "kind": "supply" if kind == 0 else "demand", "supplyKind": item.get("kind"), "quantity": item["quantity"],
                                   "balance": balance, "planId": item.get("planId"), "evidenceIds": item["evidenceIds"]})
            if balance < 0:
                if not active_window:
                    active_window = {"start": at, "end": end, "shortage": Decimal(0)}
                    b["windows"].append(active_window)
                active_window["shortage"] = max(active_window["shortage"], -balance)
                b["firstShortageAt"] = b["firstShortageAt"] or at
                b["shortage"] = max(b["shortage"], -balance)
            elif active_window:
                active_window["end"] = at
                active_window = None
            if kind == 1:
                deficit = min(item["quantity"], max(Decimal(0), -balance) - max(Decimal(0), -before))
                if deficit:
                    vehicles = min(item["vehicles"], (deficit / item["perVehicle"]).to_integral_value(rounding=ROUND_CEILING))
                    b["affectedPlans"].append({**item, "uncovered": deficit, "affectedVehicles": vehicles})
                    # All parts use the same production order allocation; vehicle
                    # impact is max per plan, never sum across missing materials.
                    affected_union[item["planId"]] = max(affected_union.get(item["planId"], Decimal(0)), vehicles)
                rules = applicable_rules(b["factory"], b["material"], "safety_stock", at)
                if rules and 0 <= balance < Decimal(str(rules[-1]["value"]["quantity"])):
                    b["safetyWarnings"].append({"at": at, "balance": balance, "threshold": rules[-1]["value"]["quantity"]})
        results.append(b)
    return plain({"scenarioId": scenario["id"], "scenarioName": scenario["name"], "asOf": as_of,
                  "windowStart": start, "windowEnd": end, "ruleVersion": RULE_VERSION, "simulated": True,
                  "snapshotHash": fingerprint(snapshot), "results": results, "missing": sorted(set(global_missing)),
                  "affectedVehicles": sum(affected_union.values(), Decimal(0)), "evidence": list(evidence.values()),
                  "limitations": ["合成场景数据；计算窗口从场景起点开始，回放时点仅决定当时已知证据。",
                                  "采购承诺为预计到线供给；收货未放行、未到线不计入实际供给。",
                                  "影响产量按需求时间及计划顺序分配，同一计划的多物料影响不重复相加。"]})
