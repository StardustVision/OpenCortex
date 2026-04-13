# README and Architecture Docs Restructuring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the root READMEs into concise entry documents and move deep explanations of three-layer storage, cone retrieval, Autophagy, and Skill Engine into focused architecture docs under `docs/architecture/`.

**Architecture:** Keep `README.md` and `README_CN.md` as onboarding and navigation surfaces, not deep design documents. Add four subsystem-focused markdown documents in `docs/architecture/`, then replace detailed endpoint inventories and directory trees in the READMEs with grouped summaries plus deep-dive links.

**Tech Stack:** Markdown documentation, existing repository docs conventions, git

---

## File Structure

- Create: `docs/architecture/three-layer-storage.md`
- Create: `docs/architecture/cone-retrieval.md`
- Create: `docs/architecture/autophagy.md`
- Create: `docs/architecture/skill-engine.md`
- Modify: `README.md`
- Modify: `README_CN.md`

Each deep-dive file owns one subsystem:

- `three-layer-storage.md`: CortexFS, L0/L1/L2, dual-write, ingest implications, on-demand `L2`
- `cone-retrieval.md`: entity index, candidate expansion, bonus/penalty logic, graceful degradation
- `autophagy.md`: recall planning, `memory_context`, lifecycle orchestration, knowledge recall relationship
- `skill-engine.md`: extraction/evolution/approval model, quality gates, sandboxing, current maturity

The READMEs own:

- landing-page explanation
- quick start
- minimal API grouping
- minimal repository grouping
- links to the four deep-dive docs

---

### Task 1: Create the Three-Layer Storage Deep Dive

**Files:**
- Create: `docs/architecture/three-layer-storage.md`

- [ ] **Step 1: Create the document scaffold**

```md
# Three-Layer Storage

## Why This Exists

## Core Model

## Write Path

## Read Path

## Relationship Between CortexFS and Qdrant

## Document and Conversation Implications

## Constraints and Tradeoffs

## Current State
```

- [ ] **Step 2: Fill the core model and write path sections**

```md
## Core Model

OpenCortex stores each record at three levels:

- `L0`: abstract for cheap indexing and confirmation
- `L1`: overview for default retrieval payloads
- `L2`: original content for drill-down and audits

These layers are persisted in CortexFS while Qdrant stores vectors plus the payload needed for retrieval-time ranking.

## Write Path

Normal writes go through `MemoryOrchestrator.add()`, which derives or accepts the layered content, writes the filesystem representation, and stores the vector-backed payload in Qdrant. Large documents and conversation streams reuse the same storage model but reach it through different ingestion paths.
```

- [ ] **Step 3: Fill the read path, dual-write, and ingestion implications**

```md
## Read Path

Most retrieval returns `L0` or `L1`. `L2` is loaded only when the caller explicitly requests deeper detail or reads content directly.

## Relationship Between CortexFS and Qdrant

Qdrant is the fast retrieval surface. CortexFS is the canonical layered content surface. The system writes to both so search can stay fast without losing the richer filesystem representation.

## Document and Conversation Implications

Document mode turns source files into hierarchical chunks before storage. Conversation mode writes immediate records first so new turns are searchable right away, then merges them into richer chunks later.
```

- [ ] **Step 4: Fill constraints, current state, and save**

```md
## Constraints and Tradeoffs

- Dual-write increases complexity but keeps retrieval latency low.
- `L2` is deliberately not the default because it is expensive in both token cost and payload size.
- Different ingestion modes share one storage model, which keeps retrieval uniform at the cost of more write-path complexity.

## Current State

Three-layer storage is a core subsystem and already underpins memory, document, and conversation ingestion. The main evolution pressure is around retrieval quality and summarization policy, not around replacing the layered model itself.
```

- [ ] **Step 5: Verify headings exist**

Run: `rg -n "^## " docs/architecture/three-layer-storage.md`
Expected: eight second-level headings matching the planned sections

