"""Read-only, evidence-backed supply analysis; no model-generated quantities."""
import json
import re
from datetime import datetime, timezone
from decimal import Decimal

from .common import APIError

INTENTS = {"inventory": "库存查询", "quality": "质量问题分析", "readiness": "缺料分析就绪检查"}
PERIODS = ("", "未来三天", "未来一周", "未来一个月")
MATERIAL_PATTERN = r"(?<![A-Za-z0-9_-])([A-Za-z]+[-_]?\d+[A-Za-z0-9_-]*)(?![A-Za-z0-9_-])"


def normalize_question(text):
    for pattern, value in ((r'未来\s*[3三]\s*天', '未来三天'),
                           (r'未来\s*(?:[7七]\s*天|[1一]?\s*周)', '未来一周'),
                           (r'未来\s*[1一]?\s*个?月', '未来一个月')):
        text = re.sub(pattern, value, text)
    return text


def validate_plan(value, factory):
    if not isinstance(value, dict) or set(value) - {"intent", "factory", "material", "supplier", "period"}:
        raise APIError(400, "分析范围格式无效。")
    plan = {"intent": value.get("intent", ""), "factory": value.get("factory", factory),
            "material": value.get("material", ""), "supplier": value.get("supplier", ""), "period": value.get("period", "")}
    if any(not isinstance(v, str) for v in plan.values()) or any(len(v) > 120 for v in plan.values()):
        raise APIError(400, "分析字段须为不超过 120 字的文本。")
    plan = {k: v.strip() for k, v in plan.items()}
    if plan["intent"] not in (*INTENTS, "") or plan["factory"] not in ("A", "B", "all") or plan["period"] not in PERIODS:
        raise APIError(400, "不支持的分析类型、工厂或时间范围。")
    if factory != "all" and plan["factory"] != factory:
        raise APIError(400, "不能扩大或改变会话工厂范围，请新建会话。")
    return plan


def propose(text, scope):
    """Extract supported fields; request_questions decides whether they are complete."""
    text = normalize_question(text)
    plan = dict(scope)
    if not plan["intent"]:
        plan["intent"] = "readiness" if any(word in text for word in ("缺料", "风险", "交付", "到货")) else "quality" if any(word in text for word in ("质量", "换算", "隔离")) else "inventory" if "库存" in text else ""
    materials = re.findall(MATERIAL_PATTERN, text)
    if not plan["material"] and len(set(materials)) == 1:
        plan["material"] = materials[0]
    for period in PERIODS[1:]:
        if period in text and not plan["period"]:
            plan["period"] = period
    factories = re.findall(r"([AB])\s*(?:工厂|厂)", text, re.I)
    if scope["factory"] == "all" and len(set(factories)) == 1:
        plan["factory"] = factories[0].upper()
    return plan


def plan_questions(plan, source):
    """Validate executable capabilities for both automatic and clarified queries."""
    questions = []
    if not plan['intent']:
        questions.append('你要查询库存、检查数据质量，还是计算缺料风险？请选择分析类型。')
    if plan['supplier']:
        questions.append('暂不支持按供应商筛选，请清空供应商条件后查询，或停止本次任务。')
    if re.search(r'[\s,，、;/；]', plan['material']):
        questions.append('目前只支持单个物料编码或全部物料，请选择一个编码，或清空表示全部。')
    if plan['intent'] == 'readiness':
        if not source:
            questions.append('尚未接入缺料计算场景，请先接入数据库后重新提问，或改为库存查询、质量检查。')
        elif plan['period'] == '未来一个月' or (not plan['period'] and source['days'] > 14):
            questions.append('缺料计算最多支持14天，请改为未来三天或未来一周。')
    elif plan['period']:
        questions.append('库存和质量查询只支持当前导入记录，请清空时间条件，或选择缺料计算。')
    return questions


