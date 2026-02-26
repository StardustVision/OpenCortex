# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# Ported from OpenViking (https://github.com/volcengine/openviking)
# SPDX-License-Identifier: Apache-2.0
"""
OpenCortex storage module.

Provides the abstract VikingDBInterface, VikingFS filesystem abstraction,
LocalAGFS adapter, and all storage exception classes.
"""

from opencortex.storage.vikingdb_interface import (
    CollectionNotFoundError,
    ConnectionError,
    DuplicateKeyError,
    RecordNotFoundError,
    SchemaError,
    StorageException,
    VikingDBException,
    VikingDBInterface,
)
from opencortex.storage.local_agfs import LocalAGFS
from opencortex.storage.viking_fs import VikingFS, init_viking_fs, get_viking_fs
from opencortex.storage.ruvector import (
    RuVectorAdapter,
    RuVectorCLI,
    RuVectorConfig,
    SonaProfile,
    DecayResult,
)

__all__ = [
    # Abstract interface & exceptions
    "VikingDBInterface",
    "VikingDBException",
    "StorageException",
    "CollectionNotFoundError",
    "RecordNotFoundError",
    "DuplicateKeyError",
    "ConnectionError",
    "SchemaError",
    # Filesystem abstractions
    "LocalAGFS",
    "VikingFS",
    "init_viking_fs",
    "get_viking_fs",
    # RuVector backend
    "RuVectorAdapter",
    "RuVectorCLI",
    "RuVectorConfig",
    "SonaProfile",
    "DecayResult",
]
