"""T12：按任务家族累计，计数与限额均由宿主持久化。"""
import json
import time

from project.agents.logistics_protocol import ProtocolError


LIMITS = {'model_calls': 12, 'tool_calls': 16, 'plan_revisions': 3, 'model_tokens': 24000, 'active_seconds': 120}
REASONS = {'model_calls': 'model_call_limit', 'tool_calls': 'tool_call_limit',
           'plan_revisions': 'revision_limit', 'model_tokens': 'token_limit', 'active_seconds': 'active_time_limit'}


class BudgetService:
    def __init__(self, runs):
        self.runs, self.data = runs, runs.data

    def read(self, run_id):
        with self.data.lock:
            root = self.runs.saved(run_id)['family_id']
            rows = self.data.all('SELECT * FROM logistics_runs WHERE family_id=?', (root,))
            result = {'family_id': root, **dict.fromkeys(LIMITS, 0), 'model_usage_unknown': False,
                      'unknown_usage_calls': 0, 'model_transport_failed': False}
            for counter, default in LIMITS.items():
                result['max_'+counter] = min(json.loads(r['budget_json']).get('max_'+counter, default) for r in rows)
            for row in rows:
                value = json.loads(row['budget_json'])
                for counter in LIMITS.keys()-{'active_seconds'}:
                    result[counter] += value.get(counter, 0)
                live = max(0, min(time.time(), row['lease_until'])-row['accounted_at']) if row['status'] == 'running' else 0
                result['active_seconds'] += row['active_seconds']+live+value.get('standalone_seconds', 0)
                result['unknown_usage_calls'] += value.get('unknown_usage_calls', int(value.get('model_usage_unknown', False)))
                result['model_usage_unknown'] |= bool(value.get('model_usage_unknown'))
                result['model_transport_failed'] |= bool(value.get('model_transport_failed'))
            return result

    def check(self, run_id, resource=None, *, delivery=False):
        value = self.read(run_id)
        # 调用/工具额度耗尽后只允许程序收尾；最后一次合法调用可交付，不能再发新请求。
        counters = () if delivery else ('model_calls', 'tool_calls')
        for key in dict.fromkeys(('active_seconds', 'model_tokens', *counters, resource)):
            if key and value[key] >= value['max_'+key]:
                raise ProtocolError(REASONS[key])
        if resource == 'model_calls' and value['model_transport_failed']:
            raise ProtocolError('model_transport_failed')
        return value

    def reserve(self, run_id, counter):
        with self.runs.transaction():
            self.check(run_id, counter)
            row = self.runs.saved(run_id)
            value = json.loads(row['budget_json'])
            value[counter] = value.get(counter, 0)+1
            self.data.db.execute('UPDATE logistics_runs SET budget_json=? WHERE run_id=?', (json.dumps(value), run_id))

    def usage(self, run_id, tokens, seconds, *, standalone=False):
        with self.runs.transaction():
            value = json.loads(self.runs.saved(run_id)['budget_json'])
            if tokens is None:
                value['model_usage_unknown'] = True
                value['unknown_usage_calls'] = value.get('unknown_usage_calls', 0)+1
            else:
                value['model_tokens'] = value.get('model_tokens', 0)+tokens
            value['model_seconds'] = value.get('model_seconds', 0)+seconds
            if standalone:
                value['standalone_seconds'] = value.get('standalone_seconds', 0)+seconds
            self.data.db.execute('UPDATE logistics_runs SET budget_json=? WHERE run_id=?', (json.dumps(value), run_id))
