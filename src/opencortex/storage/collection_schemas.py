# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# Ported from OpenViking (https://github.com/volcengine/openviking)
# SPDX-License-Identifier: Apache-2.0
"""
Collection schema definitions for OpenCortex.

Provides centralized schema definitions and factory functions for creating collections,
similar to how init_viking_fs encapsulates VikingFS initialization.
"""

import logging
from typing import Any, Dict

from opencortex.models.embedder.base import EmbedResult  # noqa: F401 - re-exported for convenience
from opencortex.storage.vikingdb_interface import VikingDBInterface

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
                {"FieldName": "active_count", "FieldType": "int64"},
                {"FieldName": "parent_uri", "FieldType": "path"},
                {"FieldName": "is_leaf", "FieldType": "bool"},
                {"FieldName": "name", "FieldType": "string"},
                {"FieldName": "description", "FieldType": "string"},
                {"FieldName": "tags", "FieldType": "string"},
                {"FieldName": "abstract", "FieldType": "string"},
            ],
            "ScalarIndex": [
                "uri",
                "type",
                "context_type",
                "created_at",
                "updated_at",
                "active_count",
                "parent_uri",
                "is_leaf",
                "name",
                "tags",
            ],
        }


async def init_context_collection(
    storage: VikingDBInterface,
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
