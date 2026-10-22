"""Bearer-only HTTP transport over the existing Brain tool/runtime boundary."""
from contextvars import ContextVar
import json
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import ValidationError

from gremlin_town.api.auth import ActorAuthenticator, AuthenticationError
from gremlin_town.domain.errors import GremlinTownError
from gremlin_town.mcp.brain_tools import build_brain_tools, _mapped_error
from gremlin_town.mcp.brain_assembly import BrainAssemblyError, load_owner_policy
from gremlin_town.mcp.brain_usage import BrainUsageEvent
from gremlin_town.mcp.host_identity import bind_write_attribution
from gremlin_town.mcp.brain_runtime import (BrainCaller, BrainOperation, BrainRuntimeError, OwnerSelector,
    work_caller_allowed, work_check_only)

MAX_BODY_BYTES = 100_000
# Operations that need only bc:read. Step F's two asks (`orient`, `next`) write nothing but
# why-log rows, so they need no more than their MCP tools do; `next.respond` starts a note.
# `agenda` and `weekly` write nothing at all (ROA-86); `project.save` saves, so it needs bc:write.
_READ_ONLY_OPERATIONS = frozenset({
    "context.retrieve", "memory.read", "memory.search", "memory.list", "continuity.recover", "continuity.list",
    "orient", "next", "agenda", "weekly",
})

def _error(code, message, status, retryable=False, *, field="arguments", fix="correct the named field and resend"):
    return JSONResponse({"error": {"code": code, "message": message, "retryable": retryable,
        "field": field, "fix": fix}}, status_code=status)

