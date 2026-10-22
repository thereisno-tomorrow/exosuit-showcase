# Operating the integrations

An integration can pass a local test and behave differently through the surface used day to day. These examples come from the September and October 2026 development runs. They describe dated observations, not the current health of a deployment.

## The storage was fast enough and the conversation was slow

After an early speed update, I tested the daily assistant and still found it slow. The investigation separated storage time from the model calls around it.

In four observed turns on 26 September, saves and changes took about 2.3 to 2.7 seconds, and reads about 0.3 to 0.5 seconds. Complete interactions took 11.6 to 51 seconds. The traces exposed tool discovery, schema inspection, repeated save/change operations and confirmation reads. A large conversation history also affected model calls.

That changed the problem being investigated. Reducing storage time alone would leave unnecessary model steps in place. I questioned why routine persistence needed the conversational model loop and brought the scope back to shared memory across assistants when the discussion narrowed to one bot.

The technical investigation and fixes were carried out by the coding assistants. These four observations are a diagnosis of particular turns, not a controlled before-and-after latency benchmark. The current [Hermes adapter](../components/gremlin-town/adapters/hermes/) and [adapter tests](../components/gremlin-town/tests/adapters/) are available for inspection.

## The CLI passed and the connected surface failed

An October Hermes upgrade was tested in an isolated practice environment. The [acceptance budget](../components/gremlin-town/adapters/hermes/HERMES-RELIANCE.md) was 63,000 characters across the complete tool definitions, with a 12,000-character limit for each tool. A CLI conversation exercised save, recall, review answering and discovery of a less frequent tool. Its observed tool definitions fitted the budget.

I then tested rare-tool discovery through the connected Discord practice route. Additional connected tools changed the actual request. The CLI measurement had omitted part of the surface the model would receive.

| Observation | Total tool-definition characters | Meaning |
|---|---:|---|
| Initial CLI conversation | 61,939 | Passed the CLI check; did not establish the connected budget |
| Connected Discord practice | 76,643 | Exceeded the budget |
| Later corrected Discord practice | 52,680 | Required direct tools, rare-tool discovery and native help/call checks were also verified |

The correction used supported middleware to keep long documentation for infrequent native tools available on demand. Their names and input constraints remained, and the model could retrieve full help before use. The coding assistant implemented and checked the change without editing Hermes core.

A smaller payload alone was not enough. An intermediate run was refused as evidence of success because plugin registration had failed and the Brain tools were absent. Acceptance had to check both size and the presence and use of the required tools.

The final figures describe practice runs with a synthetic backing service. They do not establish a live upgrade, general model quality or a particular response-time improvement. The retained [compact-tool tests](../components/gremlin-town/tests/adapters/test_hermes_compact.py) and [practice-fixture tests](../components/gremlin-town/tests/adapters/test_hermes_practice_fixture.py) cover parts of this integration.

## Rehearse the record change before switching

The work-model migration was rehearsed on a disposable copy before changing the canonical records. The sequence moved records forward, rendered the review, moved them back and read them using the actual earlier package artifact. The old reader's compatibility needed to be checked against the bytes that would be written.

Rendering also caught four user-facing wording problems, including internal stake codes and duplicated questions. Those findings held the switch. A migration that preserves records but leaves an incomprehensible review is still unfinished work.

The agents prepared and executed the rehearsal; I supplied the product decisions and owner answers needed for the move. The public snapshot retains [migration code](../components/brain-core/src/brain_core/workmove.py) and [synthetic tests](../components/brain-core/tests/unit/test_workmove.py), rather than the personal records used in the historical rehearsal.

The [offline journey](../examples/README.md) provides a smaller executable example of the same separation: an uncertain send, a fresh read of persisted state, a distinct check and recovery of a stopping note. Its scope is explicit about the fake transport and scripted work result.
