# Skill ReAct Feedback Loop Design

**Date**: 2026-04-03
**Status**: Draft (rev.4 — superpowers + Codex x3 adversarial reviews)
**Author**: Hugo + Claude

---

## 1. Problem

The Skill Engine extracts skills from memories and stores them, but has no feedback loop. Once a skill is ACTIVE, there is no mechanism to:
- Validate the skill actually improves agent behavior (vs. baseline)
- Track whether agents use the skill when it's recalled
- Score skill quality based on real-world outcomes
- Identify low-quality skills before they pollute recall

Skills are static text with no quality signal — "dead knowledge."

## 2. Solution: Three-Phase Feedback Loop

```
Phase A: Quality Gate — validate after evolve, before saving
  Extract → Evolver → SkillRecord draft → QualityGate(rules + LLM)
    → score >= 60 → save as CANDIDATE
    → score < 60 → discard

Phase B: Sandbox TDD — verify effectiveness (optional, default OFF)
  CANDIDATE → generate scenarios → baseline(no skill) vs with-skill
    → behavior improved? → tdd_passed=True
    → not improved → discard or REFACTOR

Phase C: Online Feedback — track usage + score in production
  recall → persist SkillEvent(SELECTED) to skill_events collection
    → add_message → persist SkillEvent(CITED)
    → session_end → evaluator reads events + trace outcomes
    → 5-dim rating update → reward scoring
```

No auto-evolution. FIX/DERIVED initiated manually via SkillHub.

## 3. Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Sandbox method | LLM simulation (not real Agent) | Simpler, reuses existing LLM client |
| Sandbox default | OFF (`sandbox_tdd_enabled=False`) | LLM cost control; max ~15 calls/skill |
| Rating dimensions | SkillForge 5-dim (Practicality/Clarity/Automation/Quality/Impact) | Aligns with SkillHub ecosystem |
| SkillEvent storage | **Independent `skill_events` Qdrant collection** | NOT Observer/Trace — avoids TraceSplitter data loss and Observer tenant isolation issues |
| VERIFIED status | **NOT added** — use `tdd_passed: bool` flag on CANDIDATE | Avoids 4-state lifecycle complexity; current 3-state (CANDIDATE/ACTIVE/DEPRECATED) preserved |
| Evolution trigger | Manual only (via SkillHub) | User preference — keep human in the loop |
| Quality threshold | score >= 60 to proceed | Below 60 = too many ERRORs, not worth saving |
| Rating overall formula | Equal-weight average: `(P+C+A+Q+I) / 5` | Simple, predictable |
| Category validation | Dynamic from `SkillCategory.__members__` | Not hardcoded — survives enum additions |
| Evaluator idempotency | `evaluated_session_ids` set on evaluator or flag on events | Prevents double-counting on retry |
| Evaluator safety | try/except + per-tenant asyncio.Lock | Matches archivist pattern |

## 4. Phase A: Quality Gate

### 4.1 When

**After** `SkillEvolver.evolve()` produces a `SkillRecord` draft, **before** `SkillStore.save_record()`.

Runs on the concrete `SkillRecord` which has `name`, `description`, `content`, `category`. NOT on `EvolutionSuggestion` (which lacks content).

### 4.2 Two-Layer Check

**Input**: `SkillRecord` (with populated `name`, `content`, `description`, `category`)

**Layer 1: Rule-based (deterministic)**

| Check | Rule | Severity |
|-------|------|----------|
| Name format | lowercase-hyphenated, <= 50 chars | ERROR |
| Content not empty | len(content) > 50 chars | ERROR |
| Has steps | Contains numbered list or "## Steps" | ERROR |
| No empty sections | No "## X" followed immediately by "## Y" | WARNING |
| Token budget | Content < 5000 tokens (~20K chars) | WARNING |
| Has description | Non-empty description | ERROR |
| Category valid | `category.value in SkillCategory.__members__` | ERROR |

