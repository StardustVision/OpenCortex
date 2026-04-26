# Residual Review Findings — plan 013 Phase 4 SystemStatusService

Review run: 20260426-201700-cff75fc7  
Branch: refactor/knowledge-service-extraction  
Head at review time: 2ca628d

## Residual Review Findings

### P2 — Gated Auto (concrete fix exists, changes behavior)

- **P2** `src/opencortex/services/system_status_service.py:138` — `system_status("doctor")` key collision: `{**health, **st}` silently overwrites `storage` bool with stats dict and `embedder` bool with model name string. Downstream consumers using truthiness checks on those keys in doctor responses will see incorrect health signals. Fix: capture health booleans separately before merging. (correctness + adversarial; confidence 100; pre-existing)

- **P2** `src/opencortex/services/system_status_service.py:198` — `wait_deferred_derives`: unbounded loop with no timeout — a hung derive task blocks an HTTP worker indefinitely. Fix: add `max_wait` parameter or wrap call site in `asyncio.wait_for`. (reliability + adversarial; confidence 100; pre-existing)

### P2 — Manual (needs test work)

- **P2** `tests/test_system_status_service.py:1` — `wait_deferred_derives` polling loop (count > 0 path) has no test coverage. Fix: add a test that starts with `_deferred_derive_count = 2` and schedules a decrementer task. (testing; confidence 80)

- **P2** `tests/test_system_status_service.py:1` — `reembed_all` has zero test coverage anywhere in the test suite (no call to `reembed_all()` or `/api/v1/admin/reembed` found). Fix: add a unit test mocking `v040_reembed.reembed_all` and verifying the marker file write. (testing; confidence 75)

### Advisory

- `reembed_all` passes `None` embedder to `_reembed_all` with no guard (pre-existing; also missing at HTTP route level)
- `reembed_all` `marker.write_text` has no error handling — disk-full or permissions error surfaces as raw OSError after successful re-embed (pre-existing)
- Legacy `typing.Dict` imports — `dict[str, Any]` preferred in Python 3.10+ (matches sibling service files)
- `system_status("stats")` routing branch missing from unit tests (covered by integration tests in `test_http_server.py` and `test_multi_tenant.py`)