def install_brain_routes(app: FastAPI, runtime, *, door_enabled: bool = False) -> None:
    """Install on the existing private GT service, sharing its token resolver."""
    identity = ContextVar("brain_http_identity")
    scopes = ContextVar("brain_http_scopes")
    # Reuse this listener's resolver, including the public gateway's explicit
    # participant restriction. Never widen that listener to all private tokens.
    authenticator = app.state.actor_authenticator
    usage_log = getattr(runtime, "brain_usage", None)

    async def authorize():
        active = runtime.settings
        if active.operator_only or not active.normal_clients_allowed or not active.mcp_allowed:
            raise PermissionError("Brain unavailable")
        actor = identity.get()
        participant = runtime.policy.authenticate(actor)
        if actor.is_operator or participant.is_operator:
            raise PermissionError("Operator access is not Brain access")
        if "bc:read" not in scopes.get():
            raise PermissionError("Explicit Brain scope required")

    orientation_tools = ()
    if getattr(runtime, "orientation", None) is not None and runtime.orientation.enabled:
        from gremlin_town.mcp.brain_tools import build_orientation_tools

        orientation_tools = build_orientation_tools(
            runtime.orientation_context, identity.get,
            scopes_provider=scopes.get, authorize=authorize,
        )
    # bc_orient, bc_next and bc_next_respond ("orient", "next", "next.respond") exist only while
    # [orientation] is on (step F). bc_calendar_remove ("calendar.remove") exists only while
    # calendar entries are on (N8):
    # [review] enabled and a Google credential carrying calendar.events.
    tools = {tool.name.removeprefix("bc_").replace("_", "."): tool
        for tool in [*build_brain_tools(runtime.brain_runtime, identity.get,
            scopes_provider=scopes.get, authorize=authorize,
            usage_log=usage_log, transport="http",
            calendar_removal=getattr(runtime, "calendar_writer", None) is not None,
            door=door_enabled),
            *orientation_tools]}

    @app.post("/v1/brain/{operation}", include_in_schema=False)
    async def brain_operation(operation: str, request: Request):
        started = time.monotonic()
        response = await _brain_operation(operation, request)
        # Calls that reached a tool were logged there; log only earlier refusals here.
        if usage_log is not None and not getattr(response, "brain_tool_reached", False):
            try:
                data = json.loads(response.body)
                code = data.get("error", {}).get("code")
                outcome = data.get("status", "error")
            except (KeyError, TypeError, ValueError):
                code = None
                outcome = "error"
            actor = getattr(request.state, "brain_actor_id", None) or "unauthenticated"
            usage_log.record(BrainUsageEvent(
                actor=actor, transport="http", operation=operation, outcome=outcome,
                error_code=code, duration_ms=round((time.monotonic() - started) * 1000)))
        return response

    async def _brain_operation(operation: str, request: Request):
        try:
            actor = authenticator(request)  # Deliberately no browser-session fallback.
        except AuthenticationError:
            return _error("UNAUTHORIZED", "bearer authentication required", 401)
        request.state.brain_actor_id = actor.actor_id
        actor_token = identity.set(actor)
        scope_token = scopes.set(frozenset())
        check_token = work_check_only.set(False)
        try:
            policy = load_owner_policy(runtime.brain.policy_file)
            scopes.set(policy.subject_scopes.get(actor.actor_id, frozenset()))
            await authorize()
            door = getattr(runtime, "door_participant", "hermes-wa")
            if operation == "work" and (not door_enabled or not work_caller_allowed(actor.actor_id, door_participant=door)):
                return _error("ACCESS_DENIED", "This caller cannot change Nic's work", 403, field="caller",
                    fix="save it as a new thing with memory.save; Nic's morning review merges it")
            if operation == "calendar.remove" and (not door_enabled or actor.actor_id != door):
                return _error("ACCESS_DENIED", "Only the door participant may remove a calendar entry", 403,
                    field="caller", fix="remove it through Diablo")
            tool = tools.get(operation)
            if tool is None:
                return _error("brain.invalid_argument", "unsupported Brain operation", 422, field="operation",
                    fix="use bc_work for Nic's work, or memory.save for a new capture" if operation == "project.save"
                    else "use an operation in this listener's advertised tool list")
            if operation not in _READ_ONLY_OPERATIONS and "bc:write" not in scopes.get():
                raise PermissionError("Explicit write scope required")
            if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
                return _error("brain.invalid_argument", "JSON body required", 422)
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > MAX_BODY_BYTES:
                    return _error("brain.invalid_argument", "request too large", 413)
            arguments = json.loads(body)
            if not isinstance(arguments, dict):
                return _error("brain.invalid_argument", "arguments must be an object", 422)
            check_only = arguments.pop("check_only", False)
            if not isinstance(check_only, bool):
                return _error("brain.invalid_argument", "check_only must be a boolean", 422, field="check_only",
                    fix="send true or false")
            if check_only and operation != "work":
                return _error("brain.invalid_argument", "check_only is a door body flag", 422, field="check_only",
                    fix="use check_only with work")
            marker = getattr(getattr(runtime.brain_runtime, "_memory", None), "replay_marker", None)
            checking = check_only or bool(actor.actor_id == door and marker is not None and marker.is_file())
            work_check_only.set(checking)
            tool.fn_metadata.arg_model.model_validate(arguments)
            if checking and operation not in _READ_ONLY_OPERATIONS and operation not in {"work", "memory.save", "memory.change"}:
                if operation == "calendar.remove" or operation in {op.value for op in BrainOperation}:
                    project = arguments.get("project_id")
                    selector = OwnerSelector(arguments.get("scope") or ("project" if project else "personal"), project)
                    await runtime.brain_runtime._authorize(BrainCaller(actor.actor_id, scopes.get()),
                        BrainOperation.MEMORY_CHANGE if operation == "calendar.remove" else BrainOperation(operation),
                        selector, "write")
                return JSONResponse({"status": "validated", "remote_observed": False,
                    "say": "Checked only; nothing was saved."})
            with bind_write_attribution(actor.actor_id):
                result = await tool.run(arguments)
            data = result.structuredContent
            if data is None:
                raise RuntimeError("Brain result missing structured content")
            error = data.get("error")
            if error:
                code = error["code"]
                status = 403 if code == "ACCESS_DENIED" else 409 if code in {"brain.record.exists", "brain.record.changed", "brain.conflict"} else 503 if error.get("retryable") or code == "brain.capability.unavailable" else 500 if code == "INTERNAL_ERROR" else 422
                response = JSONResponse(data, status_code=status)
            else:
                response = JSONResponse(data)
            response.brain_tool_reached = True
            return response
        except BrainAssemblyError:
            return _error("brain.capability.unavailable", "Brain policy unavailable", 503, True)
        except BrainRuntimeError as exc:
            data = _mapped_error(exc).structuredContent
            error = data["error"]
            return _error(error["code"], error["message"], 403 if error["code"] == "ACCESS_DENIED" else 422,
                error.get("retryable", False), field=error["field"], fix=error["fix"])
        except (PermissionError, GremlinTownError):
            return _error("ACCESS_DENIED", "resource unavailable or access denied", 403)
        except ValidationError as exc:
            first = exc.errors()[0]
            field = ".".join(str(part) for part in first["loc"])
            return _error("brain.invalid_argument", first["msg"], 422, field=field,
                fix=f"correct {field}: {first['msg']}")
        except (ValueError, ToolError):
            return _error("brain.invalid_argument", "invalid Brain arguments", 422)
        finally:
            scopes.reset(scope_token)
            identity.reset(actor_token)
            work_check_only.reset(check_token)