**Layer 2: LLM-based (semantic)**

| Check | Method | Severity |
|-------|--------|----------|
| Actionability | "Can an agent follow these steps without ambiguity?" | ERROR |
| Internal consistency | "Does the description match the steps?" | WARNING |
| Specificity | "Are steps concrete or vague platitudes?" | WARNING |
| Overlap detection | "Does this duplicate any existing skill?" (pass existing list) | ERROR |

**Output**: `QualityReport(score=0-100, checks=[], errors=N, warnings=N)`

Score formula: `100 - (errors * 20) - (warnings * 5)`. Clamped to [0, 100].

### 4.3 Integration Point

```python
# In SkillManager.extract(), AFTER evolve, BEFORE save:
candidates = await self._evolver.process_suggestions(suggestions, ...)

saved = []
for candidate in candidates:
    # Phase A: Quality Gate on the concrete SkillRecord
    if self._quality_gate:
        report = await self._quality_gate.evaluate(candidate)
        candidate.quality_score = report.score
        if report.score < 60:
            continue  # Discard low-quality drafts

    # Phase B: Sandbox TDD (if enabled)
    if self._sandbox_tdd:
        tdd_result = await self._sandbox_tdd.evaluate(candidate)
        candidate.tdd_passed = tdd_result.passed
        if not tdd_result.passed:
            continue  # Discard ineffective skills

    await self._store.save_record(candidate)
    saved.append(candidate)
```

## 5. Phase B: Sandbox TDD

### 5.0 Configuration

```python
# In CortexAlphaConfig or SkillManager init:
sandbox_tdd_enabled: bool = False        # Default OFF — enable explicitly
sandbox_tdd_max_llm_calls: int = 20      # Budget cap per extraction batch
```

**LLM cost estimate**: Per skill: scenario gen (1) + baselines (3) + with-skill (3) + refactor (up to 2 × 4) = max 15 calls. With budget cap of 20, only 1 skill can be fully tested per batch.

### 5.1 When

After Quality Gate passes, before `SkillStore.save_record()`. Only runs if `sandbox_tdd_enabled=True`.

### 5.2 RED-GREEN-REFACTOR (LLM Simulation)

**Step 1: Generate scenarios**

LLM generates 2-3 pressure scenarios from the skill content:
```
"Given this skill about {skill.name}:
{skill.content}

Generate 2-3 realistic scenarios that test whether an agent would
follow this skill correctly. Each scenario should:
- Present a concrete situation with A/B/C options
- Include time pressure or competing priorities
- Have one clearly correct answer per the skill
- Be answerable without external tools"
```

**Step 2: RED — Baseline (no skill)**

For each scenario, ask LLM without the skill:
```
"You are an AI assistant. A user asks:
{scenario}
Choose an option and explain your reasoning."
```

Record: choice, reasoning.

**Step 3: GREEN — With skill**

For each scenario, ask LLM with the skill injected:
```
"You are an AI assistant with this operational skill:
{skill.content}

A user asks:
{scenario}
Choose an option and explain your reasoning."
```

Record: choice, reasoning, which skill sections cited.

**Step 4: Compare**

```python
@dataclass
class TDDResult:
    passed: bool
    scenarios_total: int
    scenarios_improved: int      # with-skill chose better than baseline
    scenarios_same: int          # no change
    scenarios_worse: int         # with-skill chose worse (red flag)
    sections_cited: List[str]    # which parts of skill were referenced
    rationalizations: List[str]  # workarounds/excuses found
    quality_delta: float         # improvement ratio
    llm_calls_used: int          # for budget tracking
```

Pass criteria: `scenarios_improved >= scenarios_total * 0.5` AND `scenarios_worse == 0`.

**Step 5: REFACTOR (optional, max 2 iterations)**

