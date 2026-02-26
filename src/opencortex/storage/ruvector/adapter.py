# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: Apache-2.0
"""
RuVector Adapter for OpenCortex.

Dual-faced design
-----------------
* **Standard face** — Implements all 25 abstract methods of
  :class:`~opencortex.storage.vikingdb_interface.VikingDBInterface` so the
  adapter can serve as a drop-in vector backend.
* **Reinforcement face** — Exposes RuVector-specific SONA capabilities
  (:meth:`update_reward`, :meth:`get_profile`, :meth:`apply_decay`,
  :meth:`set_protected`) that are *not* part of the abstract interface.

Collection simulation
---------------------
RuVector is a single-namespace store.  Collections are simulated by prefixing
every record ID with ``"{collection}::"``.  The prefix is stripped from IDs
before results are returned to callers.

Async model
-----------
:class:`VikingDBInterface` methods are ``async``.  The underlying
:class:`~opencortex.storage.ruvector.cli_client.RuVectorCLI` calls are
blocking subprocess operations that are wrapped with :func:`asyncio.to_thread`
so they run in the default thread-pool executor without blocking the event loop.
"""

import asyncio
import json
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

from opencortex.storage.vikingdb_interface import (
    CollectionNotFoundError,
    DuplicateKeyError,
    RecordNotFoundError,
    SchemaError,
    VikingDBException,
    VikingDBInterface,
)
from opencortex.storage.ruvector.filter_translator import translate_filter
from opencortex.storage.ruvector.types import DecayResult, RuVectorConfig, SonaProfile

logger = logging.getLogger(__name__)


