from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from gremlin_town.dashboard.app import create_app
from gremlin_town.dashboard.run import main


def config(tmp_path):
    secret = tmp_path / "secret"
    secret.write_bytes(b"fictional-test-session-secret!!!!")
    secret.chmod(0o600)
    path = tmp_path / "runtime.toml"
    path.write_text('''[dashboard]
enabled = true
origin = "https://dashboard.example.test"
rp_id = "dashboard.example.test"
owner_id = "fictional-owner"
state_file = "state/auth.sqlite"
session_secret_file = "secret"
''')
    return path


def test_enrolment_link_is_file_only_exclusive_and_audited(tmp_path, capsys):
    path = config(tmp_path)
    output = tmp_path / "link.txt"
    assert main(["enrol", "--config", str(path), "--output", str(output)]) == 0
    link = output.read_text().strip()
    assert link.startswith("https://dashboard.example.test/enrol#token=")
    assert link not in capsys.readouterr().out
    assert main(["enrol", "--config", str(path), "--output", str(output)]) == 2
    assert output.read_text().strip() == link
    assert main(["revoke-all", "--config", str(path)]) == 0
    from gremlin_town.dashboard.run import load_settings
    from gremlin_town.dashboard.auth import AuthService
    from gremlin_town.dashboard.store import digest
    auth = AuthService(load_settings(path))
    assert not auth.store.enrollment_valid(digest(link.split("token=")[1]), auth.now())
    with auth.store.connect() as db:
        assert {r[0] for r in db.execute("SELECT event FROM audit")} == {"enrollment_issued", "revoked_all"}


def test_disabled_cli_has_no_side_effect(tmp_path, capsys):
    path = tmp_path / "off.toml"
    path.write_text("[dashboard]\nenabled=false\n")
    output = tmp_path / "link.txt"
    assert main(["enrol", "--config", str(path), "--output", str(output)]) == 2
    assert not output.exists()
    assert "Traceback" not in capsys.readouterr().err


def test_serve_loopback_and_no_access_logs(tmp_path, monkeypatch):
    path = config(tmp_path)
    calls = []
    monkeypatch.setattr("gremlin_town.dashboard.run.uvicorn.run", lambda *a, **kw: calls.append((a, kw)))
    assert main(["serve", "--config", str(path)]) == 0
    args, kwargs = calls[0]
    assert kwargs["host"] == "127.0.0.1"
    assert kwargs["access_log"] is False and kwargs["proxy_headers"] is False
    assert kwargs["workers"] == 1
    assert TestClient(args[0], base_url="https://dashboard.example.test").get("/auth/session").status_code == 401


def test_credential_read_error_has_no_path_or_contents(tmp_path, capsys):
    path = config(tmp_path)
    (tmp_path / "secret").write_bytes(b"fictional-short-secret")
    assert main(["enrol", "--config", str(path), "--output", str(tmp_path / "link")]) == 2
    captured = capsys.readouterr()
    assert "fictional-short-secret" not in captured.err
    assert str(tmp_path) not in captured.err


def test_reject_owner_or_rp_change_for_existing_database(tmp_path):
    from gremlin_town.dashboard.run import load_settings
    path = config(tmp_path)
    settings = load_settings(path)
    create_app(settings)
    with pytest.raises(ValueError, match="identity changed"):
        create_app(replace(settings, owner_id="another-fictional-owner"))


def test_secret_creation_is_exclusive_and_never_printed(tmp_path, capsys):
    output = tmp_path / "secret"
    assert main(["init-secret", "--output", str(output)]) == 0
    assert len(output.read_bytes()) == 64
    assert main(["init-secret", "--output", str(output)]) == 2
    captured = capsys.readouterr()
    assert len(captured.out) < 120
