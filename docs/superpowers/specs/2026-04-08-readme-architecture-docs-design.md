# README and Architecture Docs Restructuring Design

## Goal

Restructure the project documentation so that `README.md` and `README_CN.md` become concise entry documents, while deeper architecture explanations move into focused documents under `docs/`.

The immediate motivation is that the current README layer has started mixing three different concerns:

1. onboarding and quick start
2. high-level product and architecture overview
3. deep subsystem design details

That makes the main README harder to scan, easier to let drift, and more likely to contain outdated low-level details such as endpoint inventories, tool counts, or implementation-specific directory trees.

## Problems to Solve

### 1. README is becoming too implementation-heavy

The current root README now carries a large amount of architecture detail. Some of that is useful, but the detailed mechanics of:

- three-layer storage
- cone retrieval
- Autophagy-level recall / lifecycle planning
- Skill Engine

are subsystem topics, not landing-page topics.

### 2. Low-level details in README go stale quickly

Detailed API endpoint tables, MCP tool counts, and fine-grained repository trees are expensive to keep current. They also distract from the primary questions a newcomer needs answered first:

- what OpenCortex is
- what it does
- how to run it
- where to go next for deeper technical reading

### 3. Deep concepts need a stable long-form home

The requested topics deserve real explanations with:

- purpose
- architecture
- data flow
- key abstractions
- tradeoffs
- operational constraints

They should not be compressed into a few README bullets.

## Design Decision

Use a two-layer documentation structure:

### Layer 1: Root README as entrypoint

`README.md` and `README_CN.md` remain the main landing documents and keep:

- project positioning
- key capabilities
- one high-level architecture overview
- quick start
- minimal API summary by area
- minimal repository layout by subsystem
- links to deeper documents

### Layer 2: Focused architecture documents under `docs/`

Create dedicated deep-dive documents for the requested topics:

- `docs/architecture/three-layer-storage.md`
- `docs/architecture/cone-retrieval.md`
- `docs/architecture/autophagy.md`
- `docs/architecture/skill-engine.md`

Each topic gets its own file so that updates remain isolated and future additions do not force a single giant architecture monolith.

## Alternatives Considered

### Option A: Keep everything in README

This was rejected because it turns the main README into a long design document. It also keeps API and structure details too close to the landing page, where drift is most visible.

### Option B: One giant architecture document

This was rejected because the four requested topics are related but not the same. A single document would become long, harder to navigate, and more likely to mix lifecycle, storage, and retrieval concerns without clear boundaries.

### Option C: Recommended approach

Keep README concise and split deep architecture into topic-focused documents. This gives the best balance between discoverability and maintainability.

## Scope

### In scope

- rewrite `README.md` to be a concise entry document
- rewrite `README_CN.md` to mirror the new information architecture
- remove low-level API endpoint tables from both READMEs
- remove detailed directory trees from both READMEs
- add a curated "deep dive" section linking to the new architecture docs
- create four focused architecture documents for:
  - three-layer storage
  - cone retrieval
  - Autophagy
  - Skill Engine

### Out of scope

- changing backend code
- changing HTTP routes
- renaming modules
- deleting existing historical design docs outside the files touched by this work
- building a full docs site or navigation framework

## README Target Structure

The target root README structure should be:

1. project identity
2. what OpenCortex is
3. key concepts at a high level
4. one high-level architecture section
5. quick start
6. core features summary
7. minimal API overview by area
8. minimal repository layout by subsystem
9. deep-dive links
10. testing / tech stack / license

### What stays in README

- short explanation of layered memory
- short explanation of recall planning and retrieval
- short explanation of Alpha / Insights / Skill Engine as optional service layers
- install / config / run / connect MCP package
- API grouped by domain, not endpoint-by-endpoint
- repository layout grouped by subsystem, not file-by-file

### What must be removed from README

- full endpoint inventories where each route is listed individually
- MCP tool counts or line-item inventories that are likely to drift
- detailed implementation mechanics that belong in the four requested deep-dive topics
- overly detailed directory trees that expose many subpackages or files

## API Cleanup Rules

README should keep only a minimal API summary, using grouped areas such as:

- memory
- context/session
- content/observability
- knowledge / insights / skills
- auth / admin

For each area, README may describe the purpose of the group, but should avoid enumerating every route unless a route is especially central to understanding the system.

The detailed endpoint lists, if still needed, should live outside README. The architecture deep dives should reference concepts and flows, not duplicate route catalogs.

## Repository Layout Cleanup Rules

README should present the repository as subsystem buckets, for example:

- `src/opencortex/`
- `web/`
- `plugins/opencortex-memory/`
- `tests/`
- `docs/`

Within `src/opencortex/`, only major subsystem directories should be mentioned. README should avoid listing large subtrees or many individual files because those details change too often.

## Deep-Dive Document Requirements

Each new architecture document should explain:

### 1. Why the subsystem exists

What problem it solves and what user-visible behavior depends on it.

### 2. Main abstractions

The central types, modules, or interfaces involved.

### 3. Data flow

How requests move through the subsystem.

### 4. Integration points

How it connects to the rest of OpenCortex.

### 5. Constraints and tradeoffs

What the subsystem optimizes for, and what it intentionally does not do.

### 6. Current maturity

Whether the subsystem is core, optional, evolving, experimental, or partially implemented.

## Topic-Specific Coverage

### `three-layer-storage.md`

Should cover:

- CortexFS and the L0/L1/L2 model
- dual-write relationship between filesystem and Qdrant payloads
- why retrieval usually stops at L0/L1
- document and conversation ingestion implications
- when `L2` is read on demand

### `cone-retrieval.md`

Should cover:

- entity extraction and indexing
- `EntityIndex`
- candidate expansion model
- cone bonuses and penalties
- how cone retrieval fits into ordinary search
- when cone retrieval is disabled or degrades gracefully

### `autophagy.md`

Should cover:

- recall planning and lifecycle orchestration
- `memory_context` prepare / commit / end
- `RecallPlanner`, `IntentRouter`, `ContextManager`
- relationship to knowledge recall and session buffering
- why this is distinct from plain search

### `skill-engine.md`

Should cover:

- why skills are modeled as a separate subsystem
- extraction, evaluation, evolution, approval, promotion
- Skill Manager / Store / Quality Gate / Sandbox TDD
- current maturity and limits
- relation to memory retrieval and API exposure

## Language Strategy

The root entry documents must remain bilingual:

- `README.md` in English
- `README_CN.md` in Chinese

The deep-dive documents may initially be written in English only unless there is already a clear bilingual convention for new architecture documents. If bilingual deep dives are added later, the README link structure should still remain stable.

## Acceptance Criteria

This work is complete when:

- both root READMEs are clearly slimmer than their current architecture-heavy form
- both READMEs retain quick-start usefulness
- endpoint-by-endpoint tables are removed from README
- detailed directory trees are reduced to subsystem-level layout
- README links to the new deep-dive documents
- the four requested deep-dive documents exist and explain the current design accurately

## Risks

### Risk: README becomes too vague

Mitigation:
keep one architecture overview and one grouped API summary so the landing page still answers "how is this system shaped?"

### Risk: deep-dive docs duplicate old design docs

Mitigation:
write them as current-state subsystem explainers, not generic historical design essays.

### Risk: Chinese and English docs drift again

Mitigation:
keep the same structural outline across the two root READMEs and use links to shared deep-dive docs rather than duplicating every advanced explanation twice immediately.
