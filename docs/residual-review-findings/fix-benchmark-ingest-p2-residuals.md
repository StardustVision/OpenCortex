# Residual Review Findings — fix/benchmark-ingest-p2-residuals

**Source:** ce-code-review run `20260425-105317-823f3ead`
**Mode:** autofix (LFG pipeline)
**Plan:** `docs/plans/2026-04-25-004-refactor-benchmark-ingest-p2-residuals-plan.md`
**Verdict:** Ready to merge
**Branch HEAD:** `2c6a388`
**Run artifact:** `.context/compound-engineering/ce-code-review/20260425-105317-823f3ead/REVIEW.md`

This file is the durable handoff for residuals because no GitHub PR existed at the time of the review. When a PR is opened, copy these items into the PR body and delete this file.

---

## Residual Review Findings

### Should consider before merging this PR

- **[P3][manual] `src/opencortex/http/server.py:589-627` — api-contract-004-residual: knowledge/archivist routes still return `{"error": ...}`**
  Plan R2's scope was bench-collections only, so this is technically out of scope for this PR. But the same envelope drift exists in knowledge/archivist routes, and `web/src/api/client.ts:82-84` has a fragile special case parsing the `error` key for those routes. If you want to extend the envelope unification beyond bench-collections, do it in a small follow-up that touches both the route AND the JS consumer at the same time — don't leave the JS half-converted.
  *Suggested fix:* convert the routes to `HTTPException(detail=...)`, drop the `error` parsing in `web/src/api/client.ts`, run the frontend regression suite.

### Defer to a follow-up polish PR (small testing + style)

- **[P3][manual] `tests/test_http_server.py:test_04h` — T-001: HEAD verb missing from multi-verb shim test.** The 410 shim explicitly registers OPTIONS and FastAPI auto-generates HEAD; the test loop covers GET/PUT/PATCH/DELETE only. Add HEAD + OPTIONS to the loop to lock the contract.

- **[P3][manual] `tests/` — T-002: `_hydrate_record_contents(overrides=...)` branch coverage gap.** Existing test_04d/04e cover end-to-end content correctness; the override short-circuit / fs-None / mixed paths are not directly exercised. Add a unit test that asserts no FS call happens when every URI is in `overrides`.

- **[P3][manual] `tests/test_http_server.py:test_04i` — T-003: envelope lock incomplete.** The test asserts `"detail" in body` and `"error" not in body` only for the 400 paths; 409 (dict detail) and 504 envelopes have no equivalent regression assertion. Add the same lock to test_04d (409 hash conflict) and test_15 (504 timeout — if exists, otherwise add).

- **[P3][manual] `tests/` — T-005: legacy-hash migration scenario untested.** CHANGELOG documents a one-time 409 on pre-existing source records ingested before U6's hash canonicalization. No test simulates this — to lock it down, write a record with a stale-format hash directly into storage, replay, assert 409 with both hashes in the detail.

- **[P3][manual] `src/opencortex/context/recomposition_types.py` — T-006: TypedDict is documentation-only at runtime.** No mypy / pyright in CI. The drift-detection value of the new TypedDict is theoretical until a type-check pass lands. Either add `mypy --strict src/opencortex/context/manager.py` to the test pipeline or accept the limitation.

- **[P3][manual] `src/opencortex/context/manager.py` consumers of RecompositionEntry — KP-10: defensive casting still present.** Consumers wrap reads in `int(entry["msg_start"])` / `str(entry["uri"])` even though the TypedDict guarantees the types. Drop the casts to actually rely on the type contract.

- **[P3][manual] `src/opencortex/context/recomposition_types.py` — KP-11: consider `@dataclass(frozen=True, slots=True)` instead of TypedDict.** Would give immutability + free `__repr__`/equality + same module-isolation benefit. TypedDict is defensible; dataclass would be stronger if you want runtime help, not just typing.

- **[P3][manual] `src/opencortex/context/manager.py:1170` — KP-12: `_canonicalize_for_hash` sort key over-engineered.** Sort key `(x is None, str(x))` defends against mixed-type lists that the inline comment says don't really occur in practice. Could simplify to `sorted(value, key=str)`.

- **[P3][manual] `src/opencortex/context/manager.py:1492` — KP-14: `overrides = overrides or {}` rebinds parameter.** Minor style; rebind to a new local for readability.

- **[P3][manual] `tests/test_http_server.py:1029` — ps-001: test method file ordering.** New `test_04i` is before `test_04h`, and both before pre-existing `test_04g`. Cosmetic; unittest doesn't depend on order. Reorder to match file-wide alphabetic suffix convention.

- **[P3][advisory] `src/opencortex/context/manager.py` + `src/opencortex/http/admin_routes.py` — M-RR1: inline `(REVIEW …)` tags pattern continues to grow.** This PR adds 7 more tags; repo-wide src/ count now 21. Strip-at-merge convention or move tags to git trailers in a future polish PR.

- **[P3][manual] `CHANGELOG.md` — M-RR4: no documented promotion ritual.** New file with no prior repo convention; relationship to `MEMORY.md` and the release-cycle ritual ("who promotes Unreleased?") undocumented. Add a brief CONTRIBUTING.md note.

- **[P3][manual] `src/opencortex/http/server.py:328-358` — M-RR5: 410 shim needs grep-able TODO marker.** CHANGELOG says removal in v0.8.0; add a `# TODO(v0.8.0): remove this shim` so the eventual cleanup is automatable rather than prose-driven.

---

## Applied This Run (autofix)

| Finding | File | Change |
|---|---|---|
| KP-09 | `src/opencortex/http/models.py` | Drop `orjson.JSONEncodeError` from except tuple — it IS `TypeError`. Inline note added. |
| KP-13 | `src/opencortex/context/manager.py` | `tuple[str, str]` → `Tuple[str, str]` for file-wide consistency. |

Committed as `2c6a388 fix(review): apply autofix feedback`. 99/99 tests pass.

---

## Capture After Merge (institutional learnings)

The learnings researcher flagged 5 strong `/ce-compound` candidates with no existing coverage — net-new institutional territory:

1. **FastAPI error envelope convention.** Document the chosen shape (`{"detail": ...}` standard, `{"error": ...}` legacy-only). Cover both string and dict `detail` cases.
2. **410 Gone deprecation shim pattern.** Status, body shape, CHANGELOG breadcrumb, expiry date, `# TODO(version)` marker.
3. **orjson migration completeness rule.** Gotchas: bytes return, `OPT_SORT_KEYS` flag (no `sort_keys=True` kwarg), no `default=str` shorthand for datetimes — use `OPT_NAIVE_UTC | OPT_SERIALIZE_NUMPY`.
4. **TypedDict for cross-call-site shape contracts.** When to promote `Dict[str, Any]` to TypedDict (rule of thumb: ≥3 construction sites + ≥1 consumer that reads named keys). The recomposition entries case is a worked example.
5. **Hash canonicalization for benign reorderings.** Any hash used for idempotency keying must canonicalize both dict keys AND list values where order is non-semantic (e.g. `time_refs`, `tags`). Note the trade-off: only sort lists whose semantics tolerate it (preserve order for `messages`, `tool_calls`).
