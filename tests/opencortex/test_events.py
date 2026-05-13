# SPDX-License-Identifier: Apache-2.0
"""Tests for memory lifecycle event dispatch."""

from __future__ import annotations

import asyncio
import tempfile
import unittest

from opencortex.core.identity import IdentityProfile
from opencortex.storage.cfs import CFS
from opencortex.storage.cfs_queue import CFSQueue
from opencortex.store.event.actions import EntityIndexAction
from opencortex.store.event.events import (
    CheckUpdateEvent,
    MemoryEvent,
    MemoryEventManager,
    MemoryStoredEvent,
    SessionEndedEvent,
    SessionTurnStoredEvent,
)
from opencortex.store.event.worker import EventWorker
from opencortex.store.types import ContextType, EventName, SessionRecordLayer
from opencortex.store.writer.reason_tree_build_writer import ReasonTreeBuildWriter
from opencortex.store.writer.reason_tree_index_writer import ReasonTreeIndexWriter
from opencortex.store.writer.search_index_writer import SearchIndexWriter
from opencortex.store.writer.semantic_derive_writer import SemanticDeriveWriter
from opencortex.vector.retrieval.retriever import evidence_snippet


class TestMemoryEventManager(unittest.IsolatedAsyncioTestCase):
    """Event bus dispatch and failure containment."""

    async def test_publish_without_subscribers_is_noop(self) -> None:
        """Publishing with no subscribers should not create tasks."""
        bus = MemoryEventManager()

        bus.publish_nowait(
            MemoryStoredEvent(
                uri="opencortex://tenant/user/memories/test",
                record_id="record-1",
                tenant_id="tenant",
                user_id="user",
                project_id="public",
                context_type="memory",
                category="general",
            )
        )

        await bus.close()


class _CaptureAction:
    """Test action that records accepted events."""

    name = "capture"

    def __init__(
        self,
        event_type: type[MemoryEvent],
        received: list[MemoryEvent],
    ) -> None:
        self.event_type = event_type
        self.received = received

    async def run(self, event: MemoryEvent) -> None:
        """Capture one event."""
        self.received.append(event)


class _FailingAction:
    """Test action that fails without stopping the worker."""

    name = "failing"
    event_type = MemoryStoredEvent

    async def run(self, event: MemoryStoredEvent) -> None:
        """Raise to verify action failure isolation."""
        _ = event
        raise RuntimeError("action failed")


class _TimedCaptureAction:
    """Test action that records concurrent execution windows."""

    name = "timed_capture"
    event_type = MemoryEvent

    def __init__(
        self,
        records: list[tuple[str, str]],
        *,
        started: asyncio.Event | None = None,
        release: asyncio.Event | None = None,
    ) -> None:
        self.records = records
        self.started = started
        self.release = release

    async def run(self, event: MemoryEvent) -> None:
        """Record start/end markers around an optional wait."""
        session_id = getattr(event, "session_id", "")
        self.records.append(("start", session_id))
        if self.started is not None:
            self.started.set()
        if self.release is not None:
            await self.release.wait()
        else:
            await asyncio.sleep(0.05)
        self.records.append(("end", session_id))


def _queue(root: str) -> CFSQueue:
    """Build a fast test queue."""
    return CFSQueue(cfs=CFS(root=root), stale_after_seconds=1)


