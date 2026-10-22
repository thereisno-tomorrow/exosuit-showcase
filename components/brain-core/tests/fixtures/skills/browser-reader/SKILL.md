---
schema: brain.skill/v1
id: browser-reader
version: "2.1.0"
description: Read an authenticated browser source.
positive_triggers: [read linkedin]
negative_triggers: []
supported_modes: [research]
required_authority: [session-read]
dependencies: [browser-session]
host_capabilities: [host-browser]
portability: host
owner: external-browser-suite
---
# Browser reader

BROWSER-INSTRUCTION-BODY
