"""Fake GitHub, disposable clones, packaged rules and legacy-shape round trips."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from copy import deepcopy
from datetime import timedelta
import zipfile

import pytest
import httpx

from brain_core import moves, work
from brain_core.github_storage import GitHubRevision, GitHubConflict, GitHubBrainHomeStorage, GitHubObjectCache, GitHubStorageError, HTTPResponse
from brain_core.records import WORK_FIELDS, first_id, parse_record, record_from_mapping, render_record
from brain_core.workmove import plan_move_over, plan_move_back

COMPONENTS = Path(__file__).parents[3]
BC = COMPONENTS / "brain-core"
GT = COMPONENTS / "gremlin-town"
REPO = COMPONENTS.parent


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    sys.modules[name] = result
    spec.loader.exec_module(result)
    return result


from gremlin_town import work_model_migration as release
helpers = module("w18_gt_fixture", BC / "tests/fixtures/work_model/helpers.py")
storage_tests = module("w18_storage_tests", BC / "tests/unit/test_github_storage.py")


@pytest.fixture(autouse=True)
def refuse_real_transport(monkeypatch):
    """Every migration proof fails closed if an actual HTTP client is reached."""
    from brain_core.github_storage import UrllibHTTPTransport
    def refused(*args, **kwargs):
        pytest.fail("W18 fixture test reached a real HTTP transport")
    monkeypatch.setattr(UrllibHTTPTransport, "request", refused)
    monkeypatch.setattr(httpx.Client, "send", refused)
    monkeypatch.setattr(httpx.AsyncClient, "send", refused)


class MigrationGitHub(storage_tests.FakeGitHub):
    """Serve complete GitHub commit metadata; storage performs all replay reads.

    The older shared fake's commit endpoint only serves identity/tree. W12's
    public observer requires the API's message and parent list as well. This
    adds those wire fields to the owned test fake, never a replay implementation.
    """

    def request(self, method, url, *, headers, body):
        response = super().request(method, url, headers=headers, body=body)
        if method == "GET" and "/git/commits/" in url:
            commit = self.commits[url.rsplit("/", 1)[-1]]
            value = json.loads(response.body)
            value["message"] = commit["message"]
            value["parents"] = commit.get("parents", [] if commit["parent"] is None else [{"sha": commit["parent"]}])
            return HTTPResponse(response.status, response.headers, json.dumps(value).encode())
        return response


def fixture(root, made_up=False):
    helpers.write_fixture(root, include_made_up=made_up)
    (root / ".work-model-fixture").write_text(release.FIXTURE_MARKER, encoding="utf-8")


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout.strip()


def clone(root):
    fixture(root)
    git(root, "init")
    git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "add", "--", "records", ".work-model-fixture")
    git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "fixture")
    return git(root, "rev-parse", "HEAD")


def pair(root, made_up=False):
    fixture(root, made_up)
    fake = MigrationGitHub()
    fake.seed({p.relative_to(root).as_posix(): p.read_bytes() for p in (root / "records").rglob("*.md")})
    return fake, GitHubBrainHomeStorage(repository=storage_tests.REPOSITORY, branch=storage_tests.BRANCH,
        token=storage_tests.TOKEN, transport=fake, object_cache=GitHubObjectCache())


def test_pc_and_fake_hub_dry_run_identical(tmp_path):
    pc, hub = tmp_path / "pc", tmp_path / "hub"
    fake, storage = pair(pc)
    revision = storage.revision()
    release.materialize(storage, revision, hub)
    rules = release.load_rules()
    local = plan_move_over(pc, rules, {}, helpers.NOW)
    remote = plan_move_over(hub, rules, {}, helpers.NOW)
    assert release.format_plan(local, revision.commit_sha) == release.format_plan(remote, revision.commit_sha)
    printed = json.loads(release.format_plan(local, revision.commit_sha))
    assert printed["counts"] == dict(audited=13, general=0, versions=13, archive=3)
    assert not any(event.startswith("POST") or event.startswith("PATCH") for event in fake.events)


@pytest.fixture(scope="module")
def migration_wheel(tmp_path_factory):
    # Build offline into the caller-provided disposable test folder.
    tmp_path = tmp_path_factory.mktemp("migration-wheel")
    result = subprocess.run(["uv", "build", "--wheel", "--offline", "--out-dir", str(tmp_path)], cwd=GT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return next(tmp_path.glob("*.whl"))


def test_rules_in_actual_wheel(tmp_path, migration_wheel):
    wheel = migration_wheel
    with zipfile.ZipFile(wheel) as archive:
        assert json.loads(archive.read("gremlin_town/move_over_rules.json")) == release.load_rules()
        assert archive.read("gremlin_town/work_model_migration.py") == (GT / "src/gremlin_town/work_model_migration.py").read_bytes()
    code = "import json; from importlib.resources import files; print(json.loads(files('gremlin_town').joinpath('move_over_rules.json').read_text())['schema'])"
    import os
    env = dict(os.environ, PYTHONPATH=str(wheel))
    result = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "work.move-over-rules/v1"


@pytest.mark.parametrize("direction", ["over", "back"])
@pytest.mark.parametrize("malicious_sibling", [False, True])
def test_hashed_scripts_pipe_from_empty_or_malicious_cwd_using_wheel(tmp_path, migration_wheel, direction, malicious_sibling):
    import os
    root = tmp_path / "clone"
    head = clone(root)
    if direction == "back":
        originals = {p.relative_to(root).as_posix() for p in (root / "records").rglob("*.md")}
        release.apply_local(root, plan_move_over(root, release.load_rules(), {}, helpers.NOW))
        files = originals | {p.relative_to(root).as_posix() for p in root.rglob("*.md")}
        git(root, "add", "--", *sorted(files))
        git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "fixture move-over")
        head = git(root, "rev-parse", "HEAD")
    cwd = tmp_path / "empty-cwd"
    cwd.mkdir()
    marker = cwd / "unchecked-sibling-executed"
    if malicious_sibling:
        payload = "from pathlib import Path\nPath('unchecked-sibling-executed').write_text('unsafe')\nraise RuntimeError('unchecked sibling executed')\n"
        for name in ("work-move-over.py", "work-move-back.py"):
            (cwd / name).write_text(payload, encoding="utf-8")
    script = GT / f"deploy/release/work-move-{direction}.py"
    arguments = [str(root), "--expected-head", head, "--now", helpers.NOW.isoformat()]
    plan = plan_move_over(root, release.load_rules(), {}, helpers.NOW) if direction == "over" else plan_move_back(root)
    # Keep the original empty/hyphenated-sibling cases, then poison each real
    # package independently as well as together. The staged script directory
    # is poisoned too, so path execution cannot prepend that directory either.
    stage = tmp_path / "staged-script"
    stage.mkdir()
    staged_script = stage / script.name
    staged_script.write_bytes(script.read_bytes())
    profiles = [()] if not malicious_sibling else [(), ("gremlin_town",), ("brain_core",), ("gremlin_town", "brain_core")]
    for packages in profiles:
        # Use fresh directories for each profile; never remove trusted paths.
        poisoned_cwd = cwd if not packages else tmp_path / ("cwd-" + "-".join(packages))
        poisoned_stage = stage if not packages else tmp_path / ("stage-" + "-".join(packages))
        if packages:
            poisoned_cwd.mkdir()
            poisoned_stage.mkdir()
            (poisoned_stage / script.name).write_bytes(script.read_bytes())
            for directory in (poisoned_cwd, poisoned_stage):
                for package in packages:
                    shadow = directory / package
                    shadow.mkdir()
                    (shadow / "__init__.py").write_text(payload, encoding="utf-8")
            for name in ("work-move-over.py", "work-move-back.py"):
                (poisoned_cwd / name).write_text(payload, encoding="utf-8")
        for trusted in (migration_wheel, GT / "src"):
            env = dict(os.environ, PYTHONPATH=os.pathsep.join((str(trusted), str(BC / "src"))))
            # -P makes this separate artifact-origin probe safe; actual wrappers
            # below must also work without it, using their own startup guard.
            origin = subprocess.run([sys.executable, "-P", "-c",
                "from gremlin_town import work_model_migration as m; from brain_core import workmove as b; print(m.__file__); print(b.__file__)"],
                cwd=poisoned_cwd, env=env, capture_output=True, text=True)
            assert origin.returncode == 0, origin.stderr
            origins = origin.stdout.splitlines()
            assert str(trusted) in origins[0]
            assert Path(origins[1]).resolve() == (BC / "src/brain_core/workmove.py").resolve()
            path_run = subprocess.run([sys.executable, str(script), *arguments], cwd=poisoned_cwd, env=env, capture_output=True, text=True)
            staged_run = subprocess.run([sys.executable, str(poisoned_stage / script.name), *arguments], cwd=poisoned_cwd, env=env, capture_output=True, text=True)
            piped = subprocess.run([sys.executable, "-", *arguments], input=script.read_text(encoding="utf-8"),
                                   cwd=poisoned_cwd, env=env, capture_output=True, text=True)
            safe_piped = subprocess.run([sys.executable, "-P", "-", *arguments], input=script.read_text(encoding="utf-8"),
                                        cwd=poisoned_cwd, env=env, capture_output=True, text=True)
            assert path_run.returncode == 0, path_run.stderr
            assert staged_run.returncode == 0, staged_run.stderr
            assert piped.returncode == 0, piped.stderr
            assert safe_piped.returncode == 0, safe_piped.stderr
            assert piped.stdout == path_run.stdout == staged_run.stdout == safe_piped.stdout
            assert piped.stdout == release.format_plan(plan, head)
            assert not marker.exists()
            assert not (poisoned_cwd / marker.name).exists()
            assert not (poisoned_stage / marker.name).exists()
            assert git(root, "rev-parse", "HEAD") == head


def test_release_wrappers_reexport_all_current_public_helpers():
    for direction in ("over", "back"):
        trusted_paths = list(sys.path)
        wrapper = module(f"w18_{direction}_wrapper", GT / f"deploy/release/work-move-{direction}.py")
        assert sys.path == trusted_paths  # import callers retain their own paths
        assert wrapper.FIXTURE_MARKER == release.FIXTURE_MARKER
        for name in release.__all__:
            if name not in {"main", "FIXTURE_MARKER"}:
                assert getattr(wrapper, name) is getattr(release, name)
        assert callable(wrapper.main)


def test_fake_atomic_batches_resume_and_head_guards(tmp_path):
    root = tmp_path / "clone"
    fake, storage = pair(root, True)
    head = storage.revision().commit_sha
    plan = plan_move_over(root, release.load_rules(), {}, helpers.NOW)
    state = tmp_path / "resume.json"
    release.apply_storage(storage, plan, head, {}, state, stop_after=1)
    saved = json.loads(state.read_text())
    assert saved["now"] == helpers.NOW.isoformat()
    assert saved["expected_head"] == head
    assert saved["pre_move_inventory"] == dict(expected_head=head, first_ids=sorted({first_id(r) for r in (*helpers.load_records(), *helpers.load_records("made_up_post_audit"))}))
    assert len(saved["batches"]) == 2 and len(saved["landed"]) == 1
    first = fake.branch
    release.apply_storage(storage, plan, head, {}, state)
    commits, archive = release.observe_migration(storage, head, storage.revision(), plan)
    assert commits[0] == first and len(commits) == 2 and archive == fake.branch
    history = storage.observe_commits_since(head, revision=storage.revision())
    assert history.base.commit_sha == head and history.head.commit_sha == archive
    assert [c.revision.commit_sha for c in history.commits] == [*commits, archive]
    assert [c.parent_sha for c in history.commits] == [head, *commits]
    assert len(fake.commits) == 5  # root, seed, two publish, archive
    for entry in plan.archive:
        after = fake.trees[fake.commits[fake.branch]["tree"]]
        assert entry.current_path not in after
        assert fake.blobs[after[entry.archive_path]] == (root / entry.current_path).read_bytes()
    assert release.apply_storage(storage, plan, head, {}, state) == fake.branch
    fake.seed({"foreign.txt": b"external"})
    with pytest.raises(ValueError, match="Foreign"):
        release.apply_storage(storage, plan, head, {}, state)
    with pytest.raises(ValueError, match="head moved"):
        release.apply_storage(storage, plan, head, {}, tmp_path / "fresh.json")
    with pytest.raises(ValueError, match="changed"):
        release.apply_storage(storage, plan, head, {"different": True}, state)


def test_state_durable_before_first_publish_and_lost_reply(tmp_path):
    fake, storage = pair(tmp_path / "clone")
    plan = plan_move_over(tmp_path / "clone", release.load_rules(), {}, helpers.NOW)
    state = tmp_path / "state.json"
    head = fake.branch
    publish = storage.publish_records
    def stopped(**kwargs):
        saved = json.loads(state.read_text())
        assert saved["expected_head"] == head and saved["batches"] and saved["landed"] == []
        publish(**kwargs)
        raise RuntimeError("fixture crash after atomic publish")
    storage.publish_records = stopped
    with pytest.raises(RuntimeError, match="fixture crash"):
        release.apply_storage(storage, plan, head, {}, state)
    storage.publish_records = publish
    own_commit = fake.branch
    release.apply_storage(storage, plan, head, {}, state)
    saved = json.loads(state.read_text())
    assert saved["landed"] == [own_commit]
    assert len(fake.commits) == 4


def test_fake_cli_replans_resume_from_original_head_and_saved_clock(tmp_path, capsys):
    root = tmp_path / "clone"
    fake, storage = pair(root, True)
    plan = plan_move_over(root, release.load_rules(), {}, helpers.NOW)
    head = fake.branch
    state, answers = tmp_path / "state.json", tmp_path / "answers.json"
    answers.write_text("{}", encoding="utf-8")
    release.apply_storage(storage, plan, head, {}, state, stop_after=1)
    recorded = json.loads(state.read_text())
    first_batch = recorded["landed"][0]
    assert release.main([str(tmp_path / "scratch"), "--expected-head", head,
                         "--answers", str(answers), "--state", str(state), "--apply"], storage=storage) == 0
    printed = json.loads(capsys.readouterr().out)
    resumed = json.loads(state.read_text())
    assert printed["now"] == helpers.NOW.isoformat()
    assert printed["head"] == head
    assert resumed["batches"] == recorded["batches"]
    assert resumed["landed"][0] == first_batch and len(resumed["landed"]) == 2
    assert len(fake.commits) == 5


@pytest.mark.parametrize("parents", [[], [{"sha": "0" * 40}, {"sha": "1" * 40}]])
def test_resume_refuses_incomplete_or_merge_history(tmp_path, parents):
    root = tmp_path / "clone"
    fake, storage = pair(root)
    plan = plan_move_over(root, release.load_rules(), {}, helpers.NOW)
    head, state = fake.branch, tmp_path / "state.json"
    release.apply_storage(storage, plan, head, {}, state, stop_after=1)
    before = state.read_bytes()
    patches = len(fake.patch_payloads)
    fake.commits[fake.branch]["parents"] = parents
    with pytest.raises(GitHubStorageError, match="complete single-parent chain"):
        release.apply_storage(storage, plan, head, {}, state)
    assert state.read_bytes() == before
    assert len(fake.patch_payloads) == patches


@pytest.mark.parametrize("direction", ["over", "back"])
def test_lost_archive_reply_resumes_exact_bytes_without_new_commit(tmp_path, direction):
    root = tmp_path / "clone"
    fake, storage = pair(root)
    plan = plan_move_over(root, release.load_rules(), {}, helpers.NOW)
    if direction == "back":
        release.apply_storage(storage, plan, fake.branch, {}, tmp_path / "over.json")
        root = tmp_path / "moved-over"
        release.materialize(storage, storage.revision(), root)
        plan = plan_move_back(root)
    head, state = fake.branch, tmp_path / "state.json"
    method = "archive_records" if direction == "over" else "archive_superseded_records"
    archive = getattr(storage, method)
    def stopped(**kwargs):
        saved = json.loads(state.read_text())
        assert len(saved["landed"]) == len(plan.batches) and saved["archive_commit"] is None
        archive(**kwargs)
        raise RuntimeError("fixture crash after atomic archive")
    setattr(storage, method, stopped)
    with pytest.raises(RuntimeError, match="crash after atomic archive"):
        release.apply_storage(storage, plan, head, {}, state)
    setattr(storage, method, archive)
    archived_head, patches = fake.branch, len(fake.patch_payloads)
    assert release.apply_storage(storage, plan, head, {}, state) == archived_head
    assert len(fake.patch_payloads) == patches
    assert json.loads(state.read_text())["archive_commit"] == archived_head
    for entry in plan.archive:
        tree = fake.trees[fake.commits[archived_head]["tree"]]
        assert entry.current_path not in tree
        assert fake.blobs[tree[entry.archive_path]] == (root / entry.current_path).read_bytes()


def test_atomic_conflict_never_partial(tmp_path):
    fake, storage = pair(tmp_path / "clone")
    plan = plan_move_over(tmp_path / "clone", release.load_rules(), {}, helpers.NOW)
    head = fake.branch
    fake.conflict_on_patch = True
    with pytest.raises(GitHubConflict):
        release.apply_storage(storage, plan, head, {}, tmp_path / "state.json")
    # FakeGitHub simulates a foreign fast-forward before rejecting our patch.
    active = fake.trees[fake.commits[fake.branch]["tree"]]
    assert not {release.record_path(r) for b in plan.batches for r in b.records}.intersection(active)
    assert json.loads((tmp_path / "state.json").read_text())["landed"] == []


def test_local_cli_is_dry_by_default_and_fixture_only(tmp_path, capsys):
    head = clone(tmp_path)
    before = {p: p.read_bytes() for p in (tmp_path / "records").rglob("*.md")}
    args = [str(tmp_path), "--expected-head", head, "--now", helpers.NOW.isoformat()]
    assert release.main(args) == 0
    assert json.loads(capsys.readouterr().out)["counts"]["audited"] == 13
    assert before == {p:p.read_bytes() for p in (tmp_path / "records").rglob("*.md")}
    (tmp_path / ".work-model-fixture").write_text("unmarked", encoding="utf-8")
    with pytest.raises(ValueError, match="disposable"):
        release.main([*args, "--local"])
    with pytest.raises(ValueError, match="head moved"):
        release.main([str(tmp_path), "--expected-head", "0" * 40])


# Closed legacy item/timing field contract. This public snapshot carries no
# private historical Git objects; these checks prove shape and current-reader
# compatibility, not execution of an unavailable historical release.
OLD_ITEM_FIELDS = {"level", "size", "owed_to", "repeat", "timing", "reason", "check", "parked_until", "aliases", "importance", "block", "raise_with", "first_step", "occurrence"}
OLD_TIMING_FIELDS = {"due", "at", "window", "on_event", "on_return", "duration_minutes", "padded_minutes", "resolved_from", "start_by", "estimated_by"}


@pytest.mark.parametrize("with_finish_line", [False, True])
def test_move_back_restores_closed_legacy_shape_and_current_reader_compatibility(tmp_path, with_finish_line):
    fixture(tmp_path)
    key = "brn_20260929_123414_d00000000000000f"
    answers = {"finish_lines": {key: dict(text="Permit approved", words="Permit approved")}} if with_finish_line else {}
    over = plan_move_over(tmp_path, release.load_rules(), answers, helpers.NOW)
    release.apply_local(tmp_path, over)
    assert not set(over.batches[0].records[0].data) <= OLD_ITEM_FIELDS, "negative control must use the new shape"
    seed = next(r for r in over.records if r.data.get("level") == "accepted")
    rowan = deepcopy(seed.as_dict())
    rowan.update(id="brn_20260930_050000_000000000000aa01", created_at="2026-09-30T05:00:00+00:00", updated_at="2026-09-30T05:00:00+00:00", supersedes=[], links=[], title="Rowan sends the Hall forms", state="open",
                   data=dict(level="accepted", size="task", whose="Rowan", expected=dict(words="by friday", day="2026-10-02", read_by="code", read_on="2026-09-30")))
    parked = deepcopy(rowan)
    parked.update(id="brn_20260930_050000_000000000000aa02", title="Parked stake and deadline", state="parked",
                  data=dict(level="accepted", size="task", parked_until="2026-10-02", reason="park until Friday", stake=dict(level="nuisance", confirmed=True, proposed_by="fixture"),
                            dates=dict(deadline=dict(words="by friday", day="2026-10-02", read_by="code", read_on="2026-09-30"), start_by=dict(words="start by today", day="2026-09-30", read_by="code", read_on="2026-09-30"))))
    new_records = [record_from_mapping(v, "fixture") for v in (rowan, parked)]
    for record in new_records:
        path = tmp_path / release.record_path(record)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_record(record), encoding="utf-8")
    back = plan_move_back(tmp_path)
    release.apply_local(tmp_path, back)
    for path in (tmp_path / "records/item").rglob("*.md"):
        record = parse_record(path.read_text(encoding="utf-8"), str(path))
        assert set(record.data) <= OLD_ITEM_FIELDS
        assert set(record.data.get("timing") or {}) <= OLD_TIMING_FIELDS
    current = {first_id(r):r for r in back.records if r.id not in {ref for p in back.records for ref in p.supersedes}}
    assert "finish_line" not in current[key].data
    j = current[new_records[0].id]
    assert j.state == "waiting" and j.data["timing"]["due"] == "2026-10-02"
    assert dict(type="waiting_on", target="Rowan") in j.links
    p = current[new_records[1].id]
    assert p.state == "parked" and p.data["timing"]["due"] == "2026-10-02" and p.data["timing"]["start_by"] == "2026-09-30"
    assert "stake" not in p.data
    for entry in back.archive:
        assert not (tmp_path / entry.current_path).exists()
        assert (tmp_path / entry.archive_path).is_file()


def test_service_credential_path_validation_never_reads_secrets(tmp_path):
    env = dict(CREDENTIALS_DIRECTORY=str(tmp_path))
    for key in ("GT_GITHUB_TOKEN_FILE", "GT_GITHUB_APP_KEY_FILE", "GT_GOOGLE_CREDENTIAL_FILE"):
        env[key] = str(tmp_path / key)
    assert len(release.validate_credentials(env)) == 3
    env["GT_GITHUB_APP_KEY_FILE"] = str(tmp_path.parent / "other")
    with pytest.raises(ValueError, match="outside"):
        release.validate_credentials(env)


def hub_config(root, *, archive=False, token_mode=False):
    """Synthetic runtime TOML; unused service paths are deliberately unreadable."""
    config = root / "runtime.toml"
    config.write_text('''[brain_core]
enabled = true
connector_url = "http://127.0.0.1:39999"
connector_token_file = "unread-connector"
github_repository = "''' + storage_tests.REPOSITORY + '''"
github_branch = "''' + storage_tests.BRANCH + '''"
policy_file = "unread-policy"
decision_staging_dir = "unopened-state"
ownership_catalog_file = "unread-owners"
source_registry_file = "unread-sources"
archive_superseded = ''' + str(archive).lower() + '\n' + (
        'github_token_file = "ignored-service-token-path"\n' if token_mode else
        'github_app_id = 101\ngithub_installation_id = 202\ngithub_app_private_key_file = "ignored-service-app-path"\n'), encoding="utf-8")
    directory = root / "service-credentials"
    env = dict(CREDENTIALS_DIRECTORY=str(directory))
    env.update({key:str(directory / key) for key in ("GT_GITHUB_TOKEN_FILE", "GT_GITHUB_APP_KEY_FILE", "GT_GOOGLE_CREDENTIAL_FILE")})
    env["GT_GITHUB_APP_KEY_FILE"] = str(directory / "brain_app_key")
    return config, env


def mock_hub_credentials(monkeypatch, fake, environment):
    from brain_core import github_storage
    from gremlin_town import github_app
    observed = []
    def key(path):
        assert path == Path(environment["GT_GITHUB_APP_KEY_FILE"])
        observed.append(("key", path))
        return "fixture PRIVATE KEY (never parsed or sent)"
    def provider(**kwargs):
        observed.append(("provider", kwargs))
        from types import SimpleNamespace
        return SimpleNamespace(token=lambda:storage_tests.TOKEN)
    monkeypatch.setattr(github_app, "load_private_key", key)
    monkeypatch.setattr(github_app, "GitHubAppTokenProvider", provider)
    monkeypatch.setattr(github_storage, "UrllibHTTPTransport", lambda:fake)
    return observed


def test_hub_constructor_and_cli_dry_are_public_and_read_only(tmp_path, monkeypatch, capsys):
    pc = tmp_path / "pc"
    fake, _storage = pair(pc)
    config, env = hub_config(tmp_path)
    observed = mock_hub_credentials(monkeypatch, fake, env)
    head = fake.branch
    assert release.main([str(tmp_path / "scratch"), "--hub", "--config", str(config),
                         "--expected-head", head, "--now", helpers.NOW.isoformat()], environment=env) == 0
    output = capsys.readouterr().out
    assert output == release.format_plan(plan_move_over(pc, release.load_rules(), {}, helpers.NOW), head)
    assert fake.branch == head and not fake.patch_payloads
    assert not any(event.startswith(("POST", "PATCH")) for event in fake.events)
    assert observed[1][1]["permissions"] == dict(contents="read")
    assert observed[1][1]["app_id"] == 101 and observed[1][1]["installation_id"] == 202
    assert not Path(env["CREDENTIALS_DIRECTORY"]).exists()


class MockMorningLog:
    """Only W8's published stage/read API; no alternate database writer."""
    def __init__(self):
        self.bootstrap = None
        self.stages = []
        self.closed = 0

    def morning_bootstrap(self):
        return self.bootstrap

    def stage_morning_bootstrap(self, *, input_sha, first_ids):
        from types import SimpleNamespace
        known = frozenset(first_ids)
        if self.bootstrap is not None:
            assert (self.bootstrap.input_sha, self.bootstrap.known_first_ids) == (input_sha, known)
        self.bootstrap = SimpleNamespace(input_sha=input_sha, known_first_ids=known, completed=False)
        self.stages.append((input_sha, known))

    def close(self):
        self.closed += 1


