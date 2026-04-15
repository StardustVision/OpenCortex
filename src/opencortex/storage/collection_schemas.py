# SPDX-License-Identifier: Apache-2.0
"""
Collection schema definitions for OpenCortex.

Provides centralized schema definitions and factory functions for creating collections,
similar to how init_cortex_fs encapsulates CortexFS initialization.
"""

import logging
from typing import Any, Dict

from opencortex.models.embedder.base import EmbedResult  # noqa: F401 - re-exported for convenience
from opencortex.storage.storage_interface import StorageInterface

logger = logging.getLogger(__name__)


class CollectionSchemas:
    """
    Centralized collection schema definitions.
    """

    @staticmethod
    def context_collection(name: str, vector_dim: int) -> Dict[str, Any]:
        """
        Get the schema for the unified context collection.

        Args:
            name: Collection name
            vector_dim: Dimension of the dense vector field

        Returns:
            Schema definition for the context collection
        """
        return {
            "CollectionName": name,
            "Description": "Unified context collection",
            "Fields": [
                {"FieldName": "id", "FieldType": "string", "IsPrimaryKey": True},
                {"FieldName": "uri", "FieldType": "path"},
                {"FieldName": "type", "FieldType": "string"},
                {"FieldName": "context_type", "FieldType": "string"},
                {"FieldName": "vector", "FieldType": "vector", "Dim": vector_dim},
                {"FieldName": "sparse_vector", "FieldType": "sparse_vector"},
                {"FieldName": "created_at", "FieldType": "date_time"},
                {"FieldName": "updated_at", "FieldType": "date_time"},
                {"FieldName": "accessed_at", "FieldType": "date_time"},
                {"FieldName": "active_count", "FieldType": "int64"},
                {"FieldName": "parent_uri", "FieldType": "path"},
                {"FieldName": "is_leaf", "FieldType": "bool"},
                {"FieldName": "name", "FieldType": "string"},
                {"FieldName": "description", "FieldType": "string"},
                {"FieldName": "tags", "FieldType": "string"},
                {"FieldName": "abstract", "FieldType": "string"},
                {"FieldName": "overview", "FieldType": "string"},
                {"FieldName": "keywords", "FieldType": "string"},
                {"FieldName": "reward_score", "FieldType": "float"},
                {"FieldName": "positive_feedback_count", "FieldType": "int64"},
                {"FieldName": "negative_feedback_count", "FieldType": "int64"},
                {"FieldName": "protected", "FieldType": "bool"},
                {"FieldName": "category", "FieldType": "string"},
                {"FieldName": "scope", "FieldType": "string"},
                {"FieldName": "session_id", "FieldType": "string"},
                {"FieldName": "source_user_id", "FieldType": "string"},
                {"FieldName": "mergeable", "FieldType": "bool"},
                {"FieldName": "ttl_expires_at", "FieldType": "string"},
                {"FieldName": "project_id", "FieldType": "string"},
                {"FieldName": "source_tenant_id", "FieldType": "string"},
                # v0.6: Document/Conversation enrichment
                {"FieldName": "source_doc_id", "FieldType": "string"},
                {"FieldName": "source_doc_title", "FieldType": "string"},
                {"FieldName": "source_section_path", "FieldType": "string"},
                {"FieldName": "chunk_role", "FieldType": "string"},
                {"FieldName": "speaker", "FieldType": "string"},
                {"FieldName": "event_date", "FieldType": "date_time"},
                {"FieldName": "retrieval_surface", "FieldType": "string"},
                {"FieldName": "anchor_surface", "FieldType": "bool"},
            ],
            "ScalarIndex": [
                "uri",
                "type",
                "context_type",
                "created_at",
                "updated_at",
                "active_count",
                "accessed_at",
                "parent_uri",
                "is_leaf",
                "name",
                "tags",
                "reward_score",
                "protected",
                "category",
                "scope",
                "session_id",
                "source_user_id",
                "mergeable",
                "ttl_expires_at",
                "project_id",
                "source_tenant_id",
                # v0.6
                "source_doc_id",
                "source_doc_title",
                "source_section_path",
                "chunk_role",
                "speaker",
                "event_date",
                "retrieval_surface",
                "anchor_surface",
            ],
        }



    @staticmethod
    def trace_collection(name: str, vector_dim: int) -> Dict[str, Any]:
        """Schema for the trace collection (Cortex Alpha)."""
        return {
            "CollectionName": name,
            "Description": "Cortex Alpha trace collection",
            "Fields": [
                {"FieldName": "id", "FieldType": "string", "IsPrimaryKey": True},
                {"FieldName": "trace_id", "FieldType": "string"},
                {"FieldName": "session_id", "FieldType": "string"},
                {"FieldName": "tenant_id", "FieldType": "string"},
                {"FieldName": "user_id", "FieldType": "string"},
                {"FieldName": "source", "FieldType": "string"},
                {"FieldName": "source_version", "FieldType": "string"},
                {"FieldName": "task_type", "FieldType": "string"},
                {"FieldName": "outcome", "FieldType": "string"},
                {"FieldName": "error_code", "FieldType": "string"},
                {"FieldName": "training_ready", "FieldType": "bool"},
                {"FieldName": "archivist_processed", "FieldType": "bool"},
                {"FieldName": "vector", "FieldType": "vector", "Dim": vector_dim},
                {"FieldName": "abstract", "FieldType": "string"},
                {"FieldName": "overview", "FieldType": "string"},
                {"FieldName": "created_at", "FieldType": "date_time"},
            ],
            "ScalarIndex": [
                "trace_id", "session_id", "tenant_id", "user_id",
                "source", "task_type", "outcome", "training_ready",
                "archivist_processed", "created_at",
            ],
        }

    @staticmethod
    def knowledge_collection(name: str, vector_dim: int) -> Dict[str, Any]:
        """Schema for the knowledge collection (Cortex Alpha)."""
        return {
            "CollectionName": name,
            "Description": "Cortex Alpha knowledge collection",
            "Fields": [
                {"FieldName": "id", "FieldType": "string", "IsPrimaryKey": True},
                {"FieldName": "knowledge_id", "FieldType": "string"},
                {"FieldName": "knowledge_type", "FieldType": "string"},
                {"FieldName": "tenant_id", "FieldType": "string"},
                {"FieldName": "user_id", "FieldType": "string"},
                {"FieldName": "scope", "FieldType": "string"},
                {"FieldName": "status", "FieldType": "string"},
                {"FieldName": "confidence", "FieldType": "float"},
                {"FieldName": "training_ready", "FieldType": "bool"},
                {"FieldName": "vector", "FieldType": "vector", "Dim": vector_dim},
                {"FieldName": "abstract", "FieldType": "string"},
                {"FieldName": "overview", "FieldType": "string"},
                {"FieldName": "created_at", "FieldType": "date_time"},
                {"FieldName": "updated_at", "FieldType": "date_time"},
            ],
            "ScalarIndex": [
                "knowledge_id", "knowledge_type", "tenant_id", "user_id",
                "scope", "status", "confidence", "training_ready",
                "created_at", "updated_at",
            ],
        }

    @staticmethod
    def skill_events_collection(name: str) -> Dict[str, Any]:
        """Skill events collection — no vectors, pure metadata."""
        return {
            "CollectionName": name,
            "Fields": [
                {"FieldName": "id", "FieldType": "string", "IsPrimaryKey": True},
                {"FieldName": "event_id", "FieldType": "string"},
                {"FieldName": "session_id", "FieldType": "string"},
                {"FieldName": "turn_id", "FieldType": "string"},
                {"FieldName": "skill_id", "FieldType": "string"},
                {"FieldName": "skill_uri", "FieldType": "string"},
                {"FieldName": "tenant_id", "FieldType": "string"},
                {"FieldName": "user_id", "FieldType": "string"},
                {"FieldName": "event_type", "FieldType": "string"},
                {"FieldName": "outcome", "FieldType": "string"},
                {"FieldName": "evaluated", "FieldType": "bool"},
                {"FieldName": "timestamp", "FieldType": "date_time"},
            ],
            "ScalarIndex": [
                "event_id", "session_id", "turn_id", "skill_id",
                "tenant_id", "user_id", "event_type", "outcome",
                "evaluated", "timestamp",
            ],
        }

    @staticmethod
    def skills_collection(name: str, vector_dim: int) -> Dict[str, Any]:
        """Skills collection schema — independent from memory collections."""
        return {
            "CollectionName": name,
            "Fields": [
                {"FieldName": "skill_id", "FieldType": "string"},
                {"FieldName": "name", "FieldType": "string"},
                {"FieldName": "description", "FieldType": "string"},
                {"FieldName": "category", "FieldType": "string"},
                {"FieldName": "status", "FieldType": "string"},
                {"FieldName": "visibility", "FieldType": "string"},
                {"FieldName": "tenant_id", "FieldType": "string"},
                {"FieldName": "user_id", "FieldType": "string"},
                {"FieldName": "project_id", "FieldType": "string"},
                {"FieldName": "uri", "FieldType": "string"},
                {"FieldName": "source_fingerprint", "FieldType": "string"},
                {"FieldName": "rating_rank", "FieldType": "string"},
                {"FieldName": "tdd_passed", "FieldType": "bool"},
                {"FieldName": "quality_score", "FieldType": "int64"},
                {"FieldName": "reward_score", "FieldType": "float"},
                {"FieldName": "vector", "FieldType": "vector", "Dim": vector_dim},
                {"FieldName": "abstract", "FieldType": "string"},
                {"FieldName": "overview", "FieldType": "string"},
                {"FieldName": "created_at", "FieldType": "date_time"},
                {"FieldName": "updated_at", "FieldType": "date_time"},
            ],
            "ScalarIndex": [
                "skill_id", "name", "category", "status", "visibility",
                "tenant_id", "user_id", "project_id", "uri", "source_fingerprint",
                "rating_rank", "tdd_passed", "quality_score", "reward_score",
                "created_at", "updated_at",
            ],
        }

    @staticmethod
    def cognitive_state_collection(name: str) -> Dict[str, Any]:
        """Schema for durable cognitive state (payload-only, no vectors)."""
        return {
            "CollectionName": name,
            "Description": "Cognitive owner state",
            "Fields": [
                {"FieldName": "id", "FieldType": "string", "IsPrimaryKey": True},
                {"FieldName": "state_id", "FieldType": "string"},
                {"FieldName": "owner_type", "FieldType": "string"},
                {"FieldName": "owner_id", "FieldType": "string"},
                {"FieldName": "tenant_id", "FieldType": "string"},
                {"FieldName": "user_id", "FieldType": "string"},
                {"FieldName": "project_id", "FieldType": "string"},
                {"FieldName": "lifecycle_state", "FieldType": "string"},
                {"FieldName": "exposure_state", "FieldType": "string"},
                {"FieldName": "consolidation_state", "FieldType": "string"},
                {"FieldName": "activation_score", "FieldType": "float"},
                {"FieldName": "stability_score", "FieldType": "float"},
                {"FieldName": "risk_score", "FieldType": "float"},
                {"FieldName": "novelty_score", "FieldType": "float"},
                {"FieldName": "evidence_residual_score", "FieldType": "float"},
                {"FieldName": "access_count", "FieldType": "int64"},
                {"FieldName": "retrieval_success_count", "FieldType": "int64"},
                {"FieldName": "retrieval_failure_count", "FieldType": "int64"},
                {"FieldName": "last_accessed_at", "FieldType": "date_time"},
                {"FieldName": "last_reinforced_at", "FieldType": "date_time"},
                {"FieldName": "last_penalized_at", "FieldType": "date_time"},
                {"FieldName": "last_mutation_at", "FieldType": "date_time"},
                {"FieldName": "last_mutation_reason", "FieldType": "string"},
                {"FieldName": "last_mutation_source", "FieldType": "string"},
                {"FieldName": "version", "FieldType": "int64"},
                {"FieldName": "metadata", "FieldType": "string"},
            ],
            "ScalarIndex": [
                "state_id",
                "owner_type",
                "owner_id",
                "tenant_id",
                "user_id",
                "project_id",
                "lifecycle_state",
                "exposure_state",
                "consolidation_state",
                "activation_score",
                "stability_score",
                "risk_score",
                "novelty_score",
                "evidence_residual_score",
                "access_count",
                "retrieval_success_count",
                "retrieval_failure_count",
                "last_accessed_at",
                "last_reinforced_at",
                "last_penalized_at",
                "last_mutation_at",
                "last_mutation_reason",
                "last_mutation_source",
                "version",
            ],
        }

    @staticmethod
    def cognitive_mutation_batch_collection(name: str) -> Dict[str, Any]:
        """Schema for cognitive mutation ledger (explicit payload-only schema)."""
        return {
            "CollectionName": name,
            "Description": "Cognitive mutation batch ledger",
            "Fields": [
                {"FieldName": "id", "FieldType": "string", "IsPrimaryKey": True},
                {"FieldName": "batch_id", "FieldType": "string"},
                {"FieldName": "owner_ids", "FieldType": "string"},
                {"FieldName": "status", "FieldType": "string"},
                {"FieldName": "error", "FieldType": "string"},
                {"FieldName": "metadata", "FieldType": "string"},
                {"FieldName": "created_at", "FieldType": "date_time"},
                {"FieldName": "updated_at", "FieldType": "date_time"},
                {"FieldName": "committed_at", "FieldType": "date_time"},
            ],
            "ScalarIndex": [
                "batch_id",
                "owner_ids",
                "status",
                "created_at",
                "updated_at",
                "committed_at",
            ],
        }

    @staticmethod
    def consolidation_candidate_collection(name: str) -> Dict[str, Any]:
        """Schema for consolidation candidates (payload-only, no vectors)."""
        return {
            "CollectionName": name,
            "Description": "Governance consolidation candidates",
            "Fields": [
                {"FieldName": "id", "FieldType": "string", "IsPrimaryKey": True},
                {"FieldName": "candidate_id", "FieldType": "string"},
                {"FieldName": "source_owner_type", "FieldType": "string"},
                {"FieldName": "source_owner_id", "FieldType": "string"},
                {"FieldName": "tenant_id", "FieldType": "string"},
                {"FieldName": "user_id", "FieldType": "string"},
                {"FieldName": "project_id", "FieldType": "string"},
                {"FieldName": "candidate_kind", "FieldType": "string"},
                {"FieldName": "statement", "FieldType": "string"},
                {"FieldName": "abstract", "FieldType": "string"},
                {"FieldName": "overview", "FieldType": "string"},
                {"FieldName": "supporting_memory_ids", "FieldType": "string"},
                {"FieldName": "supporting_trace_ids", "FieldType": "string"},
                {"FieldName": "confidence_estimate", "FieldType": "float"},
                {"FieldName": "stability_score", "FieldType": "float"},
                {"FieldName": "risk_score", "FieldType": "float"},
                {"FieldName": "conflict_summary", "FieldType": "string"},
                {"FieldName": "submission_reason", "FieldType": "string"},
                {"FieldName": "dedupe_fingerprint", "FieldType": "string"},
                {"FieldName": "created_at", "FieldType": "date_time"},
                {"FieldName": "updated_at", "FieldType": "date_time"},
            ],
            "ScalarIndex": [
                "candidate_id",
                "source_owner_type",
                "source_owner_id",
                "tenant_id",
                "user_id",
                "project_id",
                "candidate_kind",
                "dedupe_fingerprint",
                "created_at",
                "updated_at",
            ],
        }


