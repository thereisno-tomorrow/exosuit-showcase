---
name: next-action
description: Suggest organisation for part 3 of Nic's review using his goal, project, task and label rule. Nic supplies parts and next actions; ask What's in the way? when he is stuck. Propose for his yes or rewording, never write his actions for him.
---

# Next action

This is the suggestion guide for part 3, New, of Nic's review. Its job is to
clarify and organise what came in, not to write a first step. You suggest; Nic
decides. Brain Home owns his work. Diablo applies his answer through `bc_work`;
other agents capture new things as said and leave changes to Diablo or the
morning review. Never make a second writer or change an existing item through
`bc_memory_change`.

**One text for every agent.** `.claude/skills/next-action/SKILL.md` in Exosuit and
`components/gremlin-town/adapters/hermes/skills/next-action/SKILL.md` are
byte-identical copies. Claude loads the local copy; Codex uses `vault-workflows`;
Diablo loads his packaged copy. Agents without repository access receive the
whole text in their task context. This is guidance, never execution authority.

## What part 3 asks

Use the supplied new items and their line references, questions and offered
moves. Read relevant open items only when needed to resolve organisation; never
read back a write to confirm it. Don't build an unrelated inventory or force
already clear items through questions.

- **New, nothing needed:** Nic's own clear captures, such as "Pay wifi bill",
  stand as said. Nothing to answer unless he changes one by number or title.
- **For your review:** someone else's suggestions, a possible goal/project, maybe the
  same thing twice, agent wording or a date reading to confirm, or unclear words.
  Offer one short question for his yes or rewording.
- **Two minutes or less:** offer "Do it now?"; this is an offer, never an
  automatic tick, execution, or permission to invent a next action.

You may suggest kind by his rule, what it sits under, a label, merging, waiting,
someday, reference or drop, block type and rough length. Label every guess.
Never invent or write parts, next actions, dates, people or causes. Record
Nic's own parts and next actions only as he says them. Don't rewrite "decide",
"think about" or "look into" into a physical action you chose. An ordinary lunch
message ("had an amazing laksa for lunch lol") saves nothing.

## Nic's rule, drawn from his ten examples

A **goal** is an aim he wants to be true; several projects could serve it. A
**project** is one concrete pursuit he's chasing, with a path not laid out and
big, different kinds of work. A **task** is one piece of work with a finish he
can see and roughly a way there, even when it has several parts. A **label** is
a shared theme among unrelated things, with nothing itself to finish.

| Synthetic example | Suggested kind | What the example teaches |
|---|---|---|
| Job search | Goal | An aim; more than one pursuit can serve it |
| Fund-raise | Goal, if he takes it up | An aim, still an idea until his yes |
| Exhibition application | Project | One concrete pursuit towards job search |
| Exosuit | Project | He's building it; many pieces don't make it a goal |
| Exhibition permit | Task with subtasks | A visible finish and roughly known path |
| Display-stand reservation | Task with subtasks | An uncertain scheme alone doesn't make a project |
| Email migration | Task with subtasks | Weeks of work still fit a visible finish |
| Preparing for a pitch | Task with subtasks | Several pieces don't make a project |
| GTBC evaluation | Task with subtasks, a guess he let stand | Keep the limits of the example clear |
| Admin clear-out | Label: life admin | Unrelated jobs share a theme |

Size, length, number of steps and other people being involved do not decide
kind. "Path not known" alone isn't enough: display-stand reservation is still a task.
Holding many things doesn't make a goal: Exosuit is a project. The rule is a
starting point from ten things, not a finished taxonomy; his corrections add
examples. Capture saves a task as said, with no interview. Suggest "looks like
a project: yes?" in review; nothing becomes a goal or project until his yes.
A task given parts stays a task, whose parts are subtasks.

A goal holds projects and tasks. A project holds tasks, never a project. A task
can have subtasks; a subtask holds nothing. A task can stand alone or sit under
a goal or project. Suggest a parent only from named, relevant things, for his
yes; never infer a relationship from a similar title alone. A label is optional
on a goal, project or lone task; contained things take their parent's label.
A finish line is optional and in Nic's words, not your invented outcome.

A next action is an optional line on a task or subtask, never a separate record.
It is recorded only when Nic names it as a next action. "Edit my resume before
I send to Louisa" names a subtask of "Send Louisa my resume", not a next action
you inferred. Ticking a next action leaves the task open; a new one replaces a
finished one in history. Most tasks need none: the title already is the action.
Projects get their next from their tasks, never your own next-action line.

## Short suggestions, one decision at a time

Keep the supplied number and line reference. Say the proposal and its reason
only when useful: "Looks like a project, like Manus: yes?"; "Same as 'the tax
filing'? Merge?"; "Life admin label?" Nic can say yes or reword it. A merge ends
one thing, so it needs his decision; don't silently drop a duplicate. A clear
capture needs no forced confirmation just because it is new.

Other people's listed parts may be shown for his yes or rewording, clearly
attributed: "Life admin's list names three parts: <their exact list>. Yes?"
Never write an extra part or change their words to sound like Nic's. Their words
stay suggestions until he accepts the matching question. A plain yes means
only what that question offered. Keep the question, offered moves and reply
as evidence; Diablo names the supplied line reference in `answers`.

For an outcome with no parts, offer "Parts? Say them, or leave it." Breaking
down is offered, never forced. Don't draft a full step list or quietly use one
to turn a task into a project. Nic's parts can be any size and wording.

Waiting: ask who or what only when his words leave it unclear. Never supply a
person, cause, channel, expected date or dependency he didn't name. Someday is
a proposal for something he might want, not permission to park it yourself.
Reference fits a fact, not a silent conversion of a task; propose saving a fact
or dropping the task for his decision. Dropping always needs his words and
reason. Before dropping a promise ask whether the other person has been told.

Block type may be a labelled guess: deep focus for thinking work, admin for
small tasks/calls/messages/home jobs, errands for going out. Rough length is
a labelled estimate, allowing travel for errands; never pad a number Nic gave.
His own number is saved with `length_is_nics`, his words, and his confirmation.
Guesses don't authorise parts, a timetable or work on his behalf.

When FlowSavvy's `length_flow` is on, its rule overrides rough-length guesses:
never guess a length or ask “How long?” in a capture reply. A length Nic gives
is sent for scheduling; without one the thing waits for the morning review.
In New, ask when he gives it a day, not for parked, dropped or Someday. Load
`calendar-time` for the active mode's full rule. Placement-mode advice above
still applies when that feature is off.

## Stuck: Nic supplies the answer

When Nic says he is stuck or avoiding it, ask "What's in the way?" Nic supplies
the answer. Ask for the very next physical thing when the review calls for it;
never suggest a tiny first step or turn his answer into an if-then you wrote.
Age alone doesn't prove avoidance, and a wait for an appointment isn't stuck.
Save his named action as said through Diablo; don't invent dates, people or
causes to unblock him. If he parks or drops it, that is his decision too.

Dates are read by code first through the door, from his exact words. Never
invent a deadline, appointment, planned day or not-before date. A labelled
proposed start-by is the separate permitted proposal; it counts only after he
keeps it. A supplied agent date reading waits for his yes or correction.

After his answer, Diablo makes one door change for his exact raw message, with
`answers` for the supplied lines and only their offered moves. Other agents
leave existing work to that route. Say the proven receipt's `reply.say`; never
claim a suggestion was accepted or saved before the receipt proves it.
