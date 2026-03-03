"""v0.3.2 migration: re-generate L0/L1 from L2 content using overview-first flow.

Scrolls all leaf records in the context collection, reads L2 content from
CortexFS, re-generates L1 overview, extracts L0 abstract from L1, enriches
with key terms, re-embeds, and updates both Qdrant payload and CortexFS files.

Usage (standalone):
    uv run python -m opencortex.migration.v032_overview_first [--dry-run] [--batch 50]

Usage (programmatic):
    from opencortex.migration.v032_overview_first import migrate_overview_first
    result = await migrate_overview_first(orchestrator, dry_run=False)
"""

import argparse
import asyncio
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_CONTEXT_COLLECTION = "context"


async def migrate_overview_first(
    orchestrator: "MemoryOrchestrator",  # noqa: F821
    *,
    dry_run: bool = False,
    batch_size: int = 50,
) -> Dict[str, Any]:
    """Re-generate L0/L1 from L2 content for all leaf records.

    Args:
        orchestrator: Initialized MemoryOrchestrator instance.
        dry_run: If True, log changes but don't write.
        batch_size: Number of records to scroll per batch.

    Returns:
        Summary dict with counts: total, updated, skipped, errors.
    """
    from opencortex.orchestrator import MemoryOrchestrator

    storage = orchestrator._storage
    fs = orchestrator._fs
    embedder = orchestrator._embedder

    total = 0
    updated = 0
    skipped = 0
    errors = 0
    cursor: Optional[str] = None

    logger.info(
        "[Migration v0.3.2] Starting overview-first migration (dry_run=%s)",
        dry_run,
    )

    while True:
        records, cursor = await storage.scroll(
            _CONTEXT_COLLECTION,
            filter={"op": "must", "field": "is_leaf", "conds": [True]},
            limit=batch_size,
            cursor=cursor,
        )

        if not records:
            break

        for record in records:
            total += 1
            uri = record.get("uri", "")
            rid = record.get("id", "")

            if not uri or not rid:
                skipped += 1
                continue

            # Read L2 content from CortexFS
            try:
                raw = await fs.read(uri + "/content.md")
                content = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
            except Exception:
                content = ""

            if not content or not content.strip():
                skipped += 1
                logger.debug("[Migration] Skipped %s: no L2 content", uri)
                continue

            old_abstract = record.get("abstract", "")
            old_overview = record.get("overview", "")

            # Step 1: Generate L1 overview from content
            try:
                new_overview = await orchestrator._generate_overview(
                    old_abstract, content,
                )
            except Exception as e:
                errors += 1
                logger.warning("[Migration] L1 generation failed for %s: %s", uri, e)
                continue

            if not new_overview:
                skipped += 1
                continue

            # Step 2: Extract L0 abstract from L1
            new_abstract = MemoryOrchestrator._extract_abstract_from_overview(
                new_overview,
            )
            if not new_abstract:
                new_abstract = old_abstract

            # Step 3: Enrich L0 with key terms
            new_abstract = MemoryOrchestrator._enrich_abstract(
                new_abstract, content,
            )

            # Check if anything actually changed
            if new_abstract == old_abstract and new_overview == old_overview:
                skipped += 1
                continue

            logger.info(
                "[Migration] %s\n  L0: %s -> %s\n  L1: %s... -> %s...",
                uri,
                old_abstract[:60],
                new_abstract[:60],
                (old_overview or "")[:40],
                (new_overview or "")[:40],
            )

            if dry_run:
                updated += 1
                continue

            # Step 4: Re-embed with new abstract (vectorization uses L0)
            try:
                vector = None
                sparse_vector = None
                if embedder:
                    loop = asyncio.get_event_loop()
                    result = await loop.run_in_executor(
                        None, embedder.embed, new_abstract,
                    )
                    vector = result.dense_vector
                    sparse_vector = result.sparse_vector

                # Update Qdrant payload + vector
                update_data: Dict[str, Any] = {
                    "abstract": new_abstract,
                    "overview": new_overview,
                }
                if vector:
                    update_data["vector"] = vector
                if sparse_vector:
                    update_data["sparse_vector"] = sparse_vector

                await storage.update(_CONTEXT_COLLECTION, rid, update_data)

                # Update CortexFS files (L0 + L1)
                await fs.write_context(
                    uri,
                    abstract=new_abstract,
                    overview=new_overview,
                )

                updated += 1

            except Exception as e:
                errors += 1
                logger.error("[Migration] Update failed for %s: %s", uri, e)

        if cursor is None:
            break

    summary = {
        "total": total,
        "updated": updated,
        "skipped": skipped,
        "errors": errors,
        "dry_run": dry_run,
    }
    logger.info("[Migration v0.3.2] Done: %s", summary)
    return summary


async def _main(args: argparse.Namespace) -> None:
    """CLI entry point."""
    from opencortex.config import CortexConfig, init_config
    from opencortex.orchestrator import MemoryOrchestrator

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    CortexConfig.ensure_default_config()
    init_config(path=args.config)

    orch = MemoryOrchestrator()
    await orch.init()

    try:
        result = await migrate_overview_first(
            orch,
            dry_run=args.dry_run,
            batch_size=args.batch,
        )
        print(f"\nMigration complete: {result}")
    finally:
        await orch.close()


def main():
    parser = argparse.ArgumentParser(
        prog="opencortex.migration.v032_overview_first",
        description="Re-generate L0/L1 from L2 content (overview-first flow)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log changes without writing",
    )
    parser.add_argument(
        "--batch",
        type=int,
        default=50,
        help="Records per scroll batch (default: 50)",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Path to server.json config file",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()
    asyncio.run(_main(args))


if __name__ == "__main__":
    main()
