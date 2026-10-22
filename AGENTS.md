# Working on Exosuit

Start with README.md, docs/architecture.md and docs/quickstart.md. Brain Core and Gremlin Town are separate packages in one repository; review and orientation features live within them.

The [documentation index](docs/index.md) links design history, model/code responsibilities, AI-assisted development, operational examples and evaluation. These accounts distinguish historical development runs from behavior demonstrated by the current snapshot.

Use Python 3.13 for the combined project. Create both locked environments before testing. Run each component suite separately; their test module names can overlap. Run examples/offline_journey.py through Gremlin Town's environment.

Keep Brain Core deterministic and preserve explicit ownership, provenance, current-version and retry boundaries. Use invented fixtures. Do not add personal messages, credentials, deployment snapshots or live account configuration.

The local demo uses a fake transport. Live adapter execution, external messages and deployment require explicit authorization and separate acceptance checks. Tests do not establish live service health.

Update the public docs when changing behavior. Preserve unrelated work, inspect diffs and run proportionate validation. Original code is all rights reserved; this public snapshot does not grant an open-source license.