If `scenarios_worse > 0` or `scenarios_improved < threshold`:
- Capture failure reasons
- Ask LLM to suggest skill fix
- Apply fix to content
- Re-run GREEN
- Max 2 refactor iterations, then give up
- Track `llm_calls_used` against budget

## 6. Phase C: Online Feedback Loop

### 6.0 SkillEvent Storage (Independent Collection)

**Critical design**: SkillEvents are stored in a **separate `skill_events` Qdrant collection**, NOT in Observer/Trace.

**Why not Observer/Trace**:
- TraceSplitter discards non-message entries (only processes `role=user/assistant`)
- Observer is keyed by raw `session_id` without tenant/user isolation
- Trace pipeline is LLM-based — unreliable for structured metadata

```python
@dataclass
class SkillEvent:
    event_id: str           # uuid
    session_id: str
    turn_id: str
    skill_id: str
    skill_uri: str
    tenant_id: str
    user_id: str
    event_type: str         # "selected" | "cited"
    outcome: str = ""       # "" (unknown) | "success" | "failure"
    timestamp: str = ""
    evaluated: bool = False # Idempotency flag — set True after evaluator processes
```

**Collection schema**: Minimal — just enough for the evaluator to query.

```
Collection: "skill_events"
Payload (keyword indexed):
  event_id, session_id, turn_id, skill_id, skill_uri
  tenant_id, user_id, event_type, outcome, evaluated
  timestamp (date_time indexed)
Vector: NONE (no embedding needed — pure metadata store)
```

**Storage adapter**: New `SkillEventStore` class in `src/opencortex/skill_engine/event_store.py`:
```python
class SkillEventStore:
    async def init(self) -> None
    async def append(self, event: SkillEvent) -> None
    async def list_by_session(self, session_id: str, tenant_id: str, user_id: str) -> List[SkillEvent]
    async def mark_evaluated(self, event_ids: List[str]) -> None
    async def list_unevaluated(self, tenant_id: str, limit: int = 100) -> List[SkillEvent]
```

**All queries require `(tenant_id, user_id, session_id)` triple** — prevents cross-user session collision.

### 6.1 Selection Tracking (recall time)

**Where**: `context/manager.py`, in `_prepare()`, after orchestrator.search() returns.

`_prepare()` already has `session_id`, `turn_id`, `tenant_id`, `user_id` available (lines 161-177).

```python
# In _prepare(), after search returns find_result:
if find_result.skills and self._skill_event_store:
    # Build server-side selected set for this turn (used by citation validation in 6.2)
    selected_skill_uris = set()
    for mc in find_result.skills:
        skill_uri = mc.uri
        selected_skill_uris.add(skill_uri)
        await self._skill_event_store.append(SkillEvent(
            event_id=uuid4().hex,
            session_id=session_id,
            turn_id=turn_id,
            skill_id=skill_uri.split("/")[-1],
            skill_uri=skill_uri,
            tenant_id=tenant_id,
            user_id=user_id,
            event_type="selected",
            timestamp=datetime.now(timezone.utc).isoformat(),
        ))
    # Cache selected URIs for citation validation in _commit()
    sk = self._make_session_key(tenant_id, user_id, session_id)
    self._selected_skill_uris[sk] = selected_skill_uris
```

**Server-side selected set**: `_selected_skill_uris` is a dict keyed by session_key, holding the set of skill URIs the server actually returned in `_prepare()`. This is used in `_commit()` (§6.2) to validate that `cited_uris` are not forged.

**Why _prepare() not orchestrator.search()**: `_prepare()` has all 4 identity fields. `orchestrator.search()` does NOT have session_id or turn_id.

### 6.2 Citation Detection (commit time)

**Where**: `context/manager.py`, in `_commit()`.

