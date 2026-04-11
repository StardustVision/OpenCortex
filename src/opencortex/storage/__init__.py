# SPDX-License-Identifier: Apache-2.0
"""
OpenCortex storage module.

Provides the abstract StorageInterface, CortexFS filesystem abstraction,
LocalAGFS adapter, and all storage exception classes.
"""

from importlib import import_module

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

_LAZY_ATTRS = {
    "LocalAGFS": ("opencortex.storage.local_agfs", "LocalAGFS"),
    "CortexFS": ("opencortex.storage.cortex_fs", "CortexFS"),
    "init_cortex_fs": ("opencortex.storage.cortex_fs", "init_cortex_fs"),
    "get_cortex_fs": ("opencortex.storage.cortex_fs", "get_cortex_fs"),
    "QdrantStorageAdapter": ("opencortex.storage.qdrant", "QdrantStorageAdapter"),
}


def __getattr__(name: str):
    target = _LAZY_ATTRS.get(name)
    if target is None:
        raise AttributeError(name)
    module_name, attr_name = target
    module = import_module(module_name)
    value = getattr(module, attr_name)
    globals()[name] = value
    return value
