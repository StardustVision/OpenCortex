---
title: "fix: TCP CLOSE_WAIT leak — async-client lifecycle, connection limits, shutdown hooks"
type: fix
status: active
date: 2026-04-25
---

# fix: TCP CLOSE_WAIT leak — async-client lifecycle, connection limits, shutdown hooks

## Overview

Production bug: after ~24h of heavy benchmark traffic the OpenCortex HTTP server accumulates 42+ CLOSE_WAIT TCP connections (out of 45 total) and the asyncio event loop blocks. Killing eval processes + restarting the server fixes it immediately. Root cause: two `httpx.AsyncClient` instances captured in `llm_factory` closures are never closed; `RerankClient` has no `aclose()` method and is created per-request in admin routes; no `httpx.Limits` are configured anywhere so there is no backpressure.

This plan closes all three holes with conservative scope, plus adds visibility (admin health endpoint) and a defense-in-depth sweeper that prunes stale keepalive connections so a future code path that forgets `aclose()` cannot reproduce this incident.

---

## Problem Frame

**Observed**:
- Server PID 69378 ran 23h48m → 42 CLOSE_WAIT + 3 ESTABLISHED + 1 LISTEN out of 45 total sockets.
- QASPER (5h) and LongMemEval (4h) benchmark runs both froze, wasting ~10h of compute.
- "CLOSE_WAIT" means the remote side closed but the local side never called `close()` — classic httpx connection-pool leak.

**Confirmed leak surface** (audit run 2026-04-25):

1. `src/opencortex/models/llm_factory.py:92` and `:126` — `httpx.AsyncClient(timeout=60.0)` captured in `_make_openai_callable` / `_make_anthropic_callable` closures returned by `create_llm_completion()`. Created once per orchestrator init, never closed. Every IntentAnalyzer call + every LLM-mode rerank call flows through these.
2. `src/opencortex/retrieve/rerank_client.py:203-208` — `RerankClient._get_http_client` lazy-creates `self._http_client = httpx.AsyncClient(timeout=30.0)`. The class has **no** `aclose()` method. `admin_routes.py:170` constructs a fresh `RerankClient` on every `admin_search_debug` request — each call leaks one TCP connection.
3. **No `httpx.Limits` anywhere** in the codebase. Default httpx behavior is 100 connections/pool per origin with no socket cap → leaks accumulate until the kernel's ephemeral port pool is exhausted.

**Already clean** (do not touch in this plan):
- `src/opencortex/http/client.py:50` — `OpenCortexClient` has proper `async close()` calling `self._client.aclose()`.
- `benchmarks/oc_client.py:50` and `benchmarks/llm_client.py:33` — both have `async close()`.
- `src/opencortex/models/embedder/openai_embedder.py:65` — sync `httpx.Client` used as `with` context manager (one-shot per call). Safe.

---

## Requirements Trace

- **R1**. Every long-lived `httpx.AsyncClient` created by the server has a corresponding `aclose()` path that gets awaited during graceful shutdown.
- **R2**. Every `httpx.AsyncClient` created by the server has `httpx.Limits(max_connections=20, max_keepalive_connections=5)` applied to bound socket use even under pool-leak regression.
- **R3**. `MemoryOrchestrator.close()` awaits the new client `aclose()` calls in a deterministic order, integrated with the existing teardown sequence (autophagy tasks → recall tasks → derive worker → context manager → embedder → storage).
- **R4**. Operators have a `/admin/health/connections` endpoint that returns per-client pool stats so the next leak is observable before it pages someone.
- **R5**. A periodic `_connection_sweep_task` prunes idle keepalive connections older than the configured threshold, mirroring the autophagy sweeper pattern. Defense-in-depth so a future forgotten `aclose()` cannot reproduce this incident.
- **R6**. The benchmark recall hot path (LLM completion + reranker) is unchanged in throughput and behavior — fix is invisible to callers.

---

## Scope Boundaries

