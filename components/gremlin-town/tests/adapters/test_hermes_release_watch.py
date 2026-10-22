import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('release_watch_test', Path(__file__).resolve().parents[2] / 'adapters/hermes/upgrade/release_watch.py')
watch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(watch)

def candidate():
    return {'id': 123, 'name': 'Hermes Agent v0.22.0 (v2026.10.19)', 'html_url': 'https://github.com/NousResearch/hermes-agent/releases/tag/v2026.10.19'}

def setup(tmp_path, monkeypatch, due='2026-01-01T01:00:00+00:00'):
    (tmp_path / 'config.json').write_text(json.dumps({'target': 'discord:123456789012345678', 'delivery_enabled': True}))
    (tmp_path / 'state.json').write_text(json.dumps({'next_due_at': due, 'notifications': {}}))
    monkeypatch.setattr(watch, 'release', candidate)
    monkeypatch.setattr(watch, 'inspect_live', lambda: '0.21.5')

def test_notice_never_claims_practice_acceptance_and_suppresses_duplicates():
    assert 'Practice tests have not run' in watch.notice(candidate(), '0.21.5', {})
    assert watch.notice(candidate(), '0.22.0', {}) is None
    assert watch.notice(candidate(), '0.21.5', {'123': 'delivery-pending'}) is None

def test_not_due_performs_no_network(tmp_path, monkeypatch):
    setup(tmp_path, monkeypatch, '2099-10-19T01:00:00+00:00')
    monkeypatch.setattr(watch, 'release', lambda: pytest.fail('network before due'))
    watch.run(tmp_path)

def test_preview_never_sends_or_changes_state(tmp_path, monkeypatch):
    setup(tmp_path, monkeypatch)
    before = (tmp_path / 'state.json').read_bytes()
    monkeypatch.setattr(watch, 'send', lambda *a: pytest.fail('preview sent'))
    watch.run(tmp_path, preview=True)
    assert (tmp_path / 'state.json').read_bytes() == before


def test_delivery_requires_explicit_opt_in_before_network_or_state_changes(tmp_path, monkeypatch):
    setup(tmp_path, monkeypatch)
    (tmp_path / 'config.json').write_text(json.dumps({'target': 'discord:123456789012345678'}))
    before = (tmp_path / 'state.json').read_bytes()
    monkeypatch.setattr(watch, 'release', lambda: pytest.fail('network without opt-in'))
    monkeypatch.setattr(watch, 'send', lambda *a: pytest.fail('send without opt-in'))
    with pytest.raises(ValueError, match='delivery_enabled'):
        watch.run(tmp_path)
    assert (tmp_path / 'state.json').read_bytes() == before

def test_delivery_uncertainty_is_recorded_and_never_resent(tmp_path, monkeypatch):
    setup(tmp_path, monkeypatch)
    monkeypatch.setattr(watch, 'send', lambda *a: (_ for _ in ()).throw(RuntimeError('uncertain')))
    with pytest.raises(RuntimeError, match='no automatic resend'):
        watch.run(tmp_path)
    state = json.loads((tmp_path / 'state.json').read_text())
    assert 'uncertain' in state['notifications']['123']
    assert watch.notice(candidate(), '0.21.5', state['notifications']) is None

def test_failed_check_advances_due_and_reports_no_changes(tmp_path, monkeypatch):
    setup(tmp_path, monkeypatch)
    monkeypatch.setattr(watch, 'release', lambda: (_ for _ in ()).throw(ValueError('failure')))
    sent = []
    monkeypatch.setattr(watch, 'send', lambda message, target: sent.append(message))
    with pytest.raises(RuntimeError, match='no installation'):
        watch.run(tmp_path)
    state = json.loads((tmp_path / 'state.json').read_text())
    assert state['last_check']['live_changed'] is False
    assert state['next_due_at'] > '2026-01-01'
    assert len(sent) == 1 and 'Nothing was changed' in sent[0]