```python
# In _commit(), after existing cited_uris reward logic:
if cited_uris and self._skill_event_store:
    sk = self._make_session_key(tenant_id, user_id, session_id)
    server_selected = self._selected_skill_uris.get(sk, set())

    skill_uris = [u for u in cited_uris if "/skills/" in u]
    for uri in skill_uris:
        # Security: only accept URIs the server actually returned in _prepare()
        if uri not in server_selected:
            logger.debug("[ContextManager] Dropped forged skill citation: %s", uri)
            continue
        await self._skill_event_store.append(SkillEvent(
            event_id=uuid4().hex,
            session_id=session_id,
            turn_id=turn_id,
            skill_id=uri.split("/")[-1],
            skill_uri=uri,
            tenant_id=tenant_id,
            user_id=user_id,
            event_type="cited",
            timestamp=datetime.now(timezone.utc).isoformat(),
        ))
```

**Citation validation**: `_selected_skill_uris[session_key]` is the server-side set of skill URIs returned in `_prepare()`. Client-forged citations for skills never recalled are silently dropped.

**Outcome determination**: NOT set at commit time. Evaluator determines from trace results.

### 6.3 Trace-Level Evaluation (session_end time)

**Where**: `orchestrator.py`, in `session_end()`, after archivist trigger.

```python
# In session_end(), after archivist trigger:
if self._skill_evaluator:
    asyncio.create_task(self._run_skill_evaluator(tid, uid, session_id))
```

`_run_skill_evaluator(tid, uid, session_id)`:

```python
async def _run_skill_evaluator(self, tid, uid, session_id):
    """Evaluate skill usage for a completed session."""
    try:
        # 1. Fetch unevaluated events for this session (tenant+user+session triple)
        events = await self._skill_event_store.list_by_session(session_id, tid, uid)
        unevaluated = [e for e in events if not e.evaluated]
        if not unevaluated:
            return

        # 2. Fetch traces for outcome correlation
        traces = await self._trace_store.list_by_session(session_id, tid, uid)
        session_outcome = "success" if any(
            t.get("outcome") == "success" for t in traces
        ) else "failure" if traces else ""

        # 3. Group events by skill_id
        skill_events = defaultdict(list)
        for e in unevaluated:
            skill_events[e.skill_id].append(e)

        # 4. Update metrics + rating per skill
        for skill_id, events_for_skill in skill_events.items():
            was_selected = any(e.event_type == "selected" for e in events_for_skill)
            was_cited = any(e.event_type == "cited" for e in events_for_skill)

            if was_selected:
                await self._skill_manager._store.record_selection(skill_id)
            if was_cited:
                completed = session_outcome == "success"
                await self._skill_manager._store.record_application(skill_id, completed)

            # 5. LLM 5-dim rating (only for cited skills)
            if was_cited and self._llm_completion:
                # ... LLM evaluation → EMA update on rating fields
                pass

            # 6. Reward scoring
            if was_cited:
                reward = 0.1 if session_outcome == "success" else -0.05
                await self._skill_storage.update_reward(skill_id, reward)

        # 7. Mark events as evaluated (idempotency)
        await self._skill_event_store.mark_evaluated(
            [e.event_id for e in unevaluated]
        )
    except Exception as exc:
        logger.warning("[SkillEvaluator] Failed for session %s: %s", session_id, exc)
```

### 6.3.1 Startup Sweeper (Crash Recovery)

On orchestrator init, run a one-time sweep of unevaluated events to recover from crashes/restarts:

```python
# In _init_skill_engine(), after evaluator is created:
asyncio.create_task(self._sweep_unevaluated_skill_events(tid))
```

```python
async def _sweep_unevaluated_skill_events(self, tenant_id: str):
    """Process any skill events left unevaluated after crash/restart."""
    try:
        backlog = await self._skill_event_store.list_unevaluated(tenant_id, limit=200)
        if not backlog:
            return
        # Group by (session_id, user_id) and process each group
        groups = defaultdict(list)
        for e in backlog:
            groups[(e.session_id, e.user_id)].append(e)
        for (sid, uid), events in groups.items():
            await self._run_skill_evaluator(tenant_id, uid, sid)
        logger.info("[SkillEvaluator] Swept %d backlog events across %d sessions",
                     len(backlog), len(groups))
    except Exception as exc:
        logger.warning("[SkillEvaluator] Startup sweep failed: %s", exc)
```