- [ ] **Step 6: Commit**

```bash
git add docs/architecture/three-layer-storage.md
git commit -m "docs: add three-layer storage deep dive"
```

### Task 2: Create the Cone Retrieval Deep Dive

**Files:**
- Create: `docs/architecture/cone-retrieval.md`

- [ ] **Step 1: Create the document scaffold**

```md
# Cone Retrieval

## Why This Exists

## Core Components

## Expansion Flow

## Scoring Model

## Failure and Degradation Behavior

## Relationship to Normal Search

## Constraints and Tradeoffs

## Current State
```

- [ ] **Step 2: Fill the purpose, core components, and expansion flow**

```md
## Why This Exists

Pure semantic similarity misses cases where related records should travel together because they share important entities. Cone retrieval adds a neighborhood-expansion layer around those shared entities.

## Core Components

- `EntityIndex`
- cone scorer
- query entity extraction
- candidate expansion against the active collection

## Expansion Flow

The retriever first gathers ordinary candidates. Cone retrieval then extracts query entities, looks for nearby records that share those entities, and expands the candidate pool before final ordering.
```

- [ ] **Step 3: Fill scoring, degradation, and search integration**

```md
## Scoring Model

Cone retrieval adds a bonus for useful entity-neighbor hits and applies penalties to avoid letting graph expansion overwhelm direct matches.

## Failure and Degradation Behavior

If entity extraction, index building, or expansion fails, the retriever falls back to ordinary search. Cone retrieval is additive, not required for correctness.

## Relationship to Normal Search

Cone retrieval sits inside the retrieval stack after ordinary candidate gathering and before final ranking settles. It should be explained as a search enhancer, not a separate retrieval mode.
```

- [ ] **Step 4: Fill tradeoffs, current state, and save**

```md
## Constraints and Tradeoffs

- Better recall for entity-linked material
- More moving parts than plain retrieval
- Sensitive to entity quality and collection readiness

## Current State

Cone retrieval is an active retrieval feature, not just a design idea. It should be documented as current behavior, while also calling out that it gracefully disables itself when prerequisites are not ready.
```

- [ ] **Step 5: Verify headings exist**

Run: `rg -n "^## " docs/architecture/cone-retrieval.md`
Expected: eight second-level headings matching the planned sections

- [ ] **Step 6: Commit**

```bash
git add docs/architecture/cone-retrieval.md
git commit -m "docs: add cone retrieval deep dive"
```

### Task 3: Create the Autophagy Deep Dive

**Files:**
- Create: `docs/architecture/autophagy.md`

- [ ] **Step 1: Create the document scaffold**

```md
# Autophagy

## Why This Exists

## Core Components

## Prepare Flow

## Commit Flow

## End Flow

## Relationship to Search and Knowledge Recall

## Constraints and Tradeoffs

## Current State
```

- [ ] **Step 2: Fill the subsystem framing and core components**

```md
## Why This Exists

Autophagy is the lifecycle layer that turns memory from a passive search API into an active recall-and-record loop for agent sessions.

## Core Components

- `ContextManager`
- `IntentRouter`
- `RecallPlanner`
- `memory_context` prepare / commit / end
- observer and downstream trace-processing components
```

- [ ] **Step 3: Fill prepare, commit, and end flows**

```md
## Prepare Flow

`prepare` extracts the latest user query, builds an explicit recall plan, runs memory search and optional knowledge search in parallel, then returns memory, knowledge, and instructions.

## Commit Flow

`commit` records the turn, keeps conversation buffers moving, and applies asynchronous reward updates for cited URIs.

## End Flow

`end` flushes session state and, when Alpha is enabled, pushes the transcript into trace splitting, archival, and knowledge workflows.
```

- [ ] **Step 4: Fill search relationship, tradeoffs, and current state**

