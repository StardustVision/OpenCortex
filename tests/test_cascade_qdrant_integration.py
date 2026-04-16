# SPDX-License-Identifier: Apache-2.0
"""
Integration tests for Plan 006 cascade delete semantics against real Qdrant.

Background
----------
Plan 006 (fact_points + URI minimum-cost scoring) introduced derived records
at URIs like:

    {leaf_uri}/fact_points/{digest}
    {leaf_uri}/anchors/{digest}

Deletion of a leaf relies on ``QdrantStorageAdapter.remove_by_uri(uri)``, which
uses ``models.MatchText(text=uri)`` against a TEXT-indexed ``uri`` payload
field.  ``MatchText`` is token-based (not a strict prefix), so the cascade
behaviour is fundamentally different from the in-memory test double used in
Plan 006 Unit 7 (``InMemoryStorage`` does literal ``str.startswith``).

These tests exercise real embedded Qdrant to confirm whether the cascade is
safe in production or whether the adapter over-/under-deletes.  They are
intentionally low-level — no orchestrator, no embedder — so a failure points
directly at ``remove_by_uri`` semantics.

Key observed Qdrant behaviour
-----------------------------
* ``uri`` is declared as ``FieldType: "path"`` → ``_infer_payload_type``
  maps to ``PayloadSchemaType.TEXT`` with Qdrant's default tokenizer.
* ``MatchText(text=X)`` requires that every token of ``X`` is present in the
  indexed field.  With the default tokenizer the URI
  ``opencortex://t/u/mem/leafA`` produces tokens such as ``opencortex``,
  ``t``, ``u``, ``mem``, ``leafA`` (exact splits depend on the tokenizer
  version; verified empirically below).
* Because the match is token-subset, deleting ``leafA`` does NOT affect
  ``leafB`` (different token set), even though ``startswith`` would also
  leave ``leafB`` alone — so for typical URIs the behaviour matches the
  in-memory test double.
* The risk case is when two leaves share *every* token but differ only in
  positional order / duplicated tokens — MatchText does not enforce order,
  so two URIs whose tokens are identical sets would collide.  See
  ``test_token_overlap_sibling_uris``.
"""

import asyncio
import math
import os
import shutil
import sys
import tempfile
import unittest
from typing import Any, Dict, List

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from opencortex.storage.qdrant.adapter import QdrantStorageAdapter


VECTOR_DIM = 4

_CONTEXT_SCHEMA = {
    "CollectionName": "context",
    "Description": "Cascade integration test",
    "Fields": [
        {"FieldName": "id", "FieldType": "string", "IsPrimaryKey": True},
        {"FieldName": "uri", "FieldType": "path"},
        {"FieldName": "vector", "FieldType": "vector", "Dim": VECTOR_DIM},
        {"FieldName": "abstract", "FieldType": "string"},
        {"FieldName": "overview", "FieldType": "string"},
        {"FieldName": "parent_uri", "FieldType": "path"},
        {"FieldName": "is_leaf", "FieldType": "bool"},
        {"FieldName": "retrieval_surface", "FieldType": "string"},
        {"FieldName": "projection_target_uri", "FieldType": "string"},
    ],
    "ScalarIndex": [
        "uri",
        "parent_uri",
        "is_leaf",
        "retrieval_surface",
        "projection_target_uri",
    ],
}


def _vec(seed: int) -> List[float]:
    raw = [
        ((seed >> 0) & 0xFF) / 255.0,
        ((seed >> 8) & 0xFF) / 255.0,
        ((seed >> 16) & 0xFF) / 255.0,
        ((seed >> 24) & 0xFF) / 255.0,
    ]
    norm = math.sqrt(sum(x * x for x in raw)) or 1.0
    return [x / norm for x in raw]


