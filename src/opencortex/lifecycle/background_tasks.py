# SPDX-License-Identifier: Apache-2.0
"""Background task lifecycle service extracted from MemoryOrchestrator.

All background sweeper and worker methods have been
extracted from ``MemoryOrchestrator`` as part of plan 014 (Phase 3 of
the God Object decomposition). This module owns the startup and teardown
of every async background loop that runs inside the orchestrator process.

Boundary
--------
``BackgroundTaskManager`` is responsible for:
- Autophagy metabolism sweeps (``_start_autophagy_sweeper``,
  ``_run_autophagy_sweep_once``, ``_autophagy_sweep_loop``)
- Connection pool inspection sweeps (``_start_connection_sweeper``,
  ``_run_connection_sweep_once``, ``_maybe_warn_pool``,
  ``_connection_sweep_loop``)
- Document derive worker (``_start_derive_worker``, ``_derive_worker``,
  ``_process_derive_task``, ``_recover_pending_derives``,
  ``_drain_derive_queue``)
- Reverse-order teardown of all background tasks (``close``)

It is explicitly NOT responsible for:
- Memory record CRUD — owned by ``MemoryService``
- Knowledge lifecycle — owned by ``KnowledgeService``
- System status reporting — owned by ``SystemStatusService``
- Deferred derive completion — owned by ``DerivationService`` and exposed
  through orchestrator compatibility wrappers
- Subsystem boot sequencing — Phase 5 (``SubsystemBootstrapper``)

Design
------
The service holds a back-reference to the orchestrator (``self._orch``)
and reaches into orchestrator-owned subsystems at call time. All task
handles (``_connection_sweep_task``, ``_autophagy_sweep_task``, etc.)
and status attributes (``_last_connection_sweep_at``,
``_last_connection_sweep_status``) remain on the orchestrator so that
the admin health endpoint at ``/admin/health/connections`` can read
them via ``getattr(_orchestrator, ...)`` without route changes.

Construction is sync and cheap — no I/O, no model loading. The
orchestrator lazily builds a single ``BackgroundTaskManager`` instance
via the ``_background_task_manager`` property.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from opencortex.orchestrator import MemoryOrchestrator
    from opencortex.services.derivation_service import DeriveTask

logger = logging.getLogger(__name__)


class BackgroundTaskManager:
    """Owns all background sweepers, workers, and their teardown.

    All methods have been extracted from ``MemoryOrchestrator`` as
    part of plan 014 (Phase 3). The manager is lazily constructed by
    the orchestrator and reaches into orchestrator-owned subsystems
    via ``self._orch``.
    """

    def __init__(self, orchestrator: "MemoryOrchestrator") -> None:
        """Bind the manager to its parent orchestrator.

        Args:
            orchestrator: The ``MemoryOrchestrator`` instance whose
                subsystems and task handles this manager reads and
                writes at call time. Stored as ``self._orch``; not
                validated.
        """
        self._orch = orchestrator

    # =========================================================================
    # Autophagy sweeper
    # =========================================================================

    def _start_autophagy_sweeper(self) -> None:
        """Start autophagy metabolism sweeps (startup + periodic) in background."""
        from opencortex.cognition.state_types import OwnerType

        orch = self._orch
        kernel = getattr(orch, "_autophagy_kernel", None)
        if kernel is None:
            return

        # Be resilient to unit tests that bypass __init__ via __new__.
        if not hasattr(orch, "_autophagy_sweep_task"):
            orch._autophagy_sweep_task = None
        if not hasattr(orch, "_autophagy_startup_sweep_task"):
            orch._autophagy_startup_sweep_task = None
        if not hasattr(orch, "_autophagy_sweep_cursors"):
            orch._autophagy_sweep_cursors = {
                OwnerType.MEMORY: None,
                OwnerType.TRACE: None,
            }
        if not hasattr(orch, "_autophagy_sweep_guard"):
            orch._autophagy_sweep_guard = asyncio.Lock()

        if (
            orch._autophagy_sweep_task is not None
            and not orch._autophagy_sweep_task.done()
        ):
            return

        # Startup: one immediate batch (fire-and-forget) for crash recovery
        # / backlog drain.
        orch._autophagy_startup_sweep_task = asyncio.create_task(
            self._run_autophagy_sweep_once(),
            name="opencortex.autophagy.startup_sweep",
        )

        # Periodic: one bounded page per interval, cursor carried across ticks.
        orch._autophagy_sweep_task = asyncio.create_task(
            self._autophagy_sweep_loop(),
            name="opencortex.autophagy.periodic_sweep",
        )

    async def _run_autophagy_sweep_once(self) -> None:
        """Execute one autophagy metabolism sweep pass over MEMORY and TRACE owners."""
        from opencortex.cognition.state_types import OwnerType

        orch = self._orch
        kernel = getattr(orch, "_autophagy_kernel", None)
        if kernel is None:
            return

        # Be resilient to unit tests that bypass __init__ via __new__.
        if (
            not hasattr(orch, "_autophagy_sweep_guard")
            or orch._autophagy_sweep_guard is None
        ):
            orch._autophagy_sweep_guard = asyncio.Lock()
        if (
            not hasattr(orch, "_autophagy_sweep_cursors")
            or orch._autophagy_sweep_cursors is None
        ):
            orch._autophagy_sweep_cursors = {
                OwnerType.MEMORY: None,
                OwnerType.TRACE: None,
            }

        async with orch._autophagy_sweep_guard:
            limit = int(getattr(orch._config, "autophagy_sweep_batch_size", 200))
            for owner_type in (OwnerType.MEMORY, OwnerType.TRACE):
                try:
                    cursor = orch._autophagy_sweep_cursors.get(owner_type)
                    result = await kernel.sweep_metabolism(
                        owner_type=owner_type,
                        limit=limit,
                        cursor=cursor,
                    )
                    # Reset to None when exhausted; next sweep restarts cleanly.
                    orch._autophagy_sweep_cursors[owner_type] = getattr(
                        result, "next_cursor", None
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning(
                        "[BackgroundTaskManager] Autophagy metabolism sweep failed "
                        "(owner_type=%s): %s",
                        owner_type.value,
                        exc,
                    )
                    continue

    async def _autophagy_sweep_loop(self) -> None:
        """Periodic sleep-then-sweep loop for autophagy metabolism."""
        orch = self._orch
        interval = float(getattr(orch._config, "autophagy_sweep_interval_seconds", 900))
        if interval <= 0:
            interval = 0.01  # allow fast unit tests; never busy-loop.
        try:
            while True:
                await asyncio.sleep(interval)
                await self._run_autophagy_sweep_once()
        except asyncio.CancelledError:
            raise

    # =========================================================================
    # Connection sweeper (plan 009 / R5)
    # =========================================================================

    def _start_connection_sweeper(self) -> None:
        """Start the periodic httpx-pool inspector in background.

        Mirrors ``_start_autophagy_sweeper`` exactly — same naming
        convention, same re-entrancy lock, same defensive ``getattr``
        for orchestrators built via ``__new__`` bypass.
        """
        orch = self._orch
        # Be resilient to unit tests that bypass __init__ via __new__.
        if not hasattr(orch, "_connection_sweep_task"):
            orch._connection_sweep_task = None
        if not hasattr(orch, "_connection_sweep_guard"):
            orch._connection_sweep_guard = asyncio.Lock()
        if not hasattr(orch, "_last_connection_sweep_at"):
            orch._last_connection_sweep_at = None
        if not hasattr(orch, "_last_connection_sweep_status"):
            orch._last_connection_sweep_status = "not_started"

        if (
            orch._connection_sweep_task is not None
            and not orch._connection_sweep_task.done()
        ):
            return

        orch._connection_sweep_task = asyncio.create_task(
            self._connection_sweep_loop(),
            name="opencortex.connections.periodic_sweep",
        )

    async def _run_connection_sweep_once(self) -> None:
        """Inspect every pooled client; log WARN when pool nears cap.

        REVIEW closure tracker (plan 009 review):
        - adv-004 / RELY-03: persistent failure of the sweep itself
          must NOT leave ``_last_connection_sweep_status="ok"``. The
          loop's outer try/except routes "sweep raised" through this
          method, so we update the status field BEFORE returning,
          including on the failure path. The loop wrapper sets the
          status to ``"error"`` when this method raises.
        - adv-006: when ``stats_source`` is ``"unavailable"`` for any
          client, treat that as a warn condition (status="warn"). A
          silent "ok" while the pool inspector is broken would re-
          create the very invisible-leak failure mode this PR exists
          to fix.
        """
        orch = self._orch
        # Be resilient to unit tests that bypass __init__ via __new__.
        if (
            not hasattr(orch, "_connection_sweep_guard")
            or orch._connection_sweep_guard is None
        ):
            orch._connection_sweep_guard = asyncio.Lock()

        from opencortex.observability.pool_stats import extract_pool_stats

        async with orch._connection_sweep_guard:
            from datetime import datetime, timezone
            warn_count = 0
            unavailable_count = 0

            llm_completion = getattr(orch, "_llm_completion", None)
            llm_client = (
                getattr(llm_completion, "client", None) if llm_completion else None
            )
            if llm_client is not None:
                stats = extract_pool_stats(llm_client)
                warn_count += self._maybe_warn_pool("llm_completion", stats)
                if stats.get("stats_source") != "transport_pool":
                    unavailable_count += 1

            rerank_singleton = getattr(orch, "_rerank_client", None)
            rerank_inner = (
                getattr(rerank_singleton, "_http_client", None)
                if rerank_singleton is not None
                else None
            )
            if rerank_inner is not None:
                stats = extract_pool_stats(rerank_inner)
                warn_count += self._maybe_warn_pool("rerank", stats)
                if stats.get("stats_source") != "transport_pool":
                    unavailable_count += 1

            orch._last_connection_sweep_at = datetime.now(timezone.utc)
            if warn_count:
                orch._last_connection_sweep_status = "warn"
            elif unavailable_count:
                # adv-006: stat extraction failed on some client. Don't
                # silently report "ok" — operators need visibility that
                # the inspector itself has degraded.
                orch._last_connection_sweep_status = "warn"
                logger.warning(
                    "[BackgroundTaskManager] %d client(s) returned "
                    "stats_source=unavailable — pool inspector "
                    "degraded. Check for httpx version drift.",
                    unavailable_count,
                )
            else:
                orch._last_connection_sweep_status = "ok"

    def _maybe_warn_pool(self, label: str, stats: Dict[str, Any]) -> int:
        """Emit a WARNING when the pool exceeds the warn ratio.

        Args:
            label: Human-readable name for the pool (e.g. ``"llm_completion"``).
            stats: Pool stats dict from ``extract_pool_stats``.

        Returns:
            1 if a warning was emitted, 0 otherwise.
        """
        from opencortex.observability.pool_stats import POOL_DEGRADED_THRESHOLD

        if stats.get("stats_source") != "transport_pool":
            return 0
        open_count = stats.get("open_connections")
        limits = stats.get("limits") or {}
        max_conn = limits.get("max_connections")
        if (
            not isinstance(open_count, int)
            or not isinstance(max_conn, int)
            or max_conn <= 0
        ):
            return 0
        if open_count > POOL_DEGRADED_THRESHOLD * max_conn:
            logger.warning(
                "[BackgroundTaskManager] %s pool nearing cap: open=%d, "
                "limit=%d, keepalive=%s. If this rises further the "
                "process will start refusing connections — investigate "
                "for a missing aclose() on a request path.",
                label, open_count, max_conn,
                stats.get("keepalive_connections"),
            )
            return 1
        return 0

    async def _connection_sweep_loop(self) -> None:
        """Sleep-then-inspect loop. Mirrors ``_autophagy_sweep_loop``."""
        orch = self._orch
        interval = float(
            getattr(orch._config, "connection_sweep_interval_seconds", 600)
        )
        if interval <= 0:
            interval = 0.01  # allow fast unit tests; never busy-loop.
        try:
            while True:
                await asyncio.sleep(interval)
                try:
                    await self._run_connection_sweep_once()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # adv-004 / RELY-03: a failure inside the inspector
                    # must not kill the sweep loop — log AND update the
                    # status field so /admin/health/connections shows
                    # the degraded state instead of a stale "ok".
                    orch._last_connection_sweep_status = "error"
                    logger.warning(
                        "[BackgroundTaskManager] sweep tick failed: %s "
                        "(status flipped to 'error' for visibility; "
                        "next tick will retry)", exc,
                    )
        except asyncio.CancelledError:
            raise

    # =========================================================================
    # Document derive worker
    # =========================================================================

    def _start_derive_worker(self) -> None:
        """Launch the background derive worker coroutine."""
        orch = self._orch
        if orch._derive_worker_task is None or orch._derive_worker_task.done():
            orch._derive_worker_task = asyncio.create_task(self._derive_worker())

    async def _derive_worker(self) -> None:
        """Consume DeriveTask items from the queue. Stops on None sentinel."""
        orch = self._orch
        while True:
            task = await orch._derive_queue.get()
            if task is None:
                orch._derive_queue.task_done()
                break
            try:
                await self._process_derive_task(task)
            except Exception as exc:
                logger.error(
                    "[BackgroundTaskManager] Failed to process %s: %s",
                    task.parent_uri,
                    exc,
                )
            finally:
                orch._derive_queue.task_done()

    async def _process_derive_task(self, task: "DeriveTask") -> None:
        """Process a single document derive task (Phase B).

        Creates parent record, derives chunks level-by-level, runs bottom-up
        summarization, then deletes the .derive_pending marker.

        Args:
            task: The ``DeriveTask`` item dequeued from ``_derive_queue``.
        """
        from opencortex.http.request_context import (
            reset_request_identity,
            set_request_identity,
        )

        orch = self._orch
        tokens = set_request_identity(task.tenant_id, task.user_id)
        try:
            # 1. Create parent record in Qdrant (is_leaf=False, skips _derive_layers)
            parent_ctx = await orch.add(
                abstract=task.abstract,
                content=task.content,
                category=task.category,
                uri=task.parent_uri,
                is_leaf=False,
                context_type=task.context_type,
                meta={
                    **task.meta,
                    "ingest_mode": "memory",
                    "source_doc_id": task.source_doc_id,
                    "source_doc_title": task.source_doc_title,
                    "source_section_path": "",
                    "chunk_role": "document",
                },
                session_id=task.session_id,
            )
            doc_parent_uri = parent_ctx.uri

            chunks = task.chunks
            # 2. Precompute topology
            is_dir_chunk = [
                any(c.parent_index == idx for c in chunks[idx + 1:])
                for idx in range(len(chunks))
            ]
            levels: Dict[int, List[int]] = {}
            for idx, chunk in enumerate(chunks):
                if chunk.parent_index < 0:
                    level = 0
                else:
                    parent_level = next(
                        (
                            lv
                            for lv, idxs in levels.items()
                            if chunk.parent_index in idxs
                        ),
                        0,
                    )
                    level = parent_level + 1
                levels.setdefault(level, []).append(idx)

            chunk_results: List[Optional[Any]] = [None] * len(chunks)
            sem = asyncio.Semaphore(orch._config.document_derive_concurrency)

            async def _process_chunk(idx: int) -> None:
                """Derive metadata for a single chunk and persist it via add."""
                chunk = chunks[idx]
                chunk_parent = doc_parent_uri
                if chunk.parent_index >= 0:
                    parent_result = chunk_results[chunk.parent_index]
                    if parent_result is not None and not parent_result.is_leaf:
                        chunk_parent = parent_result.uri

                chunk_role = "section" if is_dir_chunk[idx] else "leaf"
                sp = chunk.meta.get("source_section_path", "") or chunk.meta.get(
                    "section_path", ""
                )
                if is_dir_chunk[idx]:
                    heading = (
                        sp.split(" > ")[-1].strip()
                        if sp
                        else chunk.content[:80].strip()
                    )
                    chunk_abstract = heading
                else:
                    chunk_abstract = ""

                embed_text = ""
                if orch._config.context_flattening_enabled:
                    parts = []
                    if task.source_doc_title:
                        parts.append(f"[{task.source_doc_title}]")
                    if sp:
                        parts.append(f"[{sp}]")
                    if chunk_abstract:
                        parts.append(chunk_abstract)
                    embed_text = " ".join(parts)

                async with sem:
                    try:
                        ctx = await orch.add(
                            abstract=chunk_abstract,
                            content=chunk.content,
                            category=task.category,
                            parent_uri=chunk_parent,
                            is_leaf=not is_dir_chunk[idx],
                            context_type=task.context_type,
                            meta={
                                **task.meta,
                                "ingest_mode": "memory",
                                "chunk_index": idx,
                                "source_doc_id": task.source_doc_id,
                                "source_doc_title": task.source_doc_title,
                                "source_section_path": sp,
                                "chunk_role": chunk_role,
                            },
                            session_id=task.session_id,
                            embed_text=embed_text,
                        )
                        chunk_results[idx] = ctx
                    except Exception as exc:
                        logger.warning(
                            "[BackgroundTaskManager] chunk %d/%d failed: %s",
                            idx + 1, len(chunks), exc,
                        )

            # 3. Level-by-level concurrent derive
            for level in sorted(levels.keys()):
                level_tasks = [_process_chunk(idx) for idx in levels[level]]
                await asyncio.gather(*level_tasks)

            # 4. Bottom-up summarization
            for level in sorted(levels.keys(), reverse=True):
                for si in [i for i in levels[level] if is_dir_chunk[i]]:
                    if chunk_results[si] is None:
                        continue
                    child_indices = [
                        j for j in range(len(chunks)) if chunks[j].parent_index == si
                    ]
                    available = [
                        chunk_results[j].abstract
                        for j in child_indices
                        if chunk_results[j] is not None
                    ]
                    if not available:
                        continue
                    if len(available) < len(child_indices) / 2:
                        logger.warning(
                            "[BackgroundTaskManager] section %d: >50%% children "
                            "failed, skipping bottom-up",
                            si,
                        )
                        continue
                    summary = await orch._derive_parent_summary(
                        task.abstract, available
                    )
                    if summary.get("abstract"):
                        try:
                            await orch.update(
                                chunk_results[si].uri,
                                abstract=summary["abstract"],
                                overview=summary["overview"],
                                meta={"topics": summary.get("keywords", [])},
                            )
                            chunk_results[si].abstract = summary["abstract"]
                            chunk_results[si].overview = summary["overview"]
                        except Exception as exc:
                            logger.warning(
                                "[BackgroundTaskManager] section %d "
                                "bottom-up failed: %s",
                                si, exc,
                            )

            # 5. Parent summary from top-level children
            top_children = [
                chunk_results[i].abstract
                for i in range(len(chunks))
                if chunks[i].parent_index < 0 and chunk_results[i] is not None
            ]
            if top_children:
                summary = await orch._derive_parent_summary(task.abstract, top_children)
                if summary.get("abstract"):
                    try:
                        await orch.update(
                            doc_parent_uri,
                            abstract=summary["abstract"],
                            overview=summary["overview"],
                            meta={"topics": summary.get("keywords", [])},
                        )
                    except Exception as exc:
                        logger.warning(
                            "[BackgroundTaskManager] parent bottom-up failed: %s", exc,
                        )

            # 6. Delete .derive_pending marker on success
            try:
                fs_path = orch._fs._uri_to_path(task.parent_uri)
                orch._fs.agfs.rm(f"{fs_path}/.derive_pending")
            except Exception:
                pass

            logger.info(
                "[BackgroundTaskManager] Completed %s (%d chunks)",
                task.parent_uri, len(chunks),
            )
        finally:
            orch._inflight_derive_uris.discard(task.parent_uri)
            reset_request_identity(tokens)

    async def _recover_pending_derives(self) -> None:
        """Scan for .derive_pending markers and re-enqueue incomplete derives."""
        import json as _json

        from opencortex.services.derivation_service import DeriveTask

        orch = self._orch
        data_root = Path(orch._config.data_root).resolve()
        markers = list(data_root.rglob(".derive_pending"))
        if not markers:
            return

        if orch._parser_registry is None:
            from opencortex.parse.registry import ParserRegistry
            orch._parser_registry = ParserRegistry()

        recovered = 0
        for marker_path in markers:
            try:
                marker_data = _json.loads(marker_path.read_bytes())
                parent_uri = marker_data["parent_uri"]

                if parent_uri in orch._inflight_derive_uris:
                    continue

                content_path = marker_path.parent / "content.md"
                if not content_path.exists():
                    logger.warning(
                        "[BackgroundTaskManager] Stale marker (no content.md) at %s "
                        "— removing",
                        marker_path,
                    )
                    marker_path.unlink(missing_ok=True)
                    continue

                content = content_path.read_text(encoding="utf-8")
                source_path = marker_data.get("source_path", "")
                if source_path:
                    parser = orch._parser_registry.get_parser_for_file(source_path)
                else:
                    parser = None

                if parser:
                    chunks = await parser.parse_content(
                        content, source_path=source_path
                    )
                else:
                    chunks = await orch._parser_registry.parse_content(
                        content, source_format="markdown"
                    )

                task = DeriveTask(
                    parent_uri=parent_uri,
                    content=content,
                    abstract=marker_data.get("source_doc_title", "") or (
                        Path(source_path).stem if source_path else "Document"
                    ),
                    chunks=chunks,
                    category=marker_data.get("category", ""),
                    context_type=marker_data.get("context_type", "resource"),
                    meta=marker_data.get("meta", {}),
                    session_id=None,
                    source_path=source_path,
                    source_doc_id=marker_data.get("source_doc_id", ""),
                    source_doc_title=marker_data.get("source_doc_title", ""),
                    tenant_id=marker_data.get("tenant_id", ""),
                    user_id=marker_data.get("user_id", ""),
                )
                orch._inflight_derive_uris.add(parent_uri)
                await orch._derive_queue.put(task)
                recovered += 1
            except Exception as exc:
                logger.error(
                    "[BackgroundTaskManager] Failed to recover %s: %s", marker_path, exc
                )

        if recovered:
            logger.info(
                "[BackgroundTaskManager] Re-enqueued %d pending "
                "derive task(s)", recovered,
            )

    async def _drain_derive_queue(self) -> None:
        """Wait for all pending derive tasks to complete. Test-only."""
        await self._orch._derive_queue.join()

    # =========================================================================
    # Teardown
    # =========================================================================

    async def close(self) -> None:
        """Cancel and await all background tasks in reverse-dependency order.

        Teardown order:
        1. Connection sweeper (cancel first — must not inspect half-closed pools)
        2. Autophagy sweeper tasks (startup + periodic)
        3. Memory signal handler tasks
        4. Derive worker (graceful drain via sentinel, then forced cancel on timeout)

        Resets each task handle attribute on the orchestrator to ``None``
        after cancellation, mirroring the original ``close()`` behavior.
        """
        orch = self._orch
        # Plan 009 — cancel the connection sweeper FIRST so it doesn't
        # try to inspect a half-closed pool while the per-client
        # aclose() calls below are running.
        connection_sweep_task = getattr(orch, "_connection_sweep_task", None)
        if connection_sweep_task is not None and not connection_sweep_task.done():
            connection_sweep_task.cancel()
            with suppress(asyncio.CancelledError):
                await connection_sweep_task
        orch._connection_sweep_task = None

        startup_task = getattr(orch, "_autophagy_startup_sweep_task", None)
        if startup_task is not None and not startup_task.done():
            startup_task.cancel()
        periodic_task = getattr(orch, "_autophagy_sweep_task", None)
        if periodic_task is not None and not periodic_task.done():
            periodic_task.cancel()
        for task in (startup_task, periodic_task):
            if task is None:
                continue
            with suppress(asyncio.CancelledError):
                await task
        orch._autophagy_startup_sweep_task = None
        orch._autophagy_sweep_task = None
        # The rest of teardown mirrors the defensive autophagy pattern
        # above: every attribute is guarded with ``getattr(...)`` so
        # ``close()`` is safe on partially-constructed orchestrators —
        # unit tests that build instances via ``__new__`` to skip
        # ``__init__`` (e.g. tests/test_perf_fixes.py) used to crash
        # here on the first attribute miss.
        signal_bus = getattr(orch, "_memory_signal_bus", None)
        if signal_bus is not None:
            await signal_bus.close()

        derive_worker_task = getattr(orch, "_derive_worker_task", None)
        if derive_worker_task and not derive_worker_task.done():
            derive_queue = getattr(orch, "_derive_queue", None)
            if derive_queue is not None:
                await derive_queue.put(None)
            try:
                await asyncio.wait_for(derive_worker_task, timeout=30.0)
            except asyncio.TimeoutError:
                derive_worker_task.cancel()
                with suppress(asyncio.CancelledError):
                    await derive_worker_task
        orch._derive_worker_task = None
