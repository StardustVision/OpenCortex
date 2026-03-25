# SPDX-License-Identifier: Apache-2.0
"""v0.4.x -> v0.5.0 URI rewrite migration. Idempotent.

The v0.5.0 release removed the redundant /user/ segment from private URIs:

  OLD: opencortex://{tid}/user/{uid}/{sub_scope}/...
  NEW: opencortex://{tid}/{uid}/{sub_scope}/...

This script rewrites all stored URI strings in Qdrant (and CortexFS paths if
accessible) from the old format to the new format.

Safe to run multiple times — URIs that do not contain /user/ are skipped.
"""

import logging
import re

logger = logging.getLogger(__name__)

# Regex: matches old-style private URIs with the /user/ segment
# Group 1: scheme://tenant_id
# Group 2: /user/uid/rest
_OLD_URI_PATTERN = re.compile(
    r"^(opencortex://[^/]+)/user/(.+)$"
)


def _rewrite(uri: str) -> str | None:
    """Return the new URI if rewrite is needed, else None."""
    m = _OLD_URI_PATTERN.match(uri)
    if not m:
        return None
    return f"{m.group(1)}/{m.group(2)}"


async def rewrite_uris(storage, collection: str) -> int:
    """Rewrite old /user/ URIs in a Qdrant collection.

    Idempotent: records that do not match the old pattern are skipped.

    Returns:
        Number of records updated.
    """
    all_records = await storage.filter(collection, None, limit=10000)
    updated = 0
    for record in all_records:
        uri = record.get("uri", "")
        rid = record.get("id", "")
        if not uri or not rid:
            continue

        new_uri = _rewrite(uri)
        if new_uri is None:
            continue  # Already new format or unrelated URI

        fields: dict = {"uri": new_uri}

        # Rewrite parent_uri if it also carries the old format
        parent_uri = record.get("parent_uri", "")
        if parent_uri:
            new_parent = _rewrite(parent_uri)
            if new_parent is not None:
                fields["parent_uri"] = new_parent

        await storage.update(collection, rid, fields)
        updated += 1

    logger.info(
        "[Migration v0.5.0] Rewrote %d URI(s) in collection '%s'",
        updated,
        collection,
    )
    return updated


_DEFAULT_COLLECTIONS = ["context", "traces", "knowledge"]


async def run(storage, cortex_fs=None, collections: list[str] | None = None) -> dict[str, int]:
    """Run the URI rewrite migration across all relevant collections.

    Args:
        storage:     VikingDBInterface-compatible storage adapter.
        cortex_fs:   CortexFS instance (unused currently — filesystem paths are
                     derived from URIs on-the-fly, so renaming directories is
                     not required for correctness).
        collections: List of collection names to migrate. When None, the default
                     set of known collections is used.

    Returns:
        Dict mapping collection name → number of records updated.
    """
    target_collections = collections or _DEFAULT_COLLECTIONS
    results: dict[str, int] = {}

    for coll in target_collections:
        try:
            count = await rewrite_uris(storage, coll)
            results[coll] = count
        except Exception as exc:
            logger.warning(
                "[Migration v0.5.0] Skipped collection '%s': %s", coll, exc
            )
            results[coll] = 0

    total = sum(results.values())
    logger.info("[Migration v0.5.0] Total records rewritten: %d", total)
    return results