- Not changing the embedder lifecycle (the immediate fallback embedder is sync, already closed in `orchestrator.close()`).
- Not changing storage adapter close (already correct).
- Not introducing connection retry / circuit breaker patterns — out of scope, separate concern.
- Not changing `OpenCortexClient` (HTTP SDK) or `benchmarks/oc_client.py` — already clean.
- Not changing the `openai_embedder.py` sync httpx usage.
- Not adding Prometheus / OTel metrics export — `/admin/health/connections` is a polled endpoint, not push metrics. Metrics export is a separate plan if needed.
- Not adding per-tenant pool limits — flat per-process cap is enough for the observed failure mode.

### Deferred to Follow-Up Work

- Capture this fix as a `docs/solutions/` learning entry once it lands (called out by `ce-learnings-researcher` — the codebase has no Python-side connection-lifecycle solution doc yet).
- Consider mirroring the same pool caps on the Node-side MCP client (`plugins/opencortex-memory/lib/http-client.mjs` already uses `undici` keepalive per v0.6.3 — verify the pool is bounded too).
- Audit `ContextManager._prepare()` timeout behavior under load (suspected secondary leak path: if a sub-call times out, was the connection released to the pool or orphaned?). Touched indirectly by this plan via Limits, but a dedicated audit is a separate item.

---

## Context & Research

### Relevant Code and Patterns

- **FastAPI lifespan**: `src/opencortex/http/server.py:192-282`, `_lifespan(app)` decorated with `@asynccontextmanager`. Already calls `await _orchestrator.close()` at line 278-282 in the `finally` of the `yield`. No new wiring needed in `server.py`.
- **MemoryOrchestrator.close()**: `src/opencortex/orchestrator.py:5732-5794`. Existing teardown order: autophagy tasks → recall bookkeeping tasks → derive worker → context manager → embedder fallback → storage. New close hooks must slot in before storage close (clients depend on nothing else, so order between them is internal-only).
- **Periodic task pattern**: `src/opencortex/orchestrator.py:438-521` — autophagy sweeper. `_start_autophagy_sweeper()` launches a one-shot startup task + a `while True: await asyncio.sleep(interval)` loop, both named tasks. Re-entrancy guarded by an `asyncio.Lock`. Cancelled in `close()` via `task.cancel()` + suppressed `await`. **Mirror this exactly** for the connection sweeper.
- **Admin route pattern**: `src/opencortex/http/admin_routes.py`. Imperative `_require_admin()` call (no `Depends`), inline `Dict[str, Any]` returns, `HTTPException(status_code=..., detail=...)` for errors. Module-level `_orchestrator` set by `register_admin_routes(orchestrator, jwt_secret)`. Representative example: `admin_search_debug` at lines 146-201.
- **Test patterns**:
  - `tests/test_benchmark_llm_client.py:13-21` — `_StubClient` with explicit `async def aclose(self): return None`, substituted onto `client._client`. **Template for new lifecycle tests**.
  - `tests/test_http_client.py:16` — uses `await self.client.close()` in test teardown for `OpenCortexClient`. Template for orchestrator close ordering tests.
  - No existing test asserts `aclose` was awaited via `mock.assert_awaited()` — this plan introduces that pattern.

### Institutional Learnings

- `~/.claude/projects/.../memory/project_connection_pool_leak.md` — the original observation (this is the bug being fixed). Recommends: `async with` or explicit `aclose()`, `httpx.Limits(max_connections=20, max_keepalive_connections=5)`, health endpoint, periodic cleanup. This plan implements all four.
- `docs/superpowers/plans/2026-03-28-noise-reduction-and-perf-optimization.md` — Node-side prior art: `undici` global dispatcher with `Agent({keepAliveTimeout: 30000})`. Confirms keepalive pooling is the right model on the Python side too.
- `docs/solutions/best-practices/memory-intent-hot-path-refactor-2026-04-12.md` — names the exact hot-path modules where leaks originate (ContextManager._prepare, IntentRouter, orchestrator). Confirms the singleton-on-orchestrator ownership decision (R3).

---

## Key Technical Decisions

