"""T11调查图；节点能力和磁盘检查点由宿主注入。"""
from typing import TypedDict
from langgraph.graph import StateGraph, START, END


class State(TypedDict, total=False):
    run_id: str
    question: str
    plan: dict
    plans: list
    step_states: dict
    evidence_ids: list
    trace: list
    observations: list
    selected: dict
    selected_origin: str
    selected_binding: dict | None
    turn: int
    revisions: int
    report: dict
    template_report_id: str
    revision_history: list
    pending_triggers: list
    handled_triggers: list
    constraint_version: int
    no_progress: int
    unfinished_steps: list
    protocol_error: str
    budget_report_only: bool
    stop_reason: str
    route: str


NODES = ('context', 'plan', 'select', 'execute', 'observe', 'decide', 'report', 'finish')


def build_graph(host, checkpointer):
    graph = StateGraph(State)
    for name in NODES:
        graph.add_node(name, lambda state, name=name: host.node(name, state))
    graph.add_edge(START, 'context')
    graph.add_edge('context', 'plan')
    graph.add_edge('plan', 'select')
    graph.add_conditional_edges('select', lambda s: s['route'], {'execute': 'execute', 'plan': 'plan', 'report': 'report', 'finish': 'finish'})
    graph.add_conditional_edges('execute', lambda s: 'plan' if s.get('route') == 'plan' else 'observe', {'plan': 'plan', 'observe': 'observe'})
    graph.add_edge('observe', 'decide')
    graph.add_conditional_edges('decide', lambda s: s['route'], {'plan': 'plan', 'select': 'select', 'report': 'report', 'finish': 'finish'})
    graph.add_conditional_edges('report', lambda s: 'plan' if s.get('route') == 'plan' else 'finish', {'plan': 'plan', 'finish': 'finish'})
    graph.add_edge('finish', END)
    # 每次执行一个安全节点后同步落盘，继续调度由已有单运行执行器负责。
    return graph.compile(checkpointer=checkpointer, interrupt_after=list(NODES))
