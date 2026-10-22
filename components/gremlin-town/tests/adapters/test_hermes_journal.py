"""Speed-06 (27 Sep 2026): Diablo's plugin journals every write itself.

Before, Diablo's skill told him to copy each write's exact input to a local file with his own file
tools before sending it. That cost a model step and doubled what he typed on every save. Now the
plugin appends the exact input to ``$HERMES_HOME/gt-write-journal.jsonl`` before the request, and
a short receipt line after, and the skill tells him not to copy it himself.
"""

from __future__ import annotations

import json
import os
import re
import stat
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from adapters.hermes import plugin
from adapters.hermes.hub_client import HubClientConfig, HubHTTPSClient
from tests.adapters.test_cloud import Requester, Response
from tests.adapters.test_hermes import LoaderContext
from tests.adapters.test_hermes_brain import sample


def handler_for(name: str, requester: Requester):
    ctx = LoaderContext()
    plugin.register(ctx, client=HubHTTPSClient(HubClientConfig("https://hub.test", "fixture-hermes-token"), client=requester))
    return next(r["handler"] for r in ctx.registrations if r["name"] == name)


def journal(home: Path) -> list[dict[str, Any]]:
    path = home / plugin.WRITE_JOURNAL_NAME
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def test_a_brain_save_is_journaled_before_it_is_sent_and_its_receipt_after(hermes_home: Path) -> None:
    on_disk_at_send: list[list[dict[str, Any]]] = []

    class Watching(Requester):
        def request(self, method: str, url: str, **kwargs: Any) -> Response:
            on_disk_at_send.append(journal(hermes_home))
            return super().request(method, url, **kwargs)

    receipt = {"status": "accepted", "record_id": "brn_20260927_000000_aa", "remote_observed": True,
               "record": {"title": "never journaled"}}
    requester = Watching([Response(200, receipt)])
    body = sample("memory.save")
    reply = json.loads(handler_for("bc_memory_save", requester)(deepcopy(body)))

    assert reply["ok"] is True and reply["result"] == receipt
    sent = on_disk_at_send[0]
    assert [line["phase"] for line in sent] == ["sent"], "the exact input is on disk before the request goes out"
    assert sent[0]["operation"] == "memory.save" and sent[0]["key"] == body["operation_id"]
    assert sent[0]["arguments"] == body
    lines = journal(hermes_home)
    assert [line["phase"] for line in lines] == ["sent", "result"]
    result = lines[1]
    assert {k: result[k] for k in ("operation", "key", "ok", "status", "record_id", "remote_observed")} == {
        "operation": "memory.save", "key": body["operation_id"], "ok": True, "status": "accepted",
        "record_id": "brn_20260927_000000_aa", "remote_observed": True,
    }
    assert "record" not in result and "arguments" not in result, "the result line keeps receipt fields only"


def test_a_gt_write_is_journaled_under_its_client_operation_id(hermes_home: Path) -> None:
    requester = Requester([Response(200, {"operation_id": "op_hub_1", "request_id": "req_1", "status": "queued"})])
    body = {"client_operation_id": "damien-ask-codex-1", "mode": "action_requested", "project": "gremlin-town",
            "recipient": "local-codex", "packet": {"schema_version": 1}}
    json.loads(handler_for("gremlin_town_requests_create", requester)(deepcopy(body)))
    lines = journal(hermes_home)
    assert [(line["phase"], line["operation"], line["key"]) for line in lines] == [
        ("sent", "requests.create", "damien-ask-codex-1"), ("result", "requests.create", "damien-ask-codex-1"),
    ]
    assert lines[0]["arguments"] == body
    assert lines[1]["request_id"] == "req_1" and lines[1]["operation_id"] == "op_hub_1"


@pytest.mark.parametrize("name,operation", [("bc_memory_list", "memory.list"), ("bc_memory_search", "memory.search")])
def test_reads_are_not_journaled(name: str, operation: str, hermes_home: Path) -> None:
    handler_for(name, Requester())(sample(operation))
    assert journal(hermes_home) == []


