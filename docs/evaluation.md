# Retrieval is not an answer

One of Exosuit's most useful experiment results was a refusal to ship a promising candidate. Better semantic recall did not establish that the retrieved records actually answered the question.

## Historical synthetic comparison

A 5 October 2026 experiment compared current search, FTS5 keyword retrieval, and a hybrid of keyword retrieval with local embeddings. It used invented records and held-out questions, with cutoffs frozen before evaluation.

The [retained aggregate metrics](evaluation-data/recall-summary.json) contain numeric results for the three retrieval arms, without source snapshots, messages or model packets.

| Measure | Current search | FTS5 keyword | Keyword and meaning |
|---|---:|---:|---:|
| Paraphrase queries with correct evidence among five results | 4/10 | 4/10 | 9/10 |
| Unsupported queries returning no records | 0/10 | 1/10 | 0/10 |
| Original regression queries with correct evidence among five results | 20/20 | 20/20 | 20/20 |

The hybrid retrieved useful paraphrases but returned related records for every unsupported question. A record saying where a password is stored, for example, does not contain the password. This measured retrieval, not generated hallucinations. The candidate failed the planned missing-information gate and remained experimental.

A subsequent extractive evidence checker correctly answered 12/20 supported questions and declined all 20 unsupported ones. Removing explicit absence clues reduced supported answers to 5/20. That checker also failed its usefulness gate. Exact quotation and source freshness can be checked mechanically; whether the passage supports an arbitrary answer is a separate problem.

These are historical, small, hand-authored synthetic experiments. They were not independently adjudicated, are not population estimates, and have not been rerun for this public snapshot. The old model environments, copied source snapshots and full experiment working directories are not bundled. These figures should not be presented as a benchmark for a live assistant or as the result of running the quickstart.

## What is reproducible here

The public tests retain the synthetic lexical recall fixture and its actual provider search path:

```sh
uv run --project components/gremlin-town --frozen pytest components/gremlin-town/tests/benchmarks/test_recall.py -q
```

The backend and storage suites also inject authorization, concurrency, stale-source and ambiguous-write conditions. The headless example demonstrates one recoverable handoff. See [validation](validation.md) for current results.

The next research gate is not merely a higher retrieval score. It is useful supported answers, correct abstention when evidence is missing, source/owner validity and measured behavior across repeated runs.