class TestEventWorker(unittest.IsolatedAsyncioTestCase):
    """Event worker routes supported events to matching actions."""

    async def test_worker_runs_matching_actions(self) -> None:
        """Only actions matching the event type are run."""
        events = MemoryEventManager()
        received: list[MemoryEvent] = []
        with tempfile.TemporaryDirectory() as root:
            worker = EventWorker(
                memory_events=events,
                event_queue=_queue(root),
                actions=[_CaptureAction(MemoryStoredEvent, received)],
            )
            event = MemoryStoredEvent(
                uri="opencortex://tenant/user/memories/test",
                record_id="record-1",
                tenant_id="tenant",
                user_id="user",
                project_id="public",
                context_type="memory",
                category="general",
            )

            failures = await worker.handle(event)

        self.assertEqual(received, [event])
        self.assertEqual(failures, [])

    async def test_action_failure_does_not_stop_next_action(self) -> None:
        """One failing action does not block following actions."""
        events = MemoryEventManager()
        received: list[MemoryEvent] = []
        with tempfile.TemporaryDirectory() as root:
            worker = EventWorker(
                memory_events=events,
                event_queue=_queue(root),
                actions=[
                    _FailingAction(),
                    _CaptureAction(MemoryStoredEvent, received),
                ],
            )
            event = MemoryStoredEvent(
                uri="opencortex://tenant/user/memories/test",
                record_id="record-1",
                tenant_id="tenant",
                user_id="user",
                project_id="public",
                context_type="memory",
                category="general",
            )

            failures = await worker.handle(event)

        self.assertEqual(received, [event])
        self.assertEqual(len(failures), 1)

    async def test_worker_subscribes_to_store_events(self) -> None:
        """The worker receives store/session event types."""
        events = MemoryEventManager()
        received: list[MemoryEvent] = []
        check_update_received: list[MemoryEvent] = []
        with tempfile.TemporaryDirectory() as root:
            worker = EventWorker(
                memory_events=events,
                event_queue=_queue(root),
                actions=[
                    _CaptureAction(MemoryEvent, received),
                    _CaptureAction(CheckUpdateEvent, check_update_received),
                ],
                idle_sleep_seconds=0.01,
            )
            worker.subscribe()
            await worker.start()
            memory_event = MemoryStoredEvent(
                uri="opencortex://tenant/user/memories/test",
                record_id="record-1",
                tenant_id="tenant",
                user_id="user",
                project_id="public",
                context_type="memory",
                category="general",
            )
            turn_event = SessionTurnStoredEvent(
                session_id="session-1",
                turn_id="turn-1",
                tenant_id="tenant",
                user_id="user",
                project_id="public",
                record_uris=["uri-1"],
            )
            check_update_event = CheckUpdateEvent(
                uri="opencortex://tenant/user/memories/test",
                record_id="record-1",
                tenant_id="tenant",
                user_id="user",
                project_id="public",
                context_type="memory",
                category="general",
            )

            events.publish_nowait(memory_event)
            events.publish_nowait(check_update_event)
            events.publish_nowait(turn_event)
            await asyncio.sleep(0.05)
            await worker.close()
            await events.close()

        self.assertEqual(received, [memory_event, turn_event])
        self.assertEqual(check_update_received, [check_update_event])

    async def test_wait_idle_waits_for_publish_tasks_before_queue_idle(self) -> None:
        """Idle waits for async event publication and the persisted queue."""
        events = MemoryEventManager()
        received: list[MemoryEvent] = []
        with tempfile.TemporaryDirectory() as root:
            event_queue = _queue(root)
            worker = EventWorker(
                memory_events=events,
                event_queue=event_queue,
                actions=[_CaptureAction(MemoryStoredEvent, received)],
                idle_sleep_seconds=0.01,
            )

            async def delayed_enqueue(event: MemoryEvent) -> None:
                await asyncio.sleep(0.05)
                worker.enqueue(event)

            events.subscribe(str(EventName.MEMORY_STORED), delayed_enqueue)
            await worker.start()
            event = MemoryStoredEvent(
                uri="opencortex://tenant/user/memories/test",
                record_id="record-1",
                tenant_id="tenant",
                user_id="user",
                project_id="public",
                context_type="memory",
                category="general",
            )

            events.publish_nowait(event)
            await worker.wait_idle(timeout_seconds=2)
            status = event_queue.status(worker.queue_name)
            await worker.close()
            await events.close()

        self.assertEqual(status.pending, 0)
        self.assertEqual(status.processing, 0)
        self.assertEqual(status.done, 1)
        self.assertEqual(status.failed, 0)
        self.assertEqual(received, [event])

    async def test_check_update_only_runs_dedicated_actions(self) -> None:
        """Check-update does not rerun generic memory side effects."""
        events = MemoryEventManager()
        generic_received: list[MemoryEvent] = []
        check_update_received: list[MemoryEvent] = []
        with tempfile.TemporaryDirectory() as root:
            worker = EventWorker(
                memory_events=events,
                event_queue=_queue(root),
                actions=[
                    _CaptureAction(MemoryEvent, generic_received),
                    _CaptureAction(CheckUpdateEvent, check_update_received),
                ],
            )
            event = CheckUpdateEvent(
                uri="opencortex://tenant/user/memories/test",
                record_id="record-1",
                tenant_id="tenant",
                user_id="user",
                project_id="public",
                context_type="memory",
                category="general",
            )

            failures = await worker.handle(event)

        self.assertEqual(failures, [])
        self.assertEqual(generic_received, [])
        self.assertEqual(check_update_received, [event])

    async def test_worker_replays_events_from_persistent_queue(self) -> None:
        """A new worker instance can consume events enqueued by an earlier worker."""
        events = MemoryEventManager()
        received: list[MemoryEvent] = []
        with tempfile.TemporaryDirectory() as root:
            event_queue = _queue(root)
            first_worker = EventWorker(
                memory_events=events,
                event_queue=event_queue,
                actions=[],
            )
            event = MemoryStoredEvent(
                uri="opencortex://tenant/user/memories/test",
                record_id="record-1",
                tenant_id="tenant",
                user_id="user",
                project_id="public",
                context_type="memory",
                category="general",
            )
            first_worker.enqueue(event)

            second_worker = EventWorker(
                memory_events=events,
                event_queue=_queue(root),
                actions=[_CaptureAction(MemoryStoredEvent, received)],
            )
            message = second_worker.event_queue.dequeue(second_worker.queue_name)
            self.assertIsNotNone(message)
            assert message is not None
            await second_worker.process(message)

        self.assertEqual(received, [event])

    async def test_worker_requeues_failed_persistent_message(self) -> None:
        """A failed persistent message remains retryable until max attempts."""
        events = MemoryEventManager()
        with tempfile.TemporaryDirectory() as root:
            event_queue = _queue(root)
            worker = EventWorker(
                memory_events=events,
                event_queue=event_queue,
                actions=[_FailingAction()],
                max_attempts=2,
            )
            worker.enqueue(
                MemoryStoredEvent(
                    uri="opencortex://tenant/user/memories/test",
                    record_id="record-1",
                    tenant_id="tenant",
                    user_id="user",
                    project_id="public",
                    context_type="memory",
                    category="general",
                )
            )

            first = event_queue.dequeue(worker.queue_name)
            self.assertIsNotNone(first)
            assert first is not None
            await worker.process(first)

            self.assertEqual(event_queue.status(worker.queue_name).pending, 1)

    async def test_worker_parallelizes_different_sessions(self) -> None:
        """Workers can process different ordering keys concurrently."""
        events = MemoryEventManager()
        records: list[tuple[str, str]] = []
        first_started = asyncio.Event()
        release = asyncio.Event()
        with tempfile.TemporaryDirectory() as root:
            worker = EventWorker(
                memory_events=events,
                event_queue=_queue(root),
                actions=[
                    _TimedCaptureAction(
                        records,
                        started=first_started,
                        release=release,
                    )
                ],
                concurrency=2,
                idle_sleep_seconds=0.01,
            )
            worker.enqueue(
                SessionTurnStoredEvent(
                    session_id="session-1",
                    turn_id="turn-1",
                    tenant_id="tenant",
                    user_id="user",
                    project_id="public",
                )
            )
            worker.enqueue(
                SessionTurnStoredEvent(
                    session_id="session-2",
                    turn_id="turn-1",
                    tenant_id="tenant",
                    user_id="user",
                    project_id="public",
                )
            )

            await worker.start()
            await asyncio.wait_for(first_started.wait(), timeout=1)
            await asyncio.sleep(0.05)
            release.set()
            await worker.wait_idle(timeout_seconds=2)
            await worker.close()

        self.assertEqual(
            records[:2],
            [("start", "session-1"), ("start", "session-2")],
        )

    async def test_worker_serializes_same_session(self) -> None:
        """Workers do not overlap events with the same ordering key."""
        events = MemoryEventManager()
        records: list[tuple[str, str]] = []
        with tempfile.TemporaryDirectory() as root:
            worker = EventWorker(
                memory_events=events,
                event_queue=_queue(root),
                actions=[_TimedCaptureAction(records)],
                concurrency=2,
                idle_sleep_seconds=0.01,
                locked_key_delay_seconds=0.05,
            )
            for turn_id in ("turn-1", "turn-2"):
                worker.enqueue(
                    SessionTurnStoredEvent(
                        session_id="session-1",
                        turn_id=turn_id,
                        tenant_id="tenant",
                        user_id="user",
                        project_id="public",
                    )
                )

            await worker.start()
            await worker.wait_idle(timeout_seconds=2)
            await worker.close()

        self.assertEqual(
            records,
            [
                ("start", "session-1"),
                ("end", "session-1"),
                ("start", "session-1"),
                ("end", "session-1"),
            ],
        )


