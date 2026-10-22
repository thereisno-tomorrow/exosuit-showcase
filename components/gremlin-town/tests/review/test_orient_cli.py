"""`gremlin-town orient overview|next|respond`: step F's two asks and the answer to a pick,
through the installed entry point, off by default, against a real config.toml (step F §8)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from gremlin_town.entrypoint import main as entrypoint_main
from gremlin_town.review.settings import OrientationSettingsError


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def write_config(root: Path, *, review_table: str = "", orientation_table: str = "") -> Path:
    api_key_file = root / "agentmail.key"
    api_key_file.write_text("fixture-secret", encoding="utf-8")
    participants = root / "participants.toml"
    participants.write_text(
        "\n".join([
            "[[participant]]",
            'id = "exosuit-review"',
            'display_name = "Exosuit review"',
            'address = "hub@example.test"',
            'role = "participant"',
            "enabled = true",
            f'token_sha256 = "{_hash("review-token-with-more-than-thirty-two-bytes")}"',
            'projects = ["vault_home"]',
            'sensitivities = ["private"]',
            "",
            "[[participant]]",
            'id = "hermes-wa"',
            'display_name = "Diablo"',
            'address = "hub@example.test"',
            'role = "participant"',
            "enabled = true",
            f'token_sha256 = "{_hash("damien-token-with-more-than-thirty-two-bytes")}"',
            'projects = ["vault_home"]',
            'sensitivities = ["private"]',
            "",
            "[[route]]",
            'project = "vault_home"',
            'source = "exosuit-review"',
            'destination = "hermes-wa"',
            'sensitivity = "private"',
        ]),
        encoding="utf-8",
    )
    config = root / "config.toml"
    config.write_text(
        "\n".join([
            "[server]",
            f'database = "{(root / "gremlin.sqlite3").as_posix()}"',
            "[participants]",
            f'file = "{participants.as_posix()}"',
            "[recovery]",
            "normal_clients_allowed = true",
            "outbound_dispatch_allowed = false",
            "[agentmail]",
            'inbox_id = "fixture-inbox"',
            'api_key_file_env = "GT_TEST_AGENTMAIL_KEY"',
            review_table,
            orientation_table,
        ]),
        encoding="utf-8",
    )
    return config


@pytest.fixture(autouse=True)
def agentmail_key_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GT_TEST_AGENTMAIL_KEY", str(tmp_path / "agentmail.key"))


@pytest.mark.parametrize("command", ["overview", "next"])
def test_both_asks_are_disabled_by_default(
    command: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = write_config(tmp_path)
    exit_code = entrypoint_main(["orient", command, "--config", str(config)])
    assert exit_code == 0
    assert json.loads(capsys.readouterr().out) == {"outcome": "disabled"}


def test_the_old_ask_command_is_gone(tmp_path: Path) -> None:
    """Renamed with the tools (rebuild plan G9): nothing was ever live on it."""

    config = write_config(tmp_path)
    assert entrypoint_main(["orient", "ask", "--config", str(config)]) == 2


def test_orient_respond_disabled_by_default_prints_disabled(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = write_config(tmp_path)
    exit_code = entrypoint_main([
        "orient", "respond", "--config", str(config), "--actor", "chatgpt-web",
        "--surfacing-id", "brn_x", "--response", "no",
    ])
    assert exit_code == 0
    assert json.loads(capsys.readouterr().out) == {"outcome": "disabled"}


def _enabled_config(tmp_path: Path) -> Path:
    why_log_file = tmp_path / "why.sqlite3"
    return write_config(
        tmp_path,
        review_table=(
            "[review]\nenabled = true\nsender_participant = \"exosuit-review\"\n"
            'delivery_participant = "hermes-wa"\nproject = "vault_home"\n'
            f'why_log_file = "{why_log_file.as_posix()}"\n'
        ),
        orientation_table="[orientation]\nenabled = true\n",
    )


def test_next_enabled_with_no_brain_source_says_not_enough(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _enabled_config(tmp_path)
    exit_code = entrypoint_main(["orient", "next", "--config", str(config), "--signal", "calendar_gap", "--gap-minutes", "40"])
    assert exit_code == 0
    result = json.loads(capsys.readouterr().out)
    assert result["outcome"] == "answered"
    # Brain Home isn't wired here: it couldn't be read, so not_enough says that, never "nothing open".
    assert result["not_enough"] == {
        "reason": "Couldn't see your saved items, so there's nothing safe to recommend yet.", "questions": [],
    }
    assert "brain: not_configured" in result["unknowns"]
    assert "notes: not_configured" in result["unknowns"]
    assert result["surfacing_id"] == "__orient__"


def test_overview_enabled_with_no_brain_source_names_what_it_could_not_see(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _enabled_config(tmp_path)
    assert entrypoint_main(["orient", "overview", "--config", str(config)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["outcome"] == "answered"
    [section] = result["sections"]
    assert (section["section"], section["title"]) == ("could_not_see", "Couldn't see")
    assert {"text": "brain: not_configured", "item_ref": "brain"} in section["lines"]
    assert "top_pick" not in result and "surfacing_id" not in result


def test_orient_respond_not_now_after_not_enough_round_trips(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _enabled_config(tmp_path)
    entrypoint_main(["orient", "next", "--config", str(config)])
    ask_result = json.loads(capsys.readouterr().out)

    exit_code = entrypoint_main([
        "orient", "respond", "--config", str(config), "--actor", "chatgpt-web",
        "--surfacing-id", ask_result["surfacing_id"], "--response", "not_now",
    ])
    assert exit_code == 0
    assert json.loads(capsys.readouterr().out) == {"outcome": "recorded", "response": "not_now"}


def test_orient_enabled_requires_review_enabled(tmp_path: Path) -> None:
    config = write_config(tmp_path, orientation_table="[orientation]\nenabled = true\n")
    with pytest.raises(OrientationSettingsError):
        entrypoint_main(["orient", "overview", "--config", str(config)])


def test_orient_respond_passes_the_first_step_through(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """N12c: the first step the agent suggested, or Nic's own words, reaches `respond`."""

    import gremlin_town.review.orient as orient_module

    seen: dict[str, object] = {}

    def fake_respond(context, now, **kwargs):
        seen.update(kwargs)
        return {"outcome": "started"}

    monkeypatch.setattr(orient_module, "respond", fake_respond)
    config = _enabled_config(tmp_path)
    exit_code = entrypoint_main([
        "orient", "respond", "--config", str(config), "--actor", "chatgpt-web",
        "--surfacing-id", "brn_x", "--response", "yes", "--first-step", "Open the deck",
    ])
    assert exit_code == 0
    assert json.loads(capsys.readouterr().out) == {"outcome": "started"}
    assert seen["first_step"] == "Open the deck"