def mock_morning_log(monkeypatch, path):
    from gremlin_town.review import why_log
    path.write_bytes(b"existing mocked why-log; no SQLite is opened")
    log = MockMorningLog()
    def constructor(filename):
        assert filename == str(path)
        return log
    monkeypatch.setattr(why_log, "WhyLog", constructor)
    return log


@pytest.mark.parametrize("mode", ["local", "hub"])
def test_finish_line_answers_dry_and_apply_match_in_local_and_hub(tmp_path, monkeypatch, capsys, mode):
    key = "brn_20260929_123414_d00000000000000f"
    text = "My permit is approved."
    words = f"  It's done when: {text}\nThat's my answer.  "
    answers = tmp_path / "answers.json"
    answers.write_text(json.dumps({"finish_lines": {key: dict(text=" " + text + " ", words=words)}}), encoding="utf-8")
    root = tmp_path / "pc"
    if mode == "local":
        head = clone(root)
        args = [str(root), "--expected-head", head, "--now", helpers.NOW.isoformat(), "--answers", str(answers)]
        kwargs = {}
        apply_args = [*args, "--local"]
    else:
        fake, storage = pair(root)
        head = fake.branch
        config, env = hub_config(tmp_path)
        mock_hub_credentials(monkeypatch, fake, env)
        why, state = tmp_path / "why.sqlite3", tmp_path / "over.json"
        mock_morning_log(monkeypatch, why)
        args = [str(tmp_path / "scratch"), "--hub", "--config", str(config), "--expected-head", head,
                "--now", helpers.NOW.isoformat(), "--answers", str(answers)]
        kwargs = dict(environment=env)
        apply_args = [*args, "--apply", "--state", str(state), "--why-log", str(why)]
    before = {p: p.read_bytes() for p in (root / "records").rglob("*.md")}
    assert release.main(args, **kwargs) == 0
    dry = capsys.readouterr().out
    output = json.loads(dry)
    assert output["finish_lines"] == [dict(first_id=key, text=text, words=words)]
    row = next(row for row in output["rows"] if row["first_id"] == key)
    assert not any("finish line" in q.casefold() for q in row["questions"])
    assert before == {p: p.read_bytes() for p in (root / "records").rglob("*.md")}
    if mode == "hub":
        assert fake.branch == head and not fake.patch_payloads
    assert release.main(apply_args, **kwargs) == 0
    assert capsys.readouterr().out == dry
    if mode == "local":
        landed = tuple(parse_record(p.read_text(encoding="utf-8"), str(p)) for p in (root / "records").rglob("*.md"))
    else:
        landed = tuple(item.record for item in storage.snapshot_records(kinds=frozenset({"item"}), revision=storage.revision()).records)
    current = work.write_index(landed)[key]
    assert current.data["finish_line"] == text
    assert current.source == dict(said_by="owner", came_by="direct", words=words, recorded_by="system:work-move-over")
    assert len([r for r in landed if first_id(r) == key and "finish_line" in r.data]) == 1