class TestSearchIndexWriter(unittest.IsolatedAsyncioTestCase):
    """SearchIndexWriter names the secondary search indexes explicitly."""

    async def test_builds_anchor_and_fact_indexes(self) -> None:
        """Stored primary records can produce AnchorIndex and FactIndex entries."""
        writer = SearchIndexWriter(
            vector_store=None,
            collection_resolver=lambda: "context",
        )
        event = MemoryStoredEvent(
            profile=IdentityProfile(
                tenant_id="tenant",
                user_id="user",
                project_id="public",
            ),
            uri="opencortex://tenant/user/memories/test",
            record_id="record-1",
            context_type="memory",
            category="general",
            record={
                "entities": ["Alice", "Python"],
                "keywords": "Python, Hangzhou",
                "meta": {"anchor_handles": ["alice-handle"]},
                "abstract_json": {
                    "anchors": [
                        {
                            "anchor_type": "entity",
                            "value": "Alice",
                            "text": "Alice",
                        },
                        {
                            "anchor_type": "topic",
                            "value": "OpenCortex",
                            "text": "OpenCortex",
                        },
                    ],
                    "fact_points": [
                        "Alice uses Python",
                        "too",
                        "2024-03",
                        "Alice uses Python",
                        "OpenCortex stores primary records",
                        "Alice visited Tokyo in 2024-03 with 2 teammates.",
                    ],
                },
            },
        )

        anchor_indexes = writer.anchor_indexes(event)
        fact_indexes = writer.fact_indexes(event)

        self.assertEqual(
            [item.text for item in anchor_indexes],
            ["Alice", "Python", "Hangzhou", "OpenCortex", "alice-handle"],
        )
        self.assertEqual(
            [item.anchor_type for item in anchor_indexes],
            ["entity", "entity", "keyword", "topic", "handle"],
        )
        self.assertEqual(
            [item.text for item in fact_indexes],
            [
                "Alice visited Tokyo in 2024-03 with 2 teammates.",
                "Alice uses Python",
                "OpenCortex stores primary records",
            ],
        )
        self.assertEqual(anchor_indexes[0].source_uri, event.uri)
        self.assertEqual(fact_indexes[0].source_record_id, event.record_id)
        self.assertNotIn("2024-03", [item.text for item in fact_indexes])