def test_a_refused_write_is_journaled_with_its_error_code(hermes_home: Path) -> None:
    refusal = {"error": {"code": "brain.time.unresolved", "message": "fixture", "retryable": False}}
    reply = json.loads(handler_for("bc_memory_save", Requester([Response(422, refusal)]))(sample("memory.save")))
    assert reply["ok"] is False and reply["error"]["code"] == "brain.time.unresolved"
    lines = journal(hermes_home)
    assert [line["phase"] for line in lines] == ["sent", "result"]
    assert lines[1]["ok"] is False and lines[1]["error_code"] == "brain.time.unresolved" and lines[1]["retryable"] is False


def test_no_journal_means_nothing_is_sent(hermes_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    requester = Requester()
    monkeypatch.delenv("HERMES_HOME")
    reply = json.loads(handler_for("bc_memory_save", requester)(sample("memory.save")))
    assert reply["ok"] is False and reply["error"]["code"] == "JOURNAL_UNAVAILABLE"
    assert requester.calls == []

    blocked = hermes_home / "blocked-home"
    blocked.write_text("a file where the folder should be", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(blocked))
    reply = json.loads(handler_for("bc_memory_save", requester)(sample("memory.save")))
    assert reply["ok"] is False and reply["error"]["code"] == "JOURNAL_UNAVAILABLE"
    assert requester.calls == []


def test_a_call_carrying_a_credential_shaped_argument_is_never_journaled(hermes_home: Path) -> None:
    requester = Requester()
    body = {**sample("memory.save"), "authorization": "Bearer should-never-be-written"}
    reply = json.loads(handler_for("bc_memory_save", requester)(body))
    assert reply["ok"] is False  # the hub client refuses it, as before
    assert journal(hermes_home) == [] and requester.calls == []
    assert "should-never-be-written" not in "".join(p.read_text(encoding="utf-8") for p in hermes_home.glob("*"))


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_the_journal_is_created_private(hermes_home: Path) -> None:
    previous = os.umask(0o022)
    try:
        handler_for("bc_memory_save", Requester())(sample("memory.save"))
    finally:
        os.umask(previous)
    mode = stat.S_IMODE((hermes_home / plugin.WRITE_JOURNAL_NAME).stat().st_mode)
    assert mode == 0o600


def test_the_skill_no_longer_asks_damien_to_copy_writes_himself() -> None:
    skill = " ".join((Path(plugin.__file__).parent / "skills" / "gremlin-town" / "SKILL.md").read_text(encoding="utf-8").split())
    assert "persist the COMPLETE exact" not in skill
    assert "This plugin does not journal it" not in skill
    assert "gt-write-journal.jsonl" in skill and "never write your own copy first" in skill
    assert "the client never retries by itself" in skill


def test_the_wake_route_prompt_relies_on_the_journal_too() -> None:
    """A GT request that wakes Diablo arrives with deploy/wake/receiver-prompt.txt, which told him to
    write_file every input under /opt/data/gt-operations. It now points at the plugin's journal."""

    prompt = (Path(plugin.__file__).parents[2] / "deploy" / "wake" / "receiver-prompt.txt").read_text(encoding="utf-8")
    assert "gt-write-journal.jsonl" in prompt and "do not write your own copy" in prompt
    assert "write_file" not in prompt and "/opt/data/gt-operations" not in prompt
    # Hermes fills in {name} placeholders; the prompt must carry exactly the two the wake sends.
    assert set(re.findall(r"\{([a-zA-Z0-9_.]+)\}", prompt)) == {"request_id", "delivery_id"}


def test_a_woken_run_replies_on_telegram_and_the_courier_items_stay_silent() -> None:
    """Nic, 29 Sep 2026: "i should be able to prompt damien from anywhere - he should wake and run the
    prompt". The route delivers the run's final reply to his Telegram, so the prompt says what that
    reply holds, and keeps the review and daytime returns silent: the courier already posts them."""

    wake = Path(plugin.__file__).parents[2] / "deploy" / "wake"
    prompt = (wake / "receiver-prompt.txt").read_text(encoding="utf-8")
    assert "Your final reply goes to Nic's Telegram" in prompt
    assert "Terminal is not enabled" not in prompt
    assert "exosuit-review" in prompt and "[SILENT] and nothing else" in prompt
    installer = (wake / "install_hermes.py").read_text(encoding="utf-8")
    assert "'deliver':'telegram'" in installer and "'toolsets':['hermes-telegram']" in installer
