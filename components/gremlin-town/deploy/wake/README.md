# Optional request pickup worker

This sidecar sends request/delivery IDs to an explicitly enrolled native webhook.
Task content and permission remain in Gremlin Town. It reads the work database
without acknowledging a task and keeps delivery bookkeeping in a separate store.

- Only accepted, exact, authenticated request links can wake action-requested work.
- Recheck participant policy, cancellation, holds and recovery controls before send.
- Use an explicit activation timestamp; including earlier work requires explicit IDs.
- Preserve delivery identity across bounded retries; redirects are refused.
- A notified receipt proves webhook acceptance, not completion or an external effect.
- Recipients claim work using the current revision and an invocation-specific key.

`wake.toml.example` is disabled. Review operator-owned endpoints, participant IDs,
cutoffs, authentication and container topology before enrollment. Keep keys in
protected files, never messages, logs or source control. The `--inspect` route
lists eligible request IDs without sending.

`install_hermes.py` illustrates a one-time guarded configuration projection for
a Linux container layout. Its example mount is `/srv/hermes-example`; adapt and
verify the topology for your own installation. The native sender setup helpers
are explicit enrollment tools, not background services. `key_setup.py` uses a
temporary loopback form and a reserved example SSH destination. No personal host,
bot roster, sender secret or historical activation receipt is included.

The receiver prompt and signed fixtures are inspection material. Discovery,
authenticated pickup, duplicate suppression and attributed completion need
independent acceptance in the chosen native runtime before live use.
