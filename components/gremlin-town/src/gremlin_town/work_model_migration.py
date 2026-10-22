"""Pinned PC/hub work migration, dry by default (plan W18).

Hub invocation is a future operator action using the stopped service's expanded
credential paths. This build executes only disposable fixtures and fake GitHub.
"""
from __future__ import annotations

import argparse
from datetime import UTC, datetime
import hashlib
from importlib.resources import files
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
from typing import TYPE_CHECKING, Mapping, Sequence

if TYPE_CHECKING:
    from brain_core.github_storage import GitHubBrainHomeStorage, GitHubRevision
    from gremlin_town.runtime import BrainMCPSettings

from brain_core.records import render_record
from brain_core.workmove import MovePlan, plan_move_over, plan_move_back
from brain_core.contracts import canonical_json

FIXTURE_MARKER = "work-model.disposable-fixture/v1"
BRAIN_HOME_APP_CREDENTIAL = "brain_app_key"


def _require_brain_app_key(path: Path, environment: Mapping[str, str]) -> None:
    expected = (Path(environment["CREDENTIALS_DIRECTORY"]) / BRAIN_HOME_APP_CREDENTIAL).resolve()
    if path.resolve() != expected:
        raise ValueError("Hub migration requires the Brain Home App key: set GT_GITHUB_APP_KEY_FILE "
                         "to CREDENTIALS_DIRECTORY/brain_app_key, loaded from the Brain Home App key")


def load_hub_settings(config_path: Path, *, environment: Mapping[str, str]) -> BrainMCPSettings:
    """Read only existing Brain settings, using expanded service credential paths.

    Never build a service runtime, enroll an owner, or create a config key.
    Validate credentials before opening the operator's configuration file.
    """
    token_file, app_key_file, _google_file = validate_credentials(environment)
    from gremlin_town.runtime import BrainMCPSettings, RUNTIME_CONFIG_SCHEMA

    with config_path.open("rb") as stream:
        config = tomllib.load(stream)
    raw = config.get("brain_core")
    if not isinstance(raw, dict) or raw.get("enabled") is not True:
        raise ValueError("Hub migration needs existing [brain_core] enabled settings")
    if set(raw) - RUNTIME_CONFIG_SCHEMA["brain_core"]:
        raise ValueError("Unknown [brain_core] setting")
    if raw.get("archive_superseded", False) is not False:
        raise ValueError("Migration requires storage layer 2 switched off")
    app_mode = any(k in raw for k in ("github_app_id", "github_installation_id", "github_app_private_key_file"))
    if app_mode == ("github_token_file" in raw):
        raise ValueError("Brain settings require exactly one existing GitHub credential mode")

    def text(name):
        value = raw.get(name)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"Missing Brain setting: {name}")
        return value

    def path(name):
        value = Path(text(name))
        return (value if value.is_absolute() else config_path.parent / value).resolve()

    def positive(name):
        value = raw.get(name)
        if type(value) is not int or value <= 0:
            raise ValueError(f"Brain setting must be a positive integer: {name}")
        return value

    # The service environment supplies the transient paths, but the existing
    # runtime must still declare its complete credential mode.
    text("github_app_private_key_file" if app_mode else "github_token_file")
    if app_mode:
        _require_brain_app_key(app_key_file, environment)
    return BrainMCPSettings(
        connector_url=text("connector_url"), connector_token_file=path("connector_token_file"),
        github_repository=text("github_repository"), github_branch=text("github_branch"),
        github_token_file=None if app_mode else token_file, policy_file=path("policy_file"),
        decision_staging_dir=path("decision_staging_dir"), ownership_catalog_file=path("ownership_catalog_file"),
        source_registry_file=path("source_registry_file"),
        github_app_id=positive("github_app_id") if app_mode else None,
        github_installation_id=positive("github_installation_id") if app_mode else None,
        github_app_private_key_file=app_key_file if app_mode else None, archive_superseded=False,
    )


