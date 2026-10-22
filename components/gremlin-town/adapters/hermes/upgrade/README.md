# Hermes upgrade-check examples

This directory contains a synthetic practice-home builder, bounded read-only
inventory/acceptance checks, and pure configuration helpers. It includes no
live upgrade runner or private runtime inventory. `live_switch.py` now exposes
only the pure `compact_settings` helper for compatibility.

`practice.py --root PATH` prepares invented fixtures under a caller-chosen
directory. `upgrade_check.py --help` describes the independent upstream runtime
inputs required to inspect them. A fixture response does not prove a real model
conversation, provider transport or native deployment.

`hub_observer.py` collects bounded read-only evidence from an explicitly supplied
practice installation. Missing evidence remains pending. `restart_guard.py`
validates observations and never restarts a runtime.

`release_watch.py` is an optional operator-run upstream release watcher. It can
send a notification when run without preview; do not schedule it without choosing
your own runtime, state directory and delivery route. Its default example home
is `/srv/hermes-example`. Nothing registers or runs this watcher automatically.

The adapter's optional compact surface uses `brain-and-more` and retains five
Brain tools plus the tool-discovery facade. Runtime permission and receipt checks
still apply. Observe actual tool registration and request budgets separately
before adopting it in a native host.