class TestEntityIndexAction(unittest.IsolatedAsyncioTestCase):
    """EntityIndexAction writes entity Qdrant projections."""

    async def test_writes_entity_index_records(self) -> None:
        """Stored primary entities are written as Qdrant index records."""
        vector_store = _VectorStore()
        action = EntityIndexAction(
            vector_store=vector_store,
            collection_resolver=lambda: "context",
            embedder=_Embedder(),
        )
        event = MemoryStoredEvent(
            profile=IdentityProfile(
                tenant_id="tenant",
                user_id="user",
                project_id="public",
            ),
            uri="opencortex://tenant/user/memories/public/events/1",
            record_id="record-1",
            context_type="memory",
            category="events",
            record={
                "id": "record-1",
                "uri": "opencortex://tenant/user/memories/public/events/1",
                "is_leaf": True,
                "retrieval_ready": True,
                "context_type": "memory",
                "category": "events",
                "entities": ["Alice", "PYTHON", "alice"],
            },
        )

        await action.run(event)

        self.assertEqual(len(vector_store.records), 2)
        first = vector_store.records[0]
        self.assertEqual(first["retrieval_surface"], "entity_index")
        self.assertEqual(first["source_uri"], event.uri)
        self.assertEqual(first["source_record_id"], event.record_id)
        self.assertEqual(first["entity_text"], "Alice")
        self.assertEqual(first["entities"], ["Alice"])
        self.assertEqual(
            [record["entity_text"] for record in vector_store.records],
            ["Alice", "PYTHON"],
        )
        self.assertEqual(first["vector"], [0.1, 0.2])

    async def test_async_subscriber_receives_event(self) -> None:
        """Async subscribers receive the published event payload."""
        bus = MemoryEventManager()
        received: list[MemoryStoredEvent] = []
        delivered = asyncio.Event()

        async def handler(event: MemoryStoredEvent) -> None:
            received.append(event)
            delivered.set()

        bus.subscribe("memory_stored", handler)
        event = MemoryStoredEvent(
            uri="opencortex://tenant/user/memories/test",
            record_id="record-1",
            tenant_id="tenant",
            user_id="user",
            project_id="public",
            context_type="memory",
            category="general",
        )

        bus.publish_nowait(event)
        await asyncio.wait_for(delivered.wait(), timeout=1)

        self.assertEqual(received, [event])
        await bus.close()

    async def test_handler_exception_is_contained(self) -> None:
        """A failing plugin handler should not leak to the publisher."""
        bus = MemoryEventManager()
        delivered = asyncio.Event()

        async def failing_handler(_event: MemoryStoredEvent) -> None:
            delivered.set()
            raise RuntimeError("plugin failed")

        bus.subscribe("memory_stored", failing_handler)

        bus.publish_nowait(
            MemoryStoredEvent(
                uri="opencortex://tenant/user/memories/test",
                record_id="record-1",
                tenant_id="tenant",
                user_id="user",
                project_id="public",
                context_type="memory",
                category="general",
            )
        )
        await asyncio.wait_for(delivered.wait(), timeout=1)
        await bus.close()


