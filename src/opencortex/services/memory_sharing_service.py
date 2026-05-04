# SPDX-License-Identifier: Apache-2.0
"""Memory sharing/admin mutation service for OpenCortex."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, List

from opencortex.http.request_context import get_effective_identity
from opencortex.services.memory_filters import FilterExpr
from opencortex.utils.uri import CortexURI

if TYPE_CHECKING:
    from opencortex.cortex_memory import CortexMemory


class MemorySharingService:
    """Own sharing/admin mutations while preserving orchestrator wrappers."""

    def __init__(self, orchestrator: "CortexMemory") -> None:
        self._orch = orchestrator

    async def promote_to_shared(
        self,
        uris: List[str],
        project_id: str,
    ) -> Dict[str, Any]:
        """Promote private resources to shared project scope."""
        orch = self._orch
        orch._ensure_init()
        tid, _uid = get_effective_identity()
        promoted = 0
        errors = []

        for uri in uris:
            try:
                results = await orch._storage.filter(
                    orch._get_collection(),
                    filter=FilterExpr.eq("uri", uri).to_dict(),
                    limit=1,
                )
                if not results:
                    errors.append({"uri": uri, "error": "not found"})
                    continue

                record = results[0]

                parts = uri.rstrip("/").split("/")
                node_name = parts[-1] if parts else "unnamed"
                new_uri = CortexURI.build_shared(
                    tid, "resources", project_id, "documents", node_name
                )

                record["uri"] = new_uri
                record["scope"] = "shared"
                record["project_id"] = project_id
                record["parent_uri"] = CortexURI.build_shared(
                    tid, "resources", project_id, "documents"
                )

                await orch._storage.upsert(orch._get_collection(), record)

                promoted += 1
            except Exception as exc:
                errors.append({"uri": uri, "error": str(exc)})

        return {
            "status": "ok" if not errors else "partial",
            "promoted": promoted,
            "total": len(uris),
            "errors": errors,
        }
