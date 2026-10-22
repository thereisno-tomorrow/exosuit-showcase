"""`gremlin-town review run` / `gremlin-town review moments`: the installed entry point,
off by default, exercised against a real config.toml and a real (but unused) transport."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from brain_core.timewords import today_in_singapore
from gremlin_town.entrypoint import main as entrypoint_main
from gremlin_town.review.run import ReviewPrerequisiteError
from gremlin_town.review.why_log import WhyLog


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def write_config(root: Path, *, review_table: str) -> Path:
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
            f'api_key_file_env = "GT_TEST_AGENTMAIL_KEY"',
            review_table,
        ]),
        encoding="utf-8",
    )
    return config


@pytest.fixture(autouse=True)
def agentmail_key_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GT_TEST_AGENTMAIL_KEY", str(tmp_path / "agentmail.key"))


def test_review_run_disabled_by_default_prints_disabled(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = write_config(tmp_path, review_table="")
    exit_code = entrypoint_main(["review", "run", "--config", str(config)])
    assert exit_code == 0
    assert json.loads(capsys.readouterr().out) == {"outcome": "disabled"}


def test_review_moments_disabled_by_default_prints_disabled(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = write_config(tmp_path, review_table="")
    exit_code = entrypoint_main(["review", "moments", "--config", str(config), "--signal", "pc_on"])
    assert exit_code == 0
    assert json.loads(capsys.readouterr().out) == {"outcome": "disabled"}


def test_review_run_enabled_but_unconfigured_sources_sends_empty_review(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    why_log_file = tmp_path / "why.sqlite3"
    config = write_config(
        tmp_path,
        review_table=(
            "[review]\nenabled = true\nsender_participant = \"exosuit-review\"\n"
            f'delivery_participant = "hermes-wa"\nproject = "vault_home"\n'
            f'why_log_file = "{why_log_file.as_posix()}"\n'
        ),
    )
    why_log = WhyLog(why_log_file)
    try:
        # Synthetic fixture pin: no Brain source is configured, so the complete
        # pre-move INPUT inventory is empty. This invents no prior review history.
        why_log.stage_morning_bootstrap(input_sha="a" * 40, first_ids=())
        assert why_log.last_sent_review_at() is None
        assert why_log.morning_history() == ()
    finally:
        why_log.close()
    exit_code = entrypoint_main(["review", "run", "--config", str(config)])
    assert exit_code == 0
    result = json.loads(capsys.readouterr().out)
    assert result["outcome"] == "sent"
    why_log = WhyLog(why_log_file)
    try:
        sent_at = why_log.last_sent_review_at()
        assert sent_at is not None
        packet = why_log.morning_packet(today_in_singapore(sent_at))
        assert packet is not None and packet.completed
        assert [line["question"] for line in packet.request["constraints"]["lines"]] == [
            "Today: Nothing at a set time.",
            "Couldn't see brain: not_configured",
            "Couldn't see calendar: not_configured",
            "Anything new to add before you pick your day? Say it and it's saved.",
        ]
        prompt = packet.request["constraints"]["lines"][-1]
        assert prompt["kind"] == "add_prompt" and prompt["part"] == "new" and prompt["number"] == 0
        assert prompt["item_ref"] is prompt["reference"] is None
        assert prompt["allowed_answers"] == prompt["offered"] == []
        assert len(packet.references) == 3 and len(why_log.history()) == 3
        assert "0 new, then your day" in packet.short
        assert [line for line in packet.request["text"].splitlines() if line.startswith("## ")] == [
            "## Look", "## Catch up", "## New", "## Your day: pick 3 to 7",
        ]
        assert why_log.morning_bootstrap_completed()
    finally:
        why_log.close()
    # Plan 6.3's Look also reports that nothing is at a set time.
    assert result["line_count"] == 3
    # Brain and calendar are not configured yet (morning queue); that alone
    # makes the review non-empty - it reports "Couldn't see" for both.
    assert result["is_empty"] is False
    assert why_log_file.exists()


def test_review_run_enabled_without_bootstrap_refuses_cold_start(tmp_path: Path) -> None:
    why_log_file = tmp_path / "why.sqlite3"
    config = write_config(
        tmp_path,
        review_table=(
            '[review]\nenabled = true\nsender_participant = "exosuit-review"\n'
            'delivery_participant = "hermes-wa"\nproject = "vault_home"\n'
            f'why_log_file = "{why_log_file.as_posix()}"\n'
        ),
    )
    with pytest.raises(ReviewPrerequisiteError, match="morning_bootstrap.input_sha/first_ids"):
        entrypoint_main(["review", "run", "--config", str(config)])
    why_log = WhyLog(why_log_file)
    try:
        assert why_log.morning_bootstrap() is None
        assert why_log.last_sent_review_at() is None
        assert why_log.morning_history() == ()
    finally:
        why_log.close()


def test_review_respond_is_routed_to_the_remote_client_not_the_local_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    """"review respond" is a client action (like "moments signal"); only "run",
    "moments" and "moments-poll" build a local Runtime from --config (entrypoint.py's dispatch)."""

    import gremlin_town.entrypoint as entrypoint_module

    calls: list[str] = []
    monkeypatch.setattr(entrypoint_module, "_review", lambda argv: calls.append("server") or 0)
    monkeypatch.setattr(entrypoint_module, "client_main", lambda argv: calls.append("client") or 0)

    entrypoint_module.main(["review", "respond", "--item-ref", "brn_x", "--response", "picked_today"])
    entrypoint_module.main(["review", "run", "--config", "unused.toml"])
    entrypoint_module.main(["review", "moments", "--config", "unused.toml", "--signal", "pc_on"])
    entrypoint_module.main(["review", "moments-poll", "--config", "unused.toml"])

    assert calls == ["client", "server", "server", "server"]


def test_review_moments_poll_disabled_by_default_prints_empty_list(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = write_config(tmp_path, review_table="")
    exit_code = entrypoint_main(["review", "moments-poll", "--config", str(config)])
    assert exit_code == 0
    assert json.loads(capsys.readouterr().out) == []
