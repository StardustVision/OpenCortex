# SPDX-License-Identifier: Apache-2.0
"""Tests for memory write context assembly."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock

from opencortex.http.request_context import reset_request_identity, set_request_identity
from opencortex.services.memory_write_context_builder import MemoryWriteContextBuilder


class TestMemoryWriteContextBuilder(unittest.IsolatedAsyncioTestCase):
    """Verify the extracted write context builder boundary."""

    def _build_builder(self) -> tuple[MemoryWriteContextBuilder, Any]:
        def build_abstract_json(**kwargs: Any) -> Dict[str, Any]:
            return dict(kwargs)

        def memory_object_payload(
            abstract_json: Dict[str, Any],
            *,
            is_leaf: bool,
        ) -> Dict[str, Any]:
            return {
                "memory_kind": "preference",
                "merge_signature": f"sig:{abstract_json['category']}",
                "mergeable": is_leaf,
            }

        write_service = SimpleNamespace(
            _auto_uri=MagicMock(
                return_value="opencortex://tenant/user/memories/preferences/generated"
            ),
            _resolve_unique_uri=AsyncMock(
                return_value="opencortex://tenant/user/memories/preferences/generated-1"
            ),
            _get_record_by_uri=AsyncMock(return_value={"id": "existing-id"}),
            _derive_parent_uri=MagicMock(
                return_value="opencortex://tenant/user/memories/preferences"
            ),
            _extract_category_from_uri=MagicMock(return_value="preferences"),
            _build_abstract_json=MagicMock(side_effect=build_abstract_json),
            _memory_object_payload=MagicMock(side_effect=memory_object_payload),
        )
        return MemoryWriteContextBuilder(write_service), write_service

    async def test_resolve_target_auto_uri_and_explicit_metadata(self) -> None:
        """Auto URI resolution copies meta and extracts explicit fields."""
        builder, orch = self._build_builder()
        meta = {"entities": ["Alice", "Alice"], "topics": "auth"}

        target = await builder.resolve_target(
            abstract="Alice prefers focused auth docs",
            category="preferences",
            context_type="memory",
            meta=meta,
            parent_uri=None,
            uri=None,
        )

        orch._auto_uri.assert_called_once_with(
            "memory",
            "preferences",
            abstract="Alice prefers focused auth docs",
        )
        orch._resolve_unique_uri.assert_awaited_once()
        orch._get_record_by_uri.assert_not_awaited()
        self.assertEqual(
            target.uri,
            "opencortex://tenant/user/memories/preferences/generated-1",
        )
        self.assertEqual(
            target.parent_uri,
            "opencortex://tenant/user/memories/preferences",
        )
        self.assertIsNone(target.existing_record)
        self.assertEqual(target.explicit_entities, ["Alice"])
        self.assertEqual(target.explicit_topics, ["auth"])
        self.assertIsNot(target.meta, meta)

    async def test_resolve_target_explicit_uri_loads_existing_record(self) -> None:
        """Explicit URI path loads an existing record and reuses parent input."""
        builder, orch = self._build_builder()

        target = await builder.resolve_target(
            abstract="Existing note",
            category="preferences",
            context_type="memory",
            meta={},
            parent_uri="opencortex://tenant/user/memories",
            uri="opencortex://tenant/user/memories/preferences/existing",
        )

        orch._auto_uri.assert_not_called()
        orch._get_record_by_uri.assert_awaited_once_with(
            "opencortex://tenant/user/memories/preferences/existing"
        )
        self.assertEqual(target.existing_record, {"id": "existing-id"})
        self.assertEqual(target.parent_uri, "opencortex://tenant/user/memories")

    async def test_assemble_context_merges_metadata_and_payloads(self) -> None:
        """Post-derive assembly builds Context, abstract JSON, and payload."""
        builder, _orch = self._build_builder()
        tokens = set_request_identity("tenant-1", "user-1")
        try:
            target = await builder.resolve_target(
                abstract="",
                category="preferences",
                context_type="memory",
                meta={
                    "entities": ["Alice"],
                    "topics": ["auth"],
                    "anchor_handles": ["Alice"],
                },
                parent_uri=None,
                uri=None,
            )
            assembled = builder.assemble_context(
                target=target,
                abstract="Alice prefers JWT",
                overview="Auth preference",
                content="Alice prefers JWT login flows.",
                category="preferences",
                context_type="memory",
                is_leaf=True,
                related_uri=["opencortex://related"],
                session_id="session-1",
                embed_text="custom embed",
                layers={
                    "keywords": "auth, JWT",
                    "entities": ["Alice", "JWT"],
                    "anchor_handles": ["Alice", "JWT"],
                    "fact_points": ["Alice prefers JWT"],
                },
            )
        finally:
            reset_request_identity(tokens)

        self.assertEqual(assembled.entities, ["Alice", "JWT"])
        self.assertEqual(assembled.keywords_list, ["auth", "JWT"])
        self.assertEqual(assembled.keywords, "auth, JWT")
        self.assertEqual(assembled.meta["topics"], ["auth", "JWT"])
        self.assertEqual(assembled.meta["anchor_handles"], ["Alice", "JWT"])
        self.assertEqual(assembled.effective_category, "preferences")
        self.assertEqual(assembled.merge_signature, "sig:preferences")
        self.assertTrue(assembled.mergeable)

        ctx = assembled.ctx
        self.assertEqual(ctx.user.tenant_id, "tenant-1")
        self.assertEqual(ctx.user.user_id, "user-1")
        self.assertEqual(ctx.session_id, "session-1")
        self.assertEqual(ctx.related_uri, ["opencortex://related"])
        self.assertEqual(ctx.get_vectorization_text(), "custom embed auth, JWT")
        self.assertEqual(assembled.abstract_json["fact_points"], ["Alice prefers JWT"])
        self.assertEqual(assembled.abstract_json["parent_uri"], target.parent_uri)
        self.assertEqual(assembled.object_payload["memory_kind"], "preference")

    async def test_assemble_context_reuses_existing_record_id(self) -> None:
        """Explicit URI updates keep the existing record id on the Context."""
        builder, _orch = self._build_builder()
        target = await builder.resolve_target(
            abstract="Existing note",
            category="preferences",
            context_type="memory",
            meta={},
            parent_uri=None,
            uri="opencortex://tenant/user/memories/preferences/existing",
        )

        assembled = builder.assemble_context(
            target=target,
            abstract="Existing note",
            overview="",
            content="",
            category="preferences",
            context_type="memory",
            is_leaf=True,
            related_uri=None,
            session_id=None,
            embed_text="",
            layers={},
        )

        self.assertEqual(assembled.ctx.id, "existing-id")
