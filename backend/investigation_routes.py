"""HTTP adapters for investigation, plans and approvals."""
from fastapi import APIRouter, Request


def routes(service):
    router = APIRouter(prefix='/api/supply', tags=['调查、方案与审批'])

    @router.get('/investigations/{run_id}')
    def read_run(request: Request, run_id: str):
        user = service.monitor.authorize(request)
        return service.read_run(user, run_id, service.permissions.workspace(user))

    @router.get('/business-access')
    def access_info(request: Request, eventId: str = ''):
        user = service.monitor.authorize(request)
        return service.access_info(user, eventId)

    @router.post('/business-access')
    def set_member(request: Request):
        user = service.monitor.authorize(request)
        return service.set_member(user, request.state.body)

    @router.post('/risks/{event_id}/investigate')
    def start(request: Request, event_id: str):
        user = service.monitor.authorize(request)
        return service.start_investigation(user, event_id, request.state.body.get('requestId'))

    @router.get('/risks/{event_id}/investigations')
    def runs(request: Request, event_id: str):
        user = service.monitor.authorize(request)
        return service.runs(user, event_id)

    @router.get('/investigations/{run_id}/tool-calls')
    def tool_calls(request: Request, run_id: str):
        user = service.monitor.authorize(request)
        return service.tool_calls(user, run_id)

    @router.post('/investigations/{run_id}/cancel')
    def cancel(request: Request, run_id: str):
        user = service.monitor.authorize(request)
        return service.cancel(user, run_id)

    @router.get('/approvals')
    def approval_list(request: Request):
        user = service.monitor.authorize(request)
        return service.approval_list(user)

    @router.get('/business-risks')
    def shared_events(request: Request):
        user = service.monitor.authorize(request)
        return service.shared_events(user)

    @router.get('/plans/{plan_id}')
    def plan_detail(request: Request, plan_id: str):
        user = service.monitor.authorize(request)
        return service.plan_detail(user, plan_id)

    @router.post('/plans/{plan_id}/decision')
    def decide(request: Request, plan_id: str):
        user = service.monitor.authorize(request)
        return service.decide(user, plan_id, request.state.body)

    return router
