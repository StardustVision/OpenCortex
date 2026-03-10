# SPDX-License-Identifier: Apache-2.0
"""
Qdrant storage backend for OpenCortex.

Provides QdrantStorageAdapter — a StorageInterface implementation using
Qdrant's embedded local mode (AsyncQdrantClient with path).
"""

from opencortex.storage.qdrant.adapter import QdrantStorageAdapter

__all__ = ["QdrantStorageAdapter"]