def _leaf(rid: str, uri: str, seed: int = 1) -> Dict[str, Any]:
    return {
        "id": rid,
        "uri": uri,
        "abstract": "leaf",
        "overview": "leaf overview",
        "parent_uri": "",
        "is_leaf": True,
        "retrieval_surface": "l0_object",
        "projection_target_uri": "",
        "vector": _vec(seed),
    }


def _fact_point(rid: str, leaf_uri: str, digest: str, seed: int = 2) -> Dict[str, Any]:
    return {
        "id": rid,
        "uri": f"{leaf_uri}/fact_points/{digest}",
        "abstract": "",
        "overview": f"fact {digest}",
        "parent_uri": leaf_uri,
        "is_leaf": False,
        "retrieval_surface": "fact_point",
        "projection_target_uri": leaf_uri,
        "vector": _vec(seed),
    }


def _anchor(rid: str, leaf_uri: str, digest: str, seed: int = 3) -> Dict[str, Any]:
    return {
        "id": rid,
        "uri": f"{leaf_uri}/anchors/{digest}",
        "abstract": "",
        "overview": f"anchor {digest}",
        "parent_uri": leaf_uri,
        "is_leaf": False,
        "retrieval_surface": "anchor_projection",
        "projection_target_uri": leaf_uri,
        "vector": _vec(seed),
    }


