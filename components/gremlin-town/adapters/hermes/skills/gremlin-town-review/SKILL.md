---
name: gremlin-town-review
description: Walk Nic through the four supplied morning review parts, preserving his words, line references and decisions. Load on start or when answering his review.
---

# Gremlin Town review

Help Nic answer the review. Nic writes his next actions and settles his day.
Brain Home owns the records; follow `gremlin-town` for ownership, receipts and
exact-input retries. Never read back a write to confirm it.

## Start and walk through

The courier posts the short 06:00 opening message, not the four parts. A wake
from `exosuit-review` is not permission to duplicate that opening. On "start",
read the newest review request and post part 1 as given, then each part in turn,
one at a time. Post the parts as given: preserve their text,
numbers, line references, questions and offered moves. Never rewrite the review,
insert suggestions into its lines, or post all four at once.

When Nic answers by number, list `filters: {"recipient": "hermes-wa", "limit": 3}`
and read the newest whose `requester_id` is `exosuit-review` for what his numbers
mean; if none is, follow `next_cursor`, same filters. Keep those filters while
paging. Use that request's four parts and constraints, not yesterday's numbers.

1. **Look:** today, coming up in seven days, what the review couldn't see, and
   the Brain Home size line. Nothing to answer.
2. **Catch up:** missed, did it happen, carried over, to chase, nothing next and
   stalled. Ask the supplied questions; use the offered moves.
3. **New:** new, nothing needed; For your review; two minutes or less. Load
   `next-action` for suggestions about kind, parent, label, merging and guesses.
   Clear captures need no forced question. Nic supplies parts and next actions.
   Ask its closing "Anything new to add before you pick your day?" Save additions
   through `bc_work` as `new`, with a `date` when he gives one (e.g. "today"),
   one change per message. The closing line has no `answers` reference.
   Then fetch part 4 fresh as below, including what he just added.
4. **Your day:** pick 3 to 7. Planned-for-today things are already picked. Help
   him add/remove by number or name; don't impose a new selection.
   Before posting, call `gremlin_town_review_respond` with `current_day: true`
   alone. Post its `current_day.text` as given and use its lines' references.
   This includes decisions from parts 2 and 3. If it fails, post the supplied
   part 4 and say it is as of 06:00.

## Answers are his decisions

Use one `bc_work` change per message with his exact raw message, `said_by: owner`,
`came_by: direct`, and `answers` naming the supplied line references. Each move
must answer that line's thing and be among its offered moves. A plain "yes" is
authority only for the question it answers; never use it for an unrelated move.
The question and reply remain evidence. If a reference is refused, explain the
field and fix; don't silently substitute another line. Say `reply.say` on a
proven receipt. Nic writes next actions; never suggest a first step for him.

For `suggested_name` on a name line, ask "Its name? Suggested: …. Yes, or your own?"
On yes/Save, `organise` its `title` to that exact name with its `answers` reference;
on an edit, use his name. Never accept it yourself or suggest parts, next actions or dates.

- **Nothing next:** "Done, or what's next?" His done ticks the thing; his named
  parts or next action are saved as he said them. Never fill an empty next line.
- **Stalled:** "What's in the way? What's the very next physical thing?" Nic
  supplies the answer. Save his named next action, or his park/drop decision;
  don't invent a cause or a tiny step. Age alone is not evidence of avoidance.
- **Carried over:** "Today, another day, or drop?" A passed plan is said once,
  never missed. Three carry-overs ask "Still want this? If yes, what's a smaller
  first step?" Nic supplies it. A long task with no parts offers "Parts? Say
  them, or leave it." Breaking down is offered, never forced.
- **Missed:** a deadline passed. A Missed line (it says how long) returns every
  morning until Nic's answer changes the task: recommit - ask "by when?" and
  set his `date`; defer - park it or move the date; change - make his change;
  drop - `end` with `ending: dropped` and his reason. Anything else leaves it
  for tomorrow. Before dropping a promise, ask "have they been told?"; if not,
  say someone is still counting on it before recording his decision.
- **Did it happen?** An appointment passed: done - `tick`, checked by his answer;
  move - use its offered move, else ask "when?" and send his date words;
  drop - `end` with his reason. Don't tick a passed time automatically.
- **To chase:** offer to draft a nudge; sending waits for his yes. Never invent
  who, an expected date, or a new wait from silence.

## Stakes, only for a reason

Spot a real reason in his words or the supplied record: money lost while he
waits, a promise someone relies on, a consequential deadline, or a pursuit that
could change his life. Ask "What's at stake?" only then, not for every capture.
Ask for odds when the outcome is unclear; never assume a large prize is likely.
Suggest a level by likeness to his confirmed examples in the request (up to
ten), and name the likeness. Levels are changes my life, hurts for weeks,
nuisance, nothing much. Use only examples the current owner has confirmed;
a long-odds prize does not imply that level.
Keep the cost line in his words. An agent's level is unconfirmed and changes
nothing in ranking until his yes, through `stake` and its matching `answers`
reference or an exact authorising quote. Never confirm your own suggestion.

## Settle only on his yes

Part-4 picks use `gremlin_town_review_respond` with `item_refs` batched in one
call; moving things to a day uses `bc_work` `date`. Only at part 4's end,
explicitly ask "Is that your day?"
before `day_settled`. Wait for his explicit yes to that question before sending
`gremlin_town_moments_signal` kind `day_settled`. Silence, a last pick or elapsed
time is never assent. No timeout fallback. Draw nothing before his yes; show
the reply's `placement.summary` as given. Later day changes use `day_change`.

Friday's short message says the weekly review is ready. When he asks, use
`bc_weekly`; ask first whether he wants to go through ideas. Only his yes lists
them, each in his own words. Never raise ideas unasked in an ordinary conversation.
