---
name: calendar-time
description: Use for changing Nic's day, blocking calendar time, or handling lengths in FlowSavvy mode. Keep retries exact and follow the active mode.
---

# Calendar time

Load this guide before `gremlin_town_moments_signal` with `day_change`. A setting
being documented here never switches it on. Follow the installed mode and any
disabled-feature reply; do not substitute a different writer or invent a time.

## FlowSavvy mode

`[flowsavvy] enabled` uses the schedule. With its separate `busy_calendar` switch
on, Nic's “I'm done at 4” means a busy entry from 16:00 to midnight today in
Singapore. Another finish instruction moves that day's same owned busy entry.
“Block 2 to 3” gives a separate busy interval. Ask for the missing endpoint if
he gives only one; never guess a duration or day. No Brain Home item is created.

Send `day_change` with JSON in `detail`: either `ends_at: "16:00"` or
`busy: {"start": "14:00", "end": "15:00"}`. Clock-only values mean today in
Singapore; another day needs ISO dates and times with explicit offsets. An
explicit range may have started but must not have ended. A finish time must
not already have passed. These are examples, not Nic's instructions to execute.

Include an `operation_id` generated once for this instruction (8–128 ASCII
letters, digits, hyphens, dots or underscores). It is required for new finish
times and clock-only endpoints; fully dated intervals can derive their key.
Reuse it and the exact input on
retry; use a fresh one for a new instruction, even when its time matches an old
one. Include `words` only as he said them. Never reuse a key with changed input.

Read `calendar.text` in the reply. `calendar.status: blocked` confirms the time;
`needs_check` means it is unconfirmed. Tell him plainly; do not claim success,
invent a replacement key or write again to fix it. A retry may confirm an
uncertain operation by reading but never blindly changes the calendar again.
The normal recorded-signal acknowledgment alone confirms no calendar write.
Guests are protected and no notifications are sent. FlowSavvy's schedule may
take time to reflect Google's busy entry; do not promise immediate scheduling.

“No deep focus today”, “make it 3 hours” and moving a placed slot are unavailable
in this mode. Working hours are changed in FlowSavvy. Never silently substitute
a whole-day busy entry for one of those requests.

With `[flowsavvy] length_flow` on: save a new thing as said. A length Nic supplied
is sent on for scheduling; without one it waits for the morning review. Do not
ask “How long?” in the save reply and never guess a length. The review asks when
he gives the thing a day; parked, dropped and Someday do not need that question.
Record only the number and words he gives. Prior stored estimates are not new
permission to guess. Changes still use the governed door, never FlowSavvy directly.

## Placement mode

Keep the existing controls: `deep_focus_minutes` is 0 for none or 90–360;
`ends_at` is HH:MM; `move` is the supplied record reference he said to move;
`words` is his exact text. Send those fields as JSON in `day_change` detail.
No busy control from the section above applies while FlowSavvy is off.

After his explicit yes settles the day, show `placement.summary`. If
`placement.outcome` is `plan_pending`, say the plan is still being drawn and
“where am I?” will show it. `cannot_read_brain_home` or `plan_failed` means it
was not planned: tell him `placement.error`. Silence never settles the day.
Placement-mode labelled estimates remain allowed by the existing next-action
guide; never pad a number Nic supplied.
