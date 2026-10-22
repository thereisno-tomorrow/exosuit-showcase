# Gremlin Town

An authenticated request exchange for agents. A service records requests and attributed replies in SQLite, enforces participant/project grants, and dispatches persisted operations through a transport adapter.

The outbox distinguishes provider acceptance from completed work. When a provider may have accepted a message but its reply was lost, reconciliation precedes another send. Cancellation, retries and recovery have explicit states.

Gremlin Town hosts Brain Core through MCP and local interfaces. It also contains optional review, dashboard, calendar and mail integrations. Those integration modules are source for inspection; the local showcase does not configure personal accounts or exercise live delivery.

Start with the [repository overview](../../README.md), [quickstart](../../docs/quickstart.md) and [architecture](../../docs/architecture.md). Requires Python 3.13.

```sh
uv sync --frozen --extra dev
uv run --frozen pytest -q
```

The browser demo uses fake participants, a fake provider and loopback hosting:

```sh
uv run --frozen python scripts/local_browser_demo.py
```

It prints its local URL and explicitly synthetic login. Stop it with Ctrl+C. The primary headless walkthrough in the repository's `examples/` directory demonstrates failure handling without a server or network connection.
