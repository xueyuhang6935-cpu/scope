"""Durable investigation transitions and recovery, independent of backend storage."""
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import TypedDict

from langgraph.graph import StateGraph, START, END
from langgraph.types import interrupt, Command


class State(TypedDict, total=False):
    run_id: str
    bundle: dict
    evidence: list
    report: dict
    plan_ids: list
    decision: dict
    blocked: bool
    query_context: dict


class InvestigationWorkflow:
    def __init__(self, service, checkpointer):
        self.service, self.lock = service, service.lock
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='supply-graph')
        graph = StateGraph(State)
        for name, fn in [('collect', self.collect), ('investigate', self.investigate), ('plans', self.plans), ('approval', self.approval), ('finish', self.finish)]:
            graph.add_node(name, fn)
        graph.add_edge(START, 'collect')
        graph.add_edge('collect', 'investigate')
        graph.add_edge('investigate', 'plans')
        graph.add_conditional_edges('plans', lambda s: END if s['blocked'] else 'approval')
        graph.add_edge('approval', 'finish')
        graph.add_edge('finish', END)
        self.graph = graph.compile(checkpointer=checkpointer)

    def collect(self, state):
        self.service.record_stage(state, 'collect')
        return self.service.collect_data(state)

    def investigate(self, state):
        self.service.record_stage(state, 'investigate')
        return self.service.run_investigation(state)

    def plans(self, state):
        self.service.record_stage(state, 'plans')
        return self.service.save_plans(state)

    def finish(self, state):
        self.service.record_stage(state, 'finish')
        return self.service.finish_approval(state)

    def approval(self, state):
        self.service.record_stage(state, 'approval')
        decision = interrupt({'runId': state['run_id'], 'planIds': state['plan_ids'], 'reason': '等待有权限的另一位用户审批'})
        return {'decision': decision}

    def process(self, run_id):
        # ponytail: bounded demo uses one graph worker and a local durable saver;
        # migrate the saver/leases together before deploying multiple app processes.
        with self.lock:
            try:
                prepared = self.service.prepare_run(run_id)
                if prepared is None:
                    return
                run, approval = prepared
                if run['status'] in ('replan_required', 'replanned'):
                    child_id = self.service.replan(run_id)
                    if child_id:
                        self.process(child_id)
                    return
                config = {'configurable': {'thread_id': run_id}}
                saved = self.graph.get_state(config)
                if saved.values:
                    # A crash can occur after the plans checkpoint but before the
                    # approval node actually calls interrupt. Establish it first.
                    if saved.next == ('approval',) and not saved.interrupts:
                        self.graph.invoke(None, config, durability='sync')
                        saved = self.graph.get_state(config)
                    payload = Command(resume={'approvalId': approval['id']}) if approval and saved.next == ('approval',) else None
                    if saved.next == ('approval',) and not approval:
                        return
                else:
                    payload = {'run_id': run_id}
                self.graph.invoke(payload, config, durability='sync')
                child_id = self.service.replan(run_id)
                if child_id:
                    self.process(child_id)
            except Exception as exc:
                logging.getLogger(__name__).exception('Investigation failed: %s', run_id)
                self.service.interrupt_calls(run_id)
                try:
                    self.service.fail_run(run_id, exc)
                except Exception:
                    logging.getLogger(__name__).error('Investigation status could not be saved: %s', run_id)

    def recover(self):
        if not self.service.has_recovery_config():
            return
        try:
            rows = self.service.recoverable_runs()
            for row in rows:
                self.service.interrupt_calls(row['id'], ensure_table=True)
                self.executor.submit(self.process, row['id'])
        except Exception:
            # No source DB must not prevent login/file import startup.
            pass

    def close(self):
        self.executor.shutdown(wait=True)