def request_questions(text, scope, plan, source, model_understood=False):
    """Only auto-run a fully understood read-only request; never discard extra filters."""
    text = normalize_question(text)
    questions = plan_questions(plan, source)
    explicit = propose(text, {'intent': '', 'factory': 'all', 'material': '', 'supplier': '', 'period': ''})
    for key, label in (('intent', '分析类型'), ('material', '物料'), ('period', '时间')):
        if scope[key] and explicit[key] and scope[key] != explicit[key]:
            questions.append(f'问题中的{label}与手动范围不同，请选择本次实际查询范围。')
        if explicit[key] and plan[key] != explicit[key]:
            questions.append(f'解析出的{label}与问题中的条件不同，请核对本次范围。')
    factories = {f.upper() for f in re.findall(r'([AB])\s*(?:工厂|厂)', text, re.I)}
    if re.search(r'(?:全部|所有)工厂', text) or len(factories) > 1:
        if scope['factory'] != 'all':
            questions.append('问题涉及全部或多个工厂，但当前会话限定单个工厂，请切换会话或修改本次范围。')
    elif factories and scope['factory'] != 'all' and factories != {scope['factory']}:
        questions.append('问题中的工厂与当前会话冲突，请切换工厂新建会话，或修改本次范围。')
    if scope['factory'] == 'all' and factories and plan['factory'] != (next(iter(factories)) if len(factories) == 1 else 'all'):
        questions.append('解析出的工厂与问题中的工厂不同，请核对本次范围。')
    materials = set(re.findall(MATERIAL_PATTERN, text))
    if len(materials) > 1:
        questions.append('问题包含多个物料，目前只支持单个物料或全部物料，请选择本次范围。')
    if re.search(r'(?:全部|所有)物料', text) and scope['material']:
        questions.append('问题要求全部物料，但手动范围限定了物料，请选择本次范围。')
    if sum(period in text for period in PERIODS[1:]) > 1:
        questions.append('问题包含多个时间范围，请选择一个计算窗口。')
    if ('质量' in text and any(w in text for w in ('库存', '缺料', '交付', '到货'))) or re.search(r'库存.*(?:和|及|并|再).*缺料', text):
        questions.append('问题涉及多种分析，请先选择本次要执行的分析类型。')
    if model_understood:
        for key, label in (('material', '物料'), ('period', '时间')):
            if plan[key] and plan[key] != scope[key] and plan[key] not in text:
                questions.append(f'解析出的{label}未在问题或手动范围中明确提供，请核对。')
        if plan['factory'] != scope['factory'] and plan['factory'] not in factories:
            questions.append('解析出的工厂未在问题中明确提供，请核对。')
    unsupported = re.sub('|'.join(PERIODS[1:]), '', text)
    if re.search(r'供应商|昨天|前天|明天|后天|上周|本周|下周|本月|下月|去年|今年|历史|最近|过去|\d{4}[-/年]|\d+月|[一二三四五六七八九十\d]+[天日周月]|排除|除了|不要|不含|仅限|大于|小于|超过|低于|高于|按.+分|排名|排序|删除|修改|更新|批准|审批|下单|创建|调拨|加急|付款|发货|作废', unsupported):
        questions.append('问题包含当前只读分析无法直接执行的筛选、日期或业务动作，请调整查询范围；业务动作仍需走对应审批流程。')
    # ponytail: a bounded vocabulary covers the current three query types;
    # unfamiliar conditions stay visible for clarification instead of being ignored.
    remainder = re.sub(MATERIAL_PATTERN, '', text)
    remainder = re.sub(r'[AB]\s*(?:工厂|厂)', '', remainder, flags=re.I)
    for period in PERIODS[1:]:
        remainder = remainder.replace(period, '')
    remainder = re.sub(r'全部工厂|所有工厂|全部物料|所有物料|库存|数据质量|质量问题|质量|缺料风险|缺料|交付风险|到货风险|风险|换算|隔离', '', remainder)
    remainder = re.sub(r'请问|能不能|能否|帮我|帮忙|麻烦|查询|查一下|查看|检查|分析|计算|看看|查查|看下|一下|看|查|请|现在|当前工厂|本工厂|本厂|当前|目前|最新|导入记录|物料编码|物料|数据|情况|数量|多少|是否|有没有|有无|存在|问题|有|是|的|了|吗|呢|我|件', '', remainder)
    remainder = re.sub(r'[\s，,。.!！?？:：、（）()]+', '', remainder)
    if remainder and not model_understood:
        questions.append(f'以下内容尚不能完整映射为当前只读查询条件：“{remainder[:100]}”。请核对范围；可修改后查询，或停止任务并改写问题。')
    return list(dict.fromkeys(questions))


def action_questions(text, scope, plan, source, model_understood=False):
    """Strip supported action wording, retaining unsupported filters and conflicts."""
    query = re.sub(r'继续查|深入调查|调查|查清|核实|为什么晚交|晚交原因|原因|怎么解决|如何解决|怎么办|试算|哪个方案更好|哪个方案|比较方案|调拨还是加急|并|和', '', text)
    return request_questions(query, scope, plan, source, model_understood)


