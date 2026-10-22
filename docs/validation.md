# Local validation

Prepared on 22 October 2026. This record describes local source and package checks. The GitHub Actions workflow is proposed configuration; it has not run on GitHub. No live provider, deployment or personal account was exercised.

The validated source comes from checkpoint `cc96617` in the local preparation repository. This publication snapshot preserves that source, with subsequent documentation updates and aggregate experiment data. It starts a fresh history so removed concept media is not included in earlier public commits.

## Environment

Windows, Brain Core on Python 3.13.12 and Gremlin Town on Python 3.13.1. Dependencies were installed from the component lockfiles with uv 0.10.0. A separate Python 3.13.12 environment was used for installed-wheel checks.

## Checks

| Check | Result |
|---|---|
| Brain Core full suite | 2,854 passed |
| Gremlin Town full suite | 3,703 passed, 2 skipped; 12 warnings |
| Headless offline journey | Passed, including Python `-O` |
| Both source distributions and wheels | Built successfully |
| Installed-wheel journey | Passed in a separate environment |
| Bundled synthetic ownership catalog | Installed bytes and SHA-256 match their pins |
| Fresh local clone | Offline journey passed; all 15 adapter entries match committed bytes and SHA-256 pins |
| Lockfile consistency | Both checks passed |
| Public-file Python syntax | No syntax errors |
| Privacy and secret checks | Bounded checks completed; final disposition below |

The two Windows skips cover POSIX file modes and systemd credential mounts. The Gremlin Town run emitted dependency deprecation warnings from its test-client stack. Brain Core completed in 134.43 seconds and Gremlin Town in 479.46 seconds on this computer; these are suite durations, not application performance benchmarks.

Historical parser acceptance was replaced with explicit legacy-field/current-reader roundtrip checks because this repository does not include private historical Git objects. This proves the retained shape and inverse behavior, not acceptance by an old installed release. Original response-size limits and committed-byte integrity checks remain enforced.

## Reproduce

From the repository root, with uv installed on PATH:

```sh
uv sync --project components/brain-core --frozen
uv sync --project components/gremlin-town --frozen --extra dev
uv run --project components/brain-core --frozen python -m pytest components/brain-core/tests -q
uv run --project components/gremlin-town --frozen python -m pytest components/gremlin-town/tests -q
uv run --project components/gremlin-town --frozen python examples/offline_journey.py
uv build --project components/brain-core
uv build --project components/gremlin-town
```

Integrity tests compare adapter files with committed bytes, so use a Git checkout with a commit rather than an uncommitted directory copy. The documented rollback test creates its own confined temporary directory; no custom pytest temporary root is required.

## Privacy and scope

The export excludes private Git history, memories, conversations, operational audits, enrollment snapshots, retired code and release bundles. Personal fixtures were replaced with 74 fictional records and 34 fictional messages, with regenerated evidence digests and consistent synthetic identifiers. Private ownership policy was replaced with demonstration policy that requires explicit owner-root bindings for writes.

Detect-secrets 1.5.0 was run locally with network verification disabled. Its final scan reported 86 candidates in 46 files. Review classified them as integrity digests, explicit synthetic credentials, rejection-test URLs, credential paths/field names and scanner literals. No actual credential was established by those findings. The scan excluded dependency environments, generated temporary files, lockfile hashes and rendered media. Separate path/scenario checks covered the included source.

This is not a guarantee against every privacy leak or vulnerability. Linux CI, live integrations, production operation and broad product usefulness have not been established by these checks. The historical model comparisons were not rerun.
