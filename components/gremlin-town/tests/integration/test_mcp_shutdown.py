"""MCP shutdown must drain service work before closing the shared work store."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
import json
import sqlite3
import threading
import time

import httpx
import pytest

from gremlin_town.runtime import build_runtime
from gremlin_town.transport import FakeTransport
from tests.integration.test_mcp_runtime import mcp_config


WAIT_SECONDS = 5


def test_cancelled_http_request_drains_service_before_store_close(tmp_path, monkeypatch):
    config, _ = mcp_config(tmp_path)
    runtime = build_runtime(config, provider=FakeTransport(), start_worker=False)
    oauth = runtime.oauth_provider
    with oauth._db() as db:
        db.execute('INSERT INTO grants VALUES (?, ?, ?, ?, NULL)',
                   ('shutdown-test', oauth.config.clients[0].client_id, json.dumps(['gt:read']), time.time()))
        token = oauth._issue(db, 'shutdown-test', ['gt:read']).access_token

    release_service = threading.Event()
    service_finished = threading.Event()
    service_errors = []
    events = []
    original_list = runtime.service.requests_list
    original_close = runtime.store.close
    manager = runtime.mcp_server.session_manager
    original_run = manager.run

    @asynccontextmanager
    async def observed_manager():
        # Keep the real manager and task group. Releasing the service at the
        # exact manager-exit boundary makes ordering deterministic, not timed.
        async with original_run():
            try:
                yield
            finally:
                events.append('manager draining')
                release_service.set()

    def observed_close():
        events.append('store closed')
        original_close()

    monkeypatch.setattr(manager, 'run', observed_manager)
    monkeypatch.setattr(runtime.store, 'close', observed_close)

    async def run():
        loop = asyncio.get_running_loop()
        service_entered = asyncio.Event()

        def delayed_list(*args):
            loop.call_soon_threadsafe(service_entered.set)
            try:
                if not release_service.wait(timeout=WAIT_SECONDS):
                    raise AssertionError('MCP manager never began draining')
                # Execute the real service read against disposable SQLite after
                # shutdown starts. The store must still belong to the lifespan.
                result = original_list(*args)
                events.append('service completed')
                return result
            except Exception as error:
                service_errors.append(error)
                raise
            finally:
                service_finished.set()

        monkeypatch.setattr(runtime.service, 'requests_list', delayed_list)
        async with asyncio.timeout(WAIT_SECONDS * 2):
            async with runtime.app.router.lifespan_context(runtime.app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=runtime.mcp_app),
                    base_url=oauth.config.issuer_url,
                ) as client:
                    request = asyncio.create_task(client.post('/mcp',
                        headers={'Authorization': 'Bearer ' + token,
                                 'Accept': 'application/json, text/event-stream'},
                        json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                              'params': {'name': 'gt_requests_list', 'arguments': {'project': 'demo'}}}))
                    try:
                        await asyncio.wait_for(service_entered.wait(), timeout=WAIT_SECONDS)
                    finally:
                        request.cancel()
                        with suppress(asyncio.CancelledError):
                            await asyncio.wait_for(request, timeout=WAIT_SECONDS)
                    assert not service_finished.is_set(), 'service must remain in flight after HTTP cancellation'
                    events.append('HTTP cancelled')

        assert service_finished.is_set(), 'lifespan returned before the service call drained'
        assert not service_errors, (
            'in-flight MCP service failed during shutdown: '
            + '; '.join(f'{type(error).__name__}: {error}' for error in service_errors)
        )
        assert events == ['HTTP cancelled', 'manager draining', 'service completed', 'store closed']
        with pytest.raises(sqlite3.ProgrammingError, match='closed database'):
            runtime.store.fetchone('SELECT 1')

    try:
        asyncio.run(run())
    finally:
        release_service.set()
        original_close()


def test_manager_startup_failure_closes_work_store(tmp_path, monkeypatch):
    config, _ = mcp_config(tmp_path)
    runtime = build_runtime(config, provider=FakeTransport(), start_worker=False)
    manager = runtime.mcp_server.session_manager

    class ManagerStartupFailure(RuntimeError):
        pass

    @asynccontextmanager
    async def failed_manager():
        raise ManagerStartupFailure('synthetic manager startup failure')
        yield  # Make this a context manager that fails before entering.

    monkeypatch.setattr(manager, 'run', failed_manager)

    async def run():
        async with asyncio.timeout(WAIT_SECONDS):
            with pytest.raises(ManagerStartupFailure, match='synthetic manager startup failure'):
                async with runtime.app.router.lifespan_context(runtime.app):
                    pytest.fail('application accepted a failed MCP manager startup')
        with pytest.raises(sqlite3.ProgrammingError, match='closed database'):
            runtime.store.fetchone('SELECT 1')

    try:
        asyncio.run(run())
    finally:
        runtime.close()