def build_hub_storage(settings: BrainMCPSettings, *, environment: Mapping[str, str],
                      apply: bool = False) -> GitHubBrainHomeStorage:
    """Construct the future hub client through public settings/credential seams.

    Credentials must be direct children of CREDENTIALS_DIRECTORY; no %d paths.
    Dry App mode requests read permission. Layer-2 archive is always off. Tests mock
    credentials/config/constructors and refuse every real HTTP transport call.
    """
    token_file, app_key_file, _google_file = validate_credentials(environment)
    if settings.archive_superseded:
        raise ValueError("Migration requires storage layer 2 switched off")
    from brain_core.github_storage import GitHubBrainHomeStorage, GitHubObjectCache, UrllibHTTPTransport
    if settings.github_app_id is None:
        if settings.github_token_file is None or settings.github_token_file.resolve() != token_file:
            raise ValueError("Brain token path differs from service credential path")
        token = token_file.read_text(encoding="utf-8").strip()
        transport = None
    else:
        _require_brain_app_key(app_key_file, environment)
        if settings.github_app_private_key_file is None:
            raise ValueError("Hub migration requires the Brain Home App key in Brain settings")
        _require_brain_app_key(settings.github_app_private_key_file, environment)
        from gremlin_town.github_app import (
            APP_TOKEN_PLACEHOLDER, AppTokenHTTPTransport, GitHubAppTokenProvider, load_private_key,
        )
        provider = GitHubAppTokenProvider(
            app_id=settings.github_app_id, installation_id=settings.github_installation_id,
            private_key=load_private_key(app_key_file), repository=settings.github_repository,
            permissions={"contents": "write" if apply else "read"},
        )
        token, transport = APP_TOKEN_PLACEHOLDER, AppTokenHTTPTransport(provider, UrllibHTTPTransport())
    return GitHubBrainHomeStorage(repository=settings.github_repository, branch=settings.github_branch,
                                 token=token, transport=transport, object_cache=GitHubObjectCache(),
                                 archive_superseded=False)


def pre_move_inventory(storage, revision: GitHubRevision) -> tuple[str, ...]:
    """Every item first ID from the exact pinned input, including unchanged items.

    No inference from dates, planned rows, or post-move current records. This
    complete inventory is persisted before writes and registered only after a
    full successful move-over, never by a dry/partial run or a move-back.
    """
    from brain_core.records import first_id
    snapshot = storage.snapshot_records(kinds=frozenset({"item"}), revision=revision)
    if snapshot.revision != revision:
        raise ValueError("Bootstrap inventory differs from pinned input revision")
    return tuple(sorted({first_id(item.record) for item in snapshot.records}))


def register_review_bootstrap(why_log, *, expected_head: str,
                              first_ids: Sequence[str]) -> None:
    """Use W8's public pin/content-guarded, idempotent bootstrap import seam.

    Registration creates no surfacing/review rows and never consumes the once
    morning bootstrap. W8 owns import/read/consume behavior and its schema.
    """
    why_log.stage_morning_bootstrap(input_sha=expected_head, first_ids=first_ids)


def load_rules() -> dict:
    """Read the exact JSON shipped at the installed GT package root."""
    return json.loads(files("gremlin_town").joinpath("move_over_rules.json").read_text(encoding="utf-8"))


def record_path(record) -> str:
    return f"records/item/{record.created_at.astimezone(UTC):%Y/%m}/{record.id}.md"