async def init_trace_collection(
    storage: StorageInterface, name: str, vector_dim: int,
) -> bool:
    """Initialize the trace collection with proper schema."""
    schema = CollectionSchemas.trace_collection(name, vector_dim)
    return await storage.create_collection(name, schema)


async def init_knowledge_collection(
    storage: StorageInterface, name: str, vector_dim: int,
) -> bool:
    """Initialize the knowledge collection with proper schema."""
    schema = CollectionSchemas.knowledge_collection(name, vector_dim)
    return await storage.create_collection(name, schema)


async def init_skills_collection(
    storage: StorageInterface, name: str, vector_dim: int,
) -> bool:
    """Initialize the skills collection with proper schema."""
    schema = CollectionSchemas.skills_collection(name, vector_dim)
    return await storage.create_collection(name, schema)


async def init_skill_events_collection(
    storage: StorageInterface, name: str,
) -> bool:
    """Initialize the skill events collection."""
    schema = CollectionSchemas.skill_events_collection(name)
    return await storage.create_collection(name, schema)


async def init_context_collection(
    storage: StorageInterface,
    name: str,
    vector_dim: int,
) -> bool:
    """
    Initialize the context collection with proper schema.

    Args:
        storage: Storage interface instance
        name: Collection name
        vector_dim: Dimension of the embedding vector

    Returns:
        True if collection was created, False if already exists
    """
    schema = CollectionSchemas.context_collection(name, vector_dim)
    return await storage.create_collection(name, schema)


async def init_cognitive_state_collection(
    storage: StorageInterface, name: str,
) -> bool:
    """Initialize the cognitive state collection."""
    schema = CollectionSchemas.cognitive_state_collection(name)
    return await storage.create_collection(name, schema)


async def init_cognitive_mutation_batch_collection(
    storage: StorageInterface, name: str,
) -> bool:
    """Initialize the cognitive mutation batch collection."""
    schema = CollectionSchemas.cognitive_mutation_batch_collection(name)
    return await storage.create_collection(name, schema)


async def init_consolidation_candidate_collection(
    storage: StorageInterface, name: str,
) -> bool:
    """Initialize the consolidation candidate collection."""
    schema = CollectionSchemas.consolidation_candidate_collection(name)
    return await storage.create_collection(name, schema)
