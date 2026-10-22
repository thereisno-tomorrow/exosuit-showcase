# Run Exosuit locally

Use Python 3.13 and uv. The two component environments are separate; Gremlin Town resolves Brain Core from the adjacent directory.

From the repository root:

```sh
uv sync --project components/brain-core --frozen
uv sync --project components/gremlin-town --frozen --extra dev
```

Run the two suites independently so their local test module names do not collide:

```sh
uv run --project components/brain-core --frozen pytest components/brain-core/tests -q
uv run --project components/gremlin-town --frozen pytest components/gremlin-town/tests -q
```

Some packaging checks intentionally launch the adjacent Brain Core interpreter, which is why both environments are created first. Platform-specific skips are reported by pytest. The validation record lists actual results for this snapshot.

The headless example in `examples/` uses synthetic participants and a fake provider. It needs no account, API key, model download or external service. Its README supplies the command and explains the assertions.

For an optional browser view:

```sh
cd components/gremlin-town
uv run --frozen python scripts/local_browser_demo.py
```

Open the printed localhost URL and use the printed synthetic login. The service binds to `127.0.0.1`; its SQLite database is created in an ignored local demo directory. Stop the process with Ctrl+C. This browser view demonstrates the request overview.

Production transports, account connections and external message delivery require their own credentials, configuration and verification. They are outside this local walkthrough.