def test_hub_over_apply_stages_full_inventory_and_replays_original_pin(tmp_path, monkeypatch, capsys):
    fake, _storage = pair(tmp_path / "pc")
    head = fake.branch
    config, env = hub_config(tmp_path)
    observed = mock_hub_credentials(monkeypatch, fake, env)
    why, answers, state = tmp_path / "why.sqlite3", tmp_path / "answers.json", tmp_path / "over.json"
    log = mock_morning_log(monkeypatch, why)
    answers.write_text("{}", encoding="utf-8")
    args = [str(tmp_path / "scratch"), "--hub", "--config", str(config), "--apply",
            "--expected-head", head, "--now", helpers.NOW.isoformat(), "--answers", str(answers),
            "--state", str(state), "--why-log", str(why)]
    assert release.main(args, environment=env) == 0
    assert observed[1][1]["permissions"] == dict(contents="write")
    known = frozenset(first_id(r) for r in helpers.load_records())
    assert log.stages == [(head, known)] and log.closed == 1
    assert log.bootstrap.completed is False
    assert set(known) > {r["first_id"] for r in json.loads(capsys.readouterr().out)["rows"]}
    commits, patches = len(fake.commits), len(fake.patch_payloads)
    assert release.main(args, environment=env) == 0
    assert log.stages == [(head, known), (head, known)] and log.closed == 2
    assert log.bootstrap.completed is False
    assert (len(fake.commits), len(fake.patch_payloads)) == (commits, patches)


