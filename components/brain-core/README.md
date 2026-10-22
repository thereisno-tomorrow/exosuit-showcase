# Brain Core

Deterministic records and work interpretation for agents. Brain Core validates ownership, preserves provenance, reads versioned records and returns evidence of accepted changes. It does not call a language model.

The package also supplies task ranking, stopping notes, review sections and record migration. Storage adapters sit behind explicit interfaces; the GitHub implementation reconciles ambiguous publication results before retrying.

Start with the [repository overview](../../README.md), [local quickstart](../../docs/quickstart.md) and [architecture](../../docs/architecture.md). The tests under `tests/` exercise records, ownership, storage failure handling, work readers and synthetic migrations.

Requires Python 3.12–3.14; use Python 3.13 to work with both components.

```sh
uv sync --frozen
uv run --frozen pytest -q
```

The public fixtures are fictional. They preserve behavioral cases rather than personal messages or a private memory export. See the root validation record for the checks run on this snapshot.
