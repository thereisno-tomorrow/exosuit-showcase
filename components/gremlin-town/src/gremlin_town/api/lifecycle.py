"""Private operator controls, never a public registry ingestion endpoint."""
import asyncio

from fastapi import Request
from fastapi.responses import JSONResponse

from gremlin_town.api.auth import AuthenticationError, get_actor
from gremlin_town.domain import GremlinTownError
from gremlin_town.lifecycle import GrokLifecycle


def install_lifecycle_routes(runtime, participant_path):
    # The real native registry/provisioner is intentionally unbound until a
    # supported authoritative workflow is available. No directory-scan fallback.
    from gremlin_town.lifecycle_publish import FilePolicyPublisher
    runtime.lifecycle = GrokLifecycle(runtime.store, publisher=FilePolicyPublisher(runtime, participant_path))

    async def run(request, action):
        try:
            actor = get_actor(request)
            runtime.policy.require_operator(actor)
        except (AuthenticationError, GremlinTownError):
            return JSONResponse({"error": {"code": "ACCESS_DENIED", "message": "operator access required"}}, status_code=403)
        if request.method == "POST" and await request.body():
            return JSONResponse({"error": {"code": "INVALID_INPUT", "message": "no caller-supplied registry or policy accepted"}}, status_code=422)
        def execute():
            runtime.policy.require_operator(actor)
            return action()
        try:
            return await asyncio.to_thread(execute)
        except Exception:
            return JSONResponse({"error": {"code": "LIFECYCLE_FAILED", "message": "configuration not published; inspect operator status"}}, status_code=409)

    @runtime.app.post("/v1/operator/participants/refresh", include_in_schema=False)
    async def refresh(request: Request):
        return await run(request, runtime.refresh_participants)

    @runtime.app.get("/v1/operator/grok-lifecycle", include_in_schema=False)
    async def status(request: Request):
        return await run(request, runtime.lifecycle.status)

    @runtime.app.post("/v1/operator/grok-lifecycle/reconcile", include_in_schema=False)
    async def reconcile(request: Request):
        return await run(request, runtime.lifecycle.reconcile)
