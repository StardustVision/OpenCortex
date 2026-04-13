# Skill ReAct Feedback Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a three-phase feedback loop to the Skill Engine: Quality Gate (validate before saving), Sandbox TDD (verify effectiveness), and Online Feedback (track usage + score in production).

**Architecture:** Phase A runs quality checks on SkillRecord drafts after evolution. Phase B runs LLM-simulated baseline-vs-with-skill comparisons. Phase C persists SkillEvents to an independent Qdrant collection, with a server-side selected-set to prevent forged citations. An evaluator correlates events with trace outcomes at session_end, updating 5-dim ratings and reward scores. A startup sweeper handles crash recovery.

**Tech Stack:** Python 3.10+, asyncio, unittest, Qdrant (embedded), LLM completion adapter

**Spec:** `docs/superpowers/specs/2026-04-03-skill-react-loop-design.md` (rev.4)

---

## File Structure

```
src/opencortex/skill_engine/          # EXISTING module
├── types.py                           # MODIFY: add SkillEvent, QualityCheck, QualityReport, TDDResult, SkillRating + SkillRecord new fields
├── quality_gate.py                    # CREATE: Phase A - rule + LLM dual-layer check
├── sandbox_tdd.py                     # CREATE: Phase B - LLM simulation RED-GREEN-REFACTOR
├── event_store.py                     # CREATE: Phase C - SkillEventStore (independent Qdrant collection)
├── evaluator.py                       # CREATE: Phase C - SkillEvaluator + startup sweeper
├── skill_manager.py                   # MODIFY: wire QualityGate + SandboxTDD into extract()
├── adapters/storage_adapter.py        # MODIFY: to_dict, _dict_to_record, update_reward for new fields

src/opencortex/
├── orchestrator.py                    # MODIFY: init event_store + evaluator + sweeper trigger
├── context/manager.py                 # MODIFY: selection tracking in _prepare() + citation validation in _commit()
├── storage/collection_schemas.py      # MODIFY: skills schema additions + new skill_events collection
├── config.py                          # MODIFY: sandbox_tdd_enabled + sandbox_tdd_max_llm_calls

tests/skill_engine/
├── test_types_react.py                # CREATE: tests for new types
├── test_quality_gate.py               # CREATE: Phase A tests
├── test_sandbox_tdd.py                # CREATE: Phase B tests
├── test_event_store.py                # CREATE: Phase C event store tests
├── test_evaluator.py                  # CREATE: Phase C evaluator tests
```

---

### Task 1: New Types + SkillRecord Field Extensions

**Files:**
- Modify: `src/opencortex/skill_engine/types.py:64-117`
- Modify: `src/opencortex/skill_engine/adapters/storage_adapter.py:151-190`
- Modify: `src/opencortex/storage/collection_schemas.py:172-199`
- Test: `tests/skill_engine/test_types_react.py`

- [ ] **Step 1: Write tests for new types**

Create `tests/skill_engine/test_types_react.py`:

```python
import unittest
from opencortex.skill_engine.types import (
    SkillEvent, QualityCheck, QualityReport, TDDResult, SkillRating,
    SkillRecord, SkillCategory,
)


class TestSkillEvent(unittest.TestCase):

    def test_create_selected_event(self):
        e = SkillEvent(
            event_id="ev1", session_id="s1", turn_id="t1",
            skill_id="sk-001", skill_uri="opencortex://t/u/skills/workflow/sk-001",
            tenant_id="team1", user_id="hugo", event_type="selected",
        )
        self.assertEqual(e.event_type, "selected")
        self.assertEqual(e.outcome, "")
        self.assertFalse(e.evaluated)

    def test_create_cited_event(self):
        e = SkillEvent(
            event_id="ev2", session_id="s1", turn_id="t2",
            skill_id="sk-001", skill_uri="opencortex://t/u/skills/workflow/sk-001",
            tenant_id="team1", user_id="hugo", event_type="cited",
            outcome="success",
        )
        self.assertEqual(e.event_type, "cited")
        self.assertEqual(e.outcome, "success")


class TestQualityReport(unittest.TestCase):

    def test_quality_report(self):
        checks = [
            QualityCheck(name="name_format", severity="ERROR", passed=True, message="OK"),
            QualityCheck(name="content_empty", severity="ERROR", passed=False,
                         message="Content too short", fix_suggestion="Add more steps"),
        ]
        report = QualityReport(score=80, checks=checks, errors=1, warnings=0)
        self.assertEqual(report.score, 80)
        self.assertEqual(report.errors, 1)


class TestSkillRating(unittest.TestCase):

    def test_compute_overall(self):
        r = SkillRating(practicality=8.0, clarity=7.0, automation=6.0,
                        quality=9.0, impact=5.0)
        r.compute_overall()
        self.assertAlmostEqual(r.overall, 7.0)
        self.assertEqual(r.rank, "A")

    def test_rank_s(self):
        r = SkillRating(practicality=9.5, clarity=9.0, automation=9.5,
                        quality=9.0, impact=9.0)
        r.compute_overall()
        self.assertEqual(r.rank, "S")

    def test_rank_c(self):
        r = SkillRating(practicality=2.0, clarity=3.0, automation=1.0,
                        quality=2.0, impact=1.0)
        r.compute_overall()
        self.assertEqual(r.rank, "C")


class TestSkillRecordNewFields(unittest.TestCase):

    def test_new_fields_default(self):
        r = SkillRecord(
            skill_id="sk-001", name="test", description="d",
            content="c", category=SkillCategory.WORKFLOW,
            tenant_id="t", user_id="u",
        )
        self.assertEqual(r.quality_score, 0)
        self.assertFalse(r.tdd_passed)
        self.assertEqual(r.reward_score, 0.0)
        self.assertIsInstance(r.rating, SkillRating)
        self.assertEqual(r.rating.overall, 0.0)

    def test_to_dict_includes_new_fields(self):
        r = SkillRecord(
            skill_id="sk-001", name="test", description="d",
            content="c", category=SkillCategory.WORKFLOW,
            tenant_id="t", user_id="u",
            quality_score=85, tdd_passed=True, reward_score=0.5,
        )
        d = r.to_dict()
        self.assertEqual(d["quality_score"], 85)
        self.assertTrue(d["tdd_passed"])
        self.assertEqual(d["reward_score"], 0.5)
        self.assertIn("rating", d)


class TestTDDResult(unittest.TestCase):

    def test_passed(self):
        r = TDDResult(
            passed=True, scenarios_total=3, scenarios_improved=2,
            scenarios_same=1, scenarios_worse=0,
            sections_cited=["Steps"], rationalizations=[],
            quality_delta=0.67, llm_calls_used=7,
        )
        self.assertTrue(r.passed)
        self.assertEqual(r.llm_calls_used, 7)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests — expect FAIL**

Run: `uv run python3 -m unittest tests.skill_engine.test_types_react -v`
Expected: FAIL — SkillEvent, QualityCheck, etc. not defined

- [ ] **Step 3: Add new types to types.py**

Add after `EvolutionSuggestion` (after line 126) in `src/opencortex/skill_engine/types.py`:

```python
# ---------------------------------------------------------------------------
# ReAct Feedback Loop Types (spec rev.4)
# ---------------------------------------------------------------------------

