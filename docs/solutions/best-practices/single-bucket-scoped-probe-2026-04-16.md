---
title: Keep memory probe scope single-bucket and authoritative before anchor expansion
date: 2026-04-16
category: best-practices
module: intent
problem_type: best_practice
component: assistant
severity: medium
applies_when:
  - Refactoring `src/opencortex/intent/probe.py` bucket selection or scope precedence
  - Adding anchor-guided retrieval, scoped recall, or hydration behavior to the memory hot path
  - Updating benchmark adapters or HTTP contracts that consume `memory_pipeline`
symptoms:
  - Scoped requests reopen weaker global search paths or mix roots from multiple scope levels
  - Probe traces cannot explain which scope bucket won or why a miss stayed scoped
  - Benchmark adapters and HTTP contracts drift from the retrieval behavior actually shipped
root_cause: scope_issue
resolution_type: code_fix
related_components:
  - orchestrator
  - benchmark_runner
  - context_manager
  - http_server
tags:
  - memory-probe
  - scoped-retrieval
  - root-first
  - anchor-guidance
  - hydration-arbitration
  - benchmark-contracts
---

# Keep memory probe scope single-bucket and authoritative before anchor expansion

## Context

OpenCortex's memory hot path already had the right pieces for scoped retrieval:
`probe`, `planner`, `executor`, session/document scope fields, and anchor
surfaces. What it lacked was a strict control flow.

Before the 2026-04-16 cutover, retrieval could still behave like a broad leaf
search with late correction:

- scope and anchors were both present, but not ordered strongly enough
- explicit scope could still be flattened into a merged filter instead of
  acting like an authoritative bucket choice
- downstream contracts could expose fallback-shaped fields even when the
  shipped behavior no longer widened
- benchmark adapters and HTTP payloads could drift away from the retrieval
  semantics that actually shipped

The implemented path that proved stable was:

```text
structured scope input -> choose one bucket -> retrieve anchors/objects in scope
-> scoped miss or bounded execution -> optional L1->L2 hydration arbitration
```

## Guidance

When changing the memory recall hot path, keep these rules intact.

- Build structured probe scope input in `src/opencortex/orchestrator.py`
  instead of relying only on a flattened merged filter. The important signal is
  not just the final predicate, but where scope came from:
  `target_uri`, `session_id`, `source_doc_id`, `context_type`, or global.
- Let `src/opencortex/intent/probe.py` choose exactly one active bucket before
  object and anchor retrieval. Do not union roots from multiple bucket levels
  in the same normal-path pass.
- Treat explicit scope as authoritative. If `target_uri`, `session_id`, or
  `source_doc_id` yields no usable in-scope candidates, return `scoped_miss`
  and keep the request scoped. Do not silently widen to weaker global search.
- Keep anchor guidance inside the chosen scope. Anchors are precision signals,
  not a second scope-selection system.
- Keep compatibility fields compatible but inert. Fields such as
  `fallback_ready` and runtime `trace.fallback` may remain in DTO/API surfaces
  for contract stability, but they should not accidentally reintroduce a
  widening policy that the runtime no longer uses.
- Make hydration an execution decision based on retrieved `L1` evidence, not a
  pre-committed retrieval depth escalation. In this codebase that means
  inspecting the first retrieved contexts, then only upgrading to `L2` when
  overview coverage is insufficient.
- Keep benchmark adapters and HTTP contracts aligned with the shipped path.
  If the runtime now emits selected bucket, scoped miss, hydration, and
  retrieval contract signals, adapter metadata and API tests should assert
  those exact fields rather than older fallback-oriented assumptions.

## Why This Matters

This rule set keeps the hot path explainable and prevents several subtle
regressions:

- Scoped queries stay honest. A miss under explicit scope stays a scoped miss
  instead of becoming an invisible global search success.
- Probe traces stay debuggable. Reviewers can tell which bucket won, whether it
  was authoritative, and which roots were retained.
- Planner and runtime stay phase-native. Probe chooses scope, planner shapes
  retrieval posture, runtime decides whether `L1` evidence is sufficient.
- Benchmark attribution stays trustworthy. `benchmarks/adapters/conversation.py`,
  `benchmarks/adapters/locomo.py`, and HTTP/contract tests can only validate
  the system if they consume the same semantics the runtime is actually using.
- Latency work becomes safer. `L1 -> L2` hydration only happens when the first
  pass did not already provide enough overview evidence.

## When to Apply

- When editing `src/opencortex/intent/probe.py`,
  `src/opencortex/intent/planner.py`, `src/opencortex/intent/executor.py`, or
  `src/opencortex/orchestrator.py`
- When adding new explicit scope inputs or changing scope precedence
- When introducing new anchor surfaces, rerank rules, or scoped execution
  behavior
- When changing benchmark adapters, `benchmarks/oc_client.py`, or the
  externally visible `memory_pipeline` contract

## Examples

Example 1: caller scope should remain structured until probe consumes it.

```python
scope_input = self._build_probe_scope_input(
    context_type=context_type,
    target_uri=target_uri,
    target_doc_id=target_doc_id,
    session_context=session_context,
)
return await self._memory_probe.probe(
    query,
    scope_filter=scope_filter,
    scope_input=scope_input,
)
```

This pattern in `src/opencortex/orchestrator.py` preserves whether the active
bucket came from `target_uri`, `session_id`, `source_doc_id`, or inferred
context. A merged filter alone loses that distinction.

Example 2: authoritative scope miss should stop widening immediately.

```python
if result.scope_authoritative and not result.candidate_entries and not result.anchor_hits:
    result.should_recall = False
    result.scoped_miss = True
    result.fallback_ready = False
    result.trace.scoped_miss = True
```

This behavior in `src/opencortex/intent/probe.py` is the key guardrail that
keeps explicit scope requests honest.

Example 3: runtime should arbitrate `L1` sufficiency after retrieval rather
than escalating up front.

```python
upgraded_plan, should_hydrate, actions, early_stop = runtime.arbitrate_hydration(
    bound_plan=bound_plan,
    query_results=query_results,
)
```

The implementation in `src/opencortex/intent/executor.py` checks retrieved
overview coverage before upgrading to `L2`.

Example 4: benchmark adapter metadata should describe the actual retrieval
contract, not just raw payload shape.

```python
self._set_last_retrieval_meta(
    result,
    endpoint="context_recall",
    session_scope=True,
)
```

This keeps benchmark traces aligned with the new scoped-recall path and is
locked by `tests/test_benchmark_runner.py` and `tests/test_locomo_bench.py`.

## Related

- Existing hot-path guidance:
  `docs/solutions/best-practices/memory-intent-hot-path-refactor-2026-04-12.md`
- Implementation plans:
  `docs/plans/2026-04-15-001-refactor-scoped-root-anchor-probe-plan.md`
  and `docs/plans/2026-04-14-001-refactor-memory-retrieval-openviking-alignment-plan.md`
- Key regression coverage:
  `tests/test_memory_probe.py`,
  `tests/test_recall_planner.py`,
  `tests/test_memory_runtime.py`,
  `tests/test_http_server.py`,
  `tests/test_benchmark_runner.py`,
  `tests/test_locomo_bench.py`