class _Base(unittest.TestCase):
    """Shared fixture: embedded Qdrant in a temp directory."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="qdrant_cascade_")
        self.adapter = QdrantStorageAdapter(
            path=self.temp_dir,
            embedding_dim=VECTOR_DIM,
        )
        self._run(self.adapter.create_collection("context", _CONTEXT_SCHEMA))

    def tearDown(self):
        asyncio.run(self._cleanup())

    async def _cleanup(self):
        try:
            await self.adapter.close()
        except Exception:
            pass
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _run(self, coro):
        return asyncio.run(coro)

    # --- helpers ---

    async def _count_by_uri_prefix(self, prefix: str) -> int:
        """Count records whose uri starts with *prefix* via client-side scan.

        Doesn't use MatchText — so this reflects ground truth, independent of
        the semantics we're testing.
        """
        out = 0
        cursor = None
        while True:
            records, cursor = await self.adapter.scroll(
                "context", limit=100, cursor=cursor,
            )
            for r in records:
                if str(r.get("uri", "")).startswith(prefix):
                    out += 1
            if cursor is None:
                break
        return out

    async def _uris_starting_with(self, prefix: str) -> List[str]:
        """Return every stored URI whose literal string starts with *prefix*."""
        out: List[str] = []
        cursor = None
        while True:
            records, cursor = await self.adapter.scroll(
                "context", limit=100, cursor=cursor,
            )
            for r in records:
                uri = str(r.get("uri", ""))
                if uri.startswith(prefix):
                    out.append(uri)
            if cursor is None:
                break
        return out


class TestCascadeHappyPath(_Base):
    """Happy-path cascade scenarios — do fact_points / anchors go away?"""

    def test_cascade_deletes_fact_points_under_leaf(self):
        """``remove_by_uri(leaf_uri)`` must remove fact_points below the leaf."""
        async def scenario():
            leaf_uri = "opencortex://t/u/mem/leafAlpha"
            await self.adapter.insert("context", _leaf("L1", leaf_uri))
            await self.adapter.insert("context", _fact_point("F1", leaf_uri, "aaa111"))
            await self.adapter.insert("context", _fact_point("F2", leaf_uri, "bbb222"))
            await self.adapter.insert("context", _fact_point("F3", leaf_uri, "ccc333"))

            await self.adapter.remove_by_uri("context", leaf_uri)

            remaining = await self._count_by_uri_prefix(leaf_uri)
            self.assertEqual(
                remaining, 0,
                f"Expected all records under {leaf_uri!r} removed, found {remaining}",
            )
        self._run(scenario())

    def test_cascade_deletes_anchor_projections_under_leaf(self):
        """Anchor projections below the leaf go away when the leaf is removed."""
        async def scenario():
            leaf_uri = "opencortex://t/u/mem/leafBravo"
            await self.adapter.insert("context", _leaf("L1", leaf_uri))
            await self.adapter.insert("context", _anchor("A1", leaf_uri, "deadbeef01"))
            await self.adapter.insert("context", _anchor("A2", leaf_uri, "deadbeef02"))

            await self.adapter.remove_by_uri("context", leaf_uri)

            remaining = await self._count_by_uri_prefix(leaf_uri)
            self.assertEqual(remaining, 0)
        self._run(scenario())


class TestCascadeSiblingSafety(_Base):
    """Sibling leaves must survive when only one is deleted."""

    def test_sibling_leaves_not_affected_by_cascade(self):
        """Distinct sibling leaves + fact_points are not collateral damage."""
        async def scenario():
            leaf_a = "opencortex://t/u/mem/leafCharlie"
            leaf_b = "opencortex://t/u/mem/leafDelta"
            await self.adapter.insert("context", _leaf("LA", leaf_a, seed=10))
            await self.adapter.insert("context", _leaf("LB", leaf_b, seed=20))
            await self.adapter.insert("context", _fact_point("A1", leaf_a, "1111aaaa"))
            await self.adapter.insert("context", _fact_point("A2", leaf_a, "2222bbbb"))
            await self.adapter.insert("context", _fact_point("B1", leaf_b, "3333cccc"))
            await self.adapter.insert("context", _fact_point("B2", leaf_b, "4444dddd"))

            await self.adapter.remove_by_uri("context", leaf_a)

            leaf_b_survivors = await self._count_by_uri_prefix(leaf_b)
            self.assertEqual(
                leaf_b_survivors, 3,
                f"leafDelta + 2 fact_points expected intact, got {leaf_b_survivors}",
            )
            leaf_a_left = await self._count_by_uri_prefix(leaf_a)
            self.assertEqual(leaf_a_left, 0)
        self._run(scenario())

    def test_token_overlap_sibling_uris(self):
        """Sibling URIs whose tokens partially overlap remain isolated.

        Constructed so two leaves share the ``abc123`` segment but differ in
        the suffix.  MatchText(tokenised) on ``leafA`` URI includes the
        distinguishing suffix token, so ``leafB`` records must survive.

        This test documents the boundary case for URI naming scheme safety:
        as long as the *full leaf URI* contains at least one token that is
        unique to it, MatchText cascade is safe.
        """
        async def scenario():
            leaf_a = "opencortex://t/u/mem/abc123def"
            leaf_b = "opencortex://t/u/mem/abc123xyz"
            await self.adapter.insert("context", _leaf("LA", leaf_a, seed=11))
            await self.adapter.insert("context", _leaf("LB", leaf_b, seed=21))
            await self.adapter.insert("context", _fact_point("FA", leaf_a, "f01d01"))
            await self.adapter.insert("context", _fact_point("FB", leaf_b, "f02d02"))

            await self.adapter.remove_by_uri("context", leaf_a)

            survived_b = await self._uris_starting_with(leaf_b)
            self.assertEqual(
                len(survived_b), 2,
                f"leafB (and its fact_point) must survive: got {survived_b}",
            )
            survived_a = await self._uris_starting_with(leaf_a)
            self.assertEqual(
                len(survived_a), 0,
                f"leafA and its fact_point must be gone: got {survived_a}",
            )
        self._run(scenario())

    def test_cascade_safe_when_sibling_uri_has_target_as_prefix(self):
        """Adversarial: sibling leaf URI contains target URI as literal prefix.

        ``leafA`` is a string-prefix of ``leafAB``.  A naive MatchText-based
        ``remove_by_uri`` on embedded Qdrant performs substring matching
        within tokens, which means ``MatchText("...leafA")`` also matches
        ``leafAB``.  Cascade deletion must NOT over-delete ``leafAB``.

        This is the core concern raised by ADV-004 in the Plan 006 review:
        in-memory ``startswith`` cannot reproduce this failure mode, so the
        Unit 7 cascade tests were false-green against Qdrant semantics.
        """
        async def scenario():
            leaf_a = "opencortex://t/u/mem/leafA"
            leaf_ab = "opencortex://t/u/mem/leafAB"
            await self.adapter.insert("context", _leaf("LA", leaf_a, seed=31))
            await self.adapter.insert("context", _leaf("LAB", leaf_ab, seed=41))
            await self.adapter.insert("context", _fact_point("FA", leaf_a, "fa01"))
            await self.adapter.insert("context", _fact_point("FAB", leaf_ab, "fab02"))

            await self.adapter.remove_by_uri("context", leaf_a)

            # leafAB must survive — deleting leafA must not cascade into its sibling
            survivors = await self._uris_starting_with(leaf_ab)
            self.assertEqual(
                len(survivors), 2,
                f"leafAB + its fact_point must survive removal of leafA, got {survivors}",
            )
            # leafA side must be cleaned up
            gone = await self._uris_starting_with(leaf_a + "/")
            leaf_self = await self._uris_starting_with(leaf_a)
            # leaf_self matches both leaf_a AND leaf_ab prefixes; exclude leafAB
            leaf_a_only = [u for u in leaf_self if not u.startswith(leaf_ab)]
            self.assertEqual(len(leaf_a_only), 0, f"leafA residue: {leaf_a_only}")
            self.assertEqual(len(gone), 0)
        self._run(scenario())

    def test_matchtext_prefix_deletion_with_digest_collisions(self):
        """Two leaves' fact_points with the SAME digest suffix stay isolated.

        ``leafA/fact_points/abc123`` and ``leafB/fact_points/abc123`` share
        the digest ``abc123``.  Removing leafA must only affect leafA's
        subtree — the shared digest token alone is not enough to pull in
        leafB's fact_point because the leaf token differs.
        """
        async def scenario():
            leaf_a = "opencortex://t/u/mem/leafEcho"
            leaf_b = "opencortex://t/u/mem/leafFoxtrot"
            shared_digest = "abc123def456"
            await self.adapter.insert("context", _leaf("LA", leaf_a, seed=12))
            await self.adapter.insert("context", _leaf("LB", leaf_b, seed=22))
            await self.adapter.insert(
                "context", _fact_point("FA", leaf_a, shared_digest))
            await self.adapter.insert(
                "context", _fact_point("FB", leaf_b, shared_digest))

            await self.adapter.remove_by_uri("context", leaf_a)

            left_b = await self._uris_starting_with(leaf_b)
            self.assertEqual(
                len(left_b), 2,
                f"leafB + fact_point (shared digest) must survive: {left_b}",
            )
            left_a = await self._uris_starting_with(leaf_a)
            self.assertEqual(len(left_a), 0)
        self._run(scenario())


class TestDeleteDerivedStale(_Base):
    """_delete_derived_stale contract: write new → delete old via prefix filter.

    This mimics ``orchestrator._delete_derived_stale`` without pulling in the
    orchestrator itself.  The filter-DSL ``{"op": "prefix", ...}`` call path
    ends up in ``translate_filter`` → ``_prefix_condition`` → ``MatchText``
    (same semantic as ``remove_by_uri``).
    """

    def test_delete_derived_stale_does_not_touch_sibling_prefix(self):
        """``_delete_derived_stale`` emulation must not delete sibling fact_points.

        This targets the ``{"op": "prefix", ...}`` path (filter_translator →
        MatchText), which on embedded Qdrant over-matches in the same way
        MatchText does in ``remove_by_uri``.  The orchestrator's
        ``_delete_derived_stale`` helper compares ``keep_uris`` against the
        raw DSL result, so any over-matched sibling rows would be flagged as
        stale and deleted.

        Documents a second-order failure mode of ADV-004: even with the
        ``remove_by_uri`` fix, the sibling-prefix adversary still escapes via
        ``_delete_derived_stale`` unless we also guard that code path with a
        literal ``startswith`` check.  This test asserts the expected *safe*
        behaviour — it requires the caller to filter DSL results before
        deleting.
        """
        async def scenario():
            # leaf_a is a literal string-prefix of leaf_ab (the dangerous case)
            leaf_a = "opencortex://t/u/mem/leafHotel"
            leaf_ab = "opencortex://t/u/mem/leafHotelX"
            await self.adapter.insert("context", _leaf("LA", leaf_a))
            await self.adapter.insert("context", _leaf("LAB", leaf_ab))
            await self.adapter.insert("context", _fact_point("FA_V1", leaf_a, "oldv1"))
            await self.adapter.insert("context", _fact_point("FA_V2", leaf_a, "newv2"))
            await self.adapter.insert("context", _fact_point("FAB", leaf_ab, "keepme"))

            fp_prefix_a = f"{leaf_a}/fact_points"
            keep_uris = {f"{fp_prefix_a}/newv2"}

            # Emulate orchestrator._delete_derived_stale WITH the mandatory
            # startswith guard applied by the caller.
            candidates = await self.adapter.filter(
                "context",
                {"op": "prefix", "field": "uri", "prefix": fp_prefix_a},
                limit=50,
            )
            # Defensive: only consider rows that truly live under fp_prefix_a
            descendant_prefix = fp_prefix_a + "/"
            stale_ids = [
                str(r["id"])
                for r in candidates
                if isinstance(r.get("uri"), str)
                and r["uri"].startswith(descendant_prefix)
                and r["uri"] not in keep_uris
            ]
            if stale_ids:
                await self.adapter.delete("context", stale_ids)

            # leafHotelX's fact_point must still be present
            left_b = await self._uris_starting_with(leaf_ab)
            self.assertIn(
                f"{leaf_ab}/fact_points/keepme",
                left_b,
                f"sibling fact_point wrongly deleted; remaining leafB URIs: {left_b}",
            )
            # leafHotel's v1 fact_point is gone, v2 remains
            left_a = await self._uris_starting_with(fp_prefix_a + "/")
            self.assertEqual(
                sorted(left_a),
                [f"{fp_prefix_a}/newv2"],
                f"expected only newv2 for leafA, got {left_a}",
            )
        self._run(scenario())

    def test_delete_derived_stale_with_matchtext(self):
        """After writing fp_v2 and deleting via prefix, old fp_v1 is gone."""
        async def scenario():
            leaf_uri = "opencortex://t/u/mem/leafGolf"
            await self.adapter.insert("context", _leaf("L1", leaf_uri))

            # v1 fact_point, later superseded
            fp_v1_digest = "v1digest0001"
            await self.adapter.insert(
                "context", _fact_point("FP_V1", leaf_uri, fp_v1_digest))

            # Caller writes v2 first
            fp_v2_digest = "v2digest0002"
            await self.adapter.insert(
                "context", _fact_point("FP_V2", leaf_uri, fp_v2_digest))

            fp_prefix = f"{leaf_uri}/fact_points"
            keep_uris = {f"{fp_prefix}/{fp_v2_digest}"}

            # Emulate _delete_derived_stale: filter by prefix, delete old ids
            old_records = await self.adapter.filter(
                "context",
                {"op": "prefix", "field": "uri", "prefix": fp_prefix},
                limit=50,
            )
            stale_ids = [
                str(r["id"])
                for r in old_records
                if r.get("uri", "") not in keep_uris
            ]
            if stale_ids:
                await self.adapter.delete("context", stale_ids)

            surviving = await self._uris_starting_with(fp_prefix)
            self.assertEqual(
                surviving, [f"{fp_prefix}/{fp_v2_digest}"],
                f"only v2 should remain, got {surviving}",
            )
            # Leaf itself untouched
            self.assertTrue(await self.adapter.exists("context", "L1"))
        self._run(scenario())


if __name__ == "__main__":
    unittest.main(verbosity=2)
