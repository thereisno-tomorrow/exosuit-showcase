"""Generate the hub's Brain project owners from the PC connector's enrollment (ROA-72).

Nic, 28 Sep 2026: "Same agents for every vault", generated for every vault at release time
(docs/product/steps/b-notes-in-each-vault.md §3). Input: the hub's current ``brain-policy.json`` and
the JSON ``python -m gremlin_town.mcp.local_project_connector list-projects`` prints on the PC.
Output: the new policy, checked by the hub's own loader, on standard output or ``--output``.

- Every registry vault gets one project owner: its ID, ``vault://<ID>``, the agents on the Brain
  Home cross-project owner as readers and writers, context retrieval and the five note operations.
  An owner it already has keeps its ``session_id``; a new one gets ``gt-<ID>-v1``.
- A project set by hand in the connector keeps its owner exactly as it is (``vault_gtbc_plans``).
- A project owner the connector no longer serves is dropped and named on standard error.
- Brain Home's owners and ``subject_scopes`` are left as they are; the revision moves to ``--revision``.

Nothing here reads or prints a credential.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

PROJECT_OPERATIONS = (
    "context.retrieve",
    "continuity.append",
    "continuity.close",
    "continuity.list",
    "continuity.recover",
    "continuity.start",
)


class PolicyError(ValueError):
    """The inputs can't make a policy; nothing is written."""


def generate(policy: dict[str, Any], enrollment: dict[str, Any], revision: str) -> tuple[dict[str, Any], dict[str, list[str]]]:
    """The new policy, and what changed: ``added``, ``updated``, ``kept`` and ``dropped`` project IDs."""

    if not revision.strip() or revision == policy.get("revision"):
        raise PolicyError("--revision must be new")
    owners = policy.get("owners")
    if not isinstance(owners, list):
        raise PolicyError("the policy has no owners list")
    cross = [owner for owner in owners if owner.get("kind") == "cross-project"]
    if len(cross) != 1:
        raise PolicyError("the policy needs exactly one cross-project owner: its agents are every vault's agents")
    readers = sorted(cross[0]["readers"])
    writers = sorted(cross[0]["writers"])
    projects = enrollment.get("projects")
    if not isinstance(projects, list) or not projects:
        raise PolicyError("the enrollment lists no projects")
    served = {str(item["id"]): str(item["source"]) for item in projects}
    existing = {str(owner["project_id"]): owner for owner in owners if owner.get("kind") == "project"}
    changes: dict[str, list[str]] = {"added": [], "updated": [], "kept": [], "dropped": []}
    project_owners: list[dict[str, Any]] = []
    for project_id in sorted(served):
        current = existing.get(project_id)
        if served[project_id] != "registry":
            if current is not None:
                project_owners.append(current)
                changes["kept"].append(project_id)
            continue
        owner = {
            "kind": "project",
            "project_id": project_id,
            "owner_id": project_id,
            "owner_locator": f"vault://{project_id}",
            "readers": readers,
            "writers": writers,
            "operations": list(PROJECT_OPERATIONS),
            "session_id": current["session_id"] if current is not None else f"gt-{project_id}-v1",
        }
        if current is None:
            changes["added"].append(project_id)
        elif _normalized(current) != _normalized(owner):
            changes["updated"].append(project_id)
        else:
            changes["kept"].append(project_id)
        project_owners.append(owner)
    changes["dropped"] = sorted(set(existing) - set(served))
    result = dict(policy)
    result["revision"] = revision
    result["owners"] = [owner for owner in owners if owner.get("kind") != "project"] + project_owners
    return result, changes


def _normalized(owner: dict[str, Any]) -> dict[str, Any]:
    return {key: sorted(value) if isinstance(value, list) else value for key, value in owner.items()}


def _check_with_hub_loader(policy: dict[str, Any]) -> None:
    from gremlin_town.mcp.brain_assembly import BrainAssemblyError, load_owner_policy

    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "brain-policy.json"
        path.write_text(json.dumps(policy), encoding="utf-8")
        try:
            load_owner_policy(path)
        except BrainAssemblyError as exc:
            raise PolicyError(f"the hub would refuse this policy: {exc}") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--policy", required=True, type=Path, help="the hub's current brain-policy.json")
    parser.add_argument("--projects", required=True, type=Path, help="list-projects output from the PC connector")
    parser.add_argument("--revision", required=True, help="the new policy revision")
    parser.add_argument("--output", type=Path, help="write here instead of standard output")
    args = parser.parse_args(argv)
    try:
        policy = json.loads(args.policy.read_text(encoding="utf-8"))
        enrollment = json.loads(args.projects.read_text(encoding="utf-8"))
        result, changes = generate(policy, enrollment, args.revision)
        _check_with_hub_loader(result)
    except (OSError, json.JSONDecodeError, KeyError, TypeError, PolicyError) as exc:
        print(f"brain_policy_projects: {exc}", file=sys.stderr)
        return 1
    text = json.dumps(result, indent=2) + "\n"
    if args.output is not None:
        args.output.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    for change, ids in changes.items():
        print(f"{change}: {len(ids)}{' - ' + ', '.join(ids) if ids else ''}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
