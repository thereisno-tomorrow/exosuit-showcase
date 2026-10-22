"""Narrow dashboard Ask Diablo endpoints behind owner auth and feature gates."""
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from gremlin_town.domain import GremlinTownError
from gremlin_town.mcp.brain_runtime import BrainRuntimeError
from .ask import AskRefused

logger = logging.getLogger(__name__)


def create_ask_router(ask, *, settings, require_owner, require_write_owner):
    router = APIRouter()

    def enabled():
        if not (settings.enabled and settings.reads and settings.ask_diablo):
            raise HTTPException(403, "Ask Diablo is switched off.")

    async def perform(method, *args, owner_id):
        try:
            return await method(*args, owner_id=owner_id)
        except AskRefused as error:
            return JSONResponse({"text": str(error), "retryable": False}, status_code=error.status)
        except GremlinTownError:
            return JSONResponse({"text": "This request is not available. Refresh and try again.",
                                 "retryable": False}, status_code=403)
        except BrainRuntimeError as error:
            if error.code in {"ACCESS_DENIED", "brain.policy.excluded"}:
                return JSONResponse({"text": "This thing is not available.", "retryable": False}, status_code=403)
            logger.error("Dashboard Ask Diablo could not read the current thing; operator inspection required")
            return JSONResponse({"text": "Couldn't check this thing. Try again later.",
                                 "retryable": True}, status_code=503)
        except Exception as error:
            logger.error("Dashboard Ask Diablo failed (%s); operator inspection required", type(error).__name__)
            return JSONResponse({"text": "Couldn't confirm the request. Check again with the same request.",
                                 "retryable": True}, status_code=503)

    @router.post("/api/ask-diablo")
    async def send(request: Request, owner=Depends(require_write_owner)):
        enabled()
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise HTTPException(415, "Send the request as JSON.")
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 24576:
                raise HTTPException(413, "This request is too long.")
        def unique(pairs):
            output = {}
            for key, value in pairs:
                if key in output:
                    raise ValueError("duplicate field")
                output[key] = value
            return output
        try:
            payload = json.loads(raw, object_pairs_hook=unique)
        except (UnicodeError, ValueError):
            raise HTTPException(400, "The request could not be read.") from None
        return await perform(ask.send, payload, owner_id=owner.owner_id)

    @router.get("/api/ask-diablo")
    async def read(request: Request, owner=Depends(require_owner)):
        enabled()
        if list(request.query_params.keys()) != ["ref"] or len(request.query_params.getlist("ref")) != 1:
            raise HTTPException(400, "Open the thing whose reply you want to see.")
        return await perform(ask.read, request.query_params["ref"], owner_id=owner.owner_id)

    return router