class TestRecallEvidence(unittest.TestCase):
    """Recall evidence projection prefers concrete facts."""

    def test_evidence_snippet_prefers_fact_points(self) -> None:
        """A concrete fact point beats generic summary text."""
        record = {
            "abstract": "Alice shared travel history.",
            "overview": "Alice discussed a trip.",
            "content": "Full content.",
            "abstract_json": {
                "fact_points": [
                    "Alice shared travel history.",
                    "Alice went to Tokyo in 2024-03 with 2 teammates.",
                ],
            },
        }

        self.assertEqual(
            evidence_snippet(record),
            "Alice went to Tokyo in 2024-03 with 2 teammates.",
        )


class _VectorStore:
    """Capture upserted vector payloads."""

    def __init__(self) -> None:
        self.records: list[dict[str, object]] = []

    async def upsert(self, _collection: str, record: dict[str, object]) -> str:
        """Capture one upsert."""
        self.records.append(record)
        return str(record["id"])

    async def upsert_many(
        self,
        _collection: str,
        records: list[dict[str, object]],
    ) -> list[str]:
        """Capture one batch upsert."""
        self.records.extend(records)
        return [str(record["id"]) for record in records]


class _LLM:
    """Fake LLM completion returning layer derivation JSON."""

    async def __call__(
        self,
        _prompt: str,
        *,
        system_prompt: str | None = None,
    ) -> str:
        """Return deterministic layer JSON."""
        _ = system_prompt
        return (
            "{"
            '"abstract":"Alice moved to Hangzhou.",'
            '"overview":"Alice moved to Hangzhou for work.",'
            '"keywords":["Hangzhou","work"],'
            '"entities":["Alice"],'
            '"anchor_handles":["Alice"],'
            '"fact_points":["Alice moved to Hangzhou."]'
            "}"
        )


