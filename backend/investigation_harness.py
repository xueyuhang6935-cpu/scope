"""Backend assembly and compatibility entry point for tool protection."""
from project.tools.executor import InvestigationHarness as ToolExecutor
from .investigation_audit import InvestigationAuditStore


class InvestigationHarness(ToolExecutor):
    def __init__(self, data, run_id, authorize):
        super().__init__(InvestigationAuditStore(data, run_id), authorize)

    @staticmethod
    def interrupt(data, run_id):
        InvestigationAuditStore.interrupt(data, run_id)