- **`LLMCompletion` wrapper class instead of bare callable.** `create_llm_completion()` currently returns a closure. To add `aclose()` without changing call sites we wrap the closure in a small class that exposes `__call__(...) -> Awaitable[str]` (transparent to existing `await self._llm_completion(prompt)` callers) AND `async aclose()`. The class also exposes `_client` for the health endpoint to read pool stats. Backward-compatible at every call site.

- **`RerankClient` becomes a singleton on `MemoryOrchestrator`.** Currently constructed fresh in `admin_search_debug` per request (the only production caller). Lift to `self._rerank_client = RerankClient(self._rerank_config)` in `MemoryOrchestrator.init()`. Update `admin_search_debug` to read `self._orchestrator._rerank_client`. This collapses N per-request leaks into one process-lifetime client that the orchestrator owns.

- **Conservative pool caps**: `httpx.Limits(max_connections=20, max_keepalive_connections=5)`. From the project memory's recommendation. Caps are a circuit breaker, not a perf tuning — the goal is "the next leak triggers backpressure before it triggers an outage." Per-client (LLM completion vs rerank) since they hit different origins.

- **Sweeper interval default 600s (10 min).** Autophagy sweeper uses 900s. Connection cleanup is more aggressive because leaks accumulate fast and the cost of a no-op sweep is one method call + one asyncio.sleep.

- **Pool-stat extraction is best-effort.** httpx exposes pool internals via `client._transport._pool`, which is a private API. Wrap in `try/except` and return `{"status": "unavailable", "reason": str(exc)}` rather than crash. Document fragility in the endpoint's docstring. Operators can still see "this client exists and is configured with these limits" even when live counts are unavailable.

- **Sweeper uses public httpx APIs only** — no reaching into `_pool` internals to actively close keepalive sockets. Instead the sweeper relies on the keepalive expiry settings already enforced by httpx (`httpx.Limits` controls max keepalives; the transport prunes expired ones automatically). The "sweeper" is really a periodic health check that **reports** stale connections via the health endpoint and would log a WARNING if any client's open-connection count drifts above a configured threshold. This is honest about what the layer can do — true forced-close requires private API or a fresh client.

- **Two separate sweep concerns in one task**: (a) emit log + metric if any client's open count > threshold; (b) hand off to httpx's own pool reaper by triggering a cheap pooled request that lets the transport reclaim expired sockets. Document that (b) is a tickle, not a force-close.

---

## Open Questions

### Resolved During Planning

- **Q: Wrapper class vs tuple return for llm_factory?** → Wrapper class. Preserves `await self._llm_completion(prompt)` syntax at every call site; tuple unpacking would require touching every caller.
- **Q: RerankClient singleton vs per-request?** → Singleton on orchestrator. Lifted from per-request construction in admin route.
- **Q: Pool cap values?** → `max_connections=20, max_keepalive_connections=5` per project memory; revisit if benchmark perf regresses.
- **Q: Sweep interval?** → 600s default, configurable via `CortexConfig.connection_sweep_interval_seconds`.
- **Q: How to expose pool stats safely?** → Best-effort with try/except wrapper, return `"unavailable"` on failure.
- **Q: Where to wire periodic sweeper?** → Started in `MemoryOrchestrator.init()` like autophagy sweeper, cancelled in `close()`.

### Deferred to Implementation

- **Exact field shape of pool stat dict** — depends on what httpx 0.27.x actually exposes via `client._transport._pool`. Implementer should probe at U4 time and document what's available; missing fields default to `null`, not crash.
- **Whether the sweeper should also tickle the OpenAI/Anthropic clients separately or share one tickle endpoint** — depends on whether they share an httpx connection pool. Decide during U5 implementation.
- **Test isolation strategy for the sweeper** — the periodic loop runs forever. Existing autophagy sweeper tests use `await asyncio.sleep(0)` to advance the loop; mirror that pattern but verify it works for the connection sweeper.

---

## High-Level Technical Design

> *This illustrates the intended approach and is directional guidance for review, not implementation specification. The implementing agent should treat it as context, not code to reproduce.*