class _GenericLLM:
    """Fake LLM completion that loses exact raw details."""

    async def __call__(
        self,
        _prompt: str,
        *,
        system_prompt: str | None = None,
    ) -> str:
        """Return intentionally generic layer JSON."""
        _ = system_prompt
        return (
            "{"
            '"abstract":"Alice shared travel history.",'
            '"overview":"Alice discussed a trip.",'
            '"keywords":["travel"],'
            '"entities":["Alice"],'
            '"anchor_handles":["Alice"],'
            '"fact_points":["Alice shared travel history."]'
            "}"
        )


class _TreeLLM:
    """Fake LLM completion returning a reason tree."""

    async def __call__(
        self,
        _prompt: str,
        *,
        system_prompt: str | None = None,
    ) -> str:
        """Return deterministic reason-tree JSON."""
        _ = system_prompt
        return (
            "{"
            '"abstract":"Tree abstract.",'
            '"overview":"Tree overview.",'
            '"nodes":['
            "{"
            '"title":"Decision",'
            '"summary":"Use CFS for storage.",'
            '"fact_points":["CFS stores files."],'
            '"source_refs":["message-1"],'
            '"children":[]'
            "}"
            "]"
            "}"
        )


class _Embedder:
    """Fake embedder returning dense vectors."""

    def embed(self, _text: str) -> object:
        """Return one deterministic embedding."""
        return type(
            "Embedding",
            (),
            {"dense_vector": [0.1, 0.2], "sparse_vector": None},
        )()

    def embed_batch(self, texts: list[str]) -> list[object]:
        """Return one deterministic embedding for each text."""
        return [self.embed(text) for text in texts]


class _CaptureEmbedder(_Embedder):
    """Fake embedder that records embedding texts."""

    def __init__(self) -> None:
        self.texts: list[str] = []

    def embed(self, text: str) -> object:
        """Capture embedding text before returning a vector."""
        self.texts.append(text)
        return super().embed(text)


class _Namespace:
    """Namespace fake for reason-tree tests."""

    @staticmethod
    def path(uri: str) -> object:
        """Unused compatibility helper."""
        _ = uri
        return None

    @staticmethod
    def parent(uri: str) -> str:
        """Return a simple URI parent."""
        prefix = "opencortex://"
        path = uri[len(prefix) :].strip("/")
        if "/" not in path:
            return ""
        return f"{prefix}{path.rsplit('/', maxsplit=1)[0]}"

    def parent_chain(self, parent_uri: str) -> list[str]:
        """Return root-to-leaf parent chain."""
        chain = []
        current = parent_uri
        while current:
            chain.append(current)
            current = self.parent(current)
        return list(reversed(chain))

    @staticmethod
    def segments(uri: str) -> list[str]:
        """Return URI path segments."""
        return [part for part in uri.removeprefix("opencortex://").split("/") if part]


