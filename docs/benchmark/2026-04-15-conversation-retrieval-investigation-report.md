# 2026-04-15 Conversation Retrieval Investigation Report

## Scope

This report covers the recent investigation into the conversation-memory ingest and retrieval path, with LoCoMo benchmark ingestion as the primary failing workload. The focus was not generic retrieval quality, but concrete write, merge, list, and recall behavior in the current `probe -> planner -> executor` main path.

## Executive Summary

Current evidence supports four conclusions:

1. Conversation merge lifecycle had a real correctness bug and is now fixed.
2. Cross-session recall had a real scope-filter bug and is now fixed.
3. Local embedded Qdrant `filter(order_by=...)` had a real pagination/sorting bug and is now fixed.
4. One high-confidence issue remains: merged conversation records can still end up with an empty `abstract`, which causes `memory_list` and admin list paths to hide them even though `memory_search` and `context_recall` can still retrieve them.

This remaining issue explains the observed split:

- `context_commit` after ingest can see immediate records.
- `context_end` produces merged records that are searchable.
- `memory_list` returns `0` after merge for some sessions.
- LoCoMo ground-truth URI mapping becomes empty because benchmark ingest currently depends on `memory_list`.

## Symptom Reproduction

Real HTTP verification on a temporary service showed the following sequence:

- after `commit`: `memory_list == 2`
- after `end`: `memory_list == 0`
- after `end`: `memory_search == 1`
- after `end`: `context_recall == 1`, `probe_candidate_count == 1`

This narrows the remaining fault domain to list materialization, not merge durability and not probe retrieval.

## Confirmed Fixes

### 1. Conversation merge lifecycle

The previous flow could launch floating merge tasks and race against session shutdown. The current code now serializes merge work per session and restores snapshots on failure.

- background merge worker spawning: `src/opencortex/context/manager.py:929-958`
- snapshot detach before merge: `src/opencortex/context/manager.py:967-993`
- snapshot restore on failure: `src/opencortex/context/manager.py:995-1017`
- merged record write and immediate cleanup: `src/opencortex/context/manager.py:1019-1089`
- `end` waits for in-flight merge and forces final flush: `src/opencortex/context/manager.py:1095-1124`
- end-of-session immediate cleanup and `session_end`: `src/opencortex/context/manager.py:1126-1163`

Regression coverage was added here:

- cross-session recall regression: `tests/test_context_manager.py:135-161`
- immediate replaced by merged record: `tests/test_context_manager.py:163-239`
- `end` waits for background merge: `tests/test_context_manager.py:346-412`
- failed background merge is restored then flushed on `end`: `tests/test_context_manager.py:414-460`

### 2. Cross-session recall scope bug

Conversation memories were being filtered too aggressively when the query session differed from the ingest session. In benchmark workloads this is normal, so recall was incorrectly dropping valid history.

- probe entry: `src/opencortex/orchestrator.py:1962-1984`
- shared search filter: `src/opencortex/orchestrator.py:2103-2167`
- scope filter builder: `src/opencortex/orchestrator.py:2169-2195`

The relevant change is that `_build_scope_filter()` no longer hard-binds retrieval to the current query `session_id`. That is why `prepare` can now recall memory written under a different session, as validated by `tests/test_context_manager.py:135-161`.

### 3. Local Qdrant list ordering bug

The embedded Qdrant path could return empty results when `filter()` used `order_by`. That directly affected `memory_list`, because list paths call storage `filter(..., order_by="updated_at")`.

- storage adapter `filter()`: `src/opencortex/storage/qdrant/adapter.py:558-602`
- user list path: `src/opencortex/orchestrator.py:3349-3383`
- admin list path: `src/opencortex/orchestrator.py:3497-3521`

The adapter now performs full scroll plus Python-side sort for `order_by`, avoiding the local backend behavior gap.

Regression coverage:

- `tests/test_qdrant_adapter.py:402-419`

## Remaining High-Confidence Root Cause

### Merged records may have empty `abstract`

The strongest remaining explanation is that merged records are written successfully, but sometimes with `abstract=""`.

Evidence chain:

1. Conversation merge explicitly calls `orchestrator.add()` with an empty abstract.
   - `src/opencortex/context/manager.py:1050-1067`

2. Both list paths explicitly skip records whose `abstract` is empty.
   - `src/opencortex/orchestrator.py:3359-3361`
   - `src/opencortex/orchestrator.py:3506-3520`

3. `_derive_layers()` can return empty strings when LLM derivation is configured but yields an empty result payload.
   - chunked branch return: `src/opencortex/orchestrator.py:1196-1223`
   - single-shot branch return: `src/opencortex/orchestrator.py:1228-1250`
   - fallback only activates after exception or when no LLM exists: `src/opencortex/orchestrator.py:1254-1263`

That means the current behavior is:

- merge writes `abstract=""`
- derivation tries to fill it
- if the LLM path returns a structurally valid but empty payload, no fallback is triggered
- record becomes searchable by content/vector path, but hidden from list APIs

This exactly matches the observed runtime behavior.

## Benchmark Impact

LoCoMo ingest currently builds ground-truth mappings from `memory_list`, not from direct store introspection.

- snapshot baseline via `memory_list`: `benchmarks/adapters/locomo.py:227-248`
- ingest performs `context_commit` / `context_end`: `benchmarks/adapters/locomo.py:304-397`
- new merged records are detected by diffing `before_records` and `after_records`: `benchmarks/adapters/locomo.py:325-386`
- session-to-URI mapping depends on those diffed records: `benchmarks/adapters/locomo.py:381-388`

If merged records are hidden from `memory_list`, then `new_records` becomes empty, `session_uris_by_key` becomes sparse, and LoCoMo retrieval scoring is artificially depressed even when actual search/recall can hit the record.

## Validation Status

Validated and passing:

- `uv run --group dev pytest tests/test_context_manager.py tests/test_qdrant_adapter.py -q`
- result: `43 passed`

Validated on real HTTP flow:

- merged records persist
- merged records are searchable
- merged records are recallable
- list exposure is still inconsistent after merge

## Recommended Next Fix

The next fix should be narrow and non-benchmark-specific:

1. Change `_derive_layers()` so an empty LLM-derived `abstract` falls back to `user_abstract or content`.
2. Add a regression test covering `LLM configured + empty derive result + non-empty final abstract`.
3. Add an end-to-end conversation test asserting `commit -> end -> memory_list` returns the merged record.
4. Rerun LoCoMo ingest and regenerate retrieval reports only after the list path is confirmed healthy.

## Bottom Line

This is no longer a broad “retrieval quality is bad” diagnosis. Three concrete bugs have already been fixed. The remaining blocker is much narrower: merged conversation records are likely being hidden by an empty-abstract path, and that specifically corrupts benchmark mapping built on top of `memory_list`.
