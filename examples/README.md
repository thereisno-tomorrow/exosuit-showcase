# Try the failure path

An agent says it sent something. Did the provider accept it? Can a retry send it
twice? Can the recipient certify its own work? Can another session recover where
the work stopped?

[`offline_journey.py`](offline_journey.py) runs these questions against the real
Gremlin Town service and SQLite outbox, then saves a stopping note using Brain
Core's record engine. All participants, messages and results are invented.

Use **Python 3.13** and install both component packages in the Gremlin Town
virtual environment, following the root quickstart. From the repository root:

```console
uv run --project components/gremlin-town --frozen python examples/offline_journey.py
```

The script needs the installed component dependencies, including `ruamel.yaml`.
Once dependencies are installed, execution is offline. It starts no server and
uses no model, credential, provider account or personal data. Temporary SQLite
and Brain records are removed when the script finishes.

The walkthrough checks that:

1. Repeating a client operation returns the original operation.
2. An accepted send with a lost receipt becomes **uncertain**.
3. A fresh worker reopens SQLite and recovers the provider receipt without a
   second send. Both send count and message count are checked.
4. An unrelated participant cannot read the request.
5. Returned provider bytes correlate to the authenticated operation and match
   the expected synthetic result.
6. The recipient cannot check its own result. A separate participant's check is
   recorded in the durable event log.
7. A fresh Brain Core reader recovers the result and explicit next step.

Every check fails loudly and remains active with Python's `-O` option. The
expected transcript is in [`offline_journey.txt`](offline_journey.txt).

The transport is an in-memory substitute, and the work result is scripted.
Reopening SQLite proves persistence across object reconstruction, not power-loss
durability or a full process crash. The final Brain write is an explicit
application-level handoff. This example does not prove live GitHub transport,
authentication, deployment, model quality or automatic cross-component wiring.
The independent check records the verifier's claim; it does not establish that
arbitrary prose is true.
