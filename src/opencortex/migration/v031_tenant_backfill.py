# SPDX-License-Identifier: Apache-2.0
"""v0.3.1 → v0.3.2 backfill source_tenant_id from URI.

Idempotent: skips records that already have source_tenant_id set.
"""

import logging
import re

logger = logging.getLogger(__name__)

_URI_TENANT_RE = re.compile(r"^opencortex://([^/]+)/")


def infer_tenant_from_uri(uri: str) -> str:
    """Extract tenant_id from URI: opencortex://{tenant}/..."""
    m = _URI_TENANT_RE.match(uri or "")
    return m.group(1) if m else ""


async def backfill_source_tenant_id(storage, collection: str) -> int:
    """Backfill source_tenant_id on existing records missing it.

    Infers tenant from URI prefix. Idempotent: skips records where
    source_tenant_id is already set.

    Returns count of updated records.
    """
    all_records = await storage.filter(collection, None, limit=10000)
    updated = 0
    for record in all_records:
        if record.get("source_tenant_id"):
            continue  # Already migrated
        uri = record.get("uri", "")
        rid = record.get("id", "")
        if not uri or not rid:
            continue
        tenant_id = infer_tenant_from_uri(uri)
        await storage.update(collection, rid, {
            "source_tenant_id": tenant_id,
        })
        updated += 1
    logger.info(
        "[Migration] Backfilled source_tenant_id for %d records in %s",
        updated, collection,
    )
    return updated
