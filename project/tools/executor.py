"""Live authorization and tool lifecycle, with an injected audit store."""
import time
import uuid

from .investigation_sql import TABLES


class InvestigationHarness:
    def __init__(self, store, authorize):
        self.store, self.authorize = store, authorize

    def check(self, evidence=(), candidates=()):
        scope = self.authorize()
        for item in evidence:
            if item.get('table') == 'sql_query':
                previous = item['record']['scope']
                if any(previous.get(k) != scope.get(k) for k in ('workspace', 'scenario', 'asOf')):
                    raise PermissionError('调查范围已变化。')
                for domain, factories in previous['domains'].items():
                    if not set(factories).issubset(scope['domains'].get(domain, [])):
                        raise PermissionError('证据权限已缩小。')
            elif item.get('table') in TABLES:
                domain = TABLES[item['table']][0]
                # Cached evidence can have indirect ownership; require the entire
                # captured factory scope rather than guessing ownership from a row.
                if not set(scope['factories']).issubset(scope['domains'].get(domain, [])):
                    raise PermissionError('缓存证据的数据域权限不足。')
        for plan in candidates:
            factories = {a['factory'] for a in plan.get('actions', [])}
            factories.update(a['fromFactory'] for a in plan.get('actions', []) if a['kind'] == 'transfer')
            for domain in ('inventory', 'purchase', 'production', 'quality', 'rules'):
                if not factories.issubset(scope['domains'].get(domain, [])):
                    raise PermissionError('方案来源工厂或数据域权限不足。')
        return scope

    def start(self, tool, args):
        identifier = uuid.uuid4().hex
        self.store.start(identifier, tool, args)
        return identifier, time.monotonic()

    def finish(self, ticket, result, audit):
        identifier, started = ticket
        audit['callId'] = identifier
        audit['elapsedMs'] = round((time.monotonic() - started) * 1000)
        self.store.finish(identifier, result, audit)

    def set_scope(self, ticket, scope):
        self.store.set_scope(ticket[0], scope)

    def records(self):
        return self.store.records()
