# Exosuit

**Your AI assistants don't know what the others are doing. Exosuit does.**

Exosuit is a shared memory and task list that ChatGPT, Claude, Codex and your other AI assistants can all read and write. It tracks what you asked each one to do, checks what comes back, and tells you what to work on next.

[Try the demo](#try-it) · [How it works](#how-it-works) · [What's built and what isn't](#whats-built-and-what-isnt)

## Working with agents IRL can be messy

Plenty of people use more than one AI assistant, because each is better at something different.

Take one project over a week:

- You work out the idea in ChatGPT, because talking it through is quick.
- You build it in Codex or Claude Code, which work directly on the files on your computer.
- You send the big research job to Grok Bot, which runs a fleet of agents on its own. It needs what you worked out in Claude.
- You write it up in Claude on the web, using what the others found.

Each tool may be right for one part of the job, but context is scattered across them. So the real state of the project lives in your head, and you pay for that every day:

- You copy context from one chat into the next.
- You re-explain decisions you already made.
- An assistant says "done" and you take its word, until the links turn out to be broken.
- With ten projects going, you're guessing which one to touch first.

The assistants aren't the problem. Nothing sits above them keeping track, so you do it yourself.

## What Exosuit does

Switching between assistants is only half the mess. The other half is how much you can now do at once.

Agents can do a lot of the doing. What they can't do for you is remember what you asked, notice what needs you, or decide what's worth doing next. That's executive function, the planning and staying-on-track part of your head, and it's still on you. Your memory, attention and focus are the bottleneck now, and every extra agent leans on them harder.

Exosuit has one job: take that load off you, so your agents can go and get things done while you stay in charge.

It's built around GTD (Getting Things Done), David Allen's method for keeping a clear head. You get everything out of your mind and into a system you trust. You decide what each thing is and what happens next. You review it regularly, then choose what to do. Exosuit runs that loop with you and plugs your agents into it.

It works in three parts, one for each thing you stop carrying in your head.

**1. Memory: one shared record.** Tell any assistant something once, in plain words. Exosuit saves it to a record you own and gives you a receipt, or a clear failure. It never says "saved" when it isn't. Every assistant you've connected can then read it, so they all start from the same facts. An idea stays an idea until you say it's a task, and nothing is overwritten, only versioned.

**2. Attention: handoffs that are tracked and checked.** When you give work to an assistant, Exosuit records who got it, what was asked and where it stands. When the assistant says "done", Exosuit doesn't take its word for it. The request stays open until someone else checks the result, and the assistant that did the work can't sign it off. You don't have to watch your agents. Exosuit does that, and it only comes back to you when something needs you.

**3. Focus: Ciel, the executive function layer.** Ciel does the planning and prioritising, and you approve. It follows the same GTD steps:

- **Capture and sort.** Whatever you tell an assistant is captured. A morning review helps you sort what's new into what it is and what's next.
- **Review.** The morning review shows what's coming up and what slipped. On Fridays a longer one covers everything you're waiting on and every goal.
- **Rank.** You say what a slip would cost, from "changes my life" down to "nothing much". Fixed rules rank everything and show the reason for each pick.
- **Choose.** Each morning you pick your day. Ask "what next?" any time and you get one top pick and two alternatives.

Throughout, you make the calls. Exosuit suggests, and nothing important happens without your yes.

## A day with Exosuit

Picture a normal day.

- **Morning.** A card lands in your chat app: what's due, what slipped, what's new. You pick the three things that matter today.
- **Midday.** You ask "what next?" The top pick is the intro you promised Sam by Friday, with the reason. Two alternatives sit under it.
- **Handoff.** You give the link check to Codex. Exosuit records who has it.
- **Afternoon.** Codex says it's done. Exosuit keeps the request open until Claude confirms the links work.
- **Evening.** Nothing buzzes. It only speaks up when something changes.

## How it works

Three parts, one for each job above.

- **[Brain Core](components/brain-core/)** is the memory and the rules. It holds the records and knows who owns what, who may read it, and what counts as done. It's plain deterministic code with no AI model inside, so the same input always gives the same answer.
- **[Gremlin Town](components/gremlin-town/)** is the post office. It carries requests and replies between your assistants, and they reach it over MCP, a standard plug many assistants now support. It writes down every message before sending it, so if a confirmation gets lost it checks before it resends, instead of sending twice.
- **Ciel** is the executive function layer. It runs the GTD loop: the sorting, the morning and Friday reviews, the stakes and ranking, and the routines (streaks for the things you do on repeat). It lives inside the same code as the other two, and it's the part that works out what matters.

Why rules and not an AI? An AI is good at suggesting. It shouldn't decide what counts as saved, who can read what, or whether work is finished. Plain code does that, so you can always see why.

Around the core sit optional pieces: the morning card (Discord in the reference setup), a web dashboard behind a passkey (read-only unless you switch writes on), calendar placement and the important-email ping. All of them are off until you set them up.

## Try it

Every handoff system has to answer one question: what happens when the confirmation gets lost? The demo asks it. It runs the real request service, database and record store, with made-up participants and a scripted provider. It then:

- queues a request and loses the provider's receipt
- reopens the database and recovers **without sending a second time**
- refuses an unrelated participant
- stops the recipient from checking its own result
- recovers the "where I stopped" note through a fresh reader

You need Python 3.13 and [uv](https://docs.astral.sh/uv/). From the repository root:

```sh
uv sync --project components/brain-core --frozen
uv sync --project components/gremlin-town --frozen --extra dev
uv run --project components/gremlin-town --frozen python examples/offline_journey.py
```

No account, API key or AI model involved. You can read the [captured output](examples/README.md) first, or follow the [full quickstart](docs/quickstart.md).

This demo covers the memory and handoff half. The ranking, stakes and morning review run on the same records and have their own tests, but there's no one-command demo for them yet.

## What's built and what isn't

- **Tested:** 6,557 tests pass locally (2 POSIX-only checks skipped on Windows). The Linux CI is set up but hasn't run on GitHub yet. [Validation](docs/validation.md) has the commands and limits.
- **Optional pieces** (morning card, dashboard, calendar, email ping) are in the source, off by default, and need your own accounts. The demo doesn't touch them.
- **One person per copy.** Exosuit is built for a single owner. Teams and shared records aren't supported.
- **Not shown yet:** whether it keeps being useful week after week. I built it for my own use, with AI coding assistants ([how](docs/development.md)), so the open question is whether it holds up for anyone else.

## Read more

- [Architecture](docs/architecture.md): who owns what, how writes survive failures, and where to look in the source.
- [Product case study](docs/product-case-study.md): the problem and the design choices behind it.
- [Evaluation](docs/evaluation.md): a retrieval experiment that looked promising and still got rejected, and why.

## Reuse

All rights reserved. Source is available for inspection; no open-source license is granted. Third-party dependencies keep their own terms. See [LICENSE](LICENSE).
