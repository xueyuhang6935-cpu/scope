"""Investigation tool routing; authorization and evidence delivery preserve ordering."""
import json

from .investigation_sql import TABLES, validate_sql


class InvestigationTools:
    def __init__(self, bundle, evidence, report, query_tools=None, harness=None, evidence_reader=None):
        self.bundle, self.report = bundle, report
        self.query_tools, self.harness, self.evidence_reader = query_tools, harness, evidence_reader
        self.submitted = {}
        self.schema_tables_seen = set()
        self.tools = {
            'get_inventory': [e for e in evidence if e['table'] in ('inventory_snapshots', 'inventory_reservations', 'quality_holds')],
            'get_production_plan': [e for e in evidence if e['table'] in ('production_plan_lines', 'bom_headers', 'bom_lines')],
            'get_supply_evidence': [e for e in evidence if e['table'] in ('supplier_commitments', 'purchase_order_lines', 'shipment_lines', 'receipt_lines', 'quality_releases', 'line_deliveries', 'transfer_routes', 'expedite_options', 'approval_policies')],
            'get_candidate_plans': [{'id': f'candidate:{i}', 'table': 'candidate_plan', 'record': p,
                                     'provenance': {'sourceType': 'deterministic_calculation'}}
                                    for i, p in enumerate(bundle['candidates'])],
        }

    def call(self, call, args, feedback, repair):
        """Deliver one external tool result; True requests an access-invalid stop."""
        name = call['function']['name']
        bundle, report, tools = self.bundle, self.report, self.tools
        query_tools, harness, evidence_reader = self.query_tools, self.harness, self.evidence_reader
        submitted, schema_tables_seen = self.submitted, self.schema_tables_seen
        if name == 'get_evidence':
            if set(args) != {'evidenceId'} or not isinstance(args['evidenceId'], str) or not 1 <= len(args['evidenceId']) <= 200:
                repair(call, '需要1至200字符的 evidenceId。')
                return
            try:
                result = evidence_reader(args['evidenceId'])
            except ValueError as exc:
                repair(call, str(exc))
                return
            if harness:
                harness.check([result], [])
            feedback(call, result, {'tool': name, 'status': 'completed', 'evidenceId': result['id']})
            submitted[result['id']] = result
            if not any(e['id'] == result['id'] for e in report['evidence']):
                report['evidence'].append(result)
            return
        if query_tools and name in ('get_database_schema', 'execute_sql'):
            if name == 'execute_sql':
                try:
                    refs = set()
                    validate_sql(args.get('sql'), args.get('parameters'), TABLES, referenced_tables=refs)
                    if refs - schema_tables_seen:
                        raise ValueError('先读取相关表字段：' + '、'.join(sorted(refs - schema_tables_seen)))
                    if not refs:
                        raise ValueError('调查SQL必须读取至少一张授权业务表；常量查询不能作为业务证据。')
                except ValueError as exc:
                    repair(call, str(exc))
                    return
            result, audit = query_tools.call(name, args)
            if harness:
                harness.check(report['evidence'] + ([result] if result.get('id') else []), bundle['candidates'])
            feedback(call, result, audit)
            if name == 'get_database_schema' and not result.get('error'):
                schema_tables_seen.update(t for t, detail in result.get('tables', {}).items() if detail.get('columns'))
            if result.get('id'):
                submitted[result['id']] = result
                report['evidence'].append(result)
            if result.get('error', {}).get('retryable') is False:
                return True
            return
        if query_tools and name != 'get_candidate_plans':
            repair(call, '工具未注册，请按可用工具列表更新计划。')
            return
        if name not in tools or args != {}:
            repair(call, '工具不存在或参数无效；快照工具参数必须为空对象。')
            return
        value = tools[name]
        # Bounded excerpts; IDs are usable only after their tool is called.
        shown = [dict(row) for row in value[:20]]
        for row in shown:
            if row.get('table') == 'supplier_commitments':
                row['provenance'] = {'sourceType': 'supplier_statement', 'verified': False}
        while shown and len(json.dumps(shown, ensure_ascii=False)) > 8000:
            shown = shown[:-1]
        for row in shown:
            if 'id' in row:
                row['provenance'] = {**row.get('provenance', {}), 'truncated': len(value) > len(shown)}
                submitted[row['id']] = row
                existing = next((i for i, e in enumerate(report['evidence']) if e['id'] == row['id']), None)
                if existing is None:
                    report['evidence'].append(row)
                else:
                    report['evidence'][existing] = row
        audit = {'tool': name, 'status': 'completed', 'items': len(shown), 'truncated': len(value) > len(shown)}
        feedback(call, {'items': shown, 'truncated': audit['truncated']}, audit)
