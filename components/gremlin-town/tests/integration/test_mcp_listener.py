from __future__ import annotations

import threading
import time

import httpx
import pytest
import uvicorn

from gremlin_town.entrypoint import ListenerDispatch, _listener
from gremlin_town.runtime import build_runtime
from gremlin_town.transport import FakeTransport
from tests.integration.test_dual_listener import OPERATIONS
from tests.integration.test_mcp_runtime import mcp_config


def test_three_real_sockets_preserve_private_and_existing_client_boundaries(tmp_path):
    config, tokens = mcp_config(tmp_path)
    with config.open("a", encoding="utf-8") as handle:
        handle.write('\n[public_gateway]\nhost = "127.0.0.1"\nport = 8766\nactor_id = "bob"\nallowed_operations = ' + repr(OPERATIONS).replace("'", '"') + "\n")
    runtime = build_runtime(config, provider=FakeTransport(), start_worker=False)
    sockets = [_listener("127.0.0.1", 0) for _ in range(3)]
    private_port, legacy_port, mcp_port = [item.getsockname()[1] for item in sockets]
    dispatch = ListenerDispatch(runtime.app, runtime.public_app,
                                private_port=private_port, public_port=legacy_port,
                                mcp_app=runtime.mcp_app, mcp_port=mcp_port)
    server = uvicorn.Server(uvicorn.Config(dispatch, workers=1, access_log=False,
                                          log_level="error", lifespan="on", proxy_headers=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": sockets}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        with httpx.Client(timeout=5, trust_env=False) as client:
            def url(port, path):
                return f"http://127.0.0.1:{port}{path}"

            assert client.get(url(private_port, "/health/live")).status_code == 200
            for path in ("/health/live", "/login", "/overview", "/mcp/setup", "/mcp/connections", "/v1/participants"):
                response = client.get(url(mcp_port, path), headers={
                    "Host": "gt.example.test", "X-Forwarded-Host": "private.example.test",
                    "X-Forwarded-Port": str(private_port), "Forwarded": f"host=private.example.test;proto=https",
                    "Authorization": "Bearer " + tokens["operator"],
                })
                assert response.status_code in (401, 404, 405), (path, response.text)
            assert client.get(url(mcp_port, "/mcp/consent"), headers={"Host": "gt.example.test"}).status_code == 400
            assert client.get(url(mcp_port, "/.well-known/oauth-authorization-server"), headers={"Host": "gt.example.test"}).status_code == 200
            denied = client.post(url(mcp_port, "/mcp"), headers={"Host": "gt.example.test", "Accept": "application/json, text/event-stream"}, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            assert denied.status_code == 401
            assert client.get(url(mcp_port, "/.well-known/oauth-authorization-server"), headers={"Host": "private.example.test"}).status_code == 421
            assert client.get(url(legacy_port, "/health/live")).status_code == 404
            existing = client.get(url(legacy_port, "/v1/participants?project=demo"), headers={"Authorization": "Bearer " + tokens["bob"]})
            assert existing.status_code == 200
            assert client.get(url(private_port, "/v1/participants?project=demo"), headers={"Authorization": "Bearer " + tokens["alice"]}).status_code == 200
            assert runtime.worker_task is None
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        for listener in sockets:
            listener.close()
        assert not thread.is_alive()


@pytest.mark.parametrize("kwargs", [
    {"private_port": 1, "public_port": 1, "public_app": object()},
    {"private_port": 1, "mcp_port": 1, "mcp_app": object()},
    {"private_port": 1, "mcp_port": 2},
])
def test_invalid_dispatch_configuration_fails_before_serving(kwargs):
    with pytest.raises(ValueError):
        ListenerDispatch(object(), **kwargs)
