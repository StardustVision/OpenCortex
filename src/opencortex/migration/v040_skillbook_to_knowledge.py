"""v0.3.x → v0.4.0: Migrate Skillbook entries to Knowledge Store.

Reads all records from the `skillbooks` collection and maps each to a
Knowledge item in the `knowledge` collection:
- Skills with `action_template` or `action_steps` → KnowledgeType.SOP
- Others → KnowledgeType.BELIEF

Idempotent: skips records already present in the knowledge collection.
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def classify_skill(record: Dict[str, Any]) -> str:
    """Determine knowledge type from skill record."""
    action_template = record.get("action_template", [])
    action_steps = record.get("action_steps", [])
    section = record.get("section", "")
    content = record.get("content", "")

    # Has explicit action steps → SOP
    if action_template or action_steps:
        return "sop"

    # Section hints
    if section in ("error_fixes",):
        return "root_cause"
    if section in ("patterns",):
        return "belief"

    # Content heuristics: numbered steps suggest SOP
    lines = content.split("\n") if content else []
    numbered_lines = sum(1 for l in lines if l.strip()[:2].rstrip(".").isdigit())
    if numbered_lines >= 2:
        return "sop"

    return "belief"


def map_scope(scope: str) -> str:
    """Map skill scope to knowledge scope."""
    mapping = {
        "private": "user",
        "shared": "tenant",
        "legacy": "user",
    }
    return mapping.get(scope, "user")


def map_status(status: str) -> str:
    """Map skill status to knowledge status."""
    mapping = {
        "active": "active",
        "protected": "active",
        "observation": "candidate",
        "deprecated": "deprecated",
        "invalid": "deprecated",
    }
    return mapping.get(status, "active")


def skill_to_knowledge(record: Dict[str, Any]) -> Dict[str, Any]:
    """Convert a single skill record to a knowledge record."""
    knowledge_type = classify_skill(record)
    knowledge_id = f"migrated-{record.get('id', str(uuid.uuid4())[:12])}"
    now = datetime.now(timezone.utc).isoformat()

    knowledge = {
        "id": knowledge_id,
        "knowledge_id": knowledge_id,
        "knowledge_type": knowledge_type,
        "tenant_id": record.get("tenant_id", "default"),
        "user_id": record.get("owner_user_id", "default"),
        "scope": map_scope(record.get("scope", "private")),
        "status": map_status(record.get("status", "active")),
        "confidence": record.get("confidence_score", 0.5),
        "training_ready": False,
        "abstract": record.get("content", "")[:200],
        "overview": "",
        "created_at": record.get("created_at", now),
        "updated_at": now,
    }

    content = record.get("content", "")
    justification = record.get("justification", "")

    if knowledge_type == "sop":
        action_template = record.get("action_template", [])
        knowledge["action_steps"] = action_template if action_template else [content]
        if record.get("trigger_conditions"):
            knowledge["trigger_keywords"] = record["trigger_conditions"]
        if record.get("success_metric"):
            knowledge["success_criteria"] = record["success_metric"]
    elif knowledge_type == "root_cause":
        knowledge["error_pattern"] = content
        if justification:
            knowledge["cause"] = justification
    else:
        # belief
        knowledge["statement"] = content

    # Build overview from available context
    parts = []
    if content:
        parts.append(content)
    if justification:
        parts.append(f"Justification: {justification}")
    if record.get("evidence"):
        parts.append(f"Evidence: {record['evidence']}")
    knowledge["overview"] = "\n\n".join(parts)

    return knowledge


async def migrate_skillbook_to_knowledge(
    storage,
    embedder,
    source_collection: str = "skillbooks",
    target_collection: str = "knowledge",
    embedding_dim: int = 1024,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Run the migration.

    Returns:
        Dict with migrated, skipped, errors counts.
    """
    from opencortex.storage.collection_schemas import init_knowledge_collection

    stats = {"migrated": 0, "skipped": 0, "errors": 0, "total": 0}

    # Ensure target collection exists
    await init_knowledge_collection(storage, target_collection, embedding_dim)

    # Read all skills
    try:
        all_skills = await storage.scroll(source_collection, limit=10000)
    except Exception as e:
        logger.error("Failed to read skillbooks collection: %s", e)
        return stats

    stats["total"] = len(all_skills)
    logger.info("Migration: found %d skills to migrate", len(all_skills))

    for record in all_skills:
        skill_id = record.get("id", "")
        try:
            knowledge = skill_to_knowledge(record)
            knowledge_id = knowledge["knowledge_id"]

            # Check if already migrated
            existing = await storage.get(target_collection, [knowledge_id])
            if existing:
                stats["skipped"] += 1
                continue

            if dry_run:
                logger.info("DRY RUN: would migrate skill %s → %s", skill_id, knowledge_id)
                stats["migrated"] += 1
                continue

            # Embed the abstract
            embed_text = knowledge.get("abstract", knowledge_id)
            embed_result = embedder.embed(embed_text)
            knowledge["vector"] = embed_result.dense_vector

            await storage.upsert(target_collection, knowledge)
            stats["migrated"] += 1
            logger.debug("Migrated skill %s → knowledge %s", skill_id, knowledge_id)

        except Exception as e:
            logger.warning("Failed to migrate skill %s: %s", skill_id, e)
            stats["errors"] += 1

    logger.info(
        "Migration complete: %d migrated, %d skipped, %d errors (of %d total)",
        stats["migrated"], stats["skipped"], stats["errors"], stats["total"],
    )
    return stats
