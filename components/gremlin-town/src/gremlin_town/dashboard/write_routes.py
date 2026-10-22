"""Authenticated action route; no general-purpose work/move endpoint."""
from __future__ import annotations

import json
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from gremlin_town.mcp.brain_runtime import BrainRuntimeError
from .writes import InvalidAction


def create_write_router(writer, *, settings, require_write_owner):
    router = APIRouter()

    @router.post("/api/actions")
    async def change(request: Request, owner=Depends(require_write_owner)):
        if not settings.enabled or not settings.writes:
            raise HTTPException(403, "Changes are switched off.")
        if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
            raise HTTPException(415, "Send the action as JSON.")
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > 16384:
                raise HTTPException(413, "This change is too long. Split it into smaller changes.")
        try:
            def unique_fields(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise InvalidAction("The action repeats a field. Refresh and try again.")
                    result[key] = value
                return result
            payload = json.loads(data, object_pairs_hook=unique_fields)
            result = await writer.change(payload, owner_id=owner.owner_id)
        except (UnicodeError, json.JSONDecodeError):
            raise HTTPException(400, "The action could not be read. Refresh and try again.") from None
        except InvalidAction as exc:
            return JSONResponse({"status": "refused", "text": str(exc)}, status_code=422)
        except BrainRuntimeError as exc:
            code = 409 if exc.code in {"brain.conflict", "brain.record.changed", "brain.record.exists"} else (
                403 if exc.code in {"ACCESS_DENIED", "brain.policy.excluded"} else 422)
            return JSONResponse({"status": "refused", "text": exc.message, "fix": exc.fix,
                                 "retryable": exc.retryable}, status_code=code)
        if result.get("status") not in {"accepted", "duplicate"} or result.get("remote_observed") is not True:
            return JSONResponse({"status": "unconfirmed", "text": "The save was not confirmed. Refresh before trying again."}, status_code=503)
        return {"status": result["status"], "text": result.get("say", "Saved"),
                "operation_id": payload["operation_id"], "schedule": result.get("schedule"),
                "calendar": result.get("calendar")}

    return router