def format_plan(plan: MovePlan, head: str) -> str:
    """One stable format for PC and hub snapshots; no machine/temp-root fields."""
    rows = []
    for row in plan.rows:
        before = row.before
        data = row.after.data
        rows.append(dict(first_id=row.first_id, title=before.title, group=row.group,
                         before=dict(state=before.state, size=before.data.get("size"), first_step=before.data.get("first_step"), dates=before.data.get("timing"), links=before.links, source=before.source),
                         after=dict(state=row.after.state, kind=row.reader["kind"], parent=data.get("parent"), order=data.get("order"), dates=data.get("dates"), labels=data.get("labels"), after=data.get("after"), needs=data.get("needs"), whose=data.get("whose"), marks=data.get("review_marks")),
                         reader=dict(row.reader), questions=row.questions))
    output = dict(schema="work.move-dry-run/v1", direction=plan.direction, head=head,
                  now=plan.now.isoformat(), rows=rows, test_data_to_archive=[a.current_path for a in plan.archive],
                  borderline=plan.borderline, stake_calls=plan.stake_calls,
                  finish_lines=[dict(first_id=row.first_id, text=row.after.data["finish_line"], words=row.after.source["words"])
                                for row in plan.rows if row.after is not row.before and row.after.data.get("finish_line")],
                  batches=[[record_path(r) for r in batch.records] for batch in plan.batches],
                  counts=dict(audited=sum(r.group == "audited" for r in plan.rows), general=sum(r.group == "moved by the general rules" for r in plan.rows), versions=sum(len(b.records) for b in plan.batches), archive=len(plan.archive)))
    return json.dumps(output, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def materialize(storage, revision, root: Path) -> None:
    """Materialize one immutable snapshot via the public W2 storage read seam."""
    snapshot = storage.snapshot_records(kinds=frozenset({"item"}), revision=revision)
    if snapshot.revision.commit_sha != revision.commit_sha:
        raise ValueError("Snapshot was not read at the pinned head")
    for item in snapshot.records:
        path = root / item.path
        if not item.path.startswith("records/item/") or ".." in Path(item.path).parts:
            raise ValueError("Unexpected snapshot path")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(item.content)


def validate_credentials(environment: dict[str, str]) -> tuple[Path, ...]:
    """Validate service credential paths without reading a key or creating a client.

    Each configured path must be a direct child of CREDENTIALS_DIRECTORY.
    This is a plan check, never an authorization to execute on this host.
    """
    directory = environment.get("CREDENTIALS_DIRECTORY")
    if not directory or "%d" in directory or not Path(directory).is_absolute():
        raise ValueError("Hub apply requires the service CREDENTIALS_DIRECTORY")
    base = Path(directory).resolve()
    result = []
    for name in ("GT_GITHUB_TOKEN_FILE", "GT_GITHUB_APP_KEY_FILE", "GT_GOOGLE_CREDENTIAL_FILE"):
        value = environment.get(name)
        if not value or "%d" in value or not Path(value).is_absolute():
            raise ValueError(f"Credential path must expand from CREDENTIALS_DIRECTORY: {name}")
        path = Path(value).resolve()
        if path.parent != base:
            raise ValueError(f"Credential path outside service directory: {name}")
        result.append(path)
    return tuple(result)


def _fixture(root: Path) -> None:
    marker = root / ".work-model-fixture"
    if root.is_symlink() or not marker.is_file() or marker.read_text(encoding="utf-8").strip() != FIXTURE_MARKER:
        raise ValueError("Local apply requires a marked disposable fixture clone")
    if any(path.is_symlink() for path in root.rglob("*")):
        raise ValueError("Fixture clone contains symlinks")


def apply_local(root: Path, plan: MovePlan) -> None:
    """Apply only to a marked disposable clone; preserve all archive bytes."""
    _fixture(root)
    writes = {record_path(r): render_record(r).encode("utf-8") for b in plan.batches for r in b.records}
    # Validate the entire operation before its first write/move.
    for name, content in writes.items():
        path = root / name
        if path.exists() and path.read_bytes() != content:
            raise ValueError(f"Record destination differs: {name}")
    for entry in plan.archive:
        source, target = root / entry.current_path, root / entry.archive_path
        content = source.read_bytes()
        if hashlib.sha256(content).hexdigest() != entry.content_sha256:
            raise ValueError("Archive source changed")
        if target.exists() and target.read_bytes() != content:
            raise ValueError("Archive destination differs")
    for name, content in writes.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    for entry in plan.archive:
        source, target = root / entry.current_path, root / entry.archive_path
        target.parent.mkdir(parents=True, exist_ok=True)
        source.replace(target)


def _state_write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".pending")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, sort_keys=True, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def _item_bytes(storage, revision):
    found = storage.snapshot_records(kinds=frozenset({"item"}), revision=revision)
    if found.revision != revision:
        raise ValueError("Snapshot revision differs from observed commit")
    return {item.path: item.content for item in found.records}