```
                   MemoryOrchestrator (process-lifetime singleton)
                   │
                   ├── _llm_completion: LLMCompletion          ← U1 (was: bare closure)
                   │       ├── __call__(prompt) → str          ← unchanged contract
                   │       ├── _client: httpx.AsyncClient(limits=...)
                   │       └── async aclose()                  ← new
                   │
                   ├── _rerank_client: RerankClient            ← U2 (was: per-request)
                   │       ├── _http_client: httpx.AsyncClient(limits=...)
                   │       └── async aclose()                  ← new
                   │
                   ├── _connection_sweep_task: asyncio.Task    ← U5 (new)
                   │       └── while True: sleep(600); inspect+log+tickle
                   │
                   └── async close()                            ← U3 (extend existing)
                           1. cancel _connection_sweep_task    ← new step
                           2. cancel autophagy tasks            ← unchanged
                           3. cancel recall bookkeeping        ← unchanged
                           4. cancel derive worker             ← unchanged
                           5. _context_manager.close()         ← unchanged
                           6. _llm_completion.aclose()         ← new step
                           7. _rerank_client.aclose()          ← new step
                           8. _embedder fallback.close()       ← unchanged
                           9. _storage.close()                 ← unchanged

   FastAPI app
   │
   ├── /admin/health/connections                                ← U4 (new)
   │       └── reads orchestrator._llm_completion._client + _rerank_client._http_client
   │           returns {clients: {...}, limits: {...}, sweeper: {...}, status}
   │
   └── lifespan finally → orchestrator.close()                  ← already exists
```

---

## Implementation Units

- [ ] U1. **`LLMCompletion` wrapper with `aclose()` and httpx.Limits**

**Goal:** Replace the closure-captured `httpx.AsyncClient` instances in `llm_factory` with a wrapper class that exposes the existing `__call__` contract AND a new `async aclose()`. Apply `httpx.Limits(max_connections=20, max_keepalive_connections=5)` to both OpenAI and Anthropic clients.

**Requirements:** R1, R2, R6.

**Dependencies:** None.

**Files:**
- Modify: `src/opencortex/models/llm_factory.py`
- Test: `tests/test_llm_factory_lifecycle.py` (new)

**Approach:**
- Introduce `class LLMCompletion` with `__init__(self, callable, client)`, `async def __call__(self, *args, **kwargs)` delegating to the inner callable, `async def aclose(self)` awaiting `self._client.aclose()`, and a property `client` for health-endpoint access.
- `_make_openai_callable` and `_make_anthropic_callable` now construct the httpx client with `httpx.Limits(max_connections=20, max_keepalive_connections=5)`, then wrap the closure + client in `LLMCompletion(...)` and return that instance.
- `create_llm_completion(config)` returns `LLMCompletion`, not bare callable.
- `aclose()` is idempotent (second call is no-op).
- All existing call sites (`await self._llm_completion(prompt)`) work unchanged because `LLMCompletion.__call__` is async.

**Patterns to follow:**
- `tests/test_benchmark_llm_client.py:13-21` — `_StubClient.aclose` template for the test fixture.
- `src/opencortex/http/client.py:56-61` — `OpenCortexClient.close()` shows the idempotent-close pattern.

**Test scenarios:**
- *Happy path*: `LLMCompletion(callable, client)(prompt)` returns the same response shape as the previous closure (smoke test against a `_StubClient` returning a canned response).
- *Lifecycle*: `await wrapper.aclose()` calls `client.aclose()` exactly once. Verify with an `AsyncMock` spy.
- *Limits wired*: inspect `wrapper.client._limits.max_connections == 20` and `max_keepalive_connections == 5` for both OpenAI and Anthropic factories.
- *Idempotency*: `await wrapper.aclose()` followed by `await wrapper.aclose()` doesn't raise.
- *Backward compat*: existing call site shape `result = await llm_completion(prompt, max_tokens=N)` still works — assert no kwargs are dropped by the wrapper delegation.

**Verification:**
- `LLMCompletion` wrapper exists, has the three methods, and existing IntentAnalyzer/LLM-rerank call sites compile/run unchanged.