def test_partial_hub_apply_never_stages_or_consumes_bootstrap(tmp_path, monkeypatch):
    fake, storage = pair(tmp_path / "pc", True)
    head = fake.branch
    config, env = hub_config(tmp_path)
    mock_hub_credentials(monkeypatch, fake, env)
    why, answers, state = tmp_path / "why.sqlite3", tmp_path / "answers.json", tmp_path / "over.json"
    log = mock_morning_log(monkeypatch, why)
    answers.write_text("{}", encoding="utf-8")
    apply = release.apply_storage
    def stopped(client, plan, expected_head, replies, state_path):
        apply(client, plan, expected_head, replies, state_path, stop_after=1)
        raise RuntimeError("fixture interruption before remaining batch")
    monkeypatch.setattr(release, "apply_storage", stopped)
    args = [str(tmp_path / "scratch"), "--hub", "--config", str(config), "--apply",
            "--expected-head", head, "--now", helpers.NOW.isoformat(), "--answers", str(answers),
            "--state", str(state), "--why-log", str(why)]
    with pytest.raises(RuntimeError, match="fixture interruption"):
        release.main(args, environment=env)
    assert log.stages == [] and log.bootstrap is None and log.closed == 1
    saved = json.loads(state.read_text())
    assert len(saved["landed"]) == 1 and len(saved["batches"]) == 2
    known = frozenset(first_id(r) for r in (*helpers.load_records(), *helpers.load_records("made_up_post_audit")))
    assert set(saved["pre_move_inventory"]["first_ids"]) == known
    first_batch = saved["landed"][0]
    monkeypatch.setattr(release, "apply_storage", apply)
    assert release.main(args, environment=env) == 0
    assert log.stages == [(head, known)] and log.bootstrap.completed is False
    assert json.loads(state.read_text())["landed"][0] == first_batch
    assert len(fake.commits) == 5


