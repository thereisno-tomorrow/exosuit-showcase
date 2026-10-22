# Host packaging examples

The shared contract states product trust and persistence boundaries. Host files
describe runtime-specific integration checks. They grant no permissions and
contain no live account registry or installation evidence.

From the component directory with its Python environment:

```console
python -m gremlin_town.host_packs.loader host_packs
```

The bounded loader hashes seven local Markdown files. Its output explicitly
reports `native_loaded: false` and `installed: false`. It creates no grants and
invokes no native loader or network call.

Private receipts, rosters, proof records and workspace policy are excluded.
Generate new evidence for an operator-owned installation.
