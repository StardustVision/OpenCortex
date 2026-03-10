# SPDX-License-Identifier: Apache-2.0
"""v0.4.0 migration: re-embed all records with a new embedding model.

Scrolls all records in the context collection and recomputes their
dense (and optionally sparse) vectors using the provided embedder.

This is called automatically by the orchestrator when a model change is
detected, or manually via the ``POST /api/v1/admin/reembed`` endpoint.
"""

import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)

_CONTEXT_COLLECTION = "context"


async def reembed_all(
    storage,
    collection: str,
    embedder,
    *,
    batch_size: int = 50,
) -> int:
    """Re-embed all records in *collection* using *embedder*.

    Uses the ``abstract`` field as embedding input (consistent with
    ``MemoryOrchestrator.add()``).

    Args:
        storage: StorageInterface (Qdrant adapter).
        collection: Collection name to operate on.
        embedder: EmbedderBase instance.
        batch_size: Records per scroll batch.

    Returns:
        Number of records successfully updated.
    """
    updated = 0
    errors = 0
    cursor: Optional[str] = None
    loop = asyncio.get_event_loop()

    logger.info("[Reembed] Starting re-embed on collection '%s'", collection)

    while True:
        records, cursor = await storage.scroll(
            collection,
            filter=None,
            limit=batch_size,
            cursor=cursor,
        )

        if not records:
            break

        for record in records:
            abstract = record.get("abstract", "")
            rid = record.get("id", "")
            if not abstract or not rid:
                continue

            try:
                result = await asyncio.wait_for(
                    loop.run_in_executor(None, embedder.embed, abstract),
                    timeout=2.0,
                )
            except Exception as exc:
                errors += 1
                logger.warning(
                    "[Reembed] embed failed for %s: %s", rid, exc,
                )
                continue

            update_data = {}
            if result.dense_vector:
                update_data["vector"] = result.dense_vector
            if result.sparse_vector:
                update_data["sparse_vector"] = result.sparse_vector

            if update_data:
                try:
                    await storage.update(collection, rid, update_data)
                    updated += 1
                except Exception as exc:
                    errors += 1
                    logger.warning(
                        "[Reembed] update failed for %s: %s", rid, exc,
                    )

        if cursor is None:
            break

    logger.info(
        "[Reembed] Done: updated=%d, errors=%d", updated, errors,
    )
    return updated