```md
## Relationship to Search and Knowledge Recall

Autophagy is not just another search wrapper. It coordinates recall planning, lifecycle state, observer integration, and optional knowledge recall in a single protocol.

## Constraints and Tradeoffs

- Adds statefulness to what could otherwise be a stateless search API
- Improves recall quality and lifecycle observability
- Requires more careful handling of caching, idempotency, and session cleanup

## Current State

Autophagy-level lifecycle behavior is implemented through the unified context endpoint and should be documented as current system behavior, not future design intent.
```

- [ ] **Step 5: Verify headings exist**

Run: `rg -n "^## " docs/architecture/autophagy.md`
Expected: eight second-level headings matching the planned sections

- [ ] **Step 6: Commit**

```bash
git add docs/architecture/autophagy.md
git commit -m "docs: add autophagy deep dive"
```

### Task 4: Create the Skill Engine Deep Dive

**Files:**
- Create: `docs/architecture/skill-engine.md`

- [ ] **Step 1: Create the document scaffold**

```md
# Skill Engine

## Why This Exists

## Core Components

## Extraction and Evolution Flow

## Validation and Approval Flow

## Retrieval and API Exposure

## Constraints and Tradeoffs

## Current State

## Open Boundaries
```

- [ ] **Step 2: Fill the purpose, components, and extraction flow**

```md
## Why This Exists

Some reusable procedures are richer than ordinary memories. The Skill Engine exists to extract, validate, evolve, and serve those procedures as skills.

## Core Components

- `SkillManager`
- skill store
- analyzer
- evolver
- quality gate
- sandbox TDD
- event store and evaluator

## Extraction and Evolution Flow

Candidate skills are extracted from memory-like source material, evolved into structured records, and saved as candidate skills when they pass the initial pipeline.
```

- [ ] **Step 3: Fill validation, retrieval, and API exposure**

```md
## Validation and Approval Flow

Candidate skills pass through a quality gate and may also run through sandbox TDD when configured. Approved skills become active; rejected or deprecated skills remain visible through their lifecycle state.

## Retrieval and API Exposure

Active skills can be searched directly through the Skill Engine routes and can also be merged into normal retrieval results as skill hits.
```

- [ ] **Step 4: Fill tradeoffs, current state, and open boundaries**

```md
## Constraints and Tradeoffs

- Stronger structure than plain memory
- More lifecycle management overhead
- Depends on LLM-backed analysis for richer extraction paths

## Current State

The Skill Engine is present and wired into the backend, but it should be documented as an evolving subsystem with some features more mature than others.

## Open Boundaries

The document should call out where Skill Engine behavior is already part of the product surface and where it is still subject to change.
```

- [ ] **Step 5: Verify headings exist**

Run: `rg -n "^## " docs/architecture/skill-engine.md`
Expected: eight second-level headings matching the planned sections

- [ ] **Step 6: Commit**

```bash
git add docs/architecture/skill-engine.md
git commit -m "docs: add skill engine deep dive"
```

### Task 5: Slim the Root READMEs and Link the Deep Dives

**Files:**
- Modify: `README.md`
- Modify: `README_CN.md`

- [ ] **Step 1: Replace detailed API tables with grouped summaries**

```md
## API Reference

OpenCortex exposes a unified HTTP API grouped into these areas:

- Memory
- Context and session lifecycle
- Content and observability
- Knowledge, insights, and skills
- Auth and admin

Use the deep-dive docs and source modules for implementation-level details.
```

```md
## API 参考

OpenCortex 的 HTTP API 可按以下领域理解：

- Memory
- Context 与 session lifecycle
- Content 与 observability
- Knowledge、Insights 与 Skills
- Auth 与 admin

实现级细节请查看专题文档和源码模块。
```

- [ ] **Step 2: Replace the detailed repository tree with subsystem summaries**

