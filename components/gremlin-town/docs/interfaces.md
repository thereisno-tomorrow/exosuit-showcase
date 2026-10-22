# Interface entry points

The request types and state enums are defined in `src/gremlin_town/contracts.py`. `service/` applies those transitions under authenticated participant policy. `workers/outbox.py` handles persisted delivery and reconciliation. MCP operations live under `mcp/`.

See the root [architecture](../../../docs/architecture.md), [quickstart](../../../docs/quickstart.md) and executable [offline journey](../../../examples/README.md). The original personal installation's operations and evidence documents are excluded from this snapshot.