This ensures no feedback signal is permanently lost due to process restart.

**Idempotency**: Events have `evaluated: bool` flag. `mark_evaluated()` sets it after processing. Re-running the evaluator skips already-evaluated events.

**Concurrency**: Use `asyncio.Lock` per-tenant (same pattern as archivist). Only one evaluator runs per tenant at a time.

### 6.4 5-Dimension Rating Model

Stored on SkillRecord:

```python
@dataclass
class SkillRating:
    practicality: float = 0.0   # 0-10: solves real recurring problems
    clarity: float = 0.0        # 0-10: well-structured, unambiguous
    automation: float = 0.0     # 0-10: enables autonomous execution
    quality: float = 0.0        # 0-10: format, examples, completeness
    impact: float = 0.0         # 0-10: breadth of applicability
    overall: float = 0.0        # = (P+C+A+Q+I) / 5
    rank: str = "C"             # S(9+) / A(7+) / B(5+) / C(<5)

    def compute_overall(self) -> None:
        self.overall = (self.practicality + self.clarity + self.automation
                        + self.quality + self.impact) / 5
        if self.overall >= 9.0: self.rank = "S"
        elif self.overall >= 7.0: self.rank = "A"
        elif self.overall >= 5.0: self.rank = "B"
        else: self.rank = "C"
```

**Initial rating**: Set by Quality Gate score mapped to dimensions.

**Ongoing updates**: Exponential moving average from trace evaluations:
```
new_dim = 0.7 * old_dim + 0.3 * trace_eval_dim
```

## 7. Data Model Additions

### 7.1 New types in `types.py`

```python
@dataclass
class SkillEvent:
    event_id: str
    session_id: str
    turn_id: str
    skill_id: str
    skill_uri: str
    tenant_id: str
    user_id: str
    event_type: str      # "selected" | "cited"
    outcome: str = ""    # "" | "success" | "failure"
    timestamp: str = ""
    evaluated: bool = False

@dataclass
class QualityCheck:
    name: str
    severity: str    # "ERROR" | "WARNING" | "INFO"
    passed: bool
    message: str
    fix_suggestion: str = ""

@dataclass
class QualityReport:
    score: int       # 0-100
    checks: List[QualityCheck]
    errors: int
    warnings: int

@dataclass
class TDDResult:
    passed: bool
    scenarios_total: int
    scenarios_improved: int
    scenarios_same: int
    scenarios_worse: int
    sections_cited: List[str]
    rationalizations: List[str]
    quality_delta: float
    llm_calls_used: int = 0

@dataclass
class SkillRating:
    practicality: float = 0.0
    clarity: float = 0.0
    automation: float = 0.0
    quality: float = 0.0
    impact: float = 0.0
    overall: float = 0.0
    rank: str = "C"

    def compute_overall(self) -> None:
        self.overall = (self.practicality + self.clarity + self.automation
                        + self.quality + self.impact) / 5
        if self.overall >= 9.0: self.rank = "S"
        elif self.overall >= 7.0: self.rank = "A"
        elif self.overall >= 5.0: self.rank = "B"
        else: self.rank = "C"
```

### 7.2 SkillRecord additions

```python
# Add to SkillRecord:
rating: SkillRating = field(default_factory=SkillRating)
tdd_passed: bool = False
quality_score: int = 0
reward_score: float = 0.0
```

### 7.3 Serialization changes required

**`SkillRecord.to_dict()`** — add:
```python
"rating": self.rating.__dict__,
"tdd_passed": self.tdd_passed,
"quality_score": self.quality_score,
"reward_score": self.reward_score,
```

