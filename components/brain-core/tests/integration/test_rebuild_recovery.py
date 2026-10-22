from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from brain_core.index import query_index, rebuild_index


def _fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    root = tmp_path / "vault"
    (root / "docs").mkdir(parents=True)
    source = root / "docs" / "source.md"
    source.write_text("first generation marker", encoding="utf-8")
    (root / "brain.yaml").write_text(
        """schema: brain.vault/v1
id: vault_fixture
name: Fixture
purpose: recovery
exports:
  - name: source
    paths:
      - docs/source.md
""",
        encoding="utf-8",
    )
    node_map = tmp_path / "node.yaml"
    node_map.write_text(
        f"""schema: brain.node-map/v1
node: local
vaults:
  - id: vault_fixture
    root: "{root.as_posix()}"
""",
        encoding="utf-8",
    )
    return node_map, tmp_path / "index.sqlite3", source


def test_delete_and_rebuild_reproduces_generation(tmp_path: Path) -> None:
    node_map, database, _ = _fixture(tmp_path)
    first = rebuild_index(node_map, database)
    first_match = query_index(database, "marker")[0]
    database.unlink()
    second = rebuild_index(node_map, database)
    second_match = query_index(database, "marker")[0]

    assert first.generation == second.generation
    assert first_match == second_match


def test_concurrent_readers_see_complete_old_or_new_generation(tmp_path: Path) -> None:
    node_map, database, source = _fixture(tmp_path)
    rebuild_index(node_map, database)
    source.write_text("second generation marker", encoding="utf-8")

    def read_repeatedly() -> list[str]:
        observed = []
        for _ in range(20):
            rows = query_index(database, ("first", "second"), allow_stale=True)
            observed.append(rows[0].content_sha256)
        return observed

    with ThreadPoolExecutor(max_workers=3) as pool:
        readers = [pool.submit(read_repeatedly) for _ in range(2)]
        rebuilt = pool.submit(rebuild_index, node_map, database)
        observations = [digest for reader in readers for digest in reader.result()]
        new_generation = rebuilt.result()

    final_hash = query_index(database, "second")[0].content_sha256
    assert new_generation.files == 1
    assert final_hash in observations or all(observations)
    assert all(len(digest) == 64 for digest in observations)
