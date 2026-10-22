# Synthetic work-model scenarios

Every record, identity, message and evidence excerpt in this directory is invented.
The scenario follows a community exhibition: a permit, a venue inspection, a brief
sent to a fictional organizer, rental administration, and a print appointment.
No personal messages or reconstructed personal records are included.

`audited/` is a legacy group name for 35 current synthetic records and 12 previous
versions. `made_up_post_audit/` holds 27 extra synthetic records for batching and
boundary cases. Group names are retained for fixture API compatibility.
`manifest.json` supplies explicit memberships; it makes no claim about a real store.

Tests preserve supersession chains, first identities, dependency release, unknown
relationships, relative dates, incomplete evidence, attribution, and owner approval.
Some deliberately incomplete ideas have no origin links. They exercise refusal to
invent missing history. September and October 2026 dates are fixed test clocks.

`messages_30_sep.json` contains fictional inputs with known or explicitly unknown
clocks. `moved_over.py` is an independent expected migration result. `helpers.py`
materializes an isolated tree and generates 200 independent records for reply-size
tests. Nothing reads a personal installation or contacts a live service.
