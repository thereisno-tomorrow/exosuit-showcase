# Building with coding agents

I used AI coding assistants for research, architecture proposals, implementation and review. My role was to define the outcome, make product and architecture choices, question proposals, and decide what to accept. The assistants supplied substantial design detail and code. The workflow changed as their capabilities and the work changed.

## Keep the outcome separate from the proposal

An architecture written by a model can become the starting assumption for the next model. I periodically asked for the intended outcome to be reconstructed without assuming the existing implementation was the best way to reach it.

That happened before Brain Core's early architecture review, when Gremlin Town's scope was reduced, and when the work model was redesigned. It also mattered when resuming from agent-written handoffs: a detailed instruction could faithfully preserve an earlier plan while losing the reason for doing the work.

The [design history](design-history.md) follows several of those decisions through to their consequences.

## Use different models to challenge the design

During the August architecture work, I explicitly asked for proposals to be checked across model families so one model would not have sole influence over the process. I later checked that reciprocal Claude and Codex review had actually been included in the handoff.

For the September work-model build, that became a more detailed workflow: bounded assignments, separate builder and reviewer sessions, recorded findings and repairs, and takeovers when a builder did not resolve the problems. High-stakes slices built with Codex were reviewed with Claude; other slices could use a fresh session from the same provider. The detailed assignment and verification rules were developed with the assistants.

Model diversity adds another view. Review still needs a defined question, evidence and an acceptance decision.

## Protect the lead's context

For a memory-tools research pass, there were sixteen articles to examine. I asked subagents to summarize them to preserve the lead agent's context, then had the lead assess which ideas mattered for Exosuit. Extraction and synthesis were separate jobs. The lead checked the original material and corrected claims in the summaries.

The same principle applies to tool and skill loading. Keep the ordinary path small; retrieve detailed instructions when needed. A large tool response becomes part of later model calls too, so its cost extends beyond the operation that produced it.

I also adjusted prompts to the harness. For a design session using Fable, I rejected a procedural handoff that constrained how the work should be done. I asked for context and the desired result instead, removed an unrelated repair backlog, and restored the morning-review design objective.

## Coordinate shared work

Multiple coding chats can change the same files or operate the same service. I asked for a preflight across Claude Code and Codex to detect active work that might conflict before starting another change.

The work-model build used one branch and worktree per slice, bounded file ownership, an integration branch and a ledger. Its twenty-one slices covered the record shape, shared readers, commands, reviews, adapters, migration and integrated replay. Finished source work did not authorize installation or personal-data migration.

One integration detail illustrates why the review had to inspect behavior. A repeated write needed to recover its original operation without creating another record. The integrated replay also had to work after records moved into history and later writes had resumed. Matching a title or a timestamp was insufficient; the operation needed its recorded identity and publication evidence.

The historical build report records four failure-driven builder takeovers. Final checks ran against the same frozen integration tree. The scenario suite included independent mutations of selected assertions to check that the tests would detect the corresponding regressions. Those were local build checks; current publication results are recorded separately in [validation](validation.md).

The retained [work replay tests](../components/brain-core/tests/unit/test_work_replay_history.py), [record move tests](../components/brain-core/tests/unit/test_workmove.py) and [publication tests](../components/brain-core/tests/unit/test_github_storage.py) expose relevant mechanisms in this snapshot.

## Check the answer key too

A September experiment compared models for narrow capture and deferral decisions. Before running it, I asked for another model to review the expected answers. It received shuffled items and definitions without the first model's labels, notes or control identities. I settled the disagreements.

One disagreement concerned a tentative thought about a possible project. Treating it as either a task or nothing missed the intended meaning. My ruling was that it belonged in someday/maybe. That produced an explicit idea category in the fixtures. Other disagreements clarified the difference between a report that someone did something and a claim that they approved something.

The review was two models' judgment with my adjudication of disagreements. It was not a fully human-labelled benchmark.

The experiment then compared a specialist judge, smaller general models and a stronger reference model. Threshold fitting and held-out scoring were separate. Controls included planted approval claims and questions that could not be answered from the supplied material. After changes to the prompt and deterministic checks, twenty-four fresh cases were added rather than relying only on the cases that shaped the fixes.

The fastest specialist still missed the required quality floor. Speed and estimated cost were useful only after the required behavior was satisfied. These historical trials concerned narrow synthetic tasks; the model environments and complete trial artifacts are not bundled in this snapshot.

The public [retrieval evaluation](evaluation.md) records another instance of the same question: did the improved metric establish the behavior the product needed?

For the actual package layout, publication selection and local contribution rules, see [development and source selection](development.md). For what happened when the code met its connected runtime, see [operating the integrations](operations.md).
