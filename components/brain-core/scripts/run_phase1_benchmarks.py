"""Reproducible Phase 1 latency, size, and memory acceptance benchmark."""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import shutil
import tempfile
import time
from pathlib import Path

import psutil

from brain_core.contracts import TOKENIZER_ID
from brain_core.operations import dispatch
from brain_core.protocol import ProtocolRequest


NOW = "2026-08-29T00:00:00Z"
SESSION = "synthetic-pilot-session"


def p95(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


def elapsed_ms(operation) -> tuple[float, object]:
    started = time.perf_counter()
    result = operation()
    return (time.perf_counter() - started) * 1_000, result


def request(operation: str, request_id: str = "benchmark", **parameters: object) -> dict:
    envelope = ProtocolRequest("1", request_id, operation, dict(parameters))
    return dispatch(envelope)


def cpu_name() -> str:
    declared = platform.processor() or os.environ.get("PROCESSOR_IDENTIFIER", "")
    if declared:
        return declared
    cpuinfo = Path("/proc/cpuinfo")
    if cpuinfo.is_file():
        for line in cpuinfo.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.casefold().startswith("model name"):
                return line.partition(":")[2].strip()
    return "unknown"


def prepare(project: Path, target: Path) -> tuple[Path, list[str], Path]:
    source = project / "tests" / "fixtures" / "foundation"
    fixture = target / "foundation"
    shutil.copytree(source, fixture)
    node_map = fixture / "node-map-runtime.yaml"
    node_map.write_text(
        "schema: brain.node-map/v1\n"
        "node: synthetic-local\n"
        "vaults:\n"
        "  - id: vault_career\n"
        f"    root: '{(fixture / 'vaults' / 'career').as_posix()}'\n"
        "    manifest: brain.yaml\n"
        "  - id: vault_augmentus\n"
        f"    root: '{(fixture / 'vaults' / 'augmentus').as_posix()}'\n"
        "    manifest: brain.yaml\n",
        encoding="utf-8",
    )
    grants = [
        str(fixture / "grants" / "exact-five-paths" / "career.yaml"),
        str(fixture / "grants" / "exact-five-paths" / "augmentus.yaml"),
    ]
    return node_map, grants, fixture


def context_parameters(node_map: Path, database: Path, grants: list[str]) -> dict[str, object]:
    return {
        "node_map": str(node_map),
        "database": str(database),
        "grant_files": grants,
        "session_id": SESSION,
        "intent": "Use Career evidence for honest Augmentus interview positioning and gaps",
        "vaults": ["vault_career", "vault_augmentus"],
        "facets": ["career", "augmentus", "evidence", "gaps"],
        "now": NOW,
    }


def run_benchmark(project: Path, runs: int, label: str) -> dict[str, object]:
    process = psutil.Process()
    peak_rss = process.memory_info().rss

    def observe() -> None:
        nonlocal peak_rss
        peak_rss = max(peak_rss, process.memory_info().rss)

    with tempfile.TemporaryDirectory(prefix="brain-benchmark-") as raw_target:
        target = Path(raw_target)
        node_map, grants, fixture = prepare(project, target)
        no_grant_database = target / "no-grant.db"
        request(
            "index.rebuild",
            node_map=str(node_map),
            database=str(no_grant_database),
            grant_files=[],
            session_id=SESSION,
            now=NOW,
        )

        discovery_ms: list[float] = []
        orientation_ms: list[float] = []
        orientation_tokens: list[int] = []
        for position in range(runs):
            measured, _ = elapsed_ms(
                lambda: request("vault.discover", f"discover-{position}", node_map=str(node_map))
            )
            discovery_ms.append(measured)
            measured, pack = elapsed_ms(
                lambda: request(
                    "context.build",
                    f"orientation-{position}",
                    **context_parameters(node_map, no_grant_database, []),
                )
            )
            orientation_ms.append(measured)
            orientation_tokens.append(sum(item["source_tokens"] for item in pack["sources"]))
            observe()

        full_rebuild_ms: list[float] = []
        cold_pack_ms: list[float] = []
        generations: set[str] = set()
        pack_hashes: set[str] = set()
        indexed_bytes = 0
        database_bytes = 0
        stable_database = target / "stable.db"
        for position in range(runs):
            database = target / f"cold-{position}.db"
            measured, built = elapsed_ms(
                lambda: request(
                    "index.rebuild",
                    f"rebuild-{position}",
                    node_map=str(node_map),
                    database=str(database),
                    grant_files=grants,
                    session_id=SESSION,
                    now=NOW,
                )
            )
            full_rebuild_ms.append(measured)
            generations.add(built["generation"])
            indexed_bytes = built["indexed_bytes"]
            database_bytes = built["database_bytes"]
            measured, pack = elapsed_ms(
                lambda: request(
                    "context.build",
                    **context_parameters(node_map, database, grants),
                )
            )
            cold_pack_ms.append(measured)
            pack_hashes.add(pack["pack_sha256"])
            if position == runs - 1:
                shutil.copy2(database, stable_database)
                shutil.copy2(
                    database.with_name(f"{database.name}.status.json"),
                    stable_database.with_name(f"{stable_database.name}.status.json"),
                )
            observe()

        ranking_ms: list[float] = []
        warm_pack_ms: list[float] = []
        for position in range(runs):
            measured, _ = elapsed_ms(
                lambda: request(
                    "context.plan",
                    f"plan-{position}",
                    **context_parameters(node_map, stable_database, grants),
                )
            )
            ranking_ms.append(measured)
            measured, _ = elapsed_ms(
                lambda: request(
                    "context.build",
                    **context_parameters(node_map, stable_database, grants),
                )
            )
            warm_pack_ms.append(measured)
            observe()

        incremental_ms: list[float] = []
        changed = fixture / "vaults" / "career" / "intent.md"
        for position in range(runs):
            changed.write_text(
                f"# Synthetic Career intent\n\nIncremental benchmark marker {position}.\n",
                encoding="utf-8",
            )
            measured, built = elapsed_ms(
                lambda: request(
                    "index.rebuild",
                    f"incremental-{position}",
                    node_map=str(node_map),
                    database=str(stable_database),
                    grant_files=grants,
                    session_id=SESSION,
                    now=NOW,
                )
            )
            incremental_ms.append(measured)
            indexed_bytes = built["indexed_bytes"]
            database_bytes = built["database_bytes"]
            observe()

        skill_path = str(project / "tests" / "fixtures" / "skills" / "job-match" / "SKILL.md")
        skill_batch_ms: list[float] = []
        for position in range(runs):
            measured, _ = elapsed_ms(
                lambda: [
                    request(
                        "skills.route",
                        f"skill-{position}-{iteration}",
                        skill_paths=[skill_path],
                        intent="score this job",
                        authority=["career-read"],
                        project_owner="career",
                    )
                    for iteration in range(100)
                ]
            )
            skill_batch_ms.append(measured / 100)
            observe()

        p95s = {
            "skill_route_ms": p95(skill_batch_ms),
            "discovery_ms": p95(discovery_ms),
            "discovery_and_ranking_ms": p95(ranking_ms),
            "orientation_ms": p95(orientation_ms),
            "context_pack_cold_ms": p95(cold_pack_ms),
            "context_pack_warm_ms": p95(warm_pack_ms),
            "incremental_index_ms": p95(incremental_ms),
            "full_rebuild_ms": p95(full_rebuild_ms),
        }
        limits = {
            "skill_route_ms": 50.0,
            "discovery_ms": 500.0,
            "discovery_and_ranking_ms": 500.0,
            "orientation_ms": 2_000.0,
            "context_pack_cold_ms": 2_000.0,
            "context_pack_warm_ms": 2_000.0,
            "incremental_index_ms": 2_000.0,
            "full_rebuild_ms": 60_000.0,
        }
        failures = [name for name, value in p95s.items() if value >= limits[name]]
        index_limit = max(2 * indexed_bytes, indexed_bytes + 25 * 1024 * 1024)
        if database_bytes > index_limit:
            failures.append("database_bytes")
        peak_limit = 150 * 1024 * 1024
        if peak_rss > peak_limit:
            failures.append("peak_rss_bytes")
        if max(orientation_tokens) > 1_500:
            failures.append("orientation_tokens")
        if len(generations) != 1:
            failures.append("generation_determinism")
        if len(pack_hashes) != 1:
            failures.append("pack_determinism")

        return {
            "result": "ok" if not failures else "failed",
            "failures": failures,
            "label": label,
            "runs": runs,
            "python": platform.python_version(),
            "platform": platform.platform(),
            "cpu": cpu_name(),
            "storage": "WSL2 virtual disk" if "microsoft" in platform.release().casefold() else "local filesystem",
            "vault_count": 2,
            "file_count": 7,
            "indexed_utf8_bytes": indexed_bytes,
            "sqlite_bytes": database_bytes,
            "sqlite_limit_bytes": index_limit,
            "tokenizer": TOKENIZER_ID,
            "peak_rss_bytes": peak_rss,
            "peak_rss_limit_bytes": peak_limit,
            "orientation_max_tokens": max(orientation_tokens),
            "p95": p95s,
            "limits": limits,
            "model_calls": 0,
            "network_calls": 0,
            "deterministic_generations": len(generations),
            "deterministic_pack_hashes": len(pack_hashes),
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--runs", type=int, default=20)
    parser.add_argument("--label", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.runs < 20:
        parser.error("--runs must be at least 20")
    result = run_benchmark(args.project.resolve(), args.runs, args.label)
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8", newline="\n")
    print(rendered, end="")
    return 0 if result["result"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
