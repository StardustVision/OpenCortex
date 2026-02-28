# SPDX-License-Identifier: Apache-2.0
"""Backward-compatible re-export: VikingFS → CortexFS."""

from opencortex.storage.cortex_fs import (  # noqa: F401
    CortexFS as VikingFS,
    init_cortex_fs as init_viking_fs,
    get_cortex_fs as get_viking_fs,
)

__all__ = ["VikingFS", "init_viking_fs", "get_viking_fs"]
