# SPDX-License-Identifier: Apache-2.0
"""v0.4.0 backfill project_id for legacy records.

Idempotent: sets project_id = "public" on records where it is empty or missing.
After this migration, strict project filtering works correctly — legacy records
become globally shared memories visible to all projects.
"""

import logging

logger = logging.getLogger(__name__)


async def backfill_project_id(storage, collection: str) -> int:
    """Backfill project_id on existing records missing it.

    Sets project_id = "public" for all records where project_id is empty
    or not set, so they remain accessible under the strict project filter.

    Returns count of updated records.
    """
    all_records = await storage.filter(collection, None, limit=10000)
    updated = 0
    for record in all_records:
        pid = record.get("project_id", "")
        if pid:
            continue  # Already has a project_id
        rid = record.get("id", "")
        if not rid:
            continue
        await storage.update(collection, rid, {
            "project_id": "public",
        })
        updated += 1
    logger.info(
        "[Migration] Backfilled project_id='public' for %d records in %s",
        updated, collection,
    )
    return updated
