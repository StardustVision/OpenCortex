---
title: Keep the memory recall hot path in explicit probe/planner/runtime phases
date: 2026-04-12
category: best-practices
module: intent
problem_type: best_practice
component: assistant
severity: medium
applies_when:
  - Refactoring the memory recall hot path or its public entrypoints
  - Adding retrieval behavior that depends on evidence, budgets, cone usage, or degrade
  - Moving memory retrieval code across packages or response contracts
symptoms:
  - Recall behavior is split across multiple modules with overlapping responsibilities
  - Search or HTTP responses drift between probe evidence, planner posture, and runtime execution data
  - New retrieval features try to bypass the main path and reintroduce legacy fields
root_cause: logic_error
resolution_type: code_fix
related_components:
  - orchestrator
  - context_manager
  - http_server
  - benchmark_runner
tags:
  - memory-hot-path
  - memory-probe
  - memory-planner
  - memory-runtime
  - memory-pipeline
  - recall-architecture
---

# Keep the memory recall hot path in explicit probe/planner/runtime phases

## Context

The current memory hot path is intentionally phase-native and destructive with
respect to the old classifier-first design.

The active package surface is:

- `src/opencortex/intent/probe.py`
- `src/opencortex/intent/planner.py`
- `src/opencortex/intent/executor.py`
- `src/opencortex/intent/types.py`

The active path is now:

```text
query -> probe -> planner -> runtime -> memory_pipeline
```

Phase 1 is no longer a semantic classifier. Every normal query pays one cheap
bootstrap probe toll, then planner decides whether `l0` is sufficient or
whether the system should escalate.

## Guidance

Keep the three phases strict and explicit.

- Phase 1 probe returns only first-pass signals (URIs, anchors, starting points):
  `SearchResult(should_recall=True, candidate_entries, anchor_hits, starting_points, evidence, trace)`.
  Probe does not make scope decisions or set `scoped_miss`.
- Phase 2 planner converts `query + probe_result` into retrieval decisions:
  `RetrievalPlan(target_memory_kinds, query_plan, search_profile, retrieval_depth, scope_level, scope_filter, drill_uris, expand_anchors)`.
  Planner is the sole decision center for scope, depth, and strategy.
- Phase 3 runtime binds the plan into bounded executable facts and emits
  machine-readable execution results:
  `ExecutionResult(items, trace, degrade)`. Runtime does not re-arbitrate depth.

The correct orchestration shape is:

```python
probe_result = await probe.probe(query, scope_filter=scope_filter)

retrieve_plan = planner.semantic_plan(
    query=query,
    probe_result=probe_result,
    max_items=max_items,
    recall_mode="auto",
    detail_level_override=None,
    scope_input=scope_input,
)
if retrieve_plan is None:
    return None  # scoped miss or recall_mode=never

bound_plan = runtime.bind(
    probe_result=probe_result,
    retrieve_plan=retrieve_plan,
    max_items=max_items,
    session_id=session_id,
    tenant_id=tenant_id,
    user_id=user_id,
    project_id=project_id,
    include_knowledge=include_knowledge,
)
runtime_result = runtime.finalize(
    bound_plan=bound_plan,
    items=items,
    latency_ms=latency_ms,
)
```

Keep these boundaries strict:

- Probe must not emit semantic class labels or execution-policy decisions.
- Planner may compute priors internally, but those priors stay planner-internal.
- Runtime may degrade optional work, but it must not reinterpret semantics.
- Search and HTTP serialization should expose `memory_pipeline.probe`,
  `memory_pipeline.planner`, and `memory_pipeline.runtime`.

## Why This Matters

This split makes the hot path easier to reason about and cheaper to evolve.

- Performance work becomes local. Probe stays cheap and bounded, planner stays
  evidence-driven, runtime owns execution tradeoffs.
- Benchmark attribution becomes cleaner. A bad result can be traced to weak
  probe evidence, planner escalation, or runtime execution.
- Contracts become testable. The repo now has focused tests for probe,
  planner, runtime, context-manager serialization, and benchmark attribution.
- Legacy drift is blocked. Consumers read one `probe/planner/runtime` envelope
  instead of mixing old intent labels with new execution metadata.

This shape was revalidated during the 2026-04-13 destructive refactor with:

- `tests/test_memory_probe.py`
- `tests/test_intent_planner_phase2.py`
- `tests/test_memory_runtime.py`
- `tests/test_recall_planner.py`
- `tests/test_context_manager.py`
- `tests/test_http_server.py`
- `tests/test_benchmark_runner.py`
- `tests/test_ingestion_e2e.py`
- `tests/test_eval_contract.py`

## When to Apply

- When adding new recall behavior, decide first whether it belongs in probe,
  planner, or runtime.
- When adding execution-time controls such as timeout, degrade, or hydration,
  keep them in runtime instead of leaking them upward.
- When changing HTTP or benchmark payloads, preserve the phase-native
  `memory_pipeline` envelope rather than reviving compatibility fields.
- When moving code, keep the hot path centered under `src/opencortex/intent/`
  and keep shared domain types under `src/opencortex/memory/`.

## Examples

Example 1: phase-native DTOs

```python
MemoryProbeResult(
    should_recall=True,
    evidence={"top_score": 0.82, "score_gap": 0.11, "candidate_count": 2},
)

MemoryRetrievePlan(
    target_memory_kinds=["event", "summary"],
    query_plan={"anchors": [], "rewrite_mode": "none"},
    search_profile={"recall_budget": 0.3, "association_budget": 0.0, "rerank": False},
    retrieval_depth="l0",
)
```

Example 2: runtime trace keeps provenance explicit

```python
payload = find_result.to_dict()
pipeline = payload["memory_pipeline"]

pipeline["probe"]
pipeline["planner"]
pipeline["runtime"]["trace"]["probe"]
pipeline["runtime"]["trace"]["planner"]
pipeline["runtime"]["trace"]["effective"]
```

Do not reintroduce patterns like:

- probe returns `task_class` directly
- planner mutates request scope
- runtime decides semantic labels
- API responses expose both `memory_pipeline` and legacy `recall_plan`

## Related

- Related implementation plan:
  `docs/plans/2026-04-13-003-refactor-memory-object-aware-retrieval-plan.md`
- Historical benchmark docs from 2026-04-11 and 2026-04-12 describe pre-refactor
  runs and should be read as historical analysis, not as the current code path.
