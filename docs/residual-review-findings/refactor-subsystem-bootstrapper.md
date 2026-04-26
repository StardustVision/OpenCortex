# Residual Review Findings

**Branch:** refactor/subsystem-bootstrapper
**Review run:** 20260426-222259-798a6189
**Plan:** docs/plans/2026-04-26-015-refactor-subsystem-bootstrapper-plan.md

## Residual Actionable Findings

### C2 — P1 — gated_auto
**File:** `src/opencortex/lifecycle/bootstrapper.py`
**Title:** Tests patch orchestrator delegates but bootstrapper calls self.method() directly
**Note:** Patching delegates becomes dead code. Phase 6 facade hardening should address test targeting.

### R1 — P1 — gated_auto
**File:** `src/opencortex/lifecycle/bootstrapper.py:710`
**Title:** Unprotected await in fire-and-forget _startup_maintenance
**Note:** Pre-existing. Wrap `_recover_pending_derives` in try/except matching surrounding pattern.

### R2 — P1 — gated_auto
**File:** `src/opencortex/lifecycle/bootstrapper.py:173,191,454`
**Title:** Three fire-and-forget create_task calls have no exception logging
**Note:** Pre-existing. Add done callbacks to surface unhandled exceptions.

### M3 — P2 — gated_auto
**File:** `src/opencortex/lifecycle/bootstrapper.py:498-502`
**Title:** Dead volcengine branch in _create_default_embedder
**Note:** Volcengine SDK removed 2026-03-22. Branch is dead code. Beyond MOVE scope but safe to remove.

## Advisory

- M1 (P1): Seven delegate pass-through methods are transient debt — Phase 6 facade hardening
- M2 (P1): Bootstrapper writes 40+ private attributes via self._orch._X — acknowledged trade-off
- R3 (P2): Entity index build task not tracked for shutdown cancellation — pre-existing
- R4 (P2): init() does not roll back partially-initialized state on failure — pre-existing
- M6 (P2): Triple import of retrieval_support in orchestrator.py — consolidate in Phase 6