class RuVectorAdapter(VikingDBInterface):
    """
    Dual-faced RuVector Adapter for OpenCortex.

    Standard face: Implements VikingDBInterface (25 abstract methods).
    Reinforcement face: Exposes SONA capabilities (update_reward, get_profile,
    apply_decay, set_protected).

    Collection simulation: RuVector is single-namespace.  Collections are
    simulated by prefixing record IDs with ``"{collection}::"``.
    """

    def __init__(self, config: Optional[RuVectorConfig] = None) -> None:
        self.config = config or RuVectorConfig()

        if self.config.use_http:
            from opencortex.storage.ruvector.http_client import RuVectorHTTPClient
            self._cli = RuVectorHTTPClient(self.config)
        else:
            from opencortex.storage.ruvector.cli_client import RuVectorCLI
            self._cli = RuVectorCLI(self.config)

        self._collections_file = os.path.join(self.config.data_dir, "collections.json")
        self._collections: Dict[str, Dict[str, Any]] = {}
        self._load_collections()
        self._closed = False

    # =========================================================================
    # Internal helpers
    # =========================================================================

    def _prefixed_id(self, collection: str, id: str) -> str:
        """Return the internal prefixed ID for a record."""
        return f"{collection}::{id}"

    def _strip_prefix(self, collection: str, prefixed_id: str) -> str:
        """Strip the collection prefix from an internal ID."""
        prefix = f"{collection}::"
        if prefixed_id.startswith(prefix):
            return prefixed_id[len(prefix):]
        return prefixed_id

    def _load_collections(self) -> None:
        """Load collection metadata from the on-disk JSON file."""
        if os.path.exists(self._collections_file):
            try:
                with open(self._collections_file, "r") as fh:
                    self._collections = json.load(fh)
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning(
                    "Could not load collections file %s: %s — starting fresh.",
                    self._collections_file,
                    exc,
                )
                self._collections = {}

    def _save_collections(self) -> None:
        """Persist collection metadata to disk."""
        os.makedirs(os.path.dirname(self._collections_file), exist_ok=True)
        with open(self._collections_file, "w") as fh:
            json.dump(self._collections, fh, indent=2)

    def _ensure_collection(self, name: str) -> None:
        """Raise :class:`CollectionNotFoundError` if *name* does not exist."""
        if name not in self._collections:
            raise CollectionNotFoundError(f"Collection '{name}' does not exist")

    def _record_to_entry(
        self, collection: str, data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Convert a VikingDB record dict to the RuVector entry format accepted by
        :class:`RuVectorCLI`.

        * ``id`` is prefixed with the collection name.
        * ``vector`` is passed through as-is.
        * ``abstract`` / ``description`` are stored as ``content``.
        * All remaining scalar fields are stored in ``metadata``.
        * String lists are stored as comma-separated strings.
        * Complex values are JSON-encoded.
        """
        record_id = data.get("id", "")
        prefixed = self._prefixed_id(collection, record_id)

        vector = data.get("vector", [])
        content = data.get("abstract", "") or data.get("description", "") or ""

        metadata: Dict[str, Any] = {}
        skip_fields = {"id", "vector", "sparse_vector"}
        for key, value in data.items():
            if key in skip_fields or value is None:
                continue
            if isinstance(value, (str, int, float, bool)):
                metadata[key] = value
            elif isinstance(value, list) and all(isinstance(x, str) for x in value):
                metadata[key] = ",".join(value)
            else:
                metadata[key] = json.dumps(value) if value else ""

        # Always tag the collection for metadata filtering.
        metadata["_collection"] = collection

        return {
            "id": prefixed,
            "vector": vector,
            "content": content,
            "metadata": metadata,
        }

    def _entry_to_record(
        self, collection: str, entry: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Convert a RuVector result entry back to the VikingDB record format.

        * The collection prefix is stripped from ``id``.
        * ``_collection`` metadata is removed.
        * Score fields are normalised to ``_score``.
        * ``content`` is restored as ``abstract`` when no ``abstract`` is
          present in metadata.
        """
        record: Dict[str, Any] = dict(entry.get("metadata", {}))
        record.pop("_collection", None)

        raw_id = entry.get("id", "")
        record["id"] = self._strip_prefix(collection, raw_id)

        # Normalise score fields.
        if "reinforced_score" in entry:
            record["_score"] = entry["reinforced_score"]
        elif "similarity_score" in entry:
            record["_score"] = entry["similarity_score"]
        elif "_score" in entry:
            record["_score"] = entry["_score"]

        if "abstract" not in record and entry.get("content"):
            record["abstract"] = entry["content"]

        return record

    async def _run_sync(self, func, *args, **kwargs):
        """
        Execute a synchronous callable in a thread-pool executor.

        Wraps blocking subprocess calls so they do not block the event loop.
        """
        return await asyncio.to_thread(func, *args, **kwargs)

    # =========================================================================
    # VikingDBInterface — Collection Management
    # =========================================================================

    async def create_collection(self, name: str, schema: Dict[str, Any]) -> bool:
        """
        Create a new (simulated) collection.

        The schema is persisted to the local ``collections.json`` file.
        Returns ``False`` if the collection already exists.
        """
        if name in self._collections:
            logger.debug("create_collection: '%s' already exists", name)
            return False
        self._collections[name] = schema
        self._save_collections()
        # Lazily initialise the underlying RuVector DB on first collection.
        await self._run_sync(self._cli.init_db)
        logger.info("Collection '%s' created", name)
        return True

    async def drop_collection(self, name: str) -> bool:
        """
        Drop a collection and remove its schema entry.

        Note: For correctness a full drop should delete all prefixed records.
        This POC removes only the schema entry; record cleanup can be added
        when rvf-cli supports a prefix-delete command.
        """
        if name not in self._collections:
            return False
        del self._collections[name]
        self._save_collections()
        logger.info("Collection '%s' dropped (schema removed)", name)
        return True

    async def collection_exists(self, name: str) -> bool:
        """Return ``True`` if the named collection exists."""
        return name in self._collections

    async def list_collections(self) -> List[str]:
        """Return a list of all known collection names."""
        return list(self._collections.keys())

    async def get_collection_info(self, name: str) -> Optional[Dict[str, Any]]:
        """
        Return metadata and statistics for a collection.

        Returns ``None`` if the collection does not exist.
        """
        if name not in self._collections:
            return None
        schema = self._collections[name]
        total = await self.count(name)
        return {
            "name": name,
            "vector_dim": schema.get("vector_dim", self.config.dimension),
            "count": total,
            "status": "ready",
        }

    # =========================================================================
    # VikingDBInterface — CRUD: Single Record
    # =========================================================================

    async def insert(self, collection: str, data: Dict[str, Any]) -> str:
        """Insert a single record into *collection*."""
        self._ensure_collection(collection)
        entry = self._record_to_entry(collection, data)
        await self._run_sync(
            self._cli.insert,
            entry["id"],
            entry["vector"],
            entry["content"],
            entry["metadata"],
        )
        return data.get("id", "")

    async def update(self, collection: str, id: str, data: Dict[str, Any]) -> bool:
        """
        Update fields of an existing record.

        Returns ``False`` if the record is not found.
        """
        self._ensure_collection(collection)
        prefixed = self._prefixed_id(collection, id)
        existing = await self._run_sync(self._cli.get, prefixed)
        if not existing:
            return False

        metadata = dict(existing.get("metadata", {}))
        skip = {"id", "vector", "sparse_vector"}
        for key, value in data.items():
            if key in skip or value is None:
                continue
            if isinstance(value, (str, int, float, bool)):
                metadata[key] = value
            else:
                metadata[key] = json.dumps(value) if value else ""

        await self._run_sync(self._cli.update_metadata, prefixed, metadata)
        return True

    async def upsert(self, collection: str, data: Dict[str, Any]) -> str:
        """Insert or update a record in *collection*."""
        self._ensure_collection(collection)
        entry = self._record_to_entry(collection, data)
        await self._run_sync(
            self._cli.upsert,
            entry["id"],
            entry["vector"],
            entry["content"],
            entry["metadata"],
        )
        return data.get("id", "")

    async def delete(self, collection: str, ids: List[str]) -> int:
        """
        Delete records by ID.

        Returns the number of records actually deleted.
        """
        self._ensure_collection(collection)
        count = 0
        for record_id in ids:
            prefixed = self._prefixed_id(collection, record_id)
            if await self._run_sync(self._cli.delete, prefixed):
                count += 1
        return count

    async def get(self, collection: str, ids: List[str]) -> List[Dict[str, Any]]:
        """
        Retrieve records by ID.

        Records not found are silently omitted from the result list.
        """
        self._ensure_collection(collection)
        results = []
        for record_id in ids:
            prefixed = self._prefixed_id(collection, record_id)
            entry = await self._run_sync(self._cli.get, prefixed)
            if entry:
                results.append(self._entry_to_record(collection, entry))
        return results

    async def exists(self, collection: str, id: str) -> bool:
        """Return ``True`` if a record with the given ID exists in *collection*."""
        self._ensure_collection(collection)
        prefixed = self._prefixed_id(collection, id)
        entry = await self._run_sync(self._cli.get, prefixed)
        return entry is not None

    # =========================================================================
    # VikingDBInterface — CRUD: Batch
    # =========================================================================

    async def batch_insert(
        self, collection: str, data: List[Dict[str, Any]]
    ) -> List[str]:
        """Batch insert multiple records and return their IDs."""
        self._ensure_collection(collection)
        entries = [self._record_to_entry(collection, d) for d in data]
        await self._run_sync(self._cli.insert_batch, entries)
        return [d.get("id", "") for d in data]

    async def batch_upsert(
        self, collection: str, data: List[Dict[str, Any]]
    ) -> List[str]:
        """Batch upsert multiple records and return their IDs."""
        self._ensure_collection(collection)
        ids = []
        for d in data:
            entry = self._record_to_entry(collection, d)
            await self._run_sync(
                self._cli.upsert,
                entry["id"],
                entry["vector"],
                entry["content"],
                entry["metadata"],
            )
            ids.append(d.get("id", ""))
        return ids

    async def batch_delete(
        self, collection: str, filter: Dict[str, Any]
    ) -> int:
        """
        Delete all records matching *filter*.

        Performs a filter query first, then deletes the matching IDs.
        """
        self._ensure_collection(collection)
        records = await self.filter(collection, filter, limit=100_000)
        ids = [r.get("id", "") for r in records]
        return await self.delete(collection, ids)

    async def remove_by_uri(self, collection: str, uri: str) -> int:
        """
        Remove all records whose ``uri`` field starts with *uri*.

        Returns the number of records removed.
        """
        self._ensure_collection(collection)
        records = await self.filter(
            collection,
            {"op": "prefix", "field": "uri", "prefix": uri},
            limit=100_000,
        )
        ids = [r.get("id", "") for r in records]
        if ids:
            return await self.delete(collection, ids)
        return 0

    # =========================================================================
    # VikingDBInterface — Search Operations
    # =========================================================================

    async def search(
        self,
        collection: str,
        query_vector: Optional[List[float]] = None,
        sparse_query_vector: Optional[Dict[str, float]] = None,
        filter: Optional[Dict[str, Any]] = None,
        limit: int = 10,
        offset: int = 0,
        output_fields: Optional[List[str]] = None,
        with_vector: bool = False,
    ) -> List[Dict[str, Any]]:
        """
        Hybrid search: dense vector similarity + scalar post-filtering.

        If *query_vector* is absent, falls back to a pure filter search via
        :meth:`filter`.  ``sparse_query_vector`` is acknowledged but not
        forwarded to the CLI because the POC CLI does not expose sparse search.
        """
        self._ensure_collection(collection)

        if sparse_query_vector and not query_vector:
            logger.warning(
                "search: sparse_query_vector provided without query_vector — "
                "sparse search is not supported by the rvf CLI POC; "
                "falling back to filter-only search."
            )

        if not query_vector:
            return await self.filter(
                collection,
                filter or {},
                limit=limit,
                offset=offset,
                output_fields=output_fields,
            )

        # Build the rvf-cli filter: always scope to the target collection.
        cli_filter: Dict[str, Any] = {"_collection": collection}
        post_filter = None
        if filter:
            translated_cli, post_filter = translate_filter(filter)
            cli_filter.update(translated_cli)

        # Fetch extra results to accommodate post-filtering and offset.
        fetch_k = limit + offset + 50

        raw_results: List[Dict[str, Any]] = await self._run_sync(
            self._cli.search,
            query_vector,
            fetch_k,
            cli_filter,
            self.config.enable_sona,
            None,  # min_score
        )

        records = []
        for entry in raw_results:
            record = self._entry_to_record(collection, entry)
            if post_filter and not post_filter(record):
                continue
            if not with_vector:
                record.pop("vector", None)
            if output_fields:
                record = {
                    k: v
                    for k, v in record.items()
                    if k in output_fields or k in ("id", "_score")
                }
            records.append(record)

        return records[offset: offset + limit]

    async def filter(
        self,
        collection: str,
        filter: Dict[str, Any],
        limit: int = 10,
        offset: int = 0,
        output_fields: Optional[List[str]] = None,
        order_by: Optional[str] = None,
        order_desc: bool = False,
    ) -> List[Dict[str, Any]]:
        """
        Pure scalar filtering without vector search.

        Uses a zero-vector ANN query internally (POC workaround — rvf-cli does
        not expose a dedicated scan/filter command).
        """
        self._ensure_collection(collection)
        dim = self._collections.get(collection, {}).get(
            "vector_dim", self.config.dimension
        )
        dummy_vector = [0.0] * dim

        cli_filter: Dict[str, Any] = {"_collection": collection}
        post_filter = None
        if filter:
            translated_cli, post_filter = translate_filter(filter)
            cli_filter.update(translated_cli)

        fetch_k = limit + offset + 100

        raw_results: List[Dict[str, Any]] = await self._run_sync(
            self._cli.search,
            dummy_vector,
            fetch_k,
            cli_filter,
            False,   # no SONA for pure filter queries
            None,
        )

        records = []
        for entry in raw_results:
            record = self._entry_to_record(collection, entry)
            if post_filter and not post_filter(record):
                continue
            if output_fields:
                record = {
                    k: v
                    for k, v in record.items()
                    if k in output_fields or k in ("id", "_score")
                }
            records.append(record)

        if order_by:
            records.sort(
                key=lambda r: r.get(order_by, ""),
                reverse=order_desc,
            )

        return records[offset: offset + limit]

    async def scroll(
        self,
        collection: str,
        filter: Optional[Dict[str, Any]] = None,
        limit: int = 100,
        cursor: Optional[str] = None,
        output_fields: Optional[List[str]] = None,
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        """
        Scroll through large result sets using an integer offset cursor.

        Returns a tuple ``(records, next_cursor)`` where *next_cursor* is
        ``None`` when the result set is exhausted.
        """
        self._ensure_collection(collection)
        offset = int(cursor) if cursor else 0
        # Fetch one extra to detect whether there is a next page.
        records = await self.filter(
            collection,
            filter or {},
            limit=limit + 1,
            offset=offset,
            output_fields=output_fields,
        )

        if len(records) > limit:
            next_cursor: Optional[str] = str(offset + limit)
            return records[:limit], next_cursor
        return records, None

    # =========================================================================
    # VikingDBInterface — Aggregation
    # =========================================================================

    async def count(
        self, collection: str, filter: Optional[Dict[str, Any]] = None
    ) -> int:
        """
        Count records in *collection*, optionally matching *filter*.

        When *filter* is provided the count is exact (via a filtered query).
        Without a filter this is an approximation using the global record count
        (POC limitation — rvf-cli does not support per-prefix count).
        """
        self._ensure_collection(collection)
        if filter:
            records = await self.filter(collection, filter, limit=100_000)
            return len(records)
        return await self._run_sync(self._cli.count)

    # =========================================================================
    # VikingDBInterface — Index Operations
    # =========================================================================

    async def create_index(
        self, collection: str, field: str, index_type: str, **kwargs
    ) -> bool:
        """
        No-op: RuVector manages its HNSW index internally.

        Returns ``True`` unconditionally so callers do not need to special-case
        the RuVector backend.
        """
        logger.debug(
            "create_index: RuVector manages indices internally — skipping "
            "(collection=%s, field=%s, type=%s)",
            collection,
            field,
            index_type,
        )
        return True

    async def drop_index(self, collection: str, field: str) -> bool:
        """No-op: RuVector manages its HNSW index internally."""
        logger.debug(
            "drop_index: RuVector manages indices internally — skipping "
            "(collection=%s, field=%s)",
            collection,
            field,
        )
        return True

    # =========================================================================
    # VikingDBInterface — Lifecycle Operations
    # =========================================================================

    async def clear(self, collection: str) -> bool:
        """
        Delete all records in *collection* while keeping the schema.

        Returns ``True`` on success.
        """
        self._ensure_collection(collection)
        records = await self.filter(collection, {}, limit=100_000)
        ids = [r.get("id", "") for r in records]
        if ids:
            await self.delete(collection, ids)
        return True

    async def optimize(self, collection: str) -> bool:
        """
        No-op: RuVector handles compaction and optimisation automatically.

        Returns ``True`` unconditionally.
        """
        logger.debug("optimize: no-op for RuVector backend (collection=%s)", collection)
        return True

    async def close(self) -> None:
        """Mark the adapter as closed.  No network connection to tear down."""
        self._closed = True
        logger.debug("RuVectorAdapter closed")

    # =========================================================================
    # VikingDBInterface — Health & Status
    # =========================================================================

    async def health_check(self) -> bool:
        """
        Verify that the rvf-cli binary is reachable and the DB is readable.

        Returns ``True`` if healthy, ``False`` otherwise.
        """
        try:
            await self._run_sync(self._cli.stats)
            return True
        except Exception as exc:
            logger.warning("health_check failed: %s", exc)
            return False

    async def get_stats(self) -> Dict[str, Any]:
        """
        Return a summary of storage statistics.

        Falls back to safe defaults if the CLI is unavailable.
        """
        try:
            raw = await self._run_sync(self._cli.stats)
            return {
                "collections": len(self._collections),
                "total_records": raw.get("total_entries", 0),
                "storage_size": raw.get("storage_bytes", 0),
                "backend": "ruvector",
                "sona_enabled": self.config.enable_sona,
            }
        except Exception as exc:
            logger.warning("get_stats: could not reach rvf-cli (%s)", exc)
            return {
                "collections": len(self._collections),
                "total_records": 0,
                "storage_size": 0,
                "backend": "ruvector",
                "sona_enabled": self.config.enable_sona,
            }

    # =========================================================================
    # Reinforcement Face — SONA-specific methods
    # (NOT part of VikingDBInterface)
    # =========================================================================

    async def update_reward(
        self, collection: str, id: str, reward: float
    ) -> None:
        """
        Submit a scalar reward signal for a single record.

        This is the primary SONA interface.  Positive rewards reinforce
        retrieval; negative rewards penalise it.

        Args:
            collection: Collection the record belongs to.
            id: Record ID (without collection prefix).
            reward: Scalar reward value.
        """
        prefixed = self._prefixed_id(collection, id)
        await self._run_sync(self._cli.update_reward, prefixed, reward)

    async def update_reward_batch(
        self, collection: str, rewards: List[Tuple[str, float]]
    ) -> None:
        """
        Submit reward signals for multiple records in a single call.

        Args:
            collection: Collection the records belong to.
            rewards: List of ``(id, reward)`` tuples.
        """
        prefixed_rewards = [
            (self._prefixed_id(collection, record_id), reward)
            for record_id, reward in rewards
        ]
        await self._run_sync(self._cli.update_reward_batch, prefixed_rewards)

    async def get_profile(
        self, collection: str, id: str
    ) -> Optional[SonaProfile]:
        """
        Retrieve the SONA behavior profile for a record.

        Returns ``None`` if the record does not exist or has no profile.

        Args:
            collection: Collection the record belongs to.
            id: Record ID (without collection prefix).

        Returns:
            :class:`~opencortex.storage.ruvector.types.SonaProfile` or ``None``.
        """
        prefixed = self._prefixed_id(collection, id)
        data = await self._run_sync(self._cli.get_profile, prefixed)
        if not data:
            return None
        return SonaProfile(
            id=id,
            reward_score=data.get("reward_score", 0.0),
            retrieval_count=data.get("retrieval_count", 0),
            positive_feedback_count=data.get("positive_feedback_count", 0),
            negative_feedback_count=data.get("negative_feedback_count", 0),
            last_retrieved_at=data.get("last_retrieved_at", 0.0),
            last_feedback_at=data.get("last_feedback_at", 0.0),
            effective_score=data.get("effective_score", 0.0),
            is_protected=data.get("is_protected", False),
        )

    async def apply_decay(self) -> DecayResult:
        """
        Trigger time-decay across all records and return a summary.

        Returns:
            :class:`~opencortex.storage.ruvector.types.DecayResult` with
            counters for processed, decayed, below-threshold, and archived
            records.
        """
        data = await self._run_sync(self._cli.apply_decay)
        return DecayResult(
            records_processed=data.get("records_processed", 0),
            records_decayed=data.get("records_decayed", 0),
            records_below_threshold=data.get("records_below_threshold", 0),
            records_archived=data.get("records_archived", 0),
        )

    async def set_protected(
        self, collection: str, id: str, protected: bool = True
    ) -> None:
        """
        Mark or unmark a record as protected.

        Protected records decay more slowly (using
        :attr:`~opencortex.storage.ruvector.types.RuVectorConfig.sona_protected_decay_rate`).

        Args:
            collection: Collection the record belongs to.
            id: Record ID (without collection prefix).
            protected: ``True`` to protect, ``False`` to unprotect.
        """
        prefixed = self._prefixed_id(collection, id)
        await self._run_sync(
            self._cli.update_metadata,
            prefixed,
            {"_protected": protected},
        )