def analyze(data, user_id, plan, workspace_id=None):
    """Caller holds data.lock while capturing the immutable evidence snapshot."""
    factory, material = plan["factory"], plan["material"]
    args = (workspace_id or ("local-"+str(user_id)), factory, factory, material, material)
    current = """ FROM current_records c JOIN source_records r ON r.id=c.source_record_id
        JOIN import_batches b ON b.id=r.batch_id
        WHERE b.workspace_id=? AND (?='all' OR b.factory=?) AND (?='' OR r.material=?) """
    inventory_count = data.one("SELECT COUNT(*) AS n" + current + "AND c.domain='inventory' AND r.status='valid'", args)["n"]
    inventory = data.all("SELECT r.id AS record_id,r.batch_id,r.row_number,r.material,r.quantity,r.unit,r.normalized_quantity,b.factory,b.filename,b.created_at AS imported_at" + current + "AND c.domain='inventory' AND r.status='valid' ORDER BY b.factory,r.material LIMIT 200", args)
    issues_from = """ FROM quality_issues i JOIN source_records r ON r.id=i.record_id
        JOIN import_batches b ON b.id=i.batch_id WHERE b.workspace_id=? AND (?='all' OR b.factory=?)
        AND (?='' OR r.material=?) AND i.status='open' """
    if plan['intent']=='inventory':
        issues_from += " AND b.domain='inventory' "
    issue_count = data.one("SELECT COUNT(*) AS n" + issues_from, args)["n"]
    issues = data.all("SELECT i.id,i.batch_id,i.record_id,i.rule,i.reason,i.impact,i.severity,r.material,r.quantity,r.unit,b.factory,b.filename" + issues_from + "ORDER BY i.id LIMIT 200", args)
    purchase_count = data.one("SELECT COUNT(*) AS n" + current + "AND c.domain='purchase'", args)["n"] if plan['intent']!='inventory' else None
    limitations = ["仅查询授权工作空间内仍为当前版本的数据；不是实时 ERP 库存。", "当前记录不等于扣除占用后的可用库存；不支持历史时点或供应商筛选。"]
    evidence = inventory if plan["intent"] != "quality" else issues
    result = {"scope": plan, "capturedAt": datetime.now(timezone.utc).isoformat(), "ruleVersion": "supply-readonly-v1",
              "inventoryCount": inventory_count, "issueCount": issue_count, "purchaseCount": purchase_count,
              "records": evidence, "truncated": (inventory_count if plan["intent"] != "quality" else issue_count) > 200,
              "limitations": limitations, "missing": [], "recommendations": []}
    if plan["supplier"] or (plan["period"] and plan["intent"] != "readiness"):
        # Never silently ignore an unsupported filter.
        result.update(records=[], summary="该范围尚不支持，未交付近似查询结果。", missing=["供应商维度或指定时间范围的数据模型"], recommendations=["清空不支持的条件并重新确认，或先补充业务数据模型。"])
        return "insufficient_data", result
    if plan["intent"] == "readiness":
        result["missing"] = ["按日期和物料拆分的生产需求", "库存占用与可用量口径", "采购未交数量、承诺到货日期和履约状态"]
        if not inventory_count:
            result["missing"].insert(0, "当前范围内可计算的库存记录")
        if issue_count:
            result["missing"].append(f"仍有 {issue_count} 项未解决的数据质量问题")
        result.update(summary="缺料分析条件尚不齐备，无法计算缺口或判断是否存在缺料风险。", recommendations=["补齐列出的业务数据并处理质量问题后，再进行缺料计算。"])
        return "insufficient_data", result
    if plan["intent"] == "quality":
        result.update(summary=f"当前账号和范围内有 {issue_count} 项未解决的质量问题。" if issue_count else "当前范围未查到未解决的质量问题；这不代表数据完整或不存在业务风险。", recommendations=["按来源批次核对问题，完成治理后重新分析。"])
        return ("completed" if issue_count else "no_data"), result
    for row in inventory:
        quantity = Decimal(row["normalized_quantity"])
        if not quantity.is_finite() or quantity < 0:
            raise ValueError("invalid normalized quantity")
        row["normalized_quantity"] = format(quantity, "f")
        row["normalized_unit"] = "件"
    result.update(summary=f"找到 {inventory_count} 条可计算的当前库存记录；各物料分别展示，不跨物料相加。" if inventory_count else "当前范围没有可计算的库存记录，不能将缺失数据视为零库存。", recommendations=["核对来源批次和实际库存时点；缺料判断还需要需求与到货数据。"])
    if issue_count:
        limitations.append(f"另有 {issue_count} 项未解决的质量问题，隔离记录未计入库存结果。")
    return ("completed" if inventory_count else "no_data"), result
