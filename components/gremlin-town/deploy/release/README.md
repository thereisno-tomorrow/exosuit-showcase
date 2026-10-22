# Release helper examples

Scripts illustrate pinned-artifact preparation, preimage checks, release
switching, record readability and work-shape migration. Private installation
narratives and the one-time connector-registry migration are excluded.

`brain-home-readable.py` and `brain-home-archived-names.py` inspect a supplied
record tree. `work-move-over.py` and `work-move-back.py` are read-only by default;
review exact options before authorizing migration. Linux hub and adapter scripts
assume an installation layout which must be reviewed for the operator's host.

`damien-map.py` and `damien-files.sh` retain historical filenames for compatibility.
Their example backing directory is `/srv/hermes-example`, not an installed store.

After modifying a pinned adapter, run `python scripts/repin_manifests.py` from
the component root. Source hashes prove bytes, not deployment.
