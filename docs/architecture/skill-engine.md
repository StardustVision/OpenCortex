# Skill Engine

## Why This Exists

OpenCortex needs a place to capture repeatable operational behavior as first-class artifacts, separate from raw memories and documents. Skills are structured procedures (name, description, content, lineage, quality signals) that can be searched, approved, and shared, while memory remains general-purpose context. This separation allows skills to have their own lifecycle (candidate → active → deprecated), quality gates, and visibility rules without overloading the memory subsystem.

## Core Components

- **SkillManager**: Orchestrates search, extraction, evolution, approval, deprecation, and promotion. Enforces visibility and ownership for mutating actions.
- **SkillStore + SkillStorageAdapter**: CRUD and vector search over a dedicated `skills` collection in Qdrant, including visibility filtering and embedding-based search.
- **SkillAnalyzer**: Extracts candidate skill directions from memory clusters using an LLM prompt, deduplicated by source fingerprint.
- **SkillEvolver**: Converts suggestions into concrete `SkillRecord` candidates via LLM evolution loops (captured, derived, fixed).
- **QualityGate**: Rule-based validation with optional LLM semantic checks, producing a quality score.
- **SandboxTDD**: Optional LLM-simulated RED-GREEN-REFACTOR evaluation that compares baseline vs with-skill behavior.
- **SkillEventStore + SkillEvaluator**: Tracks skill selection/application events and correlates them with session outcomes to update counters and reward score.
- **HTTP routes**: `/api/v1/skills` endpoints for listing, searching, extraction, approval, promotion, and evolution triggers.

## Extraction and Evolution Flow

1. **Scan and cluster**: `SkillAnalyzer` asks the source adapter (Qdrant-backed) to scan memories and cluster them.
2. **Dedup by fingerprint**: Each cluster gets a deterministic fingerprint; existing skills with the same fingerprint are skipped.
3. **LLM extraction**: The analyzer formats cluster content + existing skills and asks the LLM to return candidate directions.
4. **Evolution**: Captured skills use a fingerprint-derived ID, derived skills use a UUID and reference a parent, and fixed skills create a new candidate version of an existing skill rather than editing in place.
5. **Candidate creation**: `SkillEvolver` builds a `SkillRecord` with `CANDIDATE` status and `PRIVATE` visibility, plus lineage metadata.

## Validation and Approval Flow

1. **Quality Gate (Phase A, optional)**: The gate only exists when an LLM adapter is configured. When present, it runs deterministic checks (name format, content length, step structure, description, category) and may add semantic checks. Candidates below the score threshold (60) are rejected before persistence.
2. **Sandbox TDD (Phase B, optional)**: If enabled, the candidate is tested against generated scenarios. Failures block persistence.
3. **Persistence**: Only candidates passing the gates are saved to the skills collection.
4. **Human approval**: Approval and rejection are explicit APIs. Only the owner can change status. Approved candidates become `ACTIVE`; rejected or deprecated skills become `DEPRECATED`.
5. **Promotion**: Visibility is a separate axis from status. Owners can promote a `PRIVATE` skill to `SHARED` (URI is regenerated), and promotion is not gated on approval in current code.

## Retrieval and API Exposure

The Skill Engine is exposed in two ways:

- **REST API**: `/api/v1/skills` supports list, search, extract, get, approve, reject, deprecate, promote, fix, and derive. Identity and tenant scoping come from request context.
- **MemoryOrchestrator search**: Skill search is merged into `FindResult.skills` during normal memory search. This keeps skill retrieval aligned with user queries while leaving skill storage independent of memory records.

Skills are represented by their own URIs (`opencortex://{tenant}/shared/skills/...` or `opencortex://{tenant}/{user}/skills/...`) and travel alongside memory and resource contexts in retrieval results.

## Constraints and Tradeoffs

- Skills live in a separate Qdrant collection with their own visibility rules, which simplifies access control but adds another storage surface to manage.
- Extraction and evolution are LLM-dependent; without LLM configuration the pipeline is disabled and only manual management remains.
- Quality Gate and Sandbox TDD reduce low-quality candidates but can reject useful drafts and add latency.
- Approval is manual and owner-scoped; there is no automatic promotion based on usage metrics.
- The engine uses memory clusters rather than execution traces for extraction, which favors general patterns but can miss task-specific nuance.

## Current State

The Skill Engine is wired in `MemoryOrchestrator.init()`:

- It initializes a dedicated skills collection with embeddings.
- It enables extraction and evolution only when an LLM adapter is available.
- The Quality Gate only exists when an LLM adapter is configured; without it, candidates can persist without that gate.
- Sandbox TDD is gated behind config and is off by default.
- Skill usage events are stored independently and evaluated against trace outcomes to update metrics and reward scores, but the signal is noisy (missing or failed traces count against completion).

Skill search is currently a top-k merge into general retrieval results, not a separate retrieval mode or planner stage.

## Open Boundaries

- No auto-approval or auto-promotion based on quality/usage; the system relies on explicit human approval.
- No version rollback or diff tooling beyond lineage metadata; fixed skills create new candidates but do not manage coexistence beyond status changes.
- Cross-tenant sharing is intentionally blocked; visibility is scoped to tenant and (for private skills) user.
- Extraction does not yet integrate with execution traces, and the evaluator’s reward signal is lightweight and noisy.
- The skill lifecycle exists, but operational governance (review workflows, audit trails, or external policy hooks) is not implemented.
