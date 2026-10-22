"""The PC connector's Compass Focus route for sitreps (docs/product/steps/b-project-sitreps.md).

`POST /v1/compass/focus` returns only the Focus section of the Compass of the vault that holds an
enrolled project, over the real loopback connector with Brain Core.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from brain_local_harness import (  # noqa: E402
    CONTEXT_ONLY_TOKEN,
    build_fixture,
    create_escape_reparse,
    remove_escape_reparse,
)

from gremlin_town.mcp.local_project_connector import (  # noqa: E402
    MAX_FOCUS_CHARS,
    ConnectorError,
    ConnectorUnavailable,
    LocalProjectConnectorClient,
)

GIT = "vault_git_fixture"
NONGIT = "vault_nongit_fixture"
FOCUS = "Make the fixture the verified home for sitreps. Keep evidence first."
COMPASS = f"""<!-- Vault OS v4: declared intent only. -->

# Compass

*Updated: 2026-09-28*

## Focus

{FOCUS}

### Detail

A detail that belongs to Focus.

## Questions

1. QUESTION-CANARY: never leaves the PC through this route.

## Flags

- FLAG-CANARY
"""


def _manifest(root: Path, *, root_path: str | None = None, compass: str | None = "compass.md", body: str | None = None) -> None:
    (root / "ops").mkdir(parents=True, exist_ok=True)
    if body is None:
        lines = ["---", "vault-name: Fixture", f"root-path: {root_path or root.resolve().as_posix()}"]
        if compass is not None:
            lines += ["exports:", f"  compass: {compass}"]
        body = "\n".join(lines + ["---", ""])
    (root / "ops" / "vault-manifest.md").write_text(body, encoding="utf-8")


def _vault(root: Path, compass_text: str = COMPASS) -> None:
    _manifest(root)
    (root / "compass.md").write_text(compass_text, encoding="utf-8")


def _section() -> str:
    return f"{FOCUS}\n\n### Detail\n\nA detail that belongs to Focus."


def test_the_route_returns_only_the_focus_section_of_the_vaults_compass(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path)
    _vault(fixture.git_project)
    with fixture.running() as running:
        value = fixture.client(running.base_url, CONTEXT_ONLY_TOKEN).read_compass_focus(GIT)
    assert set(value) == {"project_id", "compass", "focus", "focus_sha256", "freshness", "observed_at"}
    assert value["project_id"] == GIT and value["compass"] is True and value["freshness"] == "current_pc"
    assert value["focus"] == _section()
    assert value["focus_sha256"] == hashlib.sha256(_section().encode("utf-8")).hexdigest()
    assert "CANARY" not in json.dumps(value)


def test_a_project_inside_a_vault_uses_the_compass_of_the_vault_that_holds_it(tmp_path: Path) -> None:
    # Like vault_gtbc_plans, enrolled at exosuit/docs/product inside the Exosuit vault.
    vault = tmp_path / "vault"
    fixture = build_fixture(vault / "docs" / "product")
    _vault(vault)
    with fixture.running() as running:
        client = fixture.client(running.base_url)
        for project in (GIT, NONGIT):
            value = client.read_compass_focus(project)
            assert value["compass"] is True and value["focus"] == _section()
        # The nearest vault wins: a project that is itself a vault uses its own Compass.
        _vault(fixture.git_project, COMPASS.replace(FOCUS, "Its own focus."))
        assert client.read_compass_focus(GIT)["focus"].startswith("Its own focus.")


@pytest.mark.parametrize(
    ("arrange", "reason"),
    [
        (lambda root: None, "no_vault_manifest"),
        (lambda root: _manifest(root, root_path="C:/somewhere/else"), "root_path_mismatch"),
        (lambda root: _manifest(root, compass=None), "no_compass_export"),
        (lambda root: _manifest(root, body="vault-name: no frontmatter\n"), "manifest_invalid"),
        (lambda root: _manifest(root, body="---\nexports: [unclosed\n---\n"), "manifest_invalid"),
        (lambda root: _manifest(root, body="---\n- a list, not a mapping\n---\n"), "manifest_invalid"),
        (lambda root: _manifest(root, body="---\nroot-path: x\n"), "manifest_invalid"),
        (lambda root: _manifest(root, body=""), "manifest_invalid"),
        (lambda root: _manifest(root, body="---\n" + "x" * 70_000 + "\n---\n"), "manifest_unreadable"),
        (lambda root: _manifest(root), "compass_unreadable"),
        (lambda root: _manifest(root, compass="../outside-enrollment/secret.md"), "compass_unreadable"),
        (lambda root: _manifest(root, compass=(root.parent / "outside-enrollment" / "secret.md").as_posix()), "compass_unreadable"),
    ],
)
def test_without_a_readable_compass_the_route_says_so_and_why(tmp_path: Path, arrange, reason: str) -> None:
    fixture = build_fixture(tmp_path)
    arrange(fixture.git_project)
    with fixture.running() as running:
        value = fixture.client(running.base_url).read_compass_focus(GIT)
    assert value["compass"] is False and value["reason"] == reason
    assert value["focus"] is None and value["focus_sha256"] is None
    assert "outside-root secret" not in json.dumps(value)


def test_a_compass_behind_a_junction_or_symlink_is_refused(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path)
    (fixture.outside_root / "compass.md").write_text(COMPASS, encoding="utf-8")
    _manifest(fixture.git_project, compass="escape/compass.md")
    link = create_escape_reparse(fixture)
    try:
        with fixture.running() as running:
            value = fixture.client(running.base_url).read_compass_focus(GIT)
        assert value["compass"] is False and value["reason"] == "compass_unreadable"
    finally:
        remove_escape_reparse(link)


def test_a_compass_without_focus_answers_with_no_section_and_a_long_focus_is_capped(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path)
    _vault(fixture.git_project, "# Compass\n\n## Questions\n\n1. Only questions.\n")
    _vault(fixture.non_git_project, "## Focus\n\n" + "word " * 2_000 + "\n")
    with fixture.running() as running:
        client = fixture.client(running.base_url)
        none = client.read_compass_focus(GIT)
        capped = client.read_compass_focus(NONGIT)
    assert none["compass"] is True and none["focus"] is None and none["focus_sha256"] is None and "reason" not in none
    assert len(capped["focus"]) == MAX_FOCUS_CHARS


def test_the_route_is_authenticated_and_takes_only_an_enrolled_project_id(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path)
    _vault(fixture.git_project)
    with fixture.running() as running:
        with pytest.raises(ConnectorError) as unauthenticated:
            LocalProjectConnectorClient(running.base_url, "not-a-token").read_compass_focus(GIT)
        assert unauthenticated.value.code == "UNAUTHENTICATED" and unauthenticated.value.status == 401
        client = fixture.client(running.base_url)
        with pytest.raises(ConnectorError) as unknown:
            client.read_compass_focus("vault_unknown")
        assert unknown.value.code == "PROJECT_NOT_ENROLLED"
        with pytest.raises(ConnectorError) as extra:
            client.call("/v1/compass/focus", {"project_id": GIT, "path": "../../etc/passwd"})
        assert extra.value.code == "INVALID_REQUEST"


def test_an_older_brain_core_on_the_pc_fails_only_this_route(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = build_fixture(tmp_path)
    _vault(fixture.git_project)
    with fixture.running() as running:
        monkeypatch.setitem(sys.modules, "brain_core.sitreps", None)
        client = fixture.client(running.base_url)
        with pytest.raises(ConnectorUnavailable):
            client.read_compass_focus(GIT)
        assert client.list_continuity(GIT)["items"] == []
