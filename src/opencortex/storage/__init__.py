# SPDX-License-Identifier: Apache-2.0
"""
OpenCortex storage module.

Provides the abstract StorageInterface, CortexFS filesystem abstraction,
LocalAGFS adapter, and all storage exception classes.
"""

from opencortex.storage.storage_interface import (
    CollectionNotFoundError,
    ConnectionError,
    DuplicateKeyError,
    RecordNotFoundError,
    SchemaError,
    StorageException,
    StorageBackendError,
    StorageInterface,
)
from opencortex.storage.local_agfs import LocalAGFS
from opencortex.storage.cortex_fs import (
    CortexFS,
    init_cortex_fs,
    get_cortex_fs,
)
from opencortex.storage.qdrant import QdrantStorageAdapter

__all__ = [
    # Abstract interface & exceptions
    "StorageInterface",
    "StorageBackendError",
    "StorageException",
    "CollectionNotFoundError",
    "RecordNotFoundError",
    "DuplicateKeyError",
    "ConnectionError",
    "SchemaError",
    # Filesystem abstractions
    "LocalAGFS",
    "CortexFS",
    "init_cortex_fs",
    "get_cortex_fs",
    # Storage backends
    "QdrantStorageAdapter",
]
