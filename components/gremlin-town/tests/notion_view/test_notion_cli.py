"""`gremlin-town notion-view`: off by default, `status`, and a dry run against a real config (step H spec §1)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from gremlin_town import entrypoint
from gremlin_town.entrypoint import main as entrypoint_main
from gremlin_town.runtime import build_runtime

from test_notion_mapping import item
from test_notion_sync import FakeBrain


def write_config(root: Path, *, review_table: str) -> Path:
    """A minimal hub config (as `tests/review/test_review_cli.py` writes it) plus ``review_table`` at the end."""

    (root / "agentmail.key").write_text("fixture-secret", encoding="utf-8")
    participants = root / "participants.toml"
    participants.write_text(
        "\n".join([
            "[[participant]]", 'id = "exosuit-review"', 'display_name = "Exosuit review"',
            'address = "hub@example.test"', 'role = "participant"', "enabled = true",
            f'token_sha256 = "{hashlib.sha256(b"review-token-with-more-than-thirty-two-bytes").hexdigest()}"', 'projects = ["vault_home"]', 'sensitivities = ["private"]',
        ]),
        encoding="utf-8",
    )
    config = root / "config.toml"
    config.write_text(
        "\n".join([
            "[server]", f'database = "{(root / "gremlin.sqlite3").as_posix()}"',
            "[participants]", f'file = "{participants.as_posix()}"',
            "[recovery]", "normal_clients_allowed = true", "outbound_dispatch_allowed = false",
            "[agentmail]", 'inbox_id = "fixture-inbox"', 'api_key_file_env = "GT_TEST_AGENTMAIL_KEY"',
            review_table,
        ]),
        encoding="utf-8",
    )
    return config


@pytest.fixture(autouse=True)
def agentmail_key_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GT_TEST_AGENTMAIL_KEY", str(tmp_path / "agentmail.key"))


def notion_table(tmp_path: Path) -> str:
    return f'[notion_view]\nenabled = true\nstate_file = "{(tmp_path / "view.sqlite3").as_posix()}"\n'


def test_off_by_default(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = write_config(tmp_path, review_table="")
    assert entrypoint_main(["notion-view", "status", "--config", str(config)]) == 2
    assert "not enabled" in capsys.readouterr().err


def test_the_hub_accepts_a_notion_view_table(tmp_path: Path) -> None:
    runtime = build_runtime(write_config(tmp_path, review_table=notion_table(tmp_path)), start_worker=False)
    runtime.close()


def test_status_before_any_copy(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = write_config(tmp_path, review_table=notion_table(tmp_path))
    assert entrypoint_main(["notion-view", "status", "--config", str(config)]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "last_commit": None, "last_error": None, "last_repair": None, "last_success": None, "rows": 0,
    }


def test_a_copy_without_brain_core_refuses(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = write_config(tmp_path, review_table=notion_table(tmp_path))
    assert entrypoint_main(["notion-view", "sync", "--dry-run", "--config", str(config)]) == 2
    assert "needs [brain_core]" in capsys.readouterr().err


def test_a_dry_run_reads_brain_home_and_needs_no_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    import brain_core.github_storage as storage_module
    import gremlin_town.mcp.brain_assembly as assembly

    real_build = entrypoint.build_runtime

    def build_with_brain(path, **kwargs):
        runtime = real_build(path, **kwargs)
        runtime.brain = SimpleNamespace(
            github_repository="trusted/brain-home", github_branch="main", decision_staging_dir=tmp_path / "brain",
            archive_superseded=False,
        )
        return runtime

    monkeypatch.setattr(entrypoint, "build_runtime", build_with_brain)
    monkeypatch.setattr(assembly, "_brain_home_credentials", lambda brain, a, b: ("token", None))
    monkeypatch.setattr(storage_module, "GitHubBrainHomeStorage", lambda **kwargs: FakeBrain([item("brn_20260901_000000_aaaa")]))
    monkeypatch.delenv("GT_NOTION_TOKEN_FILE", raising=False)

    config = write_config(tmp_path, review_table=notion_table(tmp_path))
    assert entrypoint_main(["notion-view", "sync", "--dry-run", "--config", str(config)]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "archive": 0, "commit": "c1", "create": 1, "dry_run": True, "rows": 1, "update": 0,
    }