class TestSemanticDeriveWriter(unittest.IsolatedAsyncioTestCase):
    """SemanticDeriveWriter completes raw primary records in the worker."""

    async def test_completes_raw_primary_record(self) -> None:
        """Raw records become retrieval-ready primary records."""
        vector_store = _VectorStore()
        writer = SemanticDeriveWriter(
            vector_store=vector_store,
            collection_resolver=lambda: "context",
            llm_completion=_LLM(),
            embedder=_Embedder(),
        )
        event = MemoryStoredEvent(
            profile=IdentityProfile(
                tenant_id="tenant",
                user_id="user",
                project_id="public",
            ),
            uri="opencortex://tenant/user/memories/public/events/1",
            record_id="record-1",
            context_type="memory",
            category="events",
            content="assistant: Alice moved to Hangzhou.",
            record={
                "id": "record-1",
                "uri": "opencortex://tenant/user/memories/public/events/1",
                "parent_uri": "opencortex://tenant/user/memories/public/events",
                "context_type": "memory",
                "category": "events",
                "is_leaf": True,
                "content": "assistant: Alice moved to Hangzhou.",
                "meta": {},
                "derive_status": "pending",
                "retrieval_ready": False,
            },
        )

        ready_record = await writer.write(event)

        self.assertTrue(ready_record["retrieval_ready"])
        self.assertEqual(ready_record["derive_status"], "ready")
        self.assertEqual(ready_record["abstract"], "Alice moved to Hangzhou.")
        self.assertEqual(ready_record["entities"], ["Alice"])
        self.assertEqual(ready_record["retrieval_surface"], "l0_object")
        self.assertEqual(ready_record["vector"], [0.1, 0.2])
        self.assertEqual(len(vector_store.records), 1)

    async def test_preserves_raw_facts_when_derivation_is_generic(self) -> None:
        """Raw precise details survive generic LLM-derived summaries."""
        vector_store = _VectorStore()
        embedder = _CaptureEmbedder()
        writer = SemanticDeriveWriter(
            vector_store=vector_store,
            collection_resolver=lambda: "context",
            llm_completion=_GenericLLM(),
            embedder=embedder,
        )
        content = "assistant: Alice went to Tokyo in 2024-03 with 2 teammates."
        event = MemoryStoredEvent(
            profile=IdentityProfile(
                tenant_id="tenant",
                user_id="user",
                project_id="public",
            ),
            uri="opencortex://tenant/user/memories/public/events/1",
            record_id="record-1",
            context_type="memory",
            category="events",
            content=content,
            record={
                "id": "record-1",
                "uri": "opencortex://tenant/user/memories/public/events/1",
                "parent_uri": "opencortex://tenant/user/memories/public/events",
                "context_type": "memory",
                "category": "events",
                "is_leaf": True,
                "content": content,
                "meta": {},
                "derive_status": "pending",
                "retrieval_ready": False,
            },
        )

        ready_record = await writer.write(event)

        fact_points = ready_record["abstract_json"]["fact_points"]
        self.assertIn("Alice went to Tokyo in 2024-03 with 2 teammates.", fact_points)
        self.assertIn("2024-03", ready_record["abstract"])
        self.assertIn("Facts:", ready_record["overview"])
        self.assertEqual(ready_record["meta"]["time_refs"], ["2024-03"])
        self.assertEqual(ready_record["date_range_start"], "2024-03-01T00:00:00+00:00")
        self.assertIn("2024-03", embedder.texts[0])
        self.assertIn("2 teammates", embedder.texts[0])


class TestReasonTreeIndexWriter(unittest.IsolatedAsyncioTestCase):
    """ReasonTreeIndexWriter projects ready primary records."""

    async def test_writes_ready_primary_projection(self) -> None:
        """Ready primary records produce reason-tree index payloads."""
        vector_store = _VectorStore()
        writer = ReasonTreeIndexWriter(
            vector_store=vector_store,
            collection_resolver=lambda: "context",
            namespace=_Namespace(),
            embedder=_Embedder(),
        )
        event = MemoryStoredEvent(
            profile=IdentityProfile(
                tenant_id="tenant",
                user_id="user",
                project_id="public",
            ),
            uri="opencortex://tenant/user/memories/public/events/1",
            record_id="record-1",
            context_type="memory",
            category="events",
            record={
                "id": "record-1",
                "uri": "opencortex://tenant/user/memories/public/events/1",
                "parent_uri": "opencortex://tenant/user/memories/public/events",
                "context_type": "memory",
                "category": "events",
                "is_leaf": True,
                "abstract": "Alice moved to Hangzhou.",
                "overview": "Alice moved to Hangzhou for work.",
                "retrieval_ready": True,
                "entities": ["Alice"],
                "keywords": "Hangzhou",
                "anchor_hits": ["Alice"],
                "memory_kind": "episodic",
                "meta": {
                    "layer": "merged",
                    "source_uris": [
                        "opencortex://tenant/user/memories/public/events/s1/immediate/a"
                    ],
                },
            },
        )

        await writer.write(event)

        self.assertEqual(len(vector_store.records), 1)
        record = vector_store.records[0]
        self.assertEqual(record["retrieval_surface"], "reason_tree_index")
        self.assertEqual(record["source_uri"], event.uri)
        self.assertEqual(record["reason_role"], "session_segment")
        self.assertEqual(record["context_window"], "parent_siblings")
        self.assertEqual(record["tree_uri"], event.record["parent_uri"])
        self.assertEqual(record["path_segments"][-1], "1")
        self.assertEqual(record["title"], "Alice moved to Hangzhou.")
        self.assertEqual(record["summary"], "Alice moved to Hangzhou for work.")
        self.assertEqual(
            record["source_refs"],
            [
                "opencortex://tenant/user/memories/public/events/1",
                "opencortex://tenant/user/memories/public/events/s1/immediate/a",
            ],
        )
        self.assertIn(event.record["parent_uri"], record["cone_neighbors"])
        self.assertEqual(record["vector"], [0.1, 0.2])


