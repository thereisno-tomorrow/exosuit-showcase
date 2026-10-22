# How Exosuit took shape

Exosuit grew out of several attempts to keep work connected across AI tools. Brain Core and an earlier Ciel design already existed when I built Gremlin Town. I then brought agent messaging and shared memory together, added the current review and orientation features, and connected the surfaces I used to reach them.

The parts changed as I used them. Some decisions below concern the architecture in this checkout; others explain how I arrived at it. Earlier Ciel work included a model-backed assistant. The Ciel described in the current [architecture](architecture.md) is review and orientation functionality inside the two component packages.

## Start with the work

In August, the problem was a setup assembled from several systems that had become brittle and slow. I wanted to move between projects and assistants without reconstructing the context each time. Existing capture, decision and orientation workflows were useful; carrying their machinery into every conversation was less useful.

I asked for the intended outcomes to be separated from the architecture an earlier model had suggested. Keeping a working component was a choice to examine, rather than a reason to keep its whole design. Selective context loading, speed and operating cost became explicit requirements.

When Gremlin Town was being designed in September, I brought it back to its central job: agents should be able to send context to one another. The initial proposals also covered session management and broader work ownership. I asked for a simpler alternative and compared the designs before settling the scope.

Once the communication layer existed, I asked what could be reused from Brain Core. The first integration proposal was too large, so I asked for a three-hour slice. Existing connection work also needed to be counted as existing work. That shifted the discussion toward joining capabilities already built.

The resulting boundary is still visible: [Gremlin Town](../components/gremlin-town/) carries authenticated requests and their delivery state; [Brain Core](../components/brain-core/) validates and routes governed records and applies work rules. Brain Home and project vaults own the canonical records. A message arriving and a durable record being saved are separate events.

## Where the shared record lives

The shared memory needed an authoritative home that assistants could reach independently of a particular chat. I chose GitHub for Brain Home's canonical records. One alternative put the authoritative store on the application server and used GitHub for history; I kept GitHub as the source of truth.

That decision gives the application server a specific job. It can cache records and serve views, but it must reconcile with the canonical store. A dashboard or scheduling integration does not acquire authority over a record merely by displaying or copying it. Local project content also keeps its own access and ownership boundaries.

GitHub-backed storage brings network cost, concurrent updates and uncertain write outcomes. The implementation therefore separates publication from reconciliation. If a write was accepted but the reply was lost, repeating it blindly could create another version. The storage layer first checks what happened.

See [canonical publication](../components/brain-core/src/brain_core/github_storage.py) and its [failure tests](../components/brain-core/tests/unit/test_github_storage.py). The [architecture](architecture.md) describes the current retry and ownership boundaries.

## Model and code responsibilities

A model can interpret a loose progress update, suggest steps or explain a result. Saved records, access checks and version transitions need explicit rules. Brain Core has no model inference dependency. Given the same records, clock and other inputs, its readers and ranking return the same result.

That split developed through discussion and testing with the coding assistants. I set speed and context cost as requirements, accepted deterministic machinery for routine work, and kept questioning where a model was actually needed.

One September proposal prefetched memory for the daily assistant, then still asked its conversational model to decide and perform an ordinary save. I asked why that operation needed the full model loop at all. The next investigation examined what the runtime could do directly and where language interpretation was still necessary. Faster storage alone would not remove repeated model steps around a save.

I also reopened whether Ciel's choosing should be code as agent capabilities improved. The useful distinction was between interpreting what I meant and applying the stored work rules. A model may propose a stake or a plan; a proposal does not become my confirmed choice just because the model wrote it confidently.

| Job | Responsibility |
|---|---|
| Interpret an unfamiliar phrase or propose a breakdown | Agent, with its interpretation or authorship preserved |
| Validate a record, its owner and its version | Code |
| Work out progress, waits and the current next step | Shared deterministic readers |
| Rank eligible work and expose the reasons | Code using explicit inputs and confirmed stakes |
| Confirm a suggestion or change a personal commitment | Owner action through the applicable interface |

