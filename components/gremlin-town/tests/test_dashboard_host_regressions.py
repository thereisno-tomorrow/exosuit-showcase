"""Independent D18 regression probes; invented data and offline fakes only."""
from dataclasses import replace
from types import SimpleNamespace

import anyio
import pytest
from fastapi.testclient import TestClient


def test_independent_dashboard_honors_published_gt_revocation(tmp_path):
    from tests.integration.test_runtime_flow import _write_runtime_config
    from tests.test_runtime_refresh import load, publish
    from tests.test_dashboard_host import fixture
    from tests.test_dashboard_auth import enrol, ORIGIN
    from tests.test_dashboard_reads import NOW
    from gremlin_town.runtime import build_runtime
    from gremlin_town.transport import FakeTransport
    from gremlin_town.dashboard.host import create_dashboard_app

    config, _ = _write_runtime_config(tmp_path)
    part = tmp_path / 'participants.toml'
    part.write_text(part.read_text().replace('public-test', 'private'))
    primary = build_runtime(config, provider=FakeTransport(), start_worker=False)
    secondary = build_runtime(config, provider=FakeTransport(), start_worker=False)
    settings, fake, _, own, _ = fixture(tmp_path)
    for name in ('brain_runtime', 'review_sources', 'orientation_context', 'orientation'):
        setattr(secondary, name, getattr(fake, name))
    secondary.review = SimpleNamespace(sender_participant='alice', delivery_participant='bob', project='demo')
    app = create_dashboard_app(replace(settings, ask_diablo=True), secondary, clock=lambda: NOW)
    try:
        with TestClient(app, base_url=ORIGIN) as client:
            enrol(app, client)
            ref = own.record.id
            detail = client.get('/api/work/' + ref).json()
            data = dict(operation_id='first-check', ref=ref, action='get_ready', text='Fictional question',
                        expected=dict(record_id=detail['record_id'], sha256=detail['record_sha256']))
            csrf = client.get('/auth/session').json()['csrf']
            headers = {'Origin': ORIGIN, 'X-CSRF-Token': csrf}
            assert client.post('/api/ask-diablo', json=data, headers=headers).status_code == 200
            policy = load(primary)
            next(row for row in policy['participant'] if row['id'] == 'alice')['enabled'] = False
            publish(primary, policy)
            assert primary.refresh_participants()['status'] == 'refreshed'
            data['operation_id'] = 'after-revocation'
            response = client.post('/api/ask-diablo', json=data, headers=headers)
            assert response.status_code == 403, 'dashboard accepted a new request after GT sender revocation: ' + str(response.status_code)
            assert client.get('/api/ask-diablo', params={'ref': ref}).status_code == 403
    finally:
        secondary.close()
        primary.close()


@pytest.mark.parametrize('lifespan,cleanup_failure', [(False, False), (True, False), (True, True)])
def test_independent_cli_closes_brain_owned_resources(tmp_path, monkeypatch, capsys, lifespan, cleanup_failure):
    from tests.test_dashboard_auth_cli import config
    from tests.test_dashboard_host import fixture
    from gremlin_town.dashboard.run import main

    path = config(tmp_path)
    with path.open('a') as stream:
        stream.write('reads = true\n')
    _, runtime, *_ = fixture(tmp_path)
    closed = []

    async def close_brain():
        closed.append('brain')
        if cleanup_failure:
            raise RuntimeError('fictional-private-cleanup-error')

    runtime.brain_runtime.aclose = close_brain
    runtime.close = lambda: closed.append('outer')
    monkeypatch.setattr('gremlin_town.runtime.build_runtime', lambda *a, **kw: runtime)
    def serve(app, **options):
        if lifespan:
            async def run():
                async with app.router.lifespan_context(app):
                    closed.append('serving')
            anyio.run(run)
    monkeypatch.setattr('gremlin_town.dashboard.run.uvicorn.run', serve)
    assert main(['serve', '--config', str(path)]) == (2 if cleanup_failure else 0)
    if lifespan:
        assert closed.pop(0) == 'serving'
    assert closed == ['brain', 'outer'], 'Brain-managed resources were never closed: ' + repr(closed)
    output = capsys.readouterr()
    assert 'fictional-private-cleanup-error' not in output.err + output.out
    assert ('Dashboard command failed' in output.err) is cleanup_failure
