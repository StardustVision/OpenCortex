---
title: Clean Active MCP Naming From Context Lifecycle Code
created: 2026-04-28
status: active
type: cleanup
origin: "$compound-engineering:lfg 清理 active code/test/benchmark 里的 MCP 命名残留：把 mcp-path / ingest_method=\"mcp\" / MCP-style add_message 等当前代码术语改成 context/http lifecycle 语义，保留必要的 deprecated alias，并明确 insights 历史字段 uses_mcp 不动"
---

# Clean Active MCP Naming From Context Lifecycle Code

## Problem Frame

The in-repo MCP package and current MCP entrypoints have been removed. The
active benchmark and test code still uses `mcp` as the name for the
prepare/commit/end context lifecycle path. That makes future maintenance
ambiguous: readers can mistake the old MCP transport layer for a still-current
runtime boundary.

This phase renames active code/test/benchmark terminology to `context`,
`context_lifecycle`, or `HTTP context lifecycle` semantics while preserving
backward compatibility for existing benchmark commands that still pass
`--ingest-method mcp`.

## Requirements

- R1: Rename active benchmark ingest terminology from `mcp` to
  `context_lifecycle` where it describes `/api/v1/context`
  prepare/commit/end behavior.
- R2: Preserve a deprecated `mcp` ingest-method alias so existing benchmark
  scripts and historical runbooks do not break abruptly.
- R3: Update tests and helper names/comments from `mcp-path` /
  `MCP-style add_message` to context lifecycle terminology.
- R4: Keep historical insights fields (`uses_mcp`, `sessions_using_mcp`, and
  tests that detect `mcp__*` tool names) unchanged because they describe
  captured historical tool usage, not current repository entrypoints.
- R5: Leave deprecated benchmark files and archival docs alone unless a comment
  is part of currently exercised code.

## Scope Boundaries

- Do not reintroduce an MCP package, MCP server, or MCP publish path.
- Do not change storage, retrieval, scoring, or context lifecycle behavior.
- Do not rename public insight schema fields.
- Do not bulk-edit historical `docs/` or deprecated benchmark files.
- Do not remove benchmark compatibility with `ingest_method="mcp"` in this
  phase; normalize it to the new canonical method instead.

## Current Code Evidence

- `benchmarks/unified_eval.py` exposes `--ingest-method mcp`.
- `benchmarks/adapters/conversation.py` and `benchmarks/adapters/locomo.py`
  branch on `ingest_method == "mcp"` for context lifecycle ingestion.
- `benchmarks/oc_client.py` docstrings call `context_recall`,
  `context_commit`, and `context_end` MCP operations even though they post to
  `/api/v1/context`.
- `benchmarks/adapters/conversation_mapping.py` and
  `tests/test_conversation_mapping.py` use `mcp-path` names for snapshot-based
  context lifecycle mapping.
- `tests/test_noise_reduction.py` describes the E2E flow as
  `MCP-style add_message`.
- `src/opencortex/insights/*` still has `uses_mcp` fields, which are explicitly
  out of scope.

## Key Technical Decisions

- Use `context_lifecycle` as the canonical benchmark ingest method name because
  it describes the actual prepare/commit/end API semantics.
- Treat `mcp` as a deprecated alias normalized at benchmark option boundaries
  and adapter entrypoints.
- Keep result metadata stable enough for old consumers by accepting both names;
  newly emitted metadata should prefer the canonical name.
- Keep helper function names test-local when possible; avoid broad API churn in
  benchmark helper modules unless the helper is public and the old name would
  continue to advertise MCP as current behavior.

## Implementation Units

### U1. Benchmark Method Normalization

**Goal:** Introduce canonical context lifecycle naming while preserving the
deprecated `mcp` alias.

**Files:**
- `benchmarks/unified_eval.py`
- `benchmarks/adapters/conversation.py`
- `benchmarks/adapters/locomo.py`

**Approach:**
- Add `context_lifecycle` to accepted `--ingest-method` choices.
- Keep `mcp` in choices as a deprecated alias.
- Normalize `mcp` to `context_lifecycle` before branching in adapters.
- Update comments/help text to say context lifecycle paths stay serial because
  they mutate live session buffers.

**Test Scenarios:**
- Parser accepts `--ingest-method context_lifecycle`.
- Parser still accepts `--ingest-method mcp`.
- LongMemEval and LoCoMo ingest tests cover the alias and canonical behavior.

### U2. Active Benchmark Client Docstrings

**Goal:** Make benchmark HTTP client helper docs match the current API.

**Files:**
- `benchmarks/oc_client.py`

**Approach:**
- Replace `MCP recall/commit/end` docstrings with
  `Context lifecycle prepare/commit/end`.
- Avoid behavior changes.

**Test Scenarios:**
- Existing benchmark tests still pass.

### U3. Test and Mapping Terminology

**Goal:** Remove active `mcp-path` terminology from currently exercised tests
and helper comments.

**Files:**
- `benchmarks/adapters/conversation_mapping.py`
- `tests/test_conversation_mapping.py`
- `tests/test_noise_reduction.py`
- `tests/test_locomo_bench.py`

**Approach:**
- Rename test helper methods/classes from `mcp` to `context_lifecycle` where
  local to tests.
- Update comments and docstrings.
- Update tests that pass `ingest_method="mcp"` to exercise
  `context_lifecycle`, and add/keep a focused assertion for the deprecated
  alias if low-risk.

**Test Scenarios:**
- `uv run --group dev pytest tests/test_conversation_mapping.py tests/test_noise_reduction.py tests/test_locomo_bench.py -q`
- A targeted grep over active code shows no current-entrypoint MCP naming except
  explicit legacy alias/insights history fields.

### U4. Verification, Review, Browser Gate, and PR

**Goal:** Complete LFG with focused validation and durable PR.

**Validation Commands:**
- `uv run --group dev ruff format --check .`
- `uv run --group dev ruff check .`
- `uv run --group dev pytest tests/test_conversation_mapping.py tests/test_noise_reduction.py tests/test_locomo_bench.py -q`
- `uv run --group dev pytest tests/test_http_server.py tests/test_live_servers.py -q`

## Risks

| Risk | Mitigation |
|------|------------|
| Existing benchmark scripts pass `mcp` | Keep and test deprecated alias |
| Result metadata churn breaks report comparisons | Normalize at input while keeping behavior unchanged; prefer canonical naming only for new option values |
| Over-editing historical references | Limit grep cleanup to active code/tests/benchmark files |
| Confusing insights fields with current entrypoints | Explicitly leave `uses_mcp` and `sessions_using_mcp` untouched |

## Observed Results

- Added canonical `context_lifecycle` benchmark ingest naming.
- Preserved deprecated `mcp` ingest method alias at CLI/options and adapter
  boundaries.
- Updated active benchmark/test/client comments, docstrings, and local helper
  names from `mcp-path` / `MCP-style` terminology to context lifecycle
  terminology.
- Left insights historical fields and deprecated benchmark files untouched.
- Validation passed:
  - `uv run --group dev ruff format --check .`
  - `uv run --group dev ruff check .`
  - `uv run --group dev pytest tests/test_conversation_mapping.py tests/test_noise_reduction.py tests/test_locomo_bench.py -q`
  - `uv run --group dev pytest tests/test_http_server.py tests/test_live_servers.py -q`