@pytest.mark.parametrize("interrupted", [False, True])
def test_release_cli_integrates_real_why_log_full_partial_resume_and_back(tmp_path, monkeypatch, capsys, interrupted):
    """Real W8 stage/read implementation and SQLite, with fake GitHub only."""
    from gremlin_town.review.why_log import WhyLog
    from brain_core.review import MorningBootstrap
    root = tmp_path / "pc"
    fake, storage = pair(root, interrupted)
    head = fake.branch
    config, env = hub_config(tmp_path)
    mock_hub_credentials(monkeypatch, fake, env)
    why, answers, state = tmp_path / "why.sqlite3", tmp_path / "answers.json", tmp_path / "over.json"
    WhyLog(why).close()  # existing disposable derivative state, not production config
    answers.write_text("{}", encoding="utf-8")
    known = frozenset(first_id(r) for r in helpers.load_records())
    if interrupted:
        known |= frozenset(first_id(r) for r in helpers.load_records("made_up_post_audit"))
    args = [str(tmp_path / "scratch"), "--hub", "--config", str(config), "--apply",
            "--expected-head", head, "--now", helpers.NOW.isoformat(), "--answers", str(answers),
            "--state", str(state), "--why-log", str(why)]
    apply = release.apply_storage
    first_batch = None
    if interrupted:
        def stopped(client, plan, expected_head, replies, state_path):
            apply(client, plan, expected_head, replies, state_path, stop_after=1)
            raise RuntimeError("fixture interruption with real WhyLog")
        monkeypatch.setattr(release, "apply_storage", stopped)
        with pytest.raises(RuntimeError, match="fixture interruption"):
            release.main(args, environment=env)
        saved = json.loads(state.read_text())
        first_batch = saved["landed"][0]
        assert len(saved["landed"]) == 1 and len(saved["batches"]) == 2
        assert saved["pre_move_inventory"] == dict(expected_head=head, first_ids=sorted(known))
        log = WhyLog(why)
        try:
            assert log.morning_bootstrap() is None
            assert log.morning_history() == () and log.history() == ()
            assert log.shown_item_refs() == frozenset()
        finally:
            log.close()
        monkeypatch.setattr(release, "apply_storage", apply)
    assert release.main(args, environment=env) == 0
    saved = json.loads(state.read_text())
    assert saved["pre_move_inventory"] == dict(expected_head=head, first_ids=sorted(known))
    assert len(saved["landed"]) == len(saved["batches"]) and saved["archive_commit"] == fake.branch
    if first_batch is not None:
        assert saved["landed"][0] == first_batch
    expected_bootstrap = MorningBootstrap(input_sha=head, known_first_ids=known, completed=False)
    log = WhyLog(why)
    try:
        assert log.morning_bootstrap() == expected_bootstrap
        assert log.morning_bootstrap_completed() is False
        assert log.history() == () and log.morning_history() == ()
        assert log.shown_item_refs() == frozenset()
    finally:
        log.close()
    assert known != frozenset(release.pre_move_inventory(storage, storage.revision()))
    commits, patches = len(fake.commits), len(fake.patch_payloads)
    assert release.main(args, environment=env) == 0
    assert (len(fake.commits), len(fake.patch_payloads)) == (commits, patches)
    log = WhyLog(why)
    try:
        assert log.morning_bootstrap() == expected_bootstrap
        assert log.morning_history() == ()
    finally:
        log.close()
    baseline_state = state.read_bytes()
    assert release.main([str(tmp_path / "back-scratch"), "--hub", "--config", str(config), "--apply",
                         "--expected-head", fake.branch, "--answers", str(answers),
                         "--state", str(tmp_path / "back.json")], direction="back", environment=env) == 0
    assert state.read_bytes() == baseline_state
    log = WhyLog(why)
    try:
        assert log.morning_bootstrap() == expected_bootstrap
        assert log.history() == () and log.shown_item_refs() == frozenset()
    finally:
        log.close()
    capsys.readouterr()


