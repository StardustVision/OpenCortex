---
title: Remove MCP Remnants From OpenCortex
created: 2026-04-28
status: active
type: cleanup
origin: "$compound-engineering:lfg 清理 OpenCortex 仓库内已移除的 MCP 残留：移除 plugins/opencortex-memory submodule、MCP publish workflow、MCP scripts/tests，并更新 README/README_CN 中的入口说明为 HTTP/API 当前边界"
---

# Remove MCP Remnants From OpenCortex

## Problem Frame

The in-repo MCP server has already been removed: `src/opencortex/mcp_server.py`
does not exist on `master`. The repository still advertises and wires MCP as a
current entrypoint through a submodule, publish workflow, scripts, tests,
console script, package main module, and README content. This creates a false
boundary for future refactors and test planning.

This phase removes current-entrypoint MCP remnants and rewrites the public
README path around the supported HTTP/API server boundary.

## Requirements

- R1: Remove the `plugins/opencortex-memory` submodule gitlink and `.gitmodules`
  entry.
- R2: Remove the MCP package publish workflow.
- R3: Remove stale MCP helper scripts and tests from this repository:
  - `.github/workflows/publish-opencortex-memory.yml`
  - `scripts/mcp-call.py`
  - `scripts/start-mcp.sh`
  - `tests/test_mcp_qdrant.py`
- R4: Remove broken Python package MCP entrypoints:
  - `opencortex-mcp` script in `pyproject.toml`
  - `src/opencortex/__main__.py` import of nonexistent `opencortex.mcp_server`
  - `fastmcp` runtime dependency if no current code still imports it
- R5: Preserve legacy server-config migration behavior while removing current
  MCP naming from the config migration helper.
- R6: Update `README.md` and `README_CN.md` so the current integration story is
  HTTP/API-first, not MCP-package-first.
- R7: Do not bulk-edit historical design docs, old benchmark output JSON, or
  archival plans unless they are current entrypoint instructions.

## Scope Boundaries

- Do not modify the standalone `StardustVision/OpenCortex-Memory` repository.
- Do not delete historical docs that mention MCP as past design context.
- Do not remove insight fields such as `uses_mcp` or `sessions_using_mcp`; they
  describe captured tool usage and remain meaningful for historical traces.
- Do not change memory storage, retrieval, session lifecycle, or HTTP route
  behavior.
- Do not touch frontend behavior.

## Current Code Evidence

- `src/opencortex/mcp_server.py` is absent.
- `pyproject.toml` still exposes `opencortex-mcp =
  "opencortex.mcp_server:main"` and includes `fastmcp>=3.0`.
- `src/opencortex/__main__.py` imports `opencortex.mcp_server`.
- `.gitmodules` still defines `plugins/opencortex-memory`.
- `plugins/opencortex-memory` is still a gitlink submodule.
- `README.md` and `README_CN.md` still describe the MCP package as the main
  client integration path.
- `tests/test_mcp_qdrant.py` imports `opencortex.mcp_server`, so it is stale
  under the current backend boundary.

## Key Technical Decisions

- Treat HTTP/FastAPI as the current public integration surface.
- Make `python -m opencortex` point to the HTTP server CLI rather than a removed
  MCP module.
- Keep legacy config migration tolerant of old client-only config keys, but
  avoid presenting those fields as active MCP config.
- Use `git rm` for tracked files and submodule gitlinks so git metadata is
  updated cleanly.
- Regenerate `uv.lock` after removing `fastmcp` from direct dependencies.

## Implementation Units

### U1. Remove MCP Repository Artifacts

**Goal:** Remove tracked MCP package and CI artifacts from this repository.

**Files:**
- Modify/delete: `.gitmodules`
- Delete: `.github/workflows/publish-opencortex-memory.yml`
- Delete: `plugins/opencortex-memory`

**Approach:**
- Use `git rm` on the workflow and submodule gitlink.
- Remove `.gitmodules` if it becomes empty.
- Verify `git ls-files` no longer reports the submodule or workflow.

**Test Scenarios:**
- `git status --short` shows the submodule gitlink removed.
- `git ls-tree HEAD plugins/opencortex-memory` no longer applies after commit.

### U2. Remove Stale MCP Runtime/Test Entrypoints

