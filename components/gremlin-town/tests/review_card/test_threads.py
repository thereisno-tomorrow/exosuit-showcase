"""Synthetic persistent thread tests; Discord REST is always a local fake."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
import json
from types import SimpleNamespace

import pytest

from brain_core.timewords import SINGAPORE
from gremlin_town.review_card.client import DiscordUnavailable
from gremlin_town.review_card.diablo import DiabloRefused
from gremlin_town.review_card.threads import ThreadBridge

NOW = datetime(2026, 10, 2, 12, tzinfo=SINGAPORE)
DAY = "2026-10-02"
BASE = {"day": DAY, "line_ref": "line-one", "item_ref": "fixture-item", "operation_id": "fixture-ask"}


def run(value):
    return asyncio.run(value)


class Store:
    """A disk-backed fake exercises restart and intent durability, not SQL details."""
    def __init__(self, path):
        self.path = path
        if not path.exists():
            path.write_text(json.dumps({DAY: {"message_id": "66", "channel_id": "33", "route": "discord"}}))

    def delivery(self, day):
        return json.loads(self.path.read_text()).get(day, {})

    def save_delivery(self, day, **values):
        content = json.loads(self.path.read_text())
        content.setdefault(day, {}).update(values)
        self.path.write_text(json.dumps(content))


class Engine:
    def __init__(self):
        self.binding = dict(BASE)
        self.reads, self.controls = [], []
        self.visible = True
        self.proof = {"record_id": "fixture-version", "record_sha256": "a" * 64}

    def current_diablo_binding(self):
        return self.binding

    async def authorize_item(self, owner, day, line, ref):
        self.reads.append((owner, day, line, ref))
        if not self.visible:
            raise DiabloRefused("This review item is no longer available.")
        return {"ref": ref, **self.proof}

    async def draft_control(self, binding):
        await self.authorize_item("fixture-owner", binding["day"], binding["line_ref"], binding["item_ref"])
        self.controls.append(deepcopy(binding))
        return {"type": 2, "style": 2, "label": "Open this draft", "custom_id": "rc:fixture-" + str(len(self.controls))}


class Diablo:
    def __init__(self, engine):
        self.engine = engine
        self.number = 0
        self.reads, self.refinements, self.cached = [], [], {}
        self.values = {BASE["item_ref"]: {"state": "waiting", "operation_id": BASE["operation_id"]}}
        self.lose_refinement = False

    async def revision(self, day):
        return str(self.number)

    async def read(self, base, *, owner_id, authorized_item=None):
        if authorized_item is None:
            await self.engine.authorize_item(owner_id, base["day"], base["line_ref"], base["item_ref"])
        self.reads.append(deepcopy(base))
        return deepcopy(self.values[base["item_ref"]])

    def ready(self, ref=BASE["item_ref"], *, operation="fixture-ask", count=1, nudge=False):
        steps = [{"title": "Synthetic action " + str(number), "detail": "Synthetic detail " + str(number)} for number in range(count)]
        self.values[ref] = {"state": "nudge" if nudge else "draft", "steps": steps, "operation_id": operation,
                            "text": "Synthetic follow-up text", "draft_sha256": hashlib.sha256(json.dumps([steps, nudge]).encode()).hexdigest()}
        self.number += 1

    async def refine(self, value, *, owner_id):
        key = value["operation_id"]
        self.refinements.append(deepcopy(value))
        if key in self.cached:
            assert self.cached[key] == value
        else:
            if value["previous_operation_id"] != self.values[value["item_ref"]]["operation_id"]:
                raise DiabloRefused("A newer request exists. Reply to its latest draft.")
            self.cached[key] = deepcopy(value)
            self.values[value["item_ref"]] = {"state": "waiting", "operation_id": key}
            self.number += 1
            if self.lose_refinement:
                raise TimeoutError("synthetic private exception")
        return {"state": "waiting", "operation_id": key}


class Client:
    def __init__(self, store):
        self.store = store
        self.threads, self.posts, self.lookups, self.remote = [], [], [], []
        self.failure = None

    def thread(self, channel, message):
        self.threads.append((channel, message))
        return "77"

    def post(self, channel, payload, *, nonce):
        durable = self.store.delivery(DAY)["diablo_threads"]
        assert any(post["nonce"] == nonce and post["status"] == "posting" for post in durable["posts"].values())
        assert len(payload["content"]) <= 2000
        self.posts.append((channel, deepcopy(payload), nonce))
        message = {"id": str(100 + len(self.posts)), "channel_id": channel, "author": {"id": "55"}, "nonce": nonce}
        if self.failure == "reject":
            raise DiscordUnavailable("synthetic secret rejection")
        if self.failure != "lost_absent":
            self.remote.append(message)
        if self.failure in {"lost_found", "lost_absent"}:
            raise DiscordUnavailable("synthetic secret timeout", uncertain=True)
        return message

    def find_nonce(self, channel, nonce, *, bot_user_id):
        self.lookups.append((channel, nonce, bot_user_id))
        return next((entry for entry in self.remote if entry["nonce"] == nonce and entry["author"]["id"] == bot_user_id), None)


@pytest.fixture
def env(tmp_path):
    store, engine = Store(tmp_path / "card.json"), Engine()
    client, diablo = Client(store), Diablo(engine)
    settings = SimpleNamespace(enabled=True, card=True, ask_diablo=True, owner_id="fixture-owner", owner_user_id="11",
                               guild_id="22", morning_channel_id="33")
    value = SimpleNamespace(store=store, engine=engine, client=client, diablo=diablo, settings=settings, now=NOW)
    value.bridge = ThreadBridge(client, store, engine, diablo, settings, bot_user_id="55", now=lambda: value.now)
    return value


def restart(env):
    env.store = Store(env.store.path)
    env.client.store = env.store
    env.bridge = ThreadBridge(env.client, env.store, env.engine, env.diablo, env.settings, bot_user_id="55", now=lambda: env.now)


def event(**extra):
    return {"id": "800", "guild_id": "22", "channel_id": "77", "author": {"id": "11"},
            "content": "  Use these exact words.\nKeep the second line.  ", **extra}


def ready(env):
    env.diablo.ready()
    assert run(env.bridge.sync())["outcome"] == "updated"


@pytest.mark.parametrize("flag", ["enabled", "card", "ask_diablo"])
def test_disabled_bridge_has_no_reads_requests_or_delivery(env, flag):
    setattr(env.settings, flag, False)
    assert run(env.bridge.sync())["outcome"] == "disabled"
    assert run(env.bridge.on_message(event()))["outcome"] == "disabled"
    assert not env.client.threads and not env.engine.reads and not env.diablo.reads


def test_one_thread_and_unchanged_local_revision_avoid_fresh_brain_reads(env):
    assert run(env.bridge.sync())["outcome"] == "updated"
    reads = len(env.engine.reads)
    assert run(env.bridge.sync())["outcome"] == "unchanged"
    restart(env)
    assert run(env.bridge.sync())["outcome"] == "unchanged"
    assert len(env.engine.reads) == reads and env.client.threads == [("33", "66")]


def test_skipped_item_draft_still_arrives_with_exact_item_navigation(env):
    run(env.bridge.sync())
    env.engine.binding = None
    env.diablo.ready()
    assert run(env.bridge.sync())["sent"] == 1
    assert env.engine.controls == [BASE]
    assert env.client.posts[0][1]["components"][0]["components"][0]["label"] == "Open this draft"
    assert run(env.bridge.sync())["outcome"] == "unchanged"


def test_maximum_draft_is_bounded_and_store_contains_no_draft_or_owner_words(env):
    env.diablo.ready(count=20)
    draft = env.diablo.values[BASE["item_ref"]]
    for step in draft["steps"]:
        step.update(title="t" * 120, detail="d" * 400)
    assert run(env.bridge.sync())["sent"] > 1
    assert all(not post[1]["components"] for post in env.client.posts[:-1])
    assert env.client.posts[-1][1]["components"]
    stored = env.store.path.read_text()
    assert "t" * 120 not in stored and "d" * 400 not in stored
    assert "route" in env.store.delivery(DAY)


def test_nudge_is_display_only_without_save_control(env):
    env.diablo.ready(nudge=True)
    assert run(env.bridge.sync())["sent"] == 1
    assert "Synthetic follow-up text" in env.client.posts[0][1]["content"]
    assert not env.client.posts[0][1]["components"] and not env.engine.controls


def test_lost_post_ack_reconciles_own_nonce_after_restart_without_duplicate(env):
    env.client.failure = "lost_found"
    env.diablo.ready()
    result = run(env.bridge.sync())
    assert result["outcome"] == "unavailable" and "secret" not in str(result)
    restart(env)
    env.client.failure = None
    assert run(env.bridge.sync())["outcome"] == "updated"
    assert len(env.client.posts) == 1 and len(env.client.lookups) == 1
    assert run(env.bridge.on_message(event(message_reference={"message_id": "101"})))["outcome"] == "requested"


def test_unconfirmed_post_is_never_blindly_repeated_even_after_nonce_window(env):
    env.client.failure = "lost_absent"
    env.diablo.ready(count=20)
    assert run(env.bridge.sync())["outcome"] == "unavailable"
    reads = len(env.engine.reads)
    env.now += timedelta(hours=7)
    restart(env)
    env.client.failure = None
    assert run(env.bridge.sync())["outcome"] == "unavailable"
    assert len(env.client.posts) == 1 and len(env.engine.reads) == reads


def test_definite_post_rejection_can_retry_same_intent(env):
    env.client.failure = "reject"
    env.diablo.ready()
    assert run(env.bridge.sync())["outcome"] == "unavailable"
    env.client.failure = None
    assert run(env.bridge.sync())["outcome"] == "updated"
    assert env.client.posts[0] == env.client.posts[1]


def test_superseded_definite_rejection_does_not_keep_polling_brain(env):
    env.diablo.ready()
    env.client.failure = "reject"
    assert run(env.bridge.sync())["outcome"] == "unavailable"
    env.client.failure = None
    env.diablo.ready(operation="newer-accepted-draft")
    assert run(env.bridge.sync())["sent"] == 1
    reads = len(env.engine.reads)
    assert run(env.bridge.sync())["outcome"] == "unchanged"
    assert len(env.engine.reads) == reads


def test_closed_items_definite_rejection_does_not_keep_polling_brain(env):
    env.diablo.ready()
    env.client.failure = "reject"
    assert run(env.bridge.sync())["outcome"] == "unavailable"
    env.engine.visible = False
    env.diablo.number += 1
    assert run(env.bridge.sync())["sent"] == 0
    reads = len(env.engine.reads)
    assert run(env.bridge.sync())["outcome"] == "unchanged"
    assert len(env.engine.reads) == reads


@pytest.mark.parametrize("extra", [{"guild_id": "99"}, {"channel_id": "99"}, {"author": {"id": "99"}},
                                   {"author": {"id": "11", "bot": True}}])
def test_foreign_or_bot_events_do_not_reach_brain_or_gt(env, extra):
    ready(env)
    reads = len(env.engine.reads)
    assert run(env.bridge.on_message(event(**extra)))["outcome"] == "ignored"
    assert len(env.engine.reads) == reads and not env.diablo.refinements


def test_one_active_binding_accepts_exact_owner_words_and_current_proof(env):
    ready(env)
    result = run(env.bridge.on_message(event()))
    assert result["outcome"] == "requested"
    sent = env.diablo.refinements[0]
    assert sent["text"] == event()["content"] and sent["operation_id"] == "review-reply-800"
    assert sent["previous_operation_id"] == "fixture-ask"
    assert sent["expected"] == {"record_id": "fixture-version", "sha256": "a" * 64}
    assert event()["content"] not in env.store.path.read_text()


def test_multiple_bindings_require_explicit_reply_and_never_guess_latest(env):
    ready(env)
    env.engine.binding = {**BASE, "line_ref": "line-two", "item_ref": "fixture-second", "operation_id": "second-ask"}
    env.diablo.ready("fixture-second", operation="second-ask")
    run(env.bridge.sync())
    assert run(env.bridge.on_message(event()))["outcome"] == "refused"
    assert not env.diablo.refinements
    assert run(env.bridge.on_message(event(message_reference={"message_id": "101"})))["outcome"] == "requested"
    assert env.diablo.refinements[0]["item_ref"] == BASE["item_ref"]


def test_unknown_reply_reference_does_not_fall_back_to_single_binding(env):
    ready(env)
    assert run(env.bridge.on_message(event(message_reference={"message_id": "999"})))["outcome"] == "refused"
    assert not env.diablo.refinements


def test_pending_second_request_still_makes_unreferenced_reply_ambiguous(env):
    ready(env)
    env.engine.binding = {**BASE, "line_ref": "line-two", "item_ref": "fixture-second", "operation_id": "second-ask"}
    env.diablo.values["fixture-second"] = {"state": "waiting", "operation_id": "second-ask"}
    run(env.bridge.sync())
    assert run(env.bridge.on_message(event()))["outcome"] == "refused"
    assert not env.diablo.refinements


def test_repeated_owner_event_reuses_original_refinement_after_lost_ack_and_proof_change(env):
    ready(env)
    env.diablo.lose_refinement = True
    assert run(env.bridge.on_message(event()))["outcome"] == "unavailable"
    restart(env)
    env.engine.proof["record_sha256"] = "b" * 64
    assert run(env.bridge.on_message(event()))["outcome"] == "requested"
    assert env.diablo.refinements[0] == env.diablo.refinements[1]
    assert run(env.bridge.on_message(event()))["outcome"] == "requested"
    assert len(env.diablo.cached) == 1
    assert run(env.bridge.on_message(event(content="Different text")))["outcome"] == "refused"


def test_refinement_updates_do_not_make_unchanged_card_binding_repoll_brain(env):
    ready(env)
    run(env.bridge.on_message(event()))
    run(env.bridge.sync())
    reads = len(env.engine.reads)
    assert run(env.bridge.sync())["outcome"] == "unchanged"
    assert len(env.engine.reads) == reads


def test_reply_to_old_draft_is_rejected_instead_of_refining_newest_draft(env):
    ready(env)
    run(env.bridge.on_message(event()))
    env.diablo.ready(operation="review-reply-800")
    run(env.bridge.sync())
    assert run(env.bridge.on_message(event(id="801", message_reference={"message_id": "101"})))["outcome"] == "refused"


def test_fresh_policy_denial_stops_refinement_and_draft_delivery(env):
    ready(env)
    env.engine.visible = False
    assert run(env.bridge.on_message(event()))["outcome"] == "refused"
    assert not env.diablo.refinements
    env.diablo.number += 1
    assert run(env.bridge.sync())["sent"] == 0
    assert len(env.client.posts) == 1


def test_thread_control_requires_exact_owner_guild_message_and_custom_id(env):
    ready(env)
    value = {"guild_id": "22", "channel_id": "77", "member": {"user": {"id": "11"}},
             "message": {"id": "101"}, "data": {"custom_id": "rc:fixture-1"}}
    assert env.bridge.owns_interaction(value)
    for field, wrong in (("channel_id", "33"), ("guild_id", "99"), ("message", {"id": "999"}),
                         ("member", {"user": {"id": "99"}}), ("data", {"custom_id": "rc:foreign"})):
        assert not env.bridge.owns_interaction({**value, field: wrong})


def test_review_thread_stays_current_until_next_six_am(env):
    ready(env)
    env.now = NOW + timedelta(hours=17)
    assert run(env.bridge.on_message(event()))["outcome"] == "requested"
    env.now += timedelta(hours=1)
    assert run(env.bridge.on_message(event(id="801")))["outcome"] == "ignored"


def test_closed_old_line_does_not_block_another_items_draft(env):
    ready(env)
    original = env.engine.authorize_item
    async def current(owner, day, line, ref):
        if ref == BASE["item_ref"]:
            raise ValueError("synthetic closed review line")
        return await original(owner, day, line, ref)
    env.engine.authorize_item = current
    env.engine.binding = {**BASE, "line_ref": "line-two", "item_ref": "fixture-second", "operation_id": "second-ask"}
    env.diablo.ready("fixture-second", operation="second-ask")
    assert run(env.bridge.sync())["sent"] == 1
    assert len(env.client.posts) == 2


def test_reconciliation_does_not_trust_a_foreign_bots_nonce(env):
    env.client.failure = "lost_absent"
    env.diablo.ready()
    run(env.bridge.sync())
    env.client.find_nonce = lambda channel, nonce, **kw: {"id": "900", "channel_id": channel,
                                                       "nonce": nonce, "author": {"id": "999"}}
    assert run(env.bridge.sync())["outcome"] == "unavailable"
    assert len(env.client.posts) == 1
    assert all(post["status"] == "posting" for post in env.store.delivery(DAY)["diablo_threads"]["posts"].values())


def test_concurrent_syncs_share_one_thread_and_one_draft_send(env):
    env.diablo.ready()
    async def both():
        return await asyncio.gather(env.bridge.sync(), env.bridge.sync())
    results = run(both())
    assert {result["outcome"] for result in results} == {"updated", "unchanged"}
    assert len(env.client.threads) == len(env.client.posts) == 1


def test_expired_day_is_not_reopened_or_delivered(env):
    assert run(env.bridge.sync("2026-10-01"))["outcome"] == "expired"
    assert not env.engine.reads and not env.client.threads


def test_actual_gt_refinement_replay_uses_canonical_request_not_a_second_draft_store(tmp_path, monkeypatch):
    # The existing GT fixture uses SQLite and the actual outbox/result verifier.
    from tests.review_card.test_diablo import hub as card_hub, dashboard_hub, send, reply, BASE as gt_base, OWNER
    generator = dashboard_hub.__wrapped__(tmp_path)
    gt = next(generator)
    try:
        gt = card_hub.__wrapped__(gt)
        send(gt)
        reply(gt)
        gt.worker.dispatch_once()
        store, engine = Store(tmp_path / "card.json"), Engine()
        engine.binding = {**gt_base, "operation_id": "fixture-ask"}
        engine.proof = {"record_id": gt.item["record_id"], "record_sha256": gt.item["record_sha256"]}
        settings = SimpleNamespace(enabled=True, card=True, ask_diablo=True, owner_id=OWNER,
                                   owner_user_id="11", guild_id="22", morning_channel_id="33")
        bridge = ThreadBridge(Client(store), store, engine, gt.ask, settings, bot_user_id="55", now=lambda: NOW)
        assert run(bridge.sync())["sent"] == 1
        original = gt.service.requests_create
        def lose(actor, command):
            original(actor, command)
            raise TimeoutError("synthetic lost GT acknowledgment")
        monkeypatch.setattr(gt.service, "requests_create", lose)
        assert run(bridge.on_message(event()))["outcome"] == "unavailable"
        monkeypatch.setattr(gt.service, "requests_create", original)
        engine.proof["record_sha256"] = "b" * 64
        gt.item["record_sha256"] = "b" * 64
        bridge = ThreadBridge(bridge.client, Store(store.path), engine, gt.ask, settings, bot_user_id="55", now=lambda: NOW)
        assert run(bridge.on_message(event()))["outcome"] == "requested"
        assert len(gt.store.fetchall("SELECT * FROM requests")) == 2
        assert not gt.saves
        assert event()["content"] not in store.path.read_text()
    finally:
        try:
            next(generator)
        except StopIteration:
            pass