@dataclass
class SkillEvent:
    """Durable skill usage event — stored in independent skill_events collection."""
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

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_id": self.event_id,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "skill_id": self.skill_id,
            "skill_uri": self.skill_uri,
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "event_type": self.event_type,
            "outcome": self.outcome,
            "timestamp": self.timestamp,
            "evaluated": self.evaluated,
        }


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
    checks: List[QualityCheck] = field(default_factory=list)
    errors: int = 0
    warnings: int = 0


@dataclass
class TDDResult:
    passed: bool
    scenarios_total: int = 0
    scenarios_improved: int = 0
    scenarios_same: int = 0
    scenarios_worse: int = 0
    sections_cited: List[str] = field(default_factory=list)
    rationalizations: List[str] = field(default_factory=list)
    quality_delta: float = 0.0
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
        if self.overall >= 9.0:
            self.rank = "S"
        elif self.overall >= 7.0:
            self.rank = "A"
        elif self.overall >= 5.0:
            self.rank = "B"
        else:
            self.rank = "C"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "practicality": self.practicality,
            "clarity": self.clarity,
            "automation": self.automation,
            "quality": self.quality,
            "impact": self.impact,
            "overall": self.overall,
            "rank": self.rank,
        }
```

- [ ] **Step 4: Add new fields to SkillRecord**

In `src/opencortex/skill_engine/types.py`, add after `source_fingerprint: str = ""` (line 91):

```python
    # ReAct feedback loop fields
    rating: SkillRating = field(default_factory=SkillRating)
    tdd_passed: bool = False
    quality_score: int = 0
    reward_score: float = 0.0
```

Add to `to_dict()` (after `"source_fingerprint"` line):

```python
            "rating": self.rating.to_dict(),
            "tdd_passed": self.tdd_passed,
            "quality_score": self.quality_score,
            "reward_score": self.reward_score,
```

- [ ] **Step 5: Update storage_adapter._dict_to_record()**

In `src/opencortex/skill_engine/adapters/storage_adapter.py`, in `_dict_to_record()`, add after the existing field mapping (around line 187):

```python
            # Deserialize rating
            rating_data = d.get("rating", {})
            if isinstance(rating_data, dict):
                rating = SkillRating(
                    practicality=rating_data.get("practicality", 0.0),
                    clarity=rating_data.get("clarity", 0.0),
                    automation=rating_data.get("automation", 0.0),
                    quality=rating_data.get("quality", 0.0),
                    impact=rating_data.get("impact", 0.0),
                    overall=rating_data.get("overall", 0.0),
                    rank=rating_data.get("rank", "C"),
                )
            else:
                rating = SkillRating()
```

And in the SkillRecord constructor add:

```python
            rating=rating,
            tdd_passed=d.get("tdd_passed", False),
            quality_score=d.get("quality_score", 0),
            reward_score=d.get("reward_score", 0.0),