---

- [ ] U2. **`RerankClient.aclose()` + httpx.Limits + lift to orchestrator singleton**

**Goal:** Add `aclose()` to `RerankClient`, apply httpx.Limits to its lazy `_http_client`, and lift instantiation from per-request (in `admin_search_debug`) to a process-lifetime singleton on `MemoryOrchestrator`.

**Requirements:** R1, R2, R6.

**Dependencies:** None (parallel with U1).

**Files:**
- Modify: `src/opencortex/retrieve/rerank_client.py`
- Modify: `src/opencortex/orchestrator.py` (init: hold `self._rerank_client`)
- Modify: `src/opencortex/http/admin_routes.py` (read singleton from orchestrator)
- Test: `tests/test_rerank_client_lifecycle.py` (new)

**Approach:**
- Add `async def aclose(self)` to `RerankClient` that awaits `self._http_client.aclose()` if `self._http_client is not None`. Idempotent.
- Modify `_get_http_client` to construct `httpx.AsyncClient(timeout=30.0, limits=httpx.Limits(max_connections=20, max_keepalive_connections=5))`.
- In `MemoryOrchestrator.init()`, after `self._rerank_config` is built, construct `self._rerank_client = RerankClient(self._rerank_config)`. Store but do NOT eagerly create the http client (preserve the lazy pattern — first request creates it).
- In `admin_routes.py:admin_search_debug`, replace `RerankClient(_build_rerank_config(_orchestrator))` with `_orchestrator._rerank_client`.

**Patterns to follow:**
- `src/opencortex/http/client.py:56-61` — idempotent close pattern.
- Existing orchestrator attribute lifecycle for `_storage`, `_embedder`, etc. — same defensive `getattr(...)` shape used in `close()`.

**Test scenarios:**
- *Happy path*: `RerankClient.aclose()` after at least one rerank call closes the lazy `_http_client` (assert via `AsyncMock` spy).
- *Edge — uninitialized*: `RerankClient.aclose()` before any request (when `_http_client is None`) is a safe no-op.
- *Edge — idempotent*: two consecutive `aclose()` calls don't raise.
- *Limits wired*: inspect `client._http_client._limits.max_connections == 20`.
- *Singleton*: instantiate `MemoryOrchestrator`, call `await orch.init()`, assert `orch._rerank_client is orch._rerank_client` and the same instance survives across two `admin_search_debug` invocations (use a counter in a wrapped factory).
- *Integration*: simulate 100 admin_search_debug calls back-to-back with mocked rerank responses; assert only ONE `httpx.AsyncClient` was constructed (regression lock for the original leak).

**Verification:**
- `RerankClient` has `aclose()`. Orchestrator holds singleton. `admin_search_debug` no longer constructs a fresh client per request.

---

- [ ] U3. **Extend `MemoryOrchestrator.close()` to await new client `aclose()` calls in order**

**Goal:** Wire `_llm_completion.aclose()` and `_rerank_client.aclose()` into the existing `MemoryOrchestrator.close()` teardown sequence at the right ordinal positions. Preserve the existing defensive `getattr` pattern so tests using `__new__` bypass don't crash.

**Requirements:** R3.

**Dependencies:** U1, U2.

**Files:**
- Modify: `src/opencortex/orchestrator.py`
- Test: `tests/test_orchestrator_close.py` (new — covers shutdown ordering)

**Approach:**
- In `close()` (currently lines 5732-5794), insert two new awaited calls between step 5 (`_context_manager.close()`) and step 6 (`_immediate_fallback_embedder.close()`):
  - `await getattr(self, '_llm_completion', None) and self._llm_completion.aclose()`
  - `await getattr(self, '_rerank_client', None) and self._rerank_client.aclose()`