**`SkillStorageAdapter._dict_to_record()`** — add:
```python
rating_data = d.get("rating", {})
rating = SkillRating(
    practicality=rating_data.get("practicality", 0.0),
    clarity=rating_data.get("clarity", 0.0),
    automation=rating_data.get("automation", 0.0),
    quality=rating_data.get("quality", 0.0),
    impact=rating_data.get("impact", 0.0),
    overall=rating_data.get("overall", 0.0),
    rank=rating_data.get("rank", "C"),
)
# ... in SkillRecord constructor:
rating=rating,
tdd_passed=d.get("tdd_passed", False),
quality_score=d.get("quality_score", 0),
reward_score=d.get("reward_score", 0.0),
```

**`SkillStorageAdapter.update_reward()`** — new method:
```python
async def update_reward(self, skill_id: str, reward: float) -> None:
    existing = await self.load(skill_id)
    if not existing:
        return
    new_reward = existing.reward_score + reward
    await self._storage.update(self._collection, skill_id, {
        "reward_score": new_reward,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })
```

### 7.4 Storage schema additions

**skills collection** — add to `CollectionSchemas.skills_collection()` Fields + ScalarIndex:
```
rating_practicality     float
rating_clarity          float
rating_automation       float
rating_quality          float
rating_impact           float
rating_overall          float
rating_rank             str (keyword indexed)
tdd_passed              bool
quality_score           int
reward_score            float
```

**New collection: `skill_events`** — add `CollectionSchemas.skill_events_collection()` + `init_skill_events_collection()`:
```
event_id            str (keyword indexed)
session_id          str (keyword indexed)
turn_id             str (keyword indexed)
skill_id            str (keyword indexed)
skill_uri           str
tenant_id           str (keyword indexed)
user_id             str (keyword indexed)
event_type          str (keyword indexed)
outcome             str (keyword indexed)
evaluated           bool
timestamp           date_time (indexed)
```
No vector — pure metadata store.

## 8. New Files

```
src/opencortex/skill_engine/
├── quality_gate.py      # Phase A: rule + LLM dual-layer check (~150 lines)
├── sandbox_tdd.py       # Phase B: LLM simulation RED-GREEN-REFACTOR (~250 lines)
├── evaluator.py         # Phase C: online feedback + 5-dim rating (~250 lines)
├── event_store.py       # SkillEventStore — independent Qdrant collection (~100 lines)
└── types.py             # Add: SkillEvent, QualityCheck, QualityReport, TDDResult, SkillRating
```

## 9. Modified Files

```
src/opencortex/skill_engine/types.py              # Add new dataclasses + SkillRecord fields
src/opencortex/skill_engine/skill_manager.py      # Wire QualityGate + SandboxTDD into extract()
src/opencortex/skill_engine/adapters/storage_adapter.py  # to_dict, _dict_to_record, update_reward
src/opencortex/skill_engine/store.py              # (no change — record_selection/record_application already defined)
src/opencortex/orchestrator.py                    # Init SkillEventStore + evaluator trigger in session_end
src/opencortex/context/manager.py                 # Selection + citation event recording
src/opencortex/storage/collection_schemas.py      # skills rating fields + skill_events collection
src/opencortex/config.py                          # sandbox_tdd_enabled, sandbox_tdd_max_llm_calls
```

## 10. Non-Goals

1. **Auto-evolution** — FIX/DERIVED is manual via SkillHub only
2. **Real Agent sandbox** — LLM simulation only, no sub-Agent lifecycle management
3. **Cross-tenant feedback aggregation** — per-user ratings only
4. **SkillHub publishing** — no integration with external marketplace
5. **Rationalization database** — rationalizations captured per-skill but no global DB
6. **Observer/Trace pipeline changes** — SkillEvents use independent storage, not Observer
7. **VERIFIED lifecycle state** — use `tdd_passed` flag on CANDIDATE instead