```

Also add import at top:

```python
from opencortex.skill_engine.types import (
    SkillRecord, SkillStatus, SkillVisibility, SkillLineage, SkillOrigin,
    SkillCategory, SkillRating,  # ADD SkillRating
)
```

- [ ] **Step 6: Add update_reward() to storage_adapter**

Add after `update_metrics()` in `src/opencortex/skill_engine/adapters/storage_adapter.py`:

```python
    async def update_reward(self, skill_id: str, reward: float) -> None:
        """Accumulate reward score for a skill."""
        existing = await self.load(skill_id)
        if not existing:
            return
        new_reward = existing.reward_score + reward
        await self._storage.update(self._collection, skill_id, {
            "reward_score": new_reward,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
```

- [ ] **Step 7: Update skills collection schema**

In `src/opencortex/storage/collection_schemas.py`, add to `skills_collection()` Fields list:

```python
                {"FieldName": "rating_rank", "FieldType": "string"},
                {"FieldName": "tdd_passed", "FieldType": "bool"},
                {"FieldName": "quality_score", "FieldType": "int64"},
                {"FieldName": "reward_score", "FieldType": "float"},
```

Add to ScalarIndex:

```python
                "rating_rank", "tdd_passed", "quality_score", "reward_score",
```

- [ ] **Step 8: Run tests**

Run: `uv run python3 -m unittest tests.skill_engine.test_types_react -v`
Expected: All PASS

- [ ] **Step 9: Run regression**

Run: `uv run python3 -m unittest discover -s tests/skill_engine -v`
Expected: All PASS (existing 117 tests + new tests)

- [ ] **Step 10: Commit**

```bash
git add src/opencortex/skill_engine/types.py src/opencortex/skill_engine/adapters/storage_adapter.py src/opencortex/storage/collection_schemas.py tests/skill_engine/test_types_react.py
git commit -m "feat(skill_engine): add ReAct types (SkillEvent, QualityReport, TDDResult, SkillRating) + SkillRecord fields"
```

---

### Task 2: Quality Gate (Phase A)

**Files:**
- Create: `src/opencortex/skill_engine/quality_gate.py`
- Modify: `src/opencortex/skill_engine/skill_manager.py:114-134`
- Test: `tests/skill_engine/test_quality_gate.py`

- [ ] **Step 1: Write tests**

Create `tests/skill_engine/test_quality_gate.py`:

```python
import unittest
from unittest.mock import AsyncMock
from opencortex.skill_engine.types import (
    SkillRecord, SkillCategory, SkillStatus, QualityReport,
)
from opencortex.skill_engine.quality_gate import QualityGate


class TestQualityGateRules(unittest.TestCase):

    def _make_skill(self, name="deploy-flow", content="# Deploy\n\n1. Build\n2. Test\n3. Deploy",
                    description="Standard deploy workflow"):
        return SkillRecord(
            skill_id="sk-001", name=name, description=description,
            content=content, category=SkillCategory.WORKFLOW,
            tenant_id="t", user_id="u",
        )

    def test_valid_skill_passes(self):
        gate = QualityGate()
        skill = self._make_skill()
        report = gate.rule_check(skill)
        self.assertEqual(report.errors, 0)
        self.assertGreaterEqual(report.score, 60)

    def test_empty_content_fails(self):
        gate = QualityGate()
        skill = self._make_skill(content="short")
        report = gate.rule_check(skill)
        self.assertGreater(report.errors, 0)
        self.assertLess(report.score, 60)

    def test_bad_name_format_fails(self):
        gate = QualityGate()
        skill = self._make_skill(name="Deploy Flow WITH CAPS")
        report = gate.rule_check(skill)
        self.assertGreater(report.errors, 0)

    def test_no_steps_fails(self):
        gate = QualityGate()
        skill = self._make_skill(content="# Deploy\n\nJust deploy it somehow. No steps here at all, just a paragraph of text that describes nothing actionable.")
        report = gate.rule_check(skill)
        self.assertGreater(report.errors, 0)

    def test_empty_description_fails(self):
        gate = QualityGate()
        skill = self._make_skill(description="")
        report = gate.rule_check(skill)
        self.assertGreater(report.errors, 0)


class TestQualityGateEvaluate(unittest.IsolatedAsyncioTestCase):

    async def test_evaluate_without_llm(self):
        """Without LLM, only rule checks run."""
        gate = QualityGate(llm=None)
        skill = SkillRecord(
            skill_id="sk-001", name="deploy-flow",
            description="Standard deploy workflow",
            content="# Deploy\n\n1. Build\n2. Test\n3. Deploy to staging",
            category=SkillCategory.WORKFLOW,
            tenant_id="t", user_id="u",
        )
        report = await gate.evaluate(skill)
        self.assertIsInstance(report, QualityReport)
        self.assertGreaterEqual(report.score, 60)

    async def test_evaluate_with_llm(self):
        """With LLM, semantic checks also run."""
        async def mock_llm(msgs):
            return '{"actionable": true, "consistent": true, "specific": true, "duplicate": false}'

        gate = QualityGate(llm=mock_llm)
        skill = SkillRecord(
            skill_id="sk-001", name="deploy-flow",
            description="Standard deploy",
            content="# Deploy\n\n1. Build\n2. Test\n3. Deploy",
            category=SkillCategory.WORKFLOW,
            tenant_id="t", user_id="u",
        )
        report = await gate.evaluate(skill)
        self.assertGreaterEqual(report.score, 60)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Implement quality_gate.py**

Create `src/opencortex/skill_engine/quality_gate.py`:

```python
"""
Quality Gate — dual-layer validation for skill drafts.

Layer 1: Rule-based deterministic checks (name format, content length, etc.)
Layer 2: LLM-based semantic checks (actionability, consistency, specificity)

Runs on concrete SkillRecord AFTER evolution, BEFORE saving.
"""

import logging
import re
from typing import List, Optional

from opencortex.skill_engine.types import (
    SkillRecord, SkillCategory, QualityCheck, QualityReport,
)

logger = logging.getLogger(__name__)

NAME_PATTERN = re.compile(r'^[a-z0-9]+(-[a-z0-9]+)*$')
STEP_PATTERN = re.compile(r'(?:^\d+\.|^#{1,3}\s+Step\s+\d)', re.MULTILINE)


class QualityGate:
    def __init__(self, llm=None, existing_skills: Optional[List[SkillRecord]] = None):
        self._llm = llm
        self._existing = existing_skills or []

    def rule_check(self, skill: SkillRecord) -> QualityReport:
        """Layer 1: deterministic rule checks."""
        checks = []

        # Name format
        name_ok = bool(NAME_PATTERN.match(skill.name)) and len(skill.name) <= 50
        checks.append(QualityCheck(
            name="name_format", severity="ERROR", passed=name_ok,
            message="OK" if name_ok else f"Name '{skill.name}' must be lowercase-hyphenated, <= 50 chars",
        ))

        # Content not empty
        content_ok = len(skill.content) > 50
        checks.append(QualityCheck(
            name="content_length", severity="ERROR", passed=content_ok,
            message="OK" if content_ok else f"Content too short ({len(skill.content)} chars, need > 50)",
            fix_suggestion="Add detailed steps to the skill content",
        ))

        # Has steps
        has_steps = bool(STEP_PATTERN.search(skill.content))
        checks.append(QualityCheck(
            name="has_steps", severity="ERROR", passed=has_steps,
            message="OK" if has_steps else "Content must contain numbered steps or ## Step sections",
        ))

        # Has description
        desc_ok = bool(skill.description and len(skill.description.strip()) > 0)
        checks.append(QualityCheck(
            name="has_description", severity="ERROR", passed=desc_ok,
            message="OK" if desc_ok else "Description is empty",
        ))

        # Category valid (dynamic from enum)
        cat_ok = skill.category.value in SkillCategory.__members__
        checks.append(QualityCheck(
            name="category_valid", severity="ERROR", passed=cat_ok,
            message="OK" if cat_ok else f"Invalid category: {skill.category}",
        ))

        # Token budget (WARNING)
        token_ok = len(skill.content) < 20000
        checks.append(QualityCheck(
            name="token_budget", severity="WARNING", passed=token_ok,
            message="OK" if token_ok else f"Content too long ({len(skill.content)} chars, limit ~20K)",
        ))

        # Empty sections (WARNING)
        empty_sections = bool(re.search(r'##\s+\S+.*\n##\s+\S+', skill.content))
        checks.append(QualityCheck(
            name="no_empty_sections", severity="WARNING", passed=not empty_sections,
            message="OK" if not empty_sections else "Found empty sections (## followed immediately by ##)",
        ))

        errors = sum(1 for c in checks if not c.passed and c.severity == "ERROR")
        warnings = sum(1 for c in checks if not c.passed and c.severity == "WARNING")
        score = max(0, min(100, 100 - errors * 20 - warnings * 5))

        return QualityReport(score=score, checks=checks, errors=errors, warnings=warnings)

    async def evaluate(self, skill: SkillRecord) -> QualityReport:
        """Full evaluation: rule checks + optional LLM semantic checks."""
        report = self.rule_check(skill)

        if self._llm and report.score >= 40:
            try:
                semantic = await self._semantic_check(skill)
                report.checks.extend(semantic)
                sem_errors = sum(1 for c in semantic if not c.passed and c.severity == "ERROR")
                sem_warnings = sum(1 for c in semantic if not c.passed and c.severity == "WARNING")
                report.errors += sem_errors
                report.warnings += sem_warnings
                report.score = max(0, min(100, report.score - sem_errors * 20 - sem_warnings * 5))
            except Exception as exc:
                logger.warning("[QualityGate] LLM semantic check failed: %s", exc)

        return report

    async def _semantic_check(self, skill: SkillRecord) -> List[QualityCheck]:
        """Layer 2: LLM semantic checks."""
        prompt = f"""Evaluate this skill for quality. Return JSON with boolean fields:
- actionable: Can an agent follow these steps without ambiguity?
- consistent: Does the description match the steps?
- specific: Are steps concrete, not vague platitudes?
- duplicate: Does this duplicate common knowledge that doesn't need a skill?

Skill name: {skill.name}
Description: {skill.description}
Content:
{skill.content[:3000]}

Return ONLY valid JSON: {{"actionable": true/false, "consistent": true/false, "specific": true/false, "duplicate": true/false}}"""

        import orjson
        response = await self._llm([{"role": "user", "content": prompt}])
        data = orjson.loads(response)

        checks = []
        checks.append(QualityCheck(
            name="actionability", severity="ERROR",
            passed=data.get("actionable", True),
            message="OK" if data.get("actionable") else "Steps are ambiguous or unactionable",
        ))
        checks.append(QualityCheck(
            name="consistency", severity="WARNING",
            passed=data.get("consistent", True),
            message="OK" if data.get("consistent") else "Description doesn't match content",
        ))
        checks.append(QualityCheck(
            name="specificity", severity="WARNING",
            passed=data.get("specific", True),
            message="OK" if data.get("specific") else "Steps are too vague",
        ))
        checks.append(QualityCheck(
            name="overlap", severity="ERROR",
            passed=not data.get("duplicate", False),
            message="OK" if not data.get("duplicate") else "Skill duplicates common knowledge",
        ))

        return checks
```

- [ ] **Step 3: Wire into SkillManager.extract()**

Edit `src/opencortex/skill_engine/skill_manager.py`. Change `__init__`:

```python
    def __init__(self, store, analyzer=None, evolver=None, quality_gate=None, sandbox_tdd=None):
        self._store = store
        self._analyzer = analyzer
        self._evolver = evolver
        self._quality_gate = quality_gate
        self._sandbox_tdd = sandbox_tdd
```

Replace the extract method's save loop (around line 119-134):

```python
        saved = []
        for c in candidates:
            # Phase A: Quality Gate
            if self._quality_gate:
                report = await self._quality_gate.evaluate(c)
                c.quality_score = report.score
                if report.score < 60:
                    logger.info("[SkillManager] %s failed quality gate (score=%d)",
                                c.name, report.score)
                    continue

            # Phase B: Sandbox TDD (if enabled)
            if self._sandbox_tdd:
                tdd_result = await self._sandbox_tdd.evaluate(c)
                c.tdd_passed = tdd_result.passed
                if not tdd_result.passed:
                    logger.info("[SkillManager] %s failed sandbox TDD", c.name)
                    continue

            await self._store.save_record(c)
            saved.append(c)

        return saved
```

- [ ] **Step 4: Run tests**

Run: `uv run python3 -m unittest tests.skill_engine.test_quality_gate -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add src/opencortex/skill_engine/quality_gate.py src/opencortex/skill_engine/skill_manager.py tests/skill_engine/test_quality_gate.py
git commit -m "feat(skill_engine): add Quality Gate (Phase A) with rule + LLM dual-layer check"
```

---

### Task 3: Sandbox TDD (Phase B)

**Files:**
- Create: `src/opencortex/skill_engine/sandbox_tdd.py`
- Modify: `src/opencortex/config.py:64-66`
- Test: `tests/skill_engine/test_sandbox_tdd.py`

- [ ] **Step 1: Write tests**

Create `tests/skill_engine/test_sandbox_tdd.py`:

```python
import unittest
from unittest.mock import AsyncMock
from opencortex.skill_engine.types import (
    SkillRecord, SkillCategory, TDDResult,
)
from opencortex.skill_engine.sandbox_tdd import SandboxTDD


class TestSandboxTDD(unittest.IsolatedAsyncioTestCase):

    def _make_skill(self):
        return SkillRecord(
            skill_id="sk-001", name="deploy-flow",
            description="Standard deploy workflow",
            content="# Deploy\n\n1. Build the project\n2. Run all tests\n3. Deploy to staging\n4. Verify health check",
            category=SkillCategory.WORKFLOW,
            tenant_id="t", user_id="u",
        )

    async def test_passes_when_skill_improves_behavior(self):
        call_count = 0
        async def mock_llm(msgs):
            nonlocal call_count
            call_count += 1
            content = msgs[-1]["content"] if msgs else ""
            if "Generate 2-3" in content:
                return '[{"scenario": "Deploy under time pressure", "correct": "A"}]'
            if "operational skill" in content:
                return '{"choice": "A", "reasoning": "Following the deploy skill steps", "sections_cited": ["Steps"]}'
            return '{"choice": "B", "reasoning": "Just deploy directly"}'

        tdd = SandboxTDD(llm=mock_llm, max_llm_calls=20)
        result = await tdd.evaluate(self._make_skill())
        self.assertTrue(result.passed)
        self.assertGreater(result.scenarios_improved, 0)

    async def test_fails_when_skill_makes_worse(self):
        async def mock_llm(msgs):
            content = msgs[-1]["content"] if msgs else ""
            if "Generate 2-3" in content:
                return '[{"scenario": "Deploy scenario", "correct": "A"}]'
            if "operational skill" in content:
                return '{"choice": "C", "reasoning": "Skill confused me"}'
            return '{"choice": "A", "reasoning": "Common sense"}'

        tdd = SandboxTDD(llm=mock_llm, max_llm_calls=20)
        result = await tdd.evaluate(self._make_skill())
        self.assertFalse(result.passed)
        self.assertGreater(result.scenarios_worse, 0)

    async def test_respects_llm_budget(self):
        call_count = 0
        async def mock_llm(msgs):
            nonlocal call_count
            call_count += 1
            return '[]'

        tdd = SandboxTDD(llm=mock_llm, max_llm_calls=3)
        result = await tdd.evaluate(self._make_skill())
        self.assertLessEqual(call_count, 3)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Implement sandbox_tdd.py**

Create `src/opencortex/skill_engine/sandbox_tdd.py`:

```python
"""
Sandbox TDD — LLM-simulated RED-GREEN-REFACTOR for skill validation.

Generates pressure scenarios, runs baseline (no skill) vs with-skill,
compares behavior. Default OFF — enable via config.

Spec §5.
"""

import logging
from typing import List, Optional

import orjson

from opencortex.skill_engine.types import SkillRecord, TDDResult

logger = logging.getLogger(__name__)

MAX_REFACTOR_ITERATIONS = 2


class SandboxTDD:
    def __init__(self, llm, max_llm_calls: int = 20):
        self._llm = llm
        self._max_calls = max_llm_calls
        self._calls_used = 0

    async def evaluate(self, skill: SkillRecord) -> TDDResult:
        """Run RED-GREEN-REFACTOR cycle on a skill."""
        self._calls_used = 0

        # Step 1: Generate scenarios
        scenarios = await self._generate_scenarios(skill)
        if not scenarios:
            return TDDResult(passed=False, llm_calls_used=self._calls_used)

        # Step 2: RED — Baseline
        baseline = {}
        for s in scenarios:
            if self._calls_used >= self._max_calls:
                break
            baseline[s["scenario"]] = await self._run_baseline(s["scenario"])

        # Step 3: GREEN — With skill
        with_skill = {}
        for s in scenarios:
            if self._calls_used >= self._max_calls:
                break
            with_skill[s["scenario"]] = await self._run_with_skill(
                s["scenario"], skill.content,
            )

        # Step 4: Compare
        improved = 0
        same = 0
        worse = 0
        sections_cited = []
        rationalizations = []

        for s in scenarios:
            sc = s["scenario"]
            correct = s.get("correct", "A")
            b = baseline.get(sc, {})
            w = with_skill.get(sc, {})

            b_choice = b.get("choice", "")
            w_choice = w.get("choice", "")
            w_cited = w.get("sections_cited", [])

            if w_choice == correct and b_choice != correct:
                improved += 1
            elif w_choice != correct and b_choice == correct:
                worse += 1
                rationalizations.append(w.get("reasoning", ""))
            else:
                same += 1

            sections_cited.extend(w_cited)

        total = len(scenarios)
        passed = (improved >= total * 0.5) and (worse == 0)
        delta = improved / total if total > 0 else 0.0

        return TDDResult(
            passed=passed,
            scenarios_total=total,
            scenarios_improved=improved,
            scenarios_same=same,
            scenarios_worse=worse,
            sections_cited=sections_cited,
            rationalizations=rationalizations,
            quality_delta=delta,
            llm_calls_used=self._calls_used,
        )

    async def _llm_call(self, prompt: str) -> str:
        self._calls_used += 1
        return await self._llm([{"role": "user", "content": prompt}])

    async def _generate_scenarios(self, skill: SkillRecord) -> List[dict]:
        prompt = f"""Given this skill about {skill.name}:
{skill.content[:2000]}

Generate 2-3 realistic scenarios that test whether an agent would follow this skill correctly. Each scenario should:
- Present a concrete situation with A/B/C options
- Include time pressure or competing priorities
- Have one clearly correct answer per the skill
- Be answerable without external tools

Return JSON array: [{{"scenario": "...", "correct": "A"}}]"""

        try:
            resp = await self._llm_call(prompt)
            return orjson.loads(resp)
        except Exception:
            return []

    async def _run_baseline(self, scenario: str) -> dict:
        prompt = f"""You are an AI assistant. A user asks:
{scenario}
Choose an option and explain your reasoning.
Return JSON: {{"choice": "A/B/C", "reasoning": "..."}}"""
        try:
            return orjson.loads(await self._llm_call(prompt))
        except Exception:
            return {}

    async def _run_with_skill(self, scenario: str, skill_content: str) -> dict:
        prompt = f"""You are an AI assistant with this operational skill:
{skill_content[:2000]}

A user asks:
{scenario}
Choose an option and explain your reasoning. Cite which sections of the skill guided your choice.
Return JSON: {{"choice": "A/B/C", "reasoning": "...", "sections_cited": ["..."]}}"""
        try:
            return orjson.loads(await self._llm_call(prompt))
        except Exception:
            return {}
```

- [ ] **Step 3: Add config knobs**

Edit `src/opencortex/config.py`, in `CortexAlphaConfig` after `knowledge_recall_enabled` (line 66):

```python
    # Skill Engine
    sandbox_tdd_enabled: bool = False          # Default OFF — LLM cost control
    sandbox_tdd_max_llm_calls: int = 20        # Budget cap per extraction batch
```

- [ ] **Step 4: Run tests**

Run: `uv run python3 -m unittest tests.skill_engine.test_sandbox_tdd -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add src/opencortex/skill_engine/sandbox_tdd.py src/opencortex/config.py tests/skill_engine/test_sandbox_tdd.py
git commit -m "feat(skill_engine): add Sandbox TDD (Phase B) with LLM simulation RED-GREEN compare"
```

---

### Task 4: SkillEventStore (Phase C Foundation)

**Files:**
- Create: `src/opencortex/skill_engine/event_store.py`
- Modify: `src/opencortex/storage/collection_schemas.py`
- Test: `tests/skill_engine/test_event_store.py`

- [ ] **Step 1: Write tests**

Create `tests/skill_engine/test_event_store.py`:

```python
import unittest
from unittest.mock import AsyncMock
from opencortex.skill_engine.types import SkillEvent
from opencortex.skill_engine.event_store import SkillEventStore


class TestSkillEventStore(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.storage = AsyncMock()
        self.storage.create_collection = AsyncMock(return_value=True)
        self.store = SkillEventStore(storage=self.storage)

    def _make_event(self, event_id="ev1", event_type="selected", evaluated=False):
        return SkillEvent(
            event_id=event_id, session_id="s1", turn_id="t1",
            skill_id="sk-001", skill_uri="opencortex://t/u/skills/workflow/sk-001",
            tenant_id="team1", user_id="hugo",
            event_type=event_type, evaluated=evaluated,
        )

    async def test_append(self):
        await self.store.append(self._make_event())
        self.storage.upsert.assert_called_once()

    async def test_list_by_session(self):
        self.storage.filter = AsyncMock(return_value=[
            self._make_event().to_dict(),
        ])
        events = await self.store.list_by_session("s1", "team1", "hugo")
        self.assertEqual(len(events), 1)
        self.assertIsInstance(events[0], SkillEvent)

    async def test_mark_evaluated(self):
        await self.store.mark_evaluated(["ev1", "ev2"])
        self.assertEqual(self.storage.update.call_count, 2)

    async def test_list_unevaluated(self):
        self.storage.filter = AsyncMock(return_value=[
            {**self._make_event().to_dict(), "evaluated": False},
        ])
        events = await self.store.list_unevaluated("team1")
        self.assertEqual(len(events), 1)
        self.assertFalse(events[0].evaluated)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Implement event_store.py**

Create `src/opencortex/skill_engine/event_store.py`:

```python
"""
SkillEventStore — durable skill usage events in independent Qdrant collection.

Events are keyed by (tenant_id, user_id, session_id) to prevent cross-user
collision. No vectors — pure metadata store.

Spec §6.0.
"""

import logging
from typing import Any, Dict, List

from opencortex.skill_engine.types import SkillEvent

logger = logging.getLogger(__name__)

SKILL_EVENTS_COLLECTION = "skill_events"


class SkillEventStore:
    def __init__(self, storage, collection_name: str = SKILL_EVENTS_COLLECTION):
        self._storage = storage
        self._collection = collection_name

    async def init(self) -> None:
        """Create skill_events collection if not exists."""
        from opencortex.storage.collection_schemas import init_skill_events_collection
        await init_skill_events_collection(self._storage, self._collection)

    async def append(self, event: SkillEvent) -> None:
        """Persist a skill event."""
        payload = event.to_dict()
        payload["id"] = event.event_id
        await self._storage.upsert(self._collection, payload)

    async def list_by_session(
        self, session_id: str, tenant_id: str, user_id: str,
    ) -> List[SkillEvent]:
        """List events for a session (tenant+user+session isolated)."""
        filter_expr = {"op": "and", "conds": [
            {"op": "must", "field": "session_id", "conds": [session_id]},
            {"op": "must", "field": "tenant_id", "conds": [tenant_id]},
            {"op": "must", "field": "user_id", "conds": [user_id]},
        ]}
        results = await self._storage.filter(self._collection, filter_expr, limit=200)
        return [self._dict_to_event(r) for r in results]

    async def mark_evaluated(self, event_ids: List[str]) -> None:
        """Mark events as evaluated (idempotency guard)."""
        for eid in event_ids:
            await self._storage.update(self._collection, eid, {"evaluated": True})

    async def list_unevaluated(
        self, tenant_id: str, limit: int = 100,
    ) -> List[SkillEvent]:
        """List unevaluated events for crash recovery sweeper."""
        filter_expr = {"op": "and", "conds": [
            {"op": "must", "field": "tenant_id", "conds": [tenant_id]},
            {"op": "must", "field": "evaluated", "conds": [False]},
        ]}
        results = await self._storage.filter(self._collection, filter_expr, limit=limit)
        return [self._dict_to_event(r) for r in results]

    def _dict_to_event(self, d: Dict[str, Any]) -> SkillEvent:
        return SkillEvent(
            event_id=d.get("event_id", d.get("id", "")),
            session_id=d.get("session_id", ""),
            turn_id=d.get("turn_id", ""),
            skill_id=d.get("skill_id", ""),
            skill_uri=d.get("skill_uri", ""),
            tenant_id=d.get("tenant_id", ""),
            user_id=d.get("user_id", ""),
            event_type=d.get("event_type", ""),
            outcome=d.get("outcome", ""),
            timestamp=d.get("timestamp", ""),
            evaluated=d.get("evaluated", False),
        )
```

- [ ] **Step 3: Add skill_events collection schema**

Edit `src/opencortex/storage/collection_schemas.py`. Add to `CollectionSchemas` class:

```python
    @staticmethod
    def skill_events_collection(name: str) -> Dict[str, Any]:
        """Skill events collection — no vectors, pure metadata."""
        return {
            "CollectionName": name,
            "Fields": [
                {"FieldName": "event_id", "FieldType": "string"},
                {"FieldName": "session_id", "FieldType": "string"},
                {"FieldName": "turn_id", "FieldType": "string"},
                {"FieldName": "skill_id", "FieldType": "string"},
                {"FieldName": "skill_uri", "FieldType": "string"},
                {"FieldName": "tenant_id", "FieldType": "string"},
                {"FieldName": "user_id", "FieldType": "string"},
                {"FieldName": "event_type", "FieldType": "string"},
                {"FieldName": "outcome", "FieldType": "string"},
                {"FieldName": "evaluated", "FieldType": "bool"},
                {"FieldName": "timestamp", "FieldType": "date_time"},
            ],
            "ScalarIndex": [
                "event_id", "session_id", "turn_id", "skill_id",
                "tenant_id", "user_id", "event_type", "outcome",
                "evaluated", "timestamp",
            ],
        }
```

Add init function:

```python
async def init_skill_events_collection(
    storage: StorageInterface, name: str,
) -> bool:
    """Initialize the skill events collection."""
    schema = CollectionSchemas.skill_events_collection(name)
    return await storage.create_collection(name, schema)
```

- [ ] **Step 4: Run tests**

Run: `uv run python3 -m unittest tests.skill_engine.test_event_store -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add src/opencortex/skill_engine/event_store.py src/opencortex/storage/collection_schemas.py tests/skill_engine/test_event_store.py
git commit -m "feat(skill_engine): add SkillEventStore (Phase C foundation) with independent collection"
```

---

### Task 5: ContextManager Integration (Selection + Citation Tracking)

**Files:**
- Modify: `src/opencortex/context/manager.py:161-190` (_prepare) and `380-396` (_commit)
- Test: `tests/skill_engine/test_context_tracking.py`

- [ ] **Step 1: Write tests**

Create `tests/skill_engine/test_context_tracking.py`:

```python
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from opencortex.skill_engine.types import SkillEvent


class TestSelectionTracking(unittest.TestCase):
    """Verify _prepare records SkillEvents for returned skills."""

    def test_skill_event_created_for_selected_skill(self):
        """SkillEvent with event_type='selected' should be created."""
        e = SkillEvent(
            event_id="ev1", session_id="s1", turn_id="t1",
            skill_id="sk-001",
            skill_uri="opencortex://t/u/skills/workflow/sk-001",
            tenant_id="t", user_id="u", event_type="selected",
        )
        self.assertEqual(e.event_type, "selected")
        self.assertFalse(e.evaluated)


class TestCitationValidation(unittest.TestCase):
    """Verify citation is only accepted for server-selected skills."""

    def test_valid_citation_accepted(self):
        """URI in server_selected set is accepted."""
        server_selected = {"opencortex://t/u/skills/workflow/sk-001"}
        uri = "opencortex://t/u/skills/workflow/sk-001"
        self.assertIn(uri, server_selected)

    def test_forged_citation_rejected(self):
        """URI NOT in server_selected set is rejected."""
        server_selected = {"opencortex://t/u/skills/workflow/sk-001"}
        forged = "opencortex://t/other/skills/workflow/sk-999"
        self.assertNotIn(forged, server_selected)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Add _selected_skill_uris dict to ContextManager**

In `src/opencortex/context/manager.py`, in `__init__` (around line 60), add:

```python
        self._selected_skill_uris: Dict[SessionKey, set] = {}
```

- [ ] **Step 3: Add selection tracking to _prepare()**

In `_prepare()`, after the search call and result formatting (around line 275, after `result = {...}`), add:

```python
        # Track selected skills for citation validation in _commit()
        if hasattr(self._orchestrator, '_skill_event_store') and self._orchestrator._skill_event_store:
            find_result_skills = find_result.skills if find_result else []
            if find_result_skills:
                selected_uris = set()
                for mc in find_result_skills:
                    selected_uris.add(mc.uri)
                    try:
                        from opencortex.skill_engine.types import SkillEvent
                        from uuid import uuid4
                        from datetime import datetime, timezone
                        await self._orchestrator._skill_event_store.append(SkillEvent(
                            event_id=uuid4().hex,
                            session_id=session_id,
                            turn_id=turn_id,
                            skill_id=mc.uri.split("/")[-1],
                            skill_uri=mc.uri,
                            tenant_id=tenant_id,
                            user_id=user_id,
                            event_type="selected",
                            timestamp=datetime.now(timezone.utc).isoformat(),
                        ))
                    except Exception:
                        pass
                self._selected_skill_uris[sk] = selected_uris
```

- [ ] **Step 4: Add citation validation to _commit()**

In `_commit()`, after the existing `cited_uris` reward block (around line 396), add:

```python
        # Skill citation tracking (validated against server-selected set)
        if cited_uris and hasattr(self._orchestrator, '_skill_event_store') and self._orchestrator._skill_event_store:
            skill_uris = [u for u in cited_uris if "/skills/" in u]
            server_selected = self._selected_skill_uris.get(sk, set())
            for uri in skill_uris:
                if uri not in server_selected:
                    logger.debug("[ContextManager] Dropped forged skill citation: %s", uri)
                    continue
                try:
                    from opencortex.skill_engine.types import SkillEvent
                    from uuid import uuid4
                    from datetime import datetime, timezone
                    await self._orchestrator._skill_event_store.append(SkillEvent(
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
                except Exception:
                    pass
```

- [ ] **Step 5: Run tests**

Run: `uv run python3 -m unittest tests.skill_engine.test_context_tracking tests.test_context_manager -v`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add src/opencortex/context/manager.py tests/skill_engine/test_context_tracking.py
git commit -m "feat(skill_engine): add selection + citation tracking in ContextManager (Phase C)"
```

---

### Task 6: SkillEvaluator + Orchestrator Wiring + Sweeper

**Files:**
- Create: `src/opencortex/skill_engine/evaluator.py`
- Modify: `src/opencortex/orchestrator.py:321-367` (_init_skill_engine) and session_end
- Test: `tests/skill_engine/test_evaluator.py`

- [ ] **Step 1: Write tests**

Create `tests/skill_engine/test_evaluator.py`:

```python
import unittest
from unittest.mock import AsyncMock, MagicMock
from opencortex.skill_engine.types import SkillEvent
from opencortex.skill_engine.evaluator import SkillEvaluator


class TestSkillEvaluator(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.event_store = AsyncMock()
        self.skill_store = AsyncMock()
        self.trace_store = AsyncMock()
        self.skill_storage = AsyncMock()
        self.evaluator = SkillEvaluator(
            event_store=self.event_store,
            skill_store=self.skill_store,
            trace_store=self.trace_store,
            skill_storage=self.skill_storage,
        )

    def _make_event(self, event_type="selected", evaluated=False):
        return SkillEvent(
            event_id="ev1", session_id="s1", turn_id="t1",
            skill_id="sk-001", skill_uri="opencortex://t/u/skills/workflow/sk-001",
            tenant_id="team1", user_id="hugo",
            event_type=event_type, evaluated=evaluated,
        )

    async def test_skips_when_no_events(self):
        self.event_store.list_by_session = AsyncMock(return_value=[])
        await self.evaluator.evaluate_session("team1", "hugo", "s1")
        self.skill_store.record_selection.assert_not_called()

    async def test_skips_already_evaluated(self):
        self.event_store.list_by_session = AsyncMock(return_value=[
            self._make_event(evaluated=True),
        ])
        await self.evaluator.evaluate_session("team1", "hugo", "s1")
        self.skill_store.record_selection.assert_not_called()

    async def test_records_selection_for_selected_event(self):
        self.event_store.list_by_session = AsyncMock(return_value=[
            self._make_event(event_type="selected"),
        ])
        self.trace_store.list_by_session = AsyncMock(return_value=[
            {"outcome": "success"},
        ])
        self.event_store.mark_evaluated = AsyncMock()
        await self.evaluator.evaluate_session("team1", "hugo", "s1")
        self.skill_store.record_selection.assert_called_once_with("sk-001")

    async def test_records_application_for_cited_event(self):
        self.event_store.list_by_session = AsyncMock(return_value=[
            self._make_event(event_type="cited"),
        ])
        self.trace_store.list_by_session = AsyncMock(return_value=[
            {"outcome": "success"},
        ])
        self.event_store.mark_evaluated = AsyncMock()
        await self.evaluator.evaluate_session("team1", "hugo", "s1")
        self.skill_store.record_application.assert_called_once_with("sk-001", True)

    async def test_marks_events_evaluated(self):
        self.event_store.list_by_session = AsyncMock(return_value=[
            self._make_event(event_type="selected"),
        ])
        self.trace_store.list_by_session = AsyncMock(return_value=[])
        self.event_store.mark_evaluated = AsyncMock()
        await self.evaluator.evaluate_session("team1", "hugo", "s1")
        self.event_store.mark_evaluated.assert_called_once()


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Implement evaluator.py**

Create `src/opencortex/skill_engine/evaluator.py`:

```python
"""
SkillEvaluator — correlates skill events with trace outcomes.

Updates selection/application counters and reward scores.
Includes startup sweeper for crash recovery.

Spec §6.3 + §6.3.1.
"""

import asyncio
import logging
from collections import defaultdict
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class SkillEvaluator:
    def __init__(self, event_store, skill_store, trace_store,
                 skill_storage=None, llm=None):
        self._event_store = event_store
        self._skill_store = skill_store
        self._trace_store = trace_store
        self._skill_storage = skill_storage
        self._llm = llm
        self._tenant_locks: Dict[str, asyncio.Lock] = {}

    def _get_lock(self, tenant_id: str) -> asyncio.Lock:
        if tenant_id not in self._tenant_locks:
            self._tenant_locks[tenant_id] = asyncio.Lock()
        return self._tenant_locks[tenant_id]

    async def evaluate_session(
        self, tenant_id: str, user_id: str, session_id: str,
    ) -> None:
        """Evaluate skill usage for a completed session."""
        async with self._get_lock(tenant_id):
            try:
                await self._evaluate_session_inner(tenant_id, user_id, session_id)
            except Exception as exc:
                logger.warning(
                    "[SkillEvaluator] Failed for session %s: %s",
                    session_id, exc,
                )

    async def _evaluate_session_inner(
        self, tenant_id: str, user_id: str, session_id: str,
    ) -> None:
        events = await self._event_store.list_by_session(
            session_id, tenant_id, user_id,
        )
        unevaluated = [e for e in events if not e.evaluated]
        if not unevaluated:
            return

        # Fetch traces for outcome correlation
        traces = await self._trace_store.list_by_session(
            session_id, tenant_id, user_id,
        )
        session_outcome = "success" if any(
            t.get("outcome") == "success" for t in traces
        ) else "failure" if traces else ""

        # Group events by skill_id
        skill_events: Dict[str, list] = defaultdict(list)
        for e in unevaluated:
            skill_events[e.skill_id].append(e)

        # Update metrics per skill
        for skill_id, events_for_skill in skill_events.items():
            was_selected = any(e.event_type == "selected" for e in events_for_skill)
            was_cited = any(e.event_type == "cited" for e in events_for_skill)

            if was_selected:
                await self._skill_store.record_selection(skill_id)

            if was_cited:
                completed = session_outcome == "success"
                await self._skill_store.record_application(skill_id, completed)

                # Reward scoring
                if self._skill_storage:
                    reward = 0.1 if completed else -0.05
                    await self._skill_storage.update_reward(skill_id, reward)

        # Mark events as evaluated
        await self._event_store.mark_evaluated(
            [e.event_id for e in unevaluated]
        )

    async def sweep_unevaluated(self, tenant_id: str) -> int:
        """Startup sweeper — process backlog from crash/restart."""
        try:
            backlog = await self._event_store.list_unevaluated(tenant_id, limit=200)
            if not backlog:
                return 0

            groups: Dict[tuple, list] = defaultdict(list)
            for e in backlog:
                groups[(e.session_id, e.user_id)].append(e)

            for (sid, uid), _ in groups.items():
                await self.evaluate_session(tenant_id, uid, sid)

            logger.info(
                "[SkillEvaluator] Swept %d backlog events across %d sessions",
                len(backlog), len(groups),
            )
            return len(backlog)
        except Exception as exc:
            logger.warning("[SkillEvaluator] Startup sweep failed: %s", exc)
            return 0
```

- [ ] **Step 3: Wire into orchestrator**

Edit `src/opencortex/orchestrator.py`. In `_init_skill_engine()` (around line 360), after creating SkillManager, add:

```python
            # SkillEventStore + Evaluator
            from opencortex.skill_engine.event_store import SkillEventStore
            from opencortex.skill_engine.evaluator import SkillEvaluator

            self._skill_event_store = SkillEventStore(storage=self._storage)
            await self._skill_event_store.init()

            self._skill_evaluator = SkillEvaluator(
                event_store=self._skill_event_store,
                skill_store=store,
                trace_store=self._trace_store,
                skill_storage=storage_adapter,
                llm=llm_adapter,
            )

            # Startup sweeper for crash recovery
            if tid := getattr(self, '_default_tenant_id', ''):
                asyncio.create_task(self._skill_evaluator.sweep_unevaluated(tid))
```

In `session_end()` (around line 2504, after archivist trigger), add:

```python
                    # Skill evaluator trigger
                    if hasattr(self, '_skill_evaluator') and self._skill_evaluator:
                        asyncio.create_task(
                            self._skill_evaluator.evaluate_session(tid, uid, session_id)
                        )
```

Also add `self._skill_event_store = None` and `self._skill_evaluator = None` in `__init__`.

- [ ] **Step 4: Run tests**

Run: `uv run python3 -m unittest tests.skill_engine.test_evaluator -v`
Expected: All PASS

- [ ] **Step 5: Run full regression**

Run: `uv run python3 -m unittest discover -s tests/skill_engine -v`
Expected: All PASS

Run: `uv run python3 -m unittest tests.test_alpha_types tests.test_alpha_config tests.test_context_manager -v`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add src/opencortex/skill_engine/evaluator.py src/opencortex/orchestrator.py tests/skill_engine/test_evaluator.py
git commit -m "feat(skill_engine): add SkillEvaluator + sweeper + orchestrator wiring (Phase C complete)"
```

---

### Task 7: Wire QualityGate + SandboxTDD into Orchestrator Init

**Files:**
- Modify: `src/opencortex/orchestrator.py:321-367` (_init_skill_engine)

- [ ] **Step 1: Update _init_skill_engine to create QualityGate + SandboxTDD**

In `_init_skill_engine()`, after creating `analyzer`, add:

```python
            # Quality Gate (Phase A)
            from opencortex.skill_engine.quality_gate import QualityGate
            quality_gate = QualityGate(llm=llm_adapter)

            # Sandbox TDD (Phase B — default OFF)
            sandbox_tdd = None
            if self._config.cortex_alpha.sandbox_tdd_enabled:
                from opencortex.skill_engine.sandbox_tdd import SandboxTDD
                sandbox_tdd = SandboxTDD(
                    llm=llm_adapter,
                    max_llm_calls=self._config.cortex_alpha.sandbox_tdd_max_llm_calls,
                )
```

Update the SkillManager constructor:

```python
            self._skill_manager = SkillManager(
                store=store, analyzer=analyzer, evolver=evolver,
                quality_gate=quality_gate, sandbox_tdd=sandbox_tdd,
            )
```

- [ ] **Step 2: Run full test suite**

Run: `uv run python3 -m unittest discover -s tests/skill_engine -v`
Expected: All PASS

- [ ] **Step 3: Commit**

```bash
git add src/opencortex/orchestrator.py
git commit -m "feat(skill_engine): wire QualityGate + SandboxTDD into orchestrator init"
```

---

### Task 8: Final Verification

- [ ] **Step 1: Run complete skill engine suite**

Run: `uv run python3 -m unittest discover -s tests/skill_engine -v`
Expected: All PASS

- [ ] **Step 2: Run full regression**

Run: `uv run python3 -m unittest tests.test_alpha_types tests.test_alpha_config tests.test_alpha_knowledge_store tests.test_alpha_sandbox_integration tests.test_knowledge_store tests.test_qdrant_adapter tests.test_context_manager -v`
Expected: All PASS

- [ ] **Step 3: Verify module independence**

Run: `grep -r "from opencortex.alpha\|from opencortex.context\|from opencortex.storage\|from opencortex.retrieve\|from opencortex.ingest" src/opencortex/skill_engine/ --include="*.py" | grep -v adapters/ | grep -v event_store`
Expected: No output

- [ ] **Step 4: Spec coverage check**

| Spec Section | Task |
|-------------|------|
| §4 Phase A: Quality Gate | Task 2 |
| §5 Phase B: Sandbox TDD | Task 3 |
| §6.0 SkillEvent Storage | Task 4 |
| §6.1 Selection Tracking | Task 5 |
| §6.2 Citation Validation | Task 5 |
| §6.3 Evaluator | Task 6 |
| §6.3.1 Startup Sweeper | Task 6 |
| §6.4 5-dim Rating | Task 1 (types), Task 6 (LLM rating — deferred to future) |
| §7 Data Model | Task 1 |
| §9 Modified Files | Tasks 1-7 |