- Both wrapped in try/except + log on failure (don't let one failed close abort the rest of teardown — match existing pattern at lines 5734-5746).
- The `_connection_sweep_task` cancellation lands in U5 — placeholder note here so reviewer sees the full eventual order.

**Patterns to follow:**
- Existing `close()` at `orchestrator.py:5732` — try/except per step, suppressed `asyncio.CancelledError` on task cancellation, log failures but never re-raise.

**Test scenarios:**
- *Order*: spy on each `aclose()` call (using `AsyncMock` substitution) and assert call order: context_manager → llm_completion → rerank_client → embedder → storage.
- *Idempotency*: `await orch.close()` twice doesn't crash (preserve existing defensive pattern).
- *Partial state — close before init*: construct orchestrator via `__new__` bypass without calling init, then `close()` doesn't raise (no `_llm_completion`/`_rerank_client` attributes yet).
- *One step fails, others continue*: make `_llm_completion.aclose()` raise; assert `_rerank_client.aclose()` and `_storage.close()` still get awaited.

**Verification:**
- 7-test orchestrator close suite passes. Existing tests still pass.

---

- [ ] U4. **`/admin/health/connections` endpoint with best-effort pool stats**

**Goal:** Operators can poll `/admin/health/connections` to see per-client pool state (open connections, keepalive count, configured limits), so the next leak is observable before it pages someone.

**Requirements:** R4.

**Dependencies:** U1, U2 (singleton clients must exist on orchestrator).

**Files:**
- Modify: `src/opencortex/http/admin_routes.py` (new endpoint)
- Modify: `src/opencortex/http/models.py` (response model — optional; can be inline Dict if shape is fluid)
- Test: `tests/test_admin_health_connections.py` (new)

**Approach:**
- New `@router.get("/admin/health/connections")` returning a Dict[str, Any] like:
  ```
  {
    "status": "healthy" | "degraded" | "unavailable",
    "clients": {
      "llm_completion": {"open_connections": int|null, "keepalive": int|null, "limits": {...}, "stats_source": "transport_pool"|"unavailable"},
      "rerank": {...}
    },
    "sweeper": {"last_sweep_at": iso8601, "last_sweep_status": "ok"|"warn", "interval_seconds": int}
  }
  ```
- Use `_require_admin()` (imperative, matching existing admin endpoints).
- Pool-stat extraction in a helper `_extract_pool_stats(client) -> Dict[str, Any]` that wraps `client._transport._pool` access in try/except. On failure, return `{"stats_source": "unavailable", "reason": str(exc), "limits": <readable from client._limits>}`.
- Status field: `"healthy"` if all clients report stats; `"degraded"` if any client's open_connections exceeds `0.8 * max_connections`; `"unavailable"` if no client can report stats.

**Patterns to follow:**
- `src/opencortex/http/admin_routes.py:146-201` — `admin_search_debug` shape: imperative `_require_admin()`, inline `Dict[str, Any]` return, `HTTPException(403)` on auth fail.

**Test scenarios:**
- *Happy path*: GET `/admin/health/connections` with admin JWT → 200, response has `status`, `clients.llm_completion`, `clients.rerank`, `sweeper` keys.
- *Auth*: GET with non-admin JWT → 403.
- *Edge — uninitialized client*: when `_llm_completion is None` (test fixture), endpoint returns `{"clients": {"llm_completion": {"stats_source": "uninitialized", ...}}}` — does not crash.
- *Edge — stat extraction fails*: monkeypatch `_extract_pool_stats` to raise; endpoint still returns 200 with `"stats_source": "unavailable"` and a `"reason"` field.
- *Status thresholds*: when a client reports `open_connections=17` and `max_connections=20` (>80%), the top-level `status` is `"degraded"`.

**Verification:**
- Endpoint registered, returns expected shape, auth-protected, never crashes regardless of httpx internal availability.

---

- [ ] U5. **Periodic stale-connection sweeper task**

**Goal:** Defense-in-depth periodic task that inspects pool state, logs WARNING when any client exceeds 80% of `max_connections`, and tickles each pooled client so the httpx transport prunes expired keepalive sockets. Mirrors the autophagy sweeper pattern. Cancelled in `close()`.

**Requirements:** R5.

**Dependencies:** U1, U2, U3 (close() must already extend to the new clients).

**Files:**
- Modify: `src/opencortex/orchestrator.py` (start sweeper in init, cancel in close, add `_run_connection_sweep_once` + `_connection_sweep_loop`)
- Modify: `src/opencortex/config.py` (new `connection_sweep_interval_seconds: int = 600` field with env var support)
- Test: `tests/test_connection_sweeper.py` (new)

**Approach:**
- Add `connection_sweep_interval_seconds: int = 600` to `CortexConfig` dataclass with env-var override `OPENCORTEX_CONNECTION_SWEEP_INTERVAL_SECONDS` (match existing config pattern).
- In `MemoryOrchestrator.init()`, after `_rerank_client` is set up (U2), call `self._start_connection_sweeper()` which mirrors `_start_autophagy_sweeper`:
  - `self._connection_sweep_task = asyncio.create_task(self._connection_sweep_loop(), name="opencortex.connections.periodic_sweep")`
  - `self._connection_sweep_guard = asyncio.Lock()` to prevent overlapping sweeps.
- `_connection_sweep_loop`: `while True: await asyncio.sleep(interval); await self._run_connection_sweep_once()`.
- `_run_connection_sweep_once`:
  1. For each pooled client (`self._llm_completion.client`, `self._rerank_client._http_client` if not None), extract pool stats via the same helper U4 introduces (factor it out of admin_routes into a shared utility).
  2. If `open_connections > 0.8 * max_connections`, log WARNING: `"[ConnectionSweeper] {client_name} pool nearing cap: open={open}, limit={limit}, keepalive={keepalive}"`.
  3. Update `self._last_connection_sweep_at = datetime.now(timezone.utc)` and `self._last_connection_sweep_status` ("ok" | "warn") so U4's endpoint can read them.
- In `close()`, cancel `_connection_sweep_task` BEFORE the rest of teardown (so the sweep doesn't try to inspect closing clients).

**Patterns to follow:**
- `src/opencortex/orchestrator.py:438-521` — autophagy sweeper. Same structure: starter method, named `asyncio.Task`, `asyncio.Lock` re-entrancy guard, `while True: await asyncio.sleep` loop, cancellation in `close()`.

**Test scenarios:**
- *Sweeper starts on init*: after `await orch.init()`, assert `orch._connection_sweep_task is not None and not orch._connection_sweep_task.done()`.
- *Sweeper cancelled on close*: after `await orch.close()`, assert `orch._connection_sweep_task.cancelled() or orch._connection_sweep_task.done()`.
- *Sweeper logs WARNING above threshold*: stub `_extract_pool_stats` to return `{"open_connections": 18, "max_connections": 20, ...}`, invoke `_run_connection_sweep_once()` directly, assert a WARNING was logged via `assertLogs`.
- *Sweeper updates last-sweep timestamp*: call `_run_connection_sweep_once()`, assert `_last_connection_sweep_at` is recent.
- *Re-entrancy lock*: invoke two concurrent `_run_connection_sweep_once()` tasks; the second waits on the lock (verify via `asyncio.Lock.locked()` or by spying on the inspect call count).
- *Configurable interval*: set `OPENCORTEX_CONNECTION_SWEEP_INTERVAL_SECONDS=30`, build orchestrator, assert `config.connection_sweep_interval_seconds == 30`.
- *Idempotent close*: `close()` called twice does not crash even after sweeper task is already cancelled.

**Verification:**
- Sweeper task is running after init, cancelled cleanly on close, emits warnings when thresholds breached, status visible via U4's endpoint.

---

## System-Wide Impact

- **Interaction graph:** `MemoryOrchestrator` gains two new attributes (`_rerank_client`, `_connection_sweep_task`), one mutated attribute (`_llm_completion` is now a wrapper instance, not bare callable). FastAPI lifespan unchanged (already calls `orchestrator.close()`). Admin routes touched in two places: `admin_search_debug` reads orchestrator singleton, new `/admin/health/connections` endpoint registered.
- **Error propagation:** `aclose()` failures in `MemoryOrchestrator.close()` are logged but never re-raised (matches existing pattern). Sweeper failures are logged but never crash the loop. Health endpoint returns `"unavailable"` on stat extraction failure rather than 500.
- **State lifecycle risks:** The lift to singleton means `RerankClient` now spans every admin request — must be thread-safe under asyncio. Verified: existing `RerankClient` already maintained `self._http_client` as instance state and httpx clients are asyncio-safe by design. No new race.
- **API surface parity:** `LLMCompletion.__call__` is the new external surface — existing call sites that do `await self._llm_completion(prompt, max_tokens=N)` continue to work because `__call__` is `async def`. New `/admin/health/connections` endpoint follows existing admin route conventions; no MCP/CLI/SDK parity needed (operator-only).
- **Integration coverage:** U2's "100 admin_search_debug calls accumulate ≤ 1 client" test is the regression lock for the original incident. Without it, a future refactor could silently re-introduce per-request instantiation.
- **Unchanged invariants:** `OpenCortexClient`, `benchmarks/oc_client.py`, `benchmarks/llm_client.py`, embedder lifecycle, storage adapter close — all explicitly out of scope and untouched.

---

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| `httpx._transport._pool` is a private API; httpx version bump could break stat extraction | Best-effort wrapper with try/except. Endpoint returns `"unavailable"` instead of crashing. Document fragility in endpoint docstring. |
| Pool caps (max_connections=20) too aggressive for a future high-concurrency workload | Caps are intentionally conservative for the bug-fix; raise via config if benchmark perf regresses. The sweeper's WARNING-at-80% surfaces the need to raise BEFORE it bites. |
| Singleton RerankClient introduces shared state across admin requests | httpx.AsyncClient is asyncio-safe; existing RerankClient already maintains instance state. New tests cover concurrent access. |
| Sweeper task accidentally inspects a half-closed client during shutdown race | `close()` cancels `_connection_sweep_task` BEFORE the per-client `aclose()` calls. Cancellation suppresses any in-flight inspection. |
| Mock-heavy tests miss real httpx behavior | One integration test uses real `httpx.AsyncClient` against `httpx.MockTransport` for the lifecycle assertions, not full mock substitution. |
| Existing tests using `__new__` bypass for orchestrator break when new attributes added | Defensive `getattr(self, '_X', None)` in `close()` (matches existing pattern). New attributes default to `None` if init was skipped. |

---

## Documentation / Operational Notes

- Update `CLAUDE.md` "Architecture" section with brief mention of the new sweeper task in the periodic-task section (next to autophagy mention).
- Mention the new `/admin/health/connections` endpoint in `docs/admin/` if such docs exist; otherwise inline-comment-only.
- After landing: write a `docs/solutions/best-practices/python-async-client-lifecycle-2026-04-25.md` capturing the lifecycle contract + sweeper pattern (called out by `ce-learnings-researcher`).
- No CHANGELOG entry needed for v0.8.x — bug fix, not user-facing API change.

---

## Sources & References

- Origin diagnosis: `~/.claude/projects/-Users-hugo-CodeSpace-Work-OpenCortex/memory/project_connection_pool_leak.md`
- Audit results: this conversation's Explore agent run (httpx lifecycle inventory, 2026-04-25)
- Repo research: this conversation's ce-repo-research-analyst run
- Related code:
  - `src/opencortex/models/llm_factory.py:88-130` (closure-captured clients)
  - `src/opencortex/retrieve/rerank_client.py:203-208` (lazy http client)
  - `src/opencortex/orchestrator.py:438-521` (autophagy sweeper pattern)
  - `src/opencortex/orchestrator.py:5732-5794` (existing close)
  - `src/opencortex/http/server.py:192-282` (FastAPI lifespan)
  - `src/opencortex/http/admin_routes.py:146-201` (admin route pattern)
- Node-side prior art: `docs/superpowers/plans/2026-03-28-noise-reduction-and-perf-optimization.md` (undici keepalive)
- Related learnings: `docs/solutions/best-practices/memory-intent-hot-path-refactor-2026-04-12.md`