class TestReasonTreeBuildWriter(unittest.IsolatedAsyncioTestCase):
    """ReasonTreeBuildWriter adds LLM-enhanced resource/session tree nodes."""

    async def test_writes_resource_reason_tree_nodes(self) -> None:
        """Resource primary records produce enhanced reason-tree node indexes."""
        vector_store = _VectorStore()
        writer = ReasonTreeBuildWriter(
            vector_store=vector_store,
            collection_resolver=lambda: "context",
            llm_completion=_TreeLLM(),
            embedder=_Embedder(),
        )
        event = MemoryStoredEvent(
            profile=IdentityProfile(
                tenant_id="tenant",
                user_id="user",
                project_id="public",
            ),
            uri="opencortex://tenant/user/resources/public/doc",
            record_id="record-1",
            context_type=str(ContextType.RESOURCE),
            category="semantic",
            content="Resource content about CFS.",
            record={
                "id": "record-1",
                "uri": "opencortex://tenant/user/resources/public/doc",
                "parent_uri": "opencortex://tenant/user/resources/public",
                "context_type": str(ContextType.RESOURCE),
                "category": "semantic",
                "content": "Resource content about CFS.",
                "retrieval_ready": True,
                "meta": {"source_path": "/docs/cfs.md"},
            },
        )

        await writer.write(event)

        self.assertEqual(len(vector_store.records), 1)
        record = vector_store.records[0]
        self.assertEqual(record["retrieval_surface"], "reason_tree_index")
        self.assertEqual(record["source_uri"], event.uri)
        self.assertTrue(record["uri"].startswith(f"{event.uri}/reason_tree/"))
        self.assertEqual(record["reason_role"], "resource_tree_node")
        self.assertEqual(record["title"], "Decision")
        self.assertEqual(record["summary"], "Use CFS for storage.")
        self.assertEqual(record["fact_points"], ["CFS stores files."])
        self.assertIn("/docs/cfs.md", record["source_refs"])
        self.assertEqual(record["vector"], [0.1, 0.2])

    async def test_writes_session_final_reason_tree_nodes(self) -> None:
        """Session final primary records produce enhanced reason-tree nodes."""
        vector_store = _VectorStore()
        writer = ReasonTreeBuildWriter(
            vector_store=vector_store,
            collection_resolver=lambda: "context",
            llm_completion=_TreeLLM(),
            embedder=_Embedder(),
        )
        event = SessionEndedEvent(
            profile=IdentityProfile(
                tenant_id="tenant",
                user_id="user",
                project_id="public",
                session_id="session-1",
            ),
            final_uri="opencortex://tenant/user/memories/public/events/session/final",
            merged_uris=["opencortex://tenant/user/memories/public/events/merged"],
            content="Final session content about CFS.",
            record={
                "id": "record-final",
                "uri": "opencortex://tenant/user/memories/public/events/session/final",
                "parent_uri": "opencortex://tenant/user/memories/public/events",
                "context_type": str(ContextType.MEMORY),
                "category": "events",
                "content": "Final session content about CFS.",
                "session_id": "session-1",
                "retrieval_ready": True,
                "meta": {
                    "layer": str(SessionRecordLayer.FINAL),
                    "merged_uris": [
                        "opencortex://tenant/user/memories/public/events/merged"
                    ],
                },
            },
        )

        await writer.write(event)

        self.assertEqual(len(vector_store.records), 1)
        record = vector_store.records[0]
        self.assertEqual(record["reason_role"], "session_tree_node")
        self.assertEqual(record["session_id"], "session-1")
        self.assertIn(event.merged_uris[0], record["source_refs"])


if __name__ == "__main__":
    unittest.main()