def observe_migration(storage, expected_head, current, plan):
    """Verify an exact own prefix using W12 history and W2 pinned snapshots.

    No storage internals or transport fallback. The accepted-batch helper proves
    every ID/hash was accepted together and was absent in the immediate parent.
    Snapshots additionally refuse unrelated item edits in those commits.
    """
    history = storage.observe_commits_since(expected_head, revision=current)
    if history.base.commit_sha != expected_head or history.head != current:
        raise ValueError("Unverified migration history")
    before = history.base
    landed, archived = [], None
    for position, commit in enumerate(history.commits):
        if commit.parent_sha != before.commit_sha:
            raise ValueError("Foreign intervening commit or incomplete chain")
        old_tree = _item_bytes(storage, before)
        new_tree = _item_bytes(storage, commit.revision)
        if position < len(plan.batches):
            records = plan.batches[position].records
            expected_message = f"Publish {len(records)} Brain Core records together\n\n" + "".join(
                f"Brain-Core-Batch-Record: {r.id} {hashlib.sha256(render_record(r).encode()).hexdigest()}\n" for r in records)
            if commit.message != expected_message:
                raise ValueError("Foreign intervening commit")
            accepted = storage.revision_before_batch(records=records, revision=commit.revision)
            if accepted.accepted != commit.revision or accepted.before != before:
                raise ValueError("Batch not accepted at this exact parent/head")
            writes = {record_path(r): render_record(r).encode() for r in records}
            if set(writes).intersection(old_tree) or new_tree != {**old_tree, **writes}:
                raise ValueError("Foreign intervening item change")
            landed.append(commit.revision.commit_sha)
        elif position == len(plan.batches) and plan.archive and position == len(history.commits) - 1:
            paths = sorted(entry.current_path for entry in plan.archive)
            digest = hashlib.sha256(canonical_json(paths).encode()).hexdigest()
            action = "Archive" if plan.direction == "over" else "Archive superseded"
            selected = " selected" if plan.direction == "over" else ""
            expected_message = f"{action} {len(paths)} Brain Core{selected} records\n\nBrain-Core-Archive-Count: {len(paths)}\nBrain-Core-Archive-SHA256: {digest}\n"
            if commit.message != expected_message or new_tree != {path: content for path, content in old_tree.items() if path not in paths}:
                raise ValueError("Foreign intervening archive commit")
            read = storage.read_records(paths, archived_ok=True)
            if read.revision != current or len(read.records) != len(paths):
                raise ValueError("Archive verification head changed")
            expected = {entry.current_path: entry.content_sha256 for entry in plan.archive}
            for item in read.records:
                if not item.archived or hashlib.sha256(item.content).hexdigest() != expected[item.path] or item.content != old_tree.get(item.path):
                    raise ValueError("Archive bytes differ from exact named source")
            archived = commit.revision.commit_sha
        else:
            raise ValueError("Foreign intervening commit after migration")
        before = commit.revision
    return landed, archived