@pytest.mark.parametrize("mismatch", ["pin", "inventory"])
def test_real_why_log_conflict_refuses_before_migration_write(tmp_path, monkeypatch, mismatch):
    from gremlin_town.review.why_log import WhyLog
    fake, _storage = pair(tmp_path / "pc")
    head = fake.branch
    config, env = hub_config(tmp_path)
    mock_hub_credentials(monkeypatch, fake, env)
    why, answers, state = tmp_path / "why.sqlite3", tmp_path / "answers.json", tmp_path / "over.json"
    known = frozenset(first_id(r) for r in helpers.load_records())
    log = WhyLog(why)
    try:
        log.stage_morning_bootstrap(input_sha="0" * 40 if mismatch == "pin" else head,
                                   first_ids=frozenset() if mismatch == "inventory" else known)
        original = log.morning_bootstrap()
    finally:
        log.close()
    answers.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="bootstrap pin/inventory"):
        release.main([str(tmp_path / "scratch"), "--hub", "--config", str(config), "--apply",
                      "--expected-head", head, "--answers", str(answers), "--state", str(state),
                      "--why-log", str(why)], environment=env)
    assert not state.exists() and not fake.patch_payloads
    log = WhyLog(why)
    try:
        assert log.morning_bootstrap() == original
        assert log.history() == () and log.morning_bootstrap_completed() is False
    finally:
        log.close()


@pytest.mark.parametrize("mismatch", ["pin", "inventory"])
def test_bootstrap_mismatch_refuses_before_state_or_record_write(tmp_path, monkeypatch, mismatch):
    from types import SimpleNamespace
    fake, _storage = pair(tmp_path / "pc")
    head = fake.branch
    config, env = hub_config(tmp_path)
    mock_hub_credentials(monkeypatch, fake, env)
    why, answers, state = tmp_path / "why.sqlite3", tmp_path / "answers.json", tmp_path / "over.json"
    log = mock_morning_log(monkeypatch, why)
    known = frozenset(first_id(r) for r in helpers.load_records())
    log.bootstrap = SimpleNamespace(input_sha="0" * 40 if mismatch == "pin" else head,
                                   known_first_ids=frozenset() if mismatch == "inventory" else known, completed=False)
    answers.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="bootstrap pin/inventory"):
        release.main([str(tmp_path / "scratch"), "--hub", "--config", str(config), "--apply",
                      "--expected-head", head, "--answers", str(answers), "--state", str(state),
                      "--why-log", str(why)], environment=env)
    assert not state.exists() and not fake.patch_payloads
    assert log.stages == [] and log.closed == 1


