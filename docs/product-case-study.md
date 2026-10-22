# Keeping your place across agents

An agent can answer a question and still leave the user with the coordination work: remembering a commitment, explaining it to another agent, finding the returned result and deciding where to resume.

I built Exosuit for my own use, to keep my work connected across AI assistants. Shared memory and explicit work state address the coordination I was doing myself. This repository shares that personal tool and its design history; I am not bringing it to market.

The [design history](design-history.md) follows how the components came together, why the work model changed, how the review interaction developed and where scheduling was delegated. The [documentation index](index.md) links the development and operating accounts.

## The interaction

A person saves a commitment. An agent receives a bounded request and returns an attributed result. The person checks the result, saves where they stopped and later asks what is next. A review assembles current records rather than treating an old chat summary as current truth.

The repository contains the record, coordination and review machinery for this interaction. The local example demonstrates selected transitions using fictional work and a fake transport.

## Three product choices

**A saved idea should stay an idea.** The record and work model distinguish ideas, commitments and other kinds of work. A convenient capture interface should not silently turn every thought into an obligation.

**A returned answer should carry evidence.** The coordination layer preserves attribution and delivery state. Canonical records preserve source/provenance fields. Neither a confident answer nor a provider's acceptance receipt establishes successful work.

**Resuming should have a concrete next step.** Stopping notes, explicit dependencies and deterministic readers support “where was I?” and “what next?” without making the model the only holder of the state.

## What this proves

The engineering evidence is strongest around record boundaries, durable publication, request transitions and recoverable failures. The source also contains a substantial review flow and optional integrations. Current local checks are listed in [validation](validation.md).

The practical test is whether Exosuit helps me resume work, keep track of commitments and spend less effort coordinating assistants over repeated use. The local checks exercise the underlying mechanisms. Capture still depends on an agent invoking the appropriate operation.

## What to improve next

Reduce the size of central interpretation/storage modules, measure completion and recovery across repeated sessions, and test whether the review makes my next step clear without requiring me to inspect the internal record model. Extend integration claims only when their account, delivery and real-use checks have been repeated.
