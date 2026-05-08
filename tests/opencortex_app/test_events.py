# SPDX-License-Identifier: Apache-2.0
"""Tests for memory lifecycle event dispatch."""

from __future__ import annotations

import asyncio
import tempfile
import unittest

from opencortex_app.core.identity import IdentityProfile
from opencortex_app.storage.cfs import CFS
from opencortex_app.storage.cfs_queue import CFSQueue
from opencortex_app.store.event.actions import EntityIndexAction
from opencortex_app.store.event.events import (
    MemoryEvent,
    MemoryEventManager,
    MemoryStoredEvent,
    SessionTurnStoredEvent,
)
from opencortex_app.store.event.worker import EventWorker
from opencortex_app.store.writer.reason_tree_index_writer import ReasonTreeIndexWriter
from opencortex_app.store.writer.search_index_writer import SearchIndexWriter
from opencortex_app.store.writer.semantic_derive_writer import SemanticDeriveWriter


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

    async def test_worker_subscribes_to_four_store_events(self) -> None:
        """The worker receives the four store/session event types."""
        events = MemoryEventManager()
        received: list[MemoryEvent] = []
        with tempfile.TemporaryDirectory() as root:
            worker = EventWorker(
                memory_events=events,
                event_queue=_queue(root),
                actions=[_CaptureAction(MemoryEvent, received)],
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

            events.publish_nowait(memory_event)
            events.publish_nowait(turn_event)
            await asyncio.sleep(0.05)
            await worker.close()
            await events.close()

        self.assertEqual(received, [memory_event, turn_event])

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
                        "Alice uses Python",
                        "OpenCortex stores primary records",
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
                "Alice uses Python",
                "OpenCortex stores primary records",
            ],
        )
        self.assertEqual(anchor_indexes[0].source_uri, event.uri)
        self.assertEqual(fact_indexes[0].source_record_id, event.record_id)


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


class _VectorStore:
    """Capture upserted vector payloads."""

    def __init__(self) -> None:
        self.records: list[dict[str, object]] = []

    async def upsert(self, _collection: str, record: dict[str, object]) -> str:
        """Capture one upsert."""
        self.records.append(record)
        return str(record["id"])


class _LLM:
    """Fake LLM completion returning layer derivation JSON."""

    async def __call__(self, _prompt: str) -> str:
        """Return deterministic layer JSON."""
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
        self.assertIn(event.record["parent_uri"], record["cone_neighbors"])
        self.assertEqual(record["vector"], [0.1, 0.2])


if __name__ == "__main__":
    unittest.main()
