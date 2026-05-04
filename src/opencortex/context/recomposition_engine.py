# SPDX-License-Identifier: Apache-2.0
"""Session recomposition engine extracted from ``ContextManager``.

Owns the segmentation, clustering, merging, and LLM-derivation pipeline
that transforms incremental conversation records into structured directory
and session-summary hierarchies.  The engine takes a back-reference to the
``ContextManager`` at construction (sync, no I/O) and manages the focused
recomposition concern.

This is part of the multi-PR decomposition documented in
``docs/plans/2026-04-27-017-refactor-contextmanager-recomposition-plan.md``.

No re-exports -- import directly from the submodule, e.g.
``from opencortex.context.recomposition_engine import SessionRecompositionEngine``.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Set, Tuple

from opencortex.context.manager import (
    ConversationBuffer,
    RecompositionError,
    SessionKey,
)
from opencortex.context.recomposition_input import RecompositionInputService
from opencortex.context.recomposition_segmentation import (
    RecompositionSegmentationService,
    _merge_unique_strings,
)
from opencortex.context.recomposition_state import RecompositionStateService
from opencortex.context.recomposition_types import RecompositionEntry
from opencortex.context.session_records import (
    record_msg_range,
    record_text,
)
from opencortex.http.request_context import (
    reset_collection_name,
    reset_request_identity,
    set_collection_name,
    set_request_identity,
)

logger = logging.getLogger(__name__)

_DIRECTORY_DERIVE_CONCURRENCY = 3

if TYPE_CHECKING:
    from opencortex.context.manager import ContextManager


class SessionRecompositionEngine:
    """Owns the conversation recomposition pipeline for ContextManager.

    The engine manages segmentation (anchor clustering + time-based splitting),
    LLM-driven parent derivation, merge buffer flushing, and session summary
    generation. It holds the task coordination state for merge and recompose
    background work.
    """

    def __init__(self, manager: ContextManager) -> None:
        self._mgr = manager
        self._input = RecompositionInputService(manager)
        self._segmentation = RecompositionSegmentationService()
        self._state = RecompositionStateService(manager, self._input)

    # =========================================================================
    # URI Construction
    # =========================================================================

    @staticmethod
    def _merged_leaf_uri(
        tenant_id: str,
        user_id: str,
        session_id: str,
        msg_range: List[int],
    ) -> str:
        """Return one stable merged-leaf URI for a session message span."""
        from opencortex.utils.uri import CortexURI

        start = int(msg_range[0])
        end = int(msg_range[1])
        session_hash = hashlib.md5(session_id.encode("utf-8")).hexdigest()[:12]
        node_name = f"conversation-{session_hash}-{start:06d}-{end:06d}"
        return CortexURI.build_private(
            tenant_id,
            user_id,
            "memories",
            "events",
            node_name,
        )

    @staticmethod
    def _directory_uri(
        tenant_id: str,
        user_id: str,
        session_id: str,
        index: int,
    ) -> str:
        """Return URI for a directory parent record."""
        from opencortex.utils.uri import CortexURI

        session_hash = hashlib.md5(session_id.encode("utf-8")).hexdigest()[:12]
        node_name = f"conversation-{session_hash}/dir-{index:03d}"
        return CortexURI.build_private(
            tenant_id,
            user_id,
            "memories",
            "events",
            node_name,
        )

    # =========================================================================
    # Cleanup
    # =========================================================================

    async def _purge_records_and_fs_subtree(self, uris: List[str]) -> None:
        """Purge each URI's record and CortexFS subtree by URI prefix.

        For every input URI:
        - ``storage.remove_by_uri(uri)`` deletes the URI itself AND every
          record whose URI starts with the same prefix (so derived
          children -- fact_points, abstract.json, etc. -- go with it).
        - ``fs.rm(uri, recursive=True)`` recursively removes the
          CortexFS subtree rooted at the URI's path.

        FS failures are logged but never raise -- the storage delete is
        the source of truth, the FS write is fire-and-forget on the
        creation path. Callers don't need to compensate.

        Renamed from ``_delete_immediate_families`` (REVIEW closure
        tracker PE-1) -- the old name encoded a stale concept ("immediate
        families" was leftover terminology from the immediate-layer era
        that no longer exists). This function deletes records + FS
        subtrees regardless of layer; the new name reflects that.
        """
        await self._state.purge_records_and_fs_subtree(uris)

    async def _list_immediate_uris(self, session_id: str) -> List[str]:
        """Return current session immediate source URIs for fallback cleanup."""
        return await self._state.list_immediate_uris(session_id)

    async def _load_immediate_records(
        self,
        immediate_uris: List[str],
    ) -> List[Dict[str, Any]]:
        """Load immediate records and return them ordered by message index."""
        return await self._state.load_immediate_records(immediate_uris)

    # =========================================================================
    # Segmentation Helpers
    # =========================================================================

    def _segment_anchor_terms(self, record: Dict[str, Any]) -> Set[str]:
        """Extract coarse anchor terms used for sequential merge boundaries."""
        return self._segmentation.segment_anchor_terms(record)

    def _segment_time_refs(self, record: Dict[str, Any]) -> Set[str]:
        """Extract normalized time references used for sequential merge boundaries."""
        return self._segmentation.segment_time_refs(record)

    def _is_coarse_time_ref(self, value: str) -> bool:
        """Return whether one time ref is too coarse to force two events together."""
        return self._segmentation.is_coarse_time_ref(value)

    def _time_refs_overlap(self, left: Set[str], right: Set[str]) -> bool:
        """Return whether two time-ref sets meaningfully overlap for segmentation."""
        return self._segmentation.time_refs_overlap(left, right)

    # =========================================================================
    # Segment Building
    # =========================================================================

    async def _select_tail_merged_records(
        self,
        *,
        session_id: str,
        source_uri: str,
    ) -> List[Dict[str, Any]]:
        """Select a bounded recent merged-tail window for online recomposition."""
        return await self._input.select_tail_merged_records(
            session_id=session_id,
            source_uri=source_uri,
        )

    async def _aggregate_records_metadata(
        self,
        records: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Collect anchor metadata from already loaded immediate records."""
        return await self._input.aggregate_records_metadata(records)

    async def _build_recomposition_entries(
        self,
        *,
        snapshot: ConversationBuffer,
        immediate_records: List[Dict[str, Any]],
        tail_records: List[Dict[str, Any]],
    ) -> List[RecompositionEntry]:
        """Build ordered recomposition entries from merged-tail + immediates."""
        return await self._input.build_recomposition_entries(
            snapshot=snapshot,
            immediate_records=immediate_records,
            tail_records=tail_records,
        )

    def _build_recomposition_segments(
        self,
        entries: List[RecompositionEntry],
    ) -> List[Dict[str, Any]]:
        """Split ordered recomposition entries into bounded semantic segments."""
        return self._segmentation.build_recomposition_segments(entries)

    def _build_anchor_clustered_segments(
        self,
        entries: List[RecompositionEntry],
    ) -> List[Dict[str, Any]]:
        """Cluster entries by anchor Jaccard similarity for full_recompose."""
        return self._segmentation.build_anchor_clustered_segments(entries)

    def _finalize_recomposition_segment(
        self,
        entries: List[RecompositionEntry],
    ) -> Dict[str, Any]:
        """Materialize one recomposition segment payload."""
        return self._segmentation.finalize_recomposition_segment(entries)

    # =========================================================================
    # Misc
    # =========================================================================

    def _merge_trigger_threshold(self) -> int:
        """Return the token threshold that triggers a background merge."""
        return self._state.merge_trigger_threshold()

    # =========================================================================
    # Snapshot / Restore
    # =========================================================================

    async def _take_merge_snapshot(
        self,
        sk: SessionKey,
        *,
        flush_all: bool,
    ) -> Optional[ConversationBuffer]:
        """Detach the current buffer snapshot for merge processing."""
        return await self._state.take_merge_snapshot(sk, flush_all=flush_all)

    async def _restore_merge_snapshot(
        self,
        sk: SessionKey,
        snapshot: ConversationBuffer,
    ) -> None:
        """Restore a detached buffer snapshot after merge failure."""
        await self._state.restore_merge_snapshot(sk, snapshot)

    # =========================================================================
    # Task Coordination
    # =========================================================================

    def _spawn_merge_task(
        self,
        sk: SessionKey,
        session_id: str,
        tenant_id: str,
        user_id: str,
    ) -> Optional[asyncio.Task]:
        """Start one background merge worker for the session if needed."""
        return self._mgr._recomposition_tasks.spawn_merge_task(
            sk,
            lambda collection_name: self._merge_buffer(
                sk,
                session_id,
                tenant_id,
                user_id,
                flush_all=False,
                collection_name=collection_name,
                raise_on_error=True,
            ),
        )

    def _spawn_full_recompose_task(
        self,
        sk: SessionKey,
        *,
        session_id: str,
        tenant_id: str,
        user_id: str,
        source_uri: Optional[str],
        raise_on_error: bool = False,
    ) -> Optional[asyncio.Task]:
        """Start one async full-session recomposition worker per session."""
        return self._mgr._recomposition_tasks.spawn_full_recompose_task(
            sk,
            lambda collection_name: self._run_full_session_recomposition(
                session_id=session_id,
                tenant_id=tenant_id,
                user_id=user_id,
                source_uri=source_uri,
                collection_name=collection_name,
                raise_on_error=raise_on_error,
            ),
        )

    async def _wait_for_merge_task(self, sk: SessionKey) -> List[BaseException]:
        """Wait until any in-flight background merge for the session finishes."""
        return await self._mgr._recomposition_tasks.wait_for_merge_task(sk)

    def _track_session_merge_followup_task(
        self,
        sk: SessionKey,
        task: asyncio.Task,
    ) -> None:
        """Track deferred tasks spawned from a session merge worker."""
        self._mgr._recomposition_tasks.track_session_merge_followup_task(sk, task)

    async def _wait_for_merge_followup_tasks(
        self, sk: SessionKey
    ) -> List[BaseException]:
        """Wait until deferred follow-up tasks for the session merge finish."""
        return await self._mgr._recomposition_tasks.wait_for_merge_followup_tasks(sk)

    # =========================================================================
    # Core Orchestration
    # =========================================================================

    async def _run_full_session_recomposition(
        self,
        *,
        session_id: str,
        tenant_id: str,
        user_id: str,
        source_uri: Optional[str],
        collection_name: Optional[str] = None,
        raise_on_error: bool = False,
        return_created_uris: bool = False,
    ) -> Optional[List[str]]:
        """Create directory parent records for semantically related leaf clusters.

        Preserves original merged leaf records. For each cluster of >=2 leaves,
        generates a directory summary from children abstracts and writes a
        directory record (layer="directory", is_leaf=False).

        When ``return_created_uris`` is True returns the list of created
        directory URIs so callers (e.g. the benchmark ingest cleanup
        tracker) can register them for rollback. Defaults to False so all
        existing call sites -- including the production conversation
        lifecycle in ``context_end`` -- preserve their previous return
        contract (None) and behavior.
        """
        tokens_for_identity = set_request_identity(tenant_id, user_id)
        coll_token = set_collection_name(collection_name) if collection_name else None
        created_directory_uris: List[str] = []
        try:
            merged_records = await self._mgr._session_records.load_merged(
                session_id=session_id,
                source_uri=source_uri,
            )
            logger.info(
                "[ContextManager] Full recompose start sid=%s tenant=%s user=%s "
                "collection=%s source_uri=%s merged=%d",
                session_id,
                tenant_id,
                user_id,
                self._mgr._orchestrator._get_collection(),
                source_uri,
                len(merged_records),
            )
            if len(merged_records) <= 1:
                return [] if return_created_uris else None

            entries: List[RecompositionEntry] = []
            for record in merged_records:
                msg_range = record_msg_range(record)
                if msg_range is None:
                    continue
                uri = str(record.get("uri", "") or "").strip()
                text = record_text(record)
                if not text:
                    continue
                entries.append(
                    RecompositionEntry(
                        text=text,
                        uri=uri,
                        msg_start=msg_range[0],
                        msg_end=msg_range[1],
                        token_count=max(self._mgr._estimate_tokens(text), 1),
                        anchor_terms=self._segment_anchor_terms(record),
                        time_refs=self._segment_time_refs(record),
                        source_record=record,
                        immediate_uris=[],
                        superseded_merged_uris=[],
                        source_segment_index=None,
                    )
                )

            segments = self._build_anchor_clustered_segments(entries)
            logger.info(
                "[ContextManager] Full recompose planned sid=%s entries=%d "
                "segments=%d ranges=%s",
                session_id,
                len(entries),
                len(segments),
                [segment.get("msg_range") for segment in segments[:8]],
            )
            if not segments:
                return [] if return_created_uris else None

            # Pre-compute eligible segments and their stable directory_index
            # so the bounded-concurrency derive loop below has deterministic
            # URIs regardless of which derive future resolves first.
            eligible: List[Tuple[int, Dict[str, Any], List[str]]] = []
            for segment in segments:
                source_records = segment.get("source_records", [])
                if len(source_records) < 2:
                    continue
                children_abstracts = [
                    str(rec.get("abstract") or "").strip()
                    for rec in source_records
                    if str(rec.get("abstract") or "").strip()
                ]
                if not children_abstracts:
                    continue
                eligible.append((len(eligible), segment, children_abstracts))

            if not eligible:
                logger.info(
                    "[ContextManager] Full recompose: no eligible directories sid=%s",
                    session_id,
                )
                return [] if return_created_uris else None

            # Use the instance-scoped semaphore so cross-conversation
            # concurrency (U13) does not multiply in-flight LLM derives:
            # one global cap of _DIRECTORY_DERIVE_CONCURRENCY across all
            # sessions, not concurrency x 3.
            derive_semaphore = self._mgr._directory_derive_semaphore

            async def _derive_one(
                directory_index: int,
                children_abstracts: List[str],
            ) -> Tuple[int, Optional[Dict[str, Any]]]:
                cluster_title = f"Directory-{directory_index:03d}"
                async with derive_semaphore:
                    return (
                        directory_index,
                        await self._mgr._orchestrator._derive_parent_summary(
                            doc_title=cluster_title,
                            children_abstracts=children_abstracts,
                        ),
                    )

            derive_results = await asyncio.gather(
                *[_derive_one(idx, kids) for idx, _, kids in eligible]
            )
            derived_by_index: Dict[int, Optional[Dict[str, Any]]] = dict(derive_results)

            # Sequential write phase preserves storage-order invariants the
            # production lifecycle relied on (URIs are written in
            # directory_index order; keywords-patch and FS writes happen
            # right after each Qdrant upsert).
            for directory_index, segment, children_abstracts in eligible:
                source_records = segment.get("source_records", [])
                logger.info(
                    "[ContextManager] Full recompose segment sid=%s dir_index=%d "
                    "msg_range=%s children=%d",
                    session_id,
                    directory_index,
                    segment.get("msg_range"),
                    len(source_records),
                )

                derived = derived_by_index.get(directory_index)
                if not derived:
                    continue

                llm_abstract = derived.get("abstract", "")
                llm_overview = derived.get("overview", "")
                keywords_list = derived.get("keywords", [])
                keywords_str = (
                    ", ".join(str(k) for k in keywords_list if k)
                    if isinstance(keywords_list, list)
                    else ""
                )

                dir_uri = self._directory_uri(
                    tenant_id,
                    user_id,
                    session_id,
                    directory_index,
                )

                aggregated_meta = await self._aggregate_records_metadata(source_records)
                all_tool_calls: List[Dict[str, Any]] = []
                for rec in source_records:
                    meta = dict(rec.get("meta") or {})
                    for call in meta.get("tool_calls", []) or []:
                        if isinstance(call, dict):
                            all_tool_calls.append(call)

                content = "\n\n".join(children_abstracts)

                await self._mgr._orchestrator.add(
                    uri=dir_uri,
                    abstract=llm_abstract,
                    content=content,
                    category="events",
                    context_type="memory",
                    is_leaf=False,
                    session_id=session_id,
                    meta={
                        **aggregated_meta,
                        "layer": "directory",
                        "ingest_mode": "memory",
                        "msg_range": list(segment["msg_range"]),
                        "source_uri": source_uri or "",
                        "session_id": session_id,
                        "child_count": len(source_records),
                        "child_uris": [str(r.get("uri", "")) for r in source_records],
                        "tool_calls": all_tool_calls if all_tool_calls else [],
                    },
                    overview=llm_overview,
                )

                created_directory_uris.append(dir_uri)

                if keywords_str:
                    try:
                        records = await self._mgr._orchestrator._storage.filter(
                            self._mgr._orchestrator._get_collection(),
                            {"op": "must", "field": "uri", "conds": [dir_uri]},
                            limit=1,
                        )
                        if records:
                            await self._mgr._orchestrator._storage.update(
                                self._mgr._orchestrator._get_collection(),
                                str(records[0].get("id", "")),
                                {"keywords": keywords_str},
                            )
                    except Exception:
                        logger.warning(
                            "[ContextManager] Failed to patch keywords for %s", dir_uri
                        )

                fs = getattr(self._mgr._orchestrator, "_fs", None)
                if fs is not None:
                    await fs.write_context(
                        uri=dir_uri,
                        content=content,
                        abstract=llm_abstract,
                        abstract_json={
                            "keywords": keywords_list,
                            "child_count": len(source_records),
                        },
                        overview=llm_overview,
                        is_leaf=False,
                    )

            logger.info(
                "[ContextManager] Full recompose completed sid=%s directories=%d "
                "leaves_preserved=%d",
                session_id,
                len(created_directory_uris),
                len(merged_records),
            )
            if return_created_uris:
                return list(created_directory_uris)
            return None
        except Exception as exc:
            logger.warning(
                "[ContextManager] Full-session recomposition failed sid=%s "
                "tenant=%s user=%s collection=%s source_uri=%s created_dirs=%d: %s",
                session_id,
                tenant_id,
                user_id,
                self._mgr._orchestrator._get_collection(),
                source_uri,
                len(created_directory_uris),
                exc,
                exc_info=True,
            )
            if raise_on_error:
                # Hand back the partial URIs to the caller via
                # RecompositionError so its run-scoped cleanup tracker
                # can drive compensation (REVIEW REL-02). The previous
                # inline ``contextlib.suppress`` cleanup was a silent
                # black box on the raise_on_error=True path: any URI
                # that failed to delete inside the suppressed block
                # became an orphan with no signal upstream.
                raise RecompositionError(exc, created_directory_uris) from exc
            # raise_on_error=False (production lifecycle) keeps the
            # legacy best-effort inline cleanup. Production callers do
            # not maintain a tracker and rely on this fallback.
            if created_directory_uris:
                with contextlib.suppress(Exception):
                    await self._purge_records_and_fs_subtree(created_directory_uris)
            if return_created_uris:
                # Failure path with reporting requested: hand back what
                # we created before the failure so the caller's tracker
                # can still record/compensate. ``raise_on_error=False``
                # callers swallow the exception, so they need this hook.
                return list(created_directory_uris)
            return None
        finally:
            reset_request_identity(tokens_for_identity)
            if coll_token is not None:
                reset_collection_name(coll_token)

    async def _generate_session_summary(
        self,
        *,
        session_id: str,
        tenant_id: str,
        user_id: str,
        source_uri: Optional[str],
    ) -> Optional[str]:
        """Generate a session summary from directory or leaf abstracts.

        Uses ``load_layers({"merged", "directory"})`` (REVIEW closure
        tracker PERF-01): a single storage scroll returns both layers.
        Previously the directory-present path called
        ``load_directories`` and then ``load_merged``, paying two full
        session scans for the same data.
        """
        layers = await self._mgr._session_records.load_layers(
            layers=["merged", "directory"],
            session_id=session_id,
            source_uri=source_uri,
        )
        directory_records = layers.get("directory", [])
        merged_records = layers.get("merged", [])

        abstracts: List[str] = []

        if directory_records:
            # Use directory abstracts as primary source
            dir_child_uris: Set[str] = set()
            for rec in directory_records:
                meta = dict(rec.get("meta") or {})
                for child_uri in meta.get("child_uris", []) or []:
                    if child_uri:
                        dir_child_uris.add(child_uri)
                abstract = str(rec.get("abstract") or "").strip()
                if abstract:
                    abstracts.append(abstract)

            # Include ungrouped leaf abstracts (leaves not in any directory)
            for rec in merged_records:
                uri = str(rec.get("uri", "") or "").strip()
                if uri and uri not in dir_child_uris:
                    abstract = str(rec.get("abstract") or "").strip()
                    if abstract:
                        abstracts.append(abstract)
        else:
            # Fallback: use leaf abstracts directly
            if len(merged_records) < 2:
                return None
            for record in merged_records:
                abstract = str(record.get("abstract") or "").strip()
                if abstract:
                    abstracts.append(abstract)

        if not abstracts:
            return None

        # REVIEW closure tracker R2-21 -- 1-directory short-circuit.
        # When recomposition produced exactly one directory whose
        # abstract is the sole contributor to ``abstracts``, the LLM
        # call below would summarize one already-summarized abstract.
        # Wasteful: 1 LLM call + 2 storage scans per session_end with
        # single-cluster recomposition. Promote the directory's
        # existing abstract / overview / topics to the session_summary
        # verbatim instead.
        #
        # The guard requires THREE conditions because a less strict
        # check would shadow ungrouped-leaf content:
        #
        #  1. ``len(directory_records) == 1`` -- exactly one directory
        #  2. ``only_dir_abstract`` is non-empty -- otherwise the
        #     directory loop's ``.strip()`` filter dropped it from
        #     ``abstracts`` and the single entry came from an
        #     ungrouped leaf (correctness bug if we promote the dir's
        #     empty abstract over the leaf's content).
        #  3. ``len(abstracts) == 1`` AND ``abstracts[0] ==
        #     only_dir_abstract`` -- belt+braces: the only entry IS
        #     the directory's abstract, not a coincidentally-equal
        #     leaf abstract.
        only_dir_abstract = (
            str(directory_records[0].get("abstract") or "").strip()
            if len(directory_records) == 1
            else ""
        )
        summary_uri = self._mgr._session_summary_uri(tenant_id, user_id, session_id)
        if (
            len(directory_records) == 1
            and only_dir_abstract
            and len(abstracts) == 1
            and abstracts[0] == only_dir_abstract
        ):
            only_dir = directory_records[0]
            only_dir_meta = dict(only_dir.get("meta") or {})
            llm_abstract = only_dir_abstract
            llm_overview = str(only_dir.get("overview") or "")
            topics = only_dir_meta.get("topics") or []
            keywords_list = list(topics) if isinstance(topics, list) else []
        else:
            derived = await self._mgr._orchestrator._derive_parent_summary(
                doc_title=session_id,
                children_abstracts=abstracts,
            )
            if not derived:
                return None
            llm_abstract = derived.get("abstract", "")
            llm_overview = derived.get("overview", "")
            keywords_list = derived.get("keywords", [])
        keywords_str = (
            ", ".join(str(k) for k in keywords_list if k)
            if isinstance(keywords_list, list)
            else ""
        )

        content = "\n\n".join(abstracts)

        await self._mgr._orchestrator.add(
            uri=summary_uri,
            abstract=llm_abstract,
            content=content,
            category="events",
            context_type="memory",
            is_leaf=False,
            session_id=session_id,
            meta={
                "layer": "session_summary",
                "session_id": session_id,
                "source_uri": source_uri or "",
                "child_count": len(abstracts),
                "topics": keywords_list,
            },
            overview=llm_overview,
        )

        # Patch keywords into the Qdrant record (add() fast-path skips them).
        if keywords_str:
            try:
                records = await self._mgr._orchestrator._storage.filter(
                    self._mgr._orchestrator._get_collection(),
                    {"op": "must", "field": "uri", "conds": [summary_uri]},
                    limit=1,
                )
                if records:
                    await self._mgr._orchestrator._storage.update(
                        self._mgr._orchestrator._get_collection(),
                        str(records[0].get("id", "")),
                        {"keywords": keywords_str},
                    )
            except Exception:
                logger.warning(
                    "[ContextManager] Failed to patch keywords for %s", summary_uri
                )

        fs = getattr(self._mgr._orchestrator, "_fs", None)
        if fs is not None:
            await fs.write_context(
                uri=summary_uri,
                content=content,
                abstract=llm_abstract,
                abstract_json={
                    "keywords": keywords_list,
                    "child_count": len(abstracts),
                },
                overview=llm_overview,
                is_leaf=False,
            )

        logger.info(
            "[ContextManager] Session summary generated sid=%s uri=%s children=%d",
            session_id,
            summary_uri,
            len(abstracts),
        )
        return summary_uri

    async def _merge_buffer(
        self,
        sk: SessionKey,
        session_id: str,
        tenant_id: str,
        user_id: str,
        *,
        flush_all: bool,
        collection_name: Optional[str] = None,
        raise_on_error: bool = False,
    ) -> None:
        """Merge accumulated buffer snapshots into durable merged records."""
        tokens_for_identity = None
        coll_token = set_collection_name(collection_name) if collection_name else None
        self._mgr._session_pending_immediate_cleanup.pop(sk, None)
        try:
            while True:
                snapshot = await self._take_merge_snapshot(
                    sk,
                    flush_all=flush_all,
                )
                if snapshot is None:
                    return
                logger.info(
                    "[ContextManager] Merge start sid=%s tenant=%s user=%s "
                    "collection=%s flush_all=%s snapshot_messages=%d "
                    "snapshot_tokens=%d snapshot_immediates=%d start_msg_index=%d",
                    session_id,
                    tenant_id,
                    user_id,
                    self._mgr._orchestrator._get_collection(),
                    flush_all,
                    len(snapshot.messages),
                    snapshot.token_count,
                    len(snapshot.immediate_uris),
                    snapshot.start_msg_index,
                )

                records = await self._load_immediate_records(snapshot.immediate_uris)
                source_uri = self._mgr._conversation_source_uri(
                    tenant_id,
                    user_id,
                    session_id,
                )
                tail_records = await self._select_tail_merged_records(
                    session_id=session_id,
                    source_uri=source_uri,
                )
                entries = await self._build_recomposition_entries(
                    snapshot=snapshot,
                    immediate_records=records,
                    tail_records=tail_records,
                )
                segments = self._build_recomposition_segments(entries)
                logger.info(
                    "[ContextManager] Merge planned sid=%s immediate_records=%d "
                    "tail_records=%d entries=%d segments=%d segment_ranges=%s",
                    session_id,
                    len(records),
                    len(tail_records),
                    len(entries),
                    len(segments),
                    [segment.get("msg_range") for segment in segments[:8]],
                )

                all_tool_calls = []
                for tc_list in snapshot.tool_calls_per_turn:
                    all_tool_calls.extend(tc_list)

                if tokens_for_identity is None:
                    tokens_for_identity = set_request_identity(tenant_id, user_id)
                created_merged_uris: List[str] = []
                for segment in segments:
                    if not segment["messages"]:
                        continue
                    combined = "\n\n".join(segment["messages"])
                    aggregated_meta = await self._aggregate_records_metadata(
                        segment["source_records"]
                    )
                    merged_context = await self._mgr._orchestrator.add(
                        uri=self._merged_leaf_uri(
                            tenant_id,
                            user_id,
                            session_id,
                            segment["msg_range"],
                        ),
                        abstract="",
                        content=combined,
                        category="events",
                        context_type="memory",
                        meta={
                            **aggregated_meta,
                            "layer": "merged",
                            "ingest_mode": "memory",
                            "msg_range": list(segment["msg_range"]),
                            "source_uri": source_uri,
                            "session_id": session_id,
                            "recomposition_stage": "online_tail",
                            "tool_calls": all_tool_calls if all_tool_calls else [],
                        },
                        session_id=session_id,
                        defer_derive=True,
                    )
                    created_merged_uris.append(merged_context.uri)

                    async def _bounded_derive(
                        sem: asyncio.Semaphore = self._mgr._derive_semaphore,
                        **dkw: Any,
                    ) -> None:
                        async with sem:
                            await self._mgr._orchestrator._complete_deferred_derive(
                                **dkw
                            )

                    _defer_task = asyncio.create_task(
                        _bounded_derive(
                            uri=merged_context.uri,
                            content=combined,
                            abstract="",
                            overview="",
                            session_id=session_id,
                            meta=aggregated_meta,
                            raise_on_error=True,
                        )
                    )
                    self._track_session_merge_followup_task(sk, _defer_task)
                    _defer_task.add_done_callback(
                        lambda t: (
                            None
                            if t.cancelled()
                            else (
                                logger.warning(
                                    "[ContextManager] deferred derive failed: %s",
                                    t.exception(),
                                )
                                if t.exception()
                                else None
                            )
                        )
                    )

                superseded_merged_uris = _merge_unique_strings(
                    *[segment.get("superseded_merged_uris", []) for segment in segments]
                )
                superseded_merged_uris = [
                    uri
                    for uri in superseded_merged_uris
                    if uri not in created_merged_uris
                ]
                if superseded_merged_uris:
                    try:
                        await self._purge_records_and_fs_subtree(superseded_merged_uris)
                    except Exception as exc:
                        logger.warning(
                            "[ContextManager] Superseded merged cleanup after "
                            "merge: %s",
                            exc,
                        )

                if snapshot.immediate_uris:
                    try:
                        await self._purge_records_and_fs_subtree(
                            snapshot.immediate_uris
                        )
                    except Exception as exc:
                        self._mgr._session_pending_immediate_cleanup[sk] = True
                        logger.warning(
                            "[ContextManager] Immediate cleanup after merge: %s", exc
                        )
        except Exception as exc:
            logger.error(
                "[ContextManager] Merge failed sid=%s tenant=%s user=%s "
                "collection=%s flush_all=%s source_uri=%s snapshot_messages=%s "
                "immediate_records=%s tail_records=%s segments=%s "
                "created_merged=%s: %s",
                session_id,
                tenant_id,
                user_id,
                self._mgr._orchestrator._get_collection(),
                flush_all,
                locals().get("source_uri"),
                len(snapshot.messages)
                if "snapshot" in locals() and snapshot is not None
                else None,
                len(records) if "records" in locals() and records is not None else None,
                len(tail_records)
                if "tail_records" in locals() and tail_records is not None
                else None,
                len(segments)
                if "segments" in locals() and segments is not None
                else None,
                len(created_merged_uris)
                if "created_merged_uris" in locals() and created_merged_uris is not None
                else None,
                exc,
                exc_info=True,
            )
            if "created_merged_uris" in locals() and created_merged_uris:
                with contextlib.suppress(Exception):
                    await self._purge_records_and_fs_subtree(created_merged_uris)
            if "snapshot" in locals() and snapshot is not None:
                await self._restore_merge_snapshot(sk, snapshot)
            if raise_on_error:
                raise
        finally:
            if tokens_for_identity:
                reset_request_identity(tokens_for_identity)
            if coll_token is not None:
                reset_collection_name(coll_token)
