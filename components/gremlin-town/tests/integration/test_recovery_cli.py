from __future__ import annotations

import json
from pathlib import Path

import pytest

from gremlin_town.entrypoint import main
from gremlin_town.storage import SQLiteStore


def _invoke(capsys: pytest.CaptureFixture[str], *arguments: str) -> tuple[int, dict[str, object]]:
    code = main(arguments)
    output = capsys.readouterr().out.strip()
    return code, json.loads(output)


def test_operator_recovery_import_and_audit_cli_seam(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "snapshot.sqlite3"
    SQLiteStore(source).close()
    destination = tmp_path / "restored.sqlite3"

    code, restored = _invoke(
        capsys,
        "recovery",
        "import",
        "--source",
        str(source),
        "--destination",
        str(destination),
        "--hold-all-unfinished",
    )
    assert code == 0
    assert restored["destination"] == str(destination)
    assert restored["audit"]["outbox_dispatch_blocked"] is True

    code, audit = _invoke(
        capsys,
        "recovery",
        "audit",
        "--database",
        str(destination),
    )
    assert code == 0
    assert audit["integrity_ok"] is True
    assert audit["outbox_dispatch_blocked"] is True
    assert audit["normal_clients_enabled"] is None

    code, refused = _invoke(
        capsys,
        "recovery",
        "import",
        "--source",
        str(source),
        "--destination",
        str(destination),
        "--hold-all-unfinished",
    )
    assert code == 2
    assert refused["error"]["code"] == "DESTINATION_EXISTS"
