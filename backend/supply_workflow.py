"""Backend resources and compatibility facade for investigation workflows."""
from pathlib import Path
import sqlite3

from langgraph.checkpoint.sqlite import SqliteSaver

from project.workflows.investigation import InvestigationWorkflow, State
from .investigation_service import InvestigationService


class SupplyWorkflow(InvestigationService):
    def __init__(self, auth, data, monitor, env):
        super().__init__(auth, data, monitor, env, self.schedule)
        db_path = data.one('PRAGMA database_list')['file']
        checkpoint_path = Path(db_path).with_name('supply-graph.sqlite')
        self.checkpoint_db = sqlite3.connect(checkpoint_path, check_same_thread=False)
        self.checkpointer = SqliteSaver(self.checkpoint_db)
        self._workflow = InvestigationWorkflow(self, self.checkpointer)
        self.graph, self.executor = self._workflow.graph, self._workflow.executor

    def schedule(self, run_id):
        return self.executor.submit(self.process, run_id)

    def process(self, run_id):
        return self._workflow.process(run_id)

    def recover(self):
        return self._workflow.recover()

    def close(self):
        self._workflow.close()
        self.checkpoint_db.close()

    def collect(self, state):
        return self._workflow.collect(state)

    def investigate(self, state):
        return self._workflow.investigate(state)

    def plans(self, state):
        return self._workflow.plans(state)

    def approval(self, state):
        return self._workflow.approval(state)

    def finish(self, state):
        return self._workflow.finish(state)