```md
## Repository Layout

- `src/opencortex/`: backend subsystems
- `web/`: React management console
- `plugins/opencortex-memory/`: MCP package submodule
- `tests/`: automated test suite
- `docs/`: architecture notes and benchmark material
```

```md
## 项目结构

- `src/opencortex/`：后端核心子系统
- `web/`：React 管理台
- `plugins/opencortex-memory/`：MCP 包子模块
- `tests/`：自动化测试
- `docs/`：架构说明与 benchmark 文档
```

- [ ] **Step 3: Add a deep-dive section linking to the four new docs**

```md
## Deep Dives

- [Three-Layer Storage](docs/architecture/three-layer-storage.md)
- [Cone Retrieval](docs/architecture/cone-retrieval.md)
- [Autophagy](docs/architecture/autophagy.md)
- [Skill Engine](docs/architecture/skill-engine.md)
```

```md
## 深入阅读

- [Three-Layer Storage](docs/architecture/three-layer-storage.md)
- [Cone Retrieval](docs/architecture/cone-retrieval.md)
- [Autophagy](docs/architecture/autophagy.md)
- [Skill Engine](docs/architecture/skill-engine.md)
```

- [ ] **Step 4: Remove low-level material that duplicates the deep dives**

```md
Delete or compress:

- endpoint-by-endpoint route inventories
- MCP tool count inventories
- detailed directory trees
- long subsystem mechanics for storage, cone retrieval, Autophagy, and Skill Engine
```

- [ ] **Step 5: Verify the READMEs are slimmer and linked correctly**

Run: `rg -n "Deep Dives|深入阅读|three-layer-storage|cone-retrieval|autophagy|skill-engine" README.md README_CN.md`
Expected: both READMEs contain the deep-dive section and all four links

- [ ] **Step 6: Commit**

```bash
git add README.md README_CN.md
git commit -m "docs: slim root readmes and link architecture deep dives"
```

### Task 6: Final Documentation Verification

**Files:**
- Modify: `README.md`
- Modify: `README_CN.md`
- Modify: `docs/architecture/three-layer-storage.md`
- Modify: `docs/architecture/cone-retrieval.md`
- Modify: `docs/architecture/autophagy.md`
- Modify: `docs/architecture/skill-engine.md`

- [ ] **Step 1: Verify the new architecture directory contents**

Run: `find docs/architecture -maxdepth 1 -type f | sort`
Expected:

```text
docs/architecture/autophagy.md
docs/architecture/cone-retrieval.md
docs/architecture/skill-engine.md
docs/architecture/three-layer-storage.md
```

- [ ] **Step 2: Verify README no longer contains detailed route inventories**

Run: `rg -n "/api/v1/" README.md README_CN.md`
Expected: no endpoint-by-endpoint route catalog remains, or only a minimal mention if one central endpoint is intentionally kept

- [ ] **Step 3: Review the documentation diff**

Run: `git diff -- README.md README_CN.md docs/architecture`
Expected: README becomes shorter and more navigational; the deep mechanics move into the four new docs

- [ ] **Step 4: Commit the final verification pass**

```bash
git add README.md README_CN.md docs/architecture
git commit -m "docs: finalize architecture doc restructure"
```

---

## Self-Review

### Spec coverage

- README slimming: covered by Task 5
- API cleanup: covered by Task 5 Step 1 and Task 6 Step 2
- repository layout cleanup: covered by Task 5 Step 2
- deep-dive docs: covered by Tasks 1 through 4
- deep-dive links in README: covered by Task 5 Step 3

No spec gaps found.

### Completeness scan

No deferred or incomplete markers remain in the plan steps.

### Consistency check

- Deep-dive paths are consistent across all tasks:
  - `docs/architecture/three-layer-storage.md`
  - `docs/architecture/cone-retrieval.md`
  - `docs/architecture/autophagy.md`
  - `docs/architecture/skill-engine.md`
- README restructuring consistently uses grouped API and grouped repository layout summaries.