def apply_storage(storage, plan: MovePlan, expected_head: str, answers: dict,
                  state_path: Path, *, stop_after: int | None = None) -> str:
    """Apply W2 publish/archive with strict public history guards on resume.

    W12's observer supplies the immutable complete linear chain; W18 verifies
    own batches/archive through public storage seams. No transport fallback.
    """
    if storage.archive_superseded:
        raise ValueError("Migration requires storage layer 2 switched off")
    batches = [[dict(path=record_path(r), sha256=hashlib.sha256(render_record(r).encode()).hexdigest()) for r in b.records] for b in plan.batches]
    identity = dict(schema="work.move-resume/v1", direction=plan.direction, now=plan.now.isoformat(), expected_head=expected_head,
                    batches=batches, archive=[dict(path=e.current_path, sha256=e.content_sha256) for e in plan.archive], answers=answers)
    current = storage.revision()
    inventory = None
    if plan.direction == "over":
        baseline = current if current.commit_sha == expected_head else storage.observe_commits_since(expected_head, revision=current).base
        inventory = dict(expected_head=expected_head, first_ids=list(pre_move_inventory(storage, baseline)))
    identity["pre_move_inventory"] = inventory
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if any(state.get(k) != value for k, value in identity.items()):
            raise ValueError("Resume plan/answers/head/now changed")
    else:
        if current.commit_sha != expected_head:
            raise ValueError("Expected head moved")
        state = dict(identity, landed=[], archive_commit=None)
        _state_write(state_path, state)  # durable before the first commit
    if current.commit_sha != expected_head:
        observed = observe_migration(storage, expected_head, current, plan)
        # The observer must return a verified prefix, including a possible lost
        # reply after an atomic commit. Foreign or non-linear commits refuse.
        commits, archive_commit = observed
        if len(commits) > len(plan.batches) or commits[:len(state["landed"])] != state["landed"]:
            raise ValueError("Foreign intervening commit or non-prefix replay")
        if state["archive_commit"] and state["archive_commit"] != archive_commit:
            raise ValueError("Archive replay differs")
        state.update(landed=list(commits), archive_commit=archive_commit)
        _state_write(state_path, state)
    elif state["landed"] or state["archive_commit"]:
        raise ValueError("Head rewound behind this migration")
    for index in range(len(state["landed"]), len(plan.batches)):
        if stop_after is not None and index >= stop_after:
            return current.commit_sha
        batch = plan.batches[index]
        result = storage.publish_records(records=[render_record(r).encode() for r in batch.records], base_revision=current)
        state["landed"].append(result.commit_sha)
        _state_write(state_path, state)
        current = storage.revision()
        if current.commit_sha != result.commit_sha:
            raise ValueError("Head moved after batch commit")
    if plan.archive and not state["archive_commit"]:
        paths = [entry.current_path for entry in plan.archive]
        if plan.direction == "over":
            result = storage.archive_records(paths=paths, expected_revision=current.commit_sha, kinds=frozenset({"item"}))
        else:
            result = storage.archive_superseded_records(paths=paths, expected_revision=current.commit_sha)
        state["archive_commit"] = result.commit_sha
        _state_write(state_path, state)
        current = storage.revision()
        if current.commit_sha != result.commit_sha:
            raise ValueError("Head moved after archive commit")
    return current.commit_sha


def _git_head(root: Path) -> str:
    return subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()