def test_hub_move_back_cli_constructs_storage_and_applies_without_baseline_write(tmp_path, monkeypatch, capsys):
    root = tmp_path / "pc"
    fake, storage = pair(root)
    over = plan_move_over(root, release.load_rules(), {}, helpers.NOW)
    over_state = tmp_path / "over.json"
    release.apply_storage(storage, over, fake.branch, {}, over_state)
    baseline = over_state.read_bytes()
    config, env = hub_config(tmp_path)
    observed = mock_hub_credentials(monkeypatch, fake, env)
    answers, state = tmp_path / "answers.json", tmp_path / "back.json"
    answers.write_text("{}", encoding="utf-8")
    head = fake.branch
    assert release.main([str(tmp_path / "scratch"), "--hub", "--config", str(config), "--apply",
                         "--expected-head", head, "--answers", str(answers), "--state", str(state)],
                        direction="back", environment=env) == 0
    assert json.loads(capsys.readouterr().out)["direction"] == "back"
    assert observed[1][1]["permissions"] == dict(contents="write")
    assert over_state.read_bytes() == baseline
    assert json.loads(state.read_text())["pre_move_inventory"] is None
    left = storage.snapshot_records(kinds=frozenset({"item"}), revision=storage.revision())
    assert all(not WORK_FIELDS.intersection(item.record.data) for item in left.records)


@pytest.mark.parametrize("problem", ["outside", "unexpanded", "relative", "layer2", "head"])
def test_hub_refusals_before_resume_or_record_writes(tmp_path, monkeypatch, problem):
    fake, _storage = pair(tmp_path / "pc")
    config, env = hub_config(tmp_path, archive=problem == "layer2")
    observed = mock_hub_credentials(monkeypatch, fake, env)
    if problem == "outside":
        env["GT_GITHUB_APP_KEY_FILE"] = str(tmp_path / "outside")
    elif problem == "unexpanded":
        env["GT_GITHUB_APP_KEY_FILE"] = "%d/private-key"
    elif problem == "relative":
        env["CREDENTIALS_DIRECTORY"] = "relative-directory"
    answers, state, why = tmp_path / "answers.json", tmp_path / "state.json", tmp_path / "why.sqlite3"
    answers.write_text("{}", encoding="utf-8")
    why.write_bytes(b"existing fake derivative; must not be opened on refusal")
    with pytest.raises(ValueError):
        release.main([str(tmp_path / "scratch"), "--hub", "--config", str(config), "--apply",
                      "--expected-head", "0" * 40 if problem == "head" else fake.branch,
                      "--answers", str(answers), "--state", str(state), "--why-log", str(why)], environment=env)
    assert not state.exists() and not fake.patch_payloads
    assert why.read_bytes() == b"existing fake derivative; must not be opened on refusal"
    if problem != "head":
        assert observed == [] and fake.events == []


@pytest.mark.parametrize("direction", ["over", "back"])
@pytest.mark.parametrize("key_name", ["github_app_key", "messages_app_key", "unknown_app_key"])
@pytest.mark.parametrize("apply", [False, True], ids=["dry", "apply"])
def test_hub_migration_refuses_non_brain_app_key_before_any_effect(tmp_path, monkeypatch, direction, key_name, apply):
    fake, _storage = pair(tmp_path / "pc")
    config, env = hub_config(tmp_path)
    env["GT_GITHUB_APP_KEY_FILE"] = str(Path(env["CREDENTIALS_DIRECTORY"]) / key_name)
    observed = mock_hub_credentials(monkeypatch, fake, env)
    state, why = tmp_path / "state.json", tmp_path / "why.sqlite3"
    why.write_bytes(b"Existing why log; must not be opened")
    args = [str(tmp_path / "scratch"), "--hub", "--config", str(config), "--expected-head", fake.branch]
    if apply:
        answers = tmp_path / "answers.json"
        answers.write_text("{}", encoding="utf-8")
        args += ["--apply", "--answers", str(answers), "--state", str(state)]
        if direction == "over":
            args += ["--why-log", str(why)]
    with pytest.raises(ValueError, match="Brain Home App key"):
        release.main(args, environment=env, direction=direction)
    assert observed == [] and fake.events == [] and not fake.patch_payloads
    assert not state.exists() and why.read_bytes() == b"Existing why log; must not be opened"


@pytest.mark.parametrize("key_name", [None, "github_app_key"])
def test_hub_constructor_independently_checks_brain_settings_key(tmp_path, monkeypatch, key_name):
    from dataclasses import replace
    from gremlin_town import github_app
    config, env = hub_config(tmp_path)
    settings = release.load_hub_settings(config, environment=env)
    key = Path(env["CREDENTIALS_DIRECTORY"]) / key_name if key_name else None
    def no_key_read(*args, **kwargs):
        raise AssertionError("must refuse before reading the key")
    monkeypatch.setattr(github_app, "load_private_key", no_key_read)
    with pytest.raises(ValueError, match="Brain Home App key"):
        release.build_hub_storage(replace(settings, github_app_private_key_file=key), environment=env)


def test_hub_token_constructor_uses_only_expanded_service_path(tmp_path, monkeypatch):
    from brain_core import github_storage
    fake, _storage = pair(tmp_path / "pc")
    config, env = hub_config(tmp_path, token_mode=True)
    settings = release.load_hub_settings(config, environment=env)
    original = Path.read_text
    seen = []
    def token(path, *args, **kwargs):
        if path == Path(env["GT_GITHUB_TOKEN_FILE"]):
            seen.append(path)
            return storage_tests.TOKEN
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", token)
    constructor = github_storage.GitHubBrainHomeStorage
    def storage(**kwargs):
        assert kwargs["archive_superseded"] is False
        assert kwargs["token"] == storage_tests.TOKEN
        assert kwargs["transport"] is None
        return constructor(**{**kwargs, "transport":fake})
    monkeypatch.setattr(github_storage, "GitHubBrainHomeStorage", storage)
    result = release.build_hub_storage(settings, environment=env, apply=True)
    assert result.revision().commit_sha == fake.branch
    assert seen == [Path(env["GT_GITHUB_TOKEN_FILE"])]


