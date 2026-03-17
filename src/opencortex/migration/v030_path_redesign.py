"""v0.2.x -> v0.3.0 storage path migration. Idempotent."""

import logging

from opencortex.retrieve.types import MERGEABLE_CATEGORIES
from opencortex.utils.uri import CortexURI

logger = logging.getLogger(__name__)


def infer_scope(uri: str) -> str:
    """Infer scope from URI path."""
    try:
        return "private" if CortexURI(uri).is_private else "shared"
    except ValueError:
        return "shared"


def infer_category(uri: str) -> str:
    """Extract category from URI path segments."""
    parts = uri.replace("opencortex://", "").split("/")
    parents = ("memories", "skills", "resources", "cases", "patterns", "staging")
    for parent in parents:
        if parent in parts:
            idx = parts.index(parent)
            if parent in ("cases", "patterns"):
                return parent
            if idx + 1 < len(parts):
                candidate = parts[idx + 1]
                # Skip node_id (12-char hex)
                if len(candidate) != 12:
                    return candidate
    return ""


def infer_mergeable(category: str) -> bool:
    """Check if a category supports merging."""
    return category in MERGEABLE_CATEGORIES


async def backfill_new_fields(storage, collection: str) -> int:
    """Backfill scope/category/mergeable on existing records missing them.

    Idempotent: skips records that already have scope set.
    Returns count of updated records.
    """
    all_records = await storage.filter(collection, None, limit=10000)
    updated = 0
    for record in all_records:
        if record.get("scope"):
            continue  # Already migrated
        uri = record.get("uri", "")
        rid = record.get("id", "")
        if not uri or not rid:
            continue
        scope = infer_scope(uri)
        category = infer_category(uri)
        await storage.update(collection, rid, {
            "scope": scope,
            "category": category,
            "mergeable": infer_mergeable(category),
            "source_user_id": record.get("owner_user_id", ""),
            "session_id": "",
            "ttl_expires_at": "",
            "project_id": record.get("project_id", ""),
            "source_tenant_id": record.get("source_tenant_id", ""),
        })
        updated += 1
    logger.info("[Migration] Backfilled %d records in %s", updated, collection)
    return updated


# Root-level junk directories created by the URI bug (pre-v0.2.3)
ROOT_JUNK = [
    "agents", "coder-frontend", "coder-go", "coder-python",
    "coder-rust", "coding-style", "git-workflow", "hooks",
    "patterns", "performance", "security", "testing",
]


async def cleanup_root_junk(storage, cortex_fs, collection: str) -> int:
    """Delete root-level junk entries from Qdrant. Best-effort CortexFS cleanup."""
    cleaned = 0
    for name in ROOT_JUNK:
        uri = f"opencortex://{name}"
        records = await storage.filter(
            collection,
            {"op": "must", "field": "uri", "conds": [uri]},
            limit=10,
        )
        for rec in records:
            rid = rec.get("id", "")
            if rid:
                await storage.delete(collection, [rid])
                cleaned += 1
        # CortexFS cleanup (best-effort)
        try:
            if hasattr(cortex_fs, '_uri_to_path'):
                path = cortex_fs._uri_to_path(uri)
                if hasattr(cortex_fs, 'agfs'):
                    cortex_fs.agfs.rm(path, recursive=True)
        except Exception:
            pass
    if cleaned:
        logger.info("[Migration] Cleaned %d root-level junk records", cleaned)
    return cleaned