def main(arguments=None, *, direction="over", storage=None,
         environment: Mapping[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, nargs="?")
    parser.add_argument("--expected-head", required=True)
    parser.add_argument("--now", default=None)
    parser.add_argument("--answers", type=Path)
    parser.add_argument("--local", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--state", type=Path)
    parser.add_argument("--hub", action="store_true", help="Use service credentials and a private pinned snapshot")
    parser.add_argument("--config", type=Path, help="Existing hub runtime TOML (required with --hub)")
    parser.add_argument("--why-log", type=Path, help="Existing derivative why-log; import baseline after complete move-over")
    opts = parser.parse_args(arguments)
    if opts.local and (opts.apply or opts.hub):
        parser.error("--local and --apply are separate modes")
    if opts.apply and (not (opts.hub or storage is not None) or opts.answers is None or opts.state is None):
        parser.error("--apply requires hub mode or injected storage, --answers and --state")
    if opts.root is None:
        parser.error("A pinned clone or explicit private hub scratch parent is required")
    if opts.hub and opts.config is None:
        parser.error("--hub requires --config")
    if opts.config is not None and not opts.hub:
        parser.error("--config requires --hub")
    if opts.hub and opts.apply and direction == "over" and opts.why_log is None:
        parser.error("Hub move-over apply requires --why-log for the existing review baseline")
    if opts.why_log is not None and (opts.local or direction != "over" or not opts.apply):
        parser.error("--why-log is only for complete storage move-over apply")
    if opts.why_log is not None and not opts.why_log.is_file():
        parser.error("--why-log must name an existing derivative state file")
    if opts.local and storage is not None:
        parser.error("--local only accepts a disposable PC fixture clone")
    if opts.hub:
        service_environment = os.environ if environment is None else environment
        settings = load_hub_settings(opts.config, environment=service_environment)
        if storage is None:
            storage = build_hub_storage(settings, environment=service_environment, apply=opts.apply)
    answers = json.loads(opts.answers.read_text(encoding="utf-8")) if opts.answers else {}
    saved = json.loads(opts.state.read_text(encoding="utf-8")) if opts.state and opts.state.exists() else None
    now = datetime.fromisoformat(saved["now"] if saved else opts.now) if saved or opts.now else datetime.now(UTC)
    if storage is None:
        root = opts.root.resolve()
        if _git_head(root) != opts.expected_head:
            raise ValueError("Expected head moved")
        if opts.local:
            _fixture(root)
        plan = plan_move_over(root, load_rules(), answers, now) if direction == "over" else plan_move_back(root)
        print(format_plan(plan, opts.expected_head), end="")
        if opts.local:
            apply_local(root, plan)
    else:
        # A new run pins the current revision. A resume's historical revision is
        # supplied by W12's public observer; never invent it or use private IO.
        revision = storage.revision()
        if revision.commit_sha != opts.expected_head:
            if saved is None:
                raise ValueError("Expected head moved")
            revision = storage.observe_commits_since(opts.expected_head, revision=revision).base
        scratch = opts.root
        if scratch is None:
            parser.error("Fake hub materialization requires an explicit disposable scratch root")
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="work-move-", dir=scratch) as private:
            materialize(storage, revision, Path(private))
            plan = plan_move_over(private, load_rules(), answers, now) if direction == "over" else plan_move_back(private)
            print(format_plan(plan, opts.expected_head), end="")
            if opts.apply:
                why_log = None
                if opts.why_log is not None:
                    from gremlin_town.review.why_log import WhyLog
                    why_log = WhyLog(str(opts.why_log))
                try:
                    if why_log is not None:
                        known = frozenset(pre_move_inventory(storage, revision))
                        staged = why_log.morning_bootstrap()
                        if staged is not None and (staged.input_sha != opts.expected_head or staged.known_first_ids != known):
                            raise ValueError("Review bootstrap pin/inventory changed")
                    apply_storage(storage, plan, opts.expected_head, answers, opts.state)
                    if why_log is not None:
                        saved = json.loads(opts.state.read_text(encoding="utf-8"))
                        if len(saved["landed"]) != len(plan.batches) or (plan.archive and not saved["archive_commit"]):
                            raise ValueError("Incomplete move-over cannot register the review baseline")
                        baseline = saved["pre_move_inventory"]
                        register_review_bootstrap(why_log, expected_head=baseline["expected_head"], first_ids=baseline["first_ids"])
                finally:
                    if why_log is not None:
                        why_log.close()
    return 0



__all__ = ['FIXTURE_MARKER', 'MovePlan', 'plan_move_over', 'plan_move_back', 'load_hub_settings', 'build_hub_storage', 'pre_move_inventory', 'register_review_bootstrap', 'load_rules', 'record_path', 'format_plan', 'materialize', 'validate_credentials', 'apply_local', 'observe_migration', 'apply_storage', 'main']