The current implementation is in [records](../components/brain-core/src/brain_core/records.py), [work interpretation](../components/brain-core/src/brain_core/work.py), [review](../components/brain-core/src/brain_core/review.py) and [orientation](../components/brain-core/src/brain_core/orientation.py). A separate reviewer's check provides an attributed sign-off; it does not mechanically establish that arbitrary prose is true.

## Work changes shape

The first work model was too flat. One item held one first step, one date and one state. Actual work had projects, steps, appointments, dependencies and progress updates. A completed first step could remain stored as the next action. Different surfaces could interpret the same records differently.

I asked for a problem-first redesign, grounded in actual messages and records, before continuing retrieval work. Improving search would not repair a record that represented the work incorrectly.

Consider this fictional update:

> I drafted the proposal. Sam needs to check it before I send it. The client call is Friday.

The distinctions matter. Drafting is complete. Checking belongs to Sam. Sending waits on that check. Friday is the client call; the sentence has not supplied a sending deadline. Saving the whole update under one due date and one next-step field loses those distinctions.

The redesign separated the work's shape, dates and waits, and put their interpretation in one shared reader. The user's next action can be stored in their words; which action is current, what is blocked and how far the project has progressed are worked out from the records. Every surface should use that interpretation.

I also kept capture light. A simple action does not need a project plan. Capturing something should not start an interview about its steps. Clarification belongs in the review or in a later progress update, when it is useful.

Inspect the [work reader](../components/brain-core/src/brain_core/work.py) and [reader tests](../components/brain-core/tests/unit/test_work_reader.py). The [product case study](product-case-study.md) explains the wider capture and resumption flow.

## Keep your place in the review

The Discord review was developed through interactive prototypes. I folded the separate Look page into the opening, narrowed the upcoming-work view, separated navigation from task actions, and asked for parent and project context to remain readable.

Asking an agent for help introduced another state. If I asked it to research a task or break it down, advancing the card immediately lost the place where its answer belonged. The card needed to stay on that item, show that work was in progress, and make the returned draft available there. Skipping still needed to be possible.

New work added during the review also needed to join that review. Leaving it for another morning made the capture action feel disconnected from the work already in progress.

These were prototype iterations using sample data, in-memory state and scripted assistant replies. The production interface needed its own integration checks. The retained [review-card source](../components/gremlin-town/src/gremlin_town/review_card/) and [tests](../components/gremlin-town/tests/review_card/) can be inspected separately from that design history.

## Delegate scheduling

I had built calendar placement, but the scheduling behavior was unsatisfactory. When considering FlowSavvy, I questioned whether continuing the custom scheduling logic made sense if an existing tool handled it well.

The boundary I chose was to use FlowSavvy behind Exosuit's interface. Brain Home would keep intent and completion; the scheduler would calculate times. I asked for the effects on the whole system to be reconciled before more interface building continued.

That changed several interactions. A sized task could be scheduled without waiting for the morning review. A task with no length could be captured immediately and clarified in the review, rather than prompting for duration at capture. Schedule times were a view returned by the integration, not new canonical task facts. A bounded trial was agreed; that decision alone did not establish the trial's usefulness.

The optional [FlowSavvy connector](../components/gremlin-town/src/gremlin_town/flowsavvy/) and [connector tests](../components/gremlin-town/tests/flowsavvy/) are retained. It is disconnected in the public demo.

## What stayed open

The design still has to earn its place through repeated ordinary use. A correct record model and a recoverable handoff are necessary, but they do not establish that maintaining the system costs less attention than it saves.

The next questions are practical: can someone understand a review without learning the record model, resume useful work after a gap, and keep the system current without extra administration? The [operating examples](operations.md) show why connected behavior needs its own checks, and the [evaluation](evaluation.md) shows why an improved component metric can still miss the requirement.