**Goal:** Remove Python entrypoints and tests that import nonexistent
`opencortex.mcp_server`.

**Files:**
- Delete: `scripts/mcp-call.py`
- Delete: `scripts/start-mcp.sh`
- Delete: `tests/test_mcp_qdrant.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `src/opencortex/__main__.py`
- Modify: `src/opencortex/config.py`
- Modify: `src/opencortex/http/models.py`
- Modify: `src/opencortex/http/server.py`
- Modify: `tests/test_http_server.py`

**Approach:**
- Remove `opencortex-mcp` from `[project.scripts]`.
- Remove direct `fastmcp` dependency if no remaining current code imports it.
- Update `src/opencortex/__main__.py` to delegate to
  `opencortex.http.__main__.main`.
- Rename config migration helper terminology from MCP-only to legacy
  client-only while preserving exclusion of old keys.
- Rewrite stale docstrings/comments that refer to `mcp_server.py` or MCP server
  parity in current HTTP code/tests.
- Run lockfile update.

**Test Scenarios:**
- `rg "opencortex.mcp_server|opencortex-mcp|fastmcp"` over current code returns
  no active runtime/test references.
- `uv run opencortex-server --help` still works.
- `uv run python -m opencortex --help` resolves the HTTP CLI.

### U3. Rewrite README Current Entrypoint Docs

**Goal:** Keep README and README_CN aligned with the current HTTP/API boundary.

**Files:**
- Modify: `README.md`
- Modify: `README_CN.md`

**Approach:**
- Replace MCP package architecture text with direct HTTP/API and optional web
  console language.
- Replace MCP client setup commands with HTTP server startup and token/client
  usage instructions.
- Remove references to `plugins/opencortex-memory` and its Node.js tests.
- Keep English and Chinese sections structurally aligned.

**Test Scenarios:**
- `rg "plugins/opencortex-memory|opencortex-memory|claude mcp|codex mcp|gemini mcp"`
  returns no README hits.
- README startup commands still use `uv run opencortex-server`.

### U4. Verification, Review, Browser Gate, and PR

**Goal:** Complete the LFG pipeline with focused validation.

**Validation Commands:**
- `uv run --group dev pytest tests/test_http_server.py tests/test_live_servers.py -q`
- `uv run --group dev pytest tests/test_memory_service.py tests/test_e2e_phase1.py -q`
- `uv run --group dev pytest tests/test_object_rerank.py tests/test_object_cone.py tests/test_retrieval_candidate_service.py -q`
- `uv run opencortex-server --help`
- `uv run python -m opencortex --help`
- `uv run --group dev ruff check .`
- `uv run --group dev ruff format --check .`

## Risks

| Risk | Mitigation |
|------|------------|
| Removing submodule leaves stale git metadata | Use `git rm` and verify `.gitmodules` / gitlink state |
| Lockfile still contains unused direct dependency | Run lock update after editing `pyproject.toml` |
| README loses integration guidance | Replace MCP setup with concrete HTTP/API startup and client guidance |
| Historical docs get over-edited | Limit changes to current entrypoint docs/code/tests |
| Package `python -m opencortex` breaks differently | Repoint it to the supported HTTP server CLI and verify `--help` |

## Observed Results

- Removed the `plugins/opencortex-memory` gitlink and `.gitmodules`.
- Removed the MCP package publish workflow plus stale MCP helper scripts/tests.
- Removed the broken `opencortex-mcp` script and direct `fastmcp` dependency.
- Repointed `python -m opencortex` to the supported HTTP server CLI.
- Updated README, README_CN, AGENTS, CLAUDE, and llms.txt current-entrypoint
  guidance to describe HTTP/API as the active boundary.
- Preserved legacy config migration filtering for old client-only keys without
  advertising MCP as current config.
- Validation passed:
  - `uv run --group dev ruff format --check .`
  - `uv run --group dev ruff check .`
  - `uv run opencortex-server --help`
  - `uv run python -m opencortex --help`
  - `uv run --group dev pytest tests/test_http_server.py tests/test_live_servers.py -q`
  - `uv run --group dev pytest tests/test_memory_service.py tests/test_e2e_phase1.py -q`
  - `uv run --group dev pytest tests/test_object_rerank.py tests/test_object_cone.py tests/test_retrieval_candidate_service.py -q`