def test_inventory_is_all_pinned_items_not_planned_rows_or_post_move(tmp_path):
    root = tmp_path / "pc"
    fake, storage = pair(root)
    original = storage.revision()
    known = release.pre_move_inventory(storage, original)
    assert known == tuple(sorted({first_id(r) for r in helpers.load_records()}))
    plan = plan_move_over(root, release.load_rules(), {}, helpers.NOW)
    assert set(known) > {row.first_id for row in plan.rows}
    unchanged = [r for r in helpers.load_records() if r.state != "open" or r.data.get("repeat") or r.data.get("level") == "possible"]
    assert {first_id(r) for r in unchanged} <= set(known)
    state = tmp_path / "resume.json"
    release.apply_storage(storage, plan, original.commit_sha, {}, state)
    post = release.pre_move_inventory(storage, storage.revision())
    assert set(known) != set(post)  # new officer and archived test data differ
    assert json.loads(state.read_text())["pre_move_inventory"] == dict(expected_head=original.commit_sha, first_ids=list(known))
    assert release.pre_move_inventory(storage, original) == known
    back_root = tmp_path / "back"
    release.materialize(storage, storage.revision(), back_root)
    baseline_bytes = state.read_bytes()
    release.apply_storage(storage, plan_move_back(back_root), fake.branch, {}, tmp_path / "back.json")
    assert state.read_bytes() == baseline_bytes


def test_routine_made_and_ticked_through_real_moves_is_untouched(tmp_path):
    from brain_core.contracts import stable_hash
    from hashlib import sha256
    fixture(tmp_path)
    def run(records, message, action, operation, now):
        change = dict(operation_id=operation, message=message, said_by="owner", came_by="direct", moves=[action])
        planned = {(0, j): f"brn_{now:%Y%m%d_%H%M%S}_{sha256(f'{operation}:{j}'.encode()).hexdigest()[:16]}" for j in range(25)}
        change["_context"] = moves.ExecutionContext(raw_records={r.id:r for r in records}, write_index=work.write_index(records), planned_ids=planned, operation_hash=stable_hash(change),
            record_envelope=dict(vaults=["vault://brain-home"], owners=["brain-home"], evidence_actor="fixture-agent"))
        outcome = moves.apply_change(work.index_things(records), change, actor="fixture-agent", now=now)
        assert outcome.refusal is None, outcome.refusal
        return outcome.records
    now = helpers.NOW.astimezone(__import__("datetime").UTC)
    made = run([], "Walk every day", dict(move="new", title="Walk", repeat="daily"), "routine-new", now)
    ticked = run(made, "did it", dict(move="tick", thing=made[0].id), "routine-tick", now+timedelta(seconds=1))
    assert ticked[0].state == "open" and ticked[0].data["occurrence"]["outcome"] == "done"
    for record in (*made, *ticked):
        assert not WORK_FIELDS.intersection(record.data)
        path = tmp_path / release.record_path(record)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_record(record), encoding="utf-8")
    preserved = {release.record_path(r):(tmp_path / release.record_path(r)).read_bytes() for r in (*made, *ticked)}
    release.apply_local(tmp_path, plan_move_over(tmp_path, release.load_rules(), {}, helpers.NOW+timedelta(seconds=3)))
    back = plan_move_back(tmp_path)
    assert not set(preserved).intersection(e.current_path for e in back.archive)
    release.apply_local(tmp_path, back)
    assert all((tmp_path / path).read_bytes() == content for path, content in preserved.items())


def test_archived_whole_test_ids_pins_not_found_and_newshape_history_stale(tmp_path):
    import anyio
    from gremlin_town.review.brain_source import brain_items_by_path_reader
    from gremlin_town.mcp.brain_runtime import BrainOperation, BrainRuntimeError
    from gremlin_town.mcp.github_brain_provider import GitHubBrainProvider, BrainCoreRecordAdapter
    provider_tests = module("w18_provider_tests", GT / "tests/mcp/test_github_brain_provider.py")
    fake, storage = pair(tmp_path / "clone")
    over = plan_move_over(tmp_path / "clone", release.load_rules(), {}, helpers.NOW)
    release.apply_storage(storage, over, fake.branch, {}, tmp_path / "over.json")
    locator = lambda auth, ref: f"records/item/{ref[4:8]}/{ref[8:10]}/{ref}.md"
    adapter = BrainCoreRecordAdapter(operation_time=lambda _: helpers.NOW, source_evidence=provider_tests._evidence, record_locator=locator)
    provider = GitHubBrainProvider(storage, adapter)
    auth = provider_tests._authorization(BrainOperation.MEMORY_READ, "read")
    pins = brain_items_by_path_reader(storage)
    for entry in over.archive:
        assert pins([entry.current_path]) == ()
        with pytest.raises(BrainRuntimeError) as error:
            anyio.run(provider.read, auth, entry.record_id)
        assert error.value.code == "brain.not_found"
    # Layer 2 archives replaced old versions, still named in the current chain.
    old = next(r for b in over.batches for r in b.records if len(r.supersedes) == 2)
    predecessor_paths = [p.relative_to(tmp_path / "clone").as_posix() for p in (tmp_path / "clone/records").rglob("*.md") if p.stem in old.supersedes]
    storage.archive_superseded_records(paths=predecessor_paths, expected_revision=fake.branch)
    stale = anyio.run(provider.read, auth, old.supersedes[0])
    assert stale.freshness == "stale" and old.id in stale.superseded_by
    # Add a real checked new version to give the inverse a historical newshape.
    newer = deepcopy(old.as_dict())
    newer.update(id="brn_20260930_060000_000000000000abcd", created_at="2026-09-30T06:00:00+00:00", updated_at="2026-09-30T06:00:00+00:00", supersedes=[*old.supersedes, old.id])
    newer = record_from_mapping(newer, "fixture newshape history")
    storage.publish_records(records=[render_record(newer).encode()], base_revision=storage.revision())
    active = tmp_path / "moved"
    release.materialize(storage, storage.revision(), active)
    back = plan_move_back(active)
    assert {old.id, newer.id} <= {e.record_id for e in back.archive}
    release.apply_storage(storage, back, fake.branch, {}, tmp_path / "back.json")
    stale = anyio.run(provider.read, auth, old.id)
    assert stale.freshness == "stale"
    assert pins([release.record_path(old)])
    inverse = next(r for b in back.batches for r in b.records if old.id in r.supersedes)
    assert inverse.id in stale.superseded_by
