"""Source adapter — reads from OpenCortex memory store (read-only)."""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol


@dataclass
class MemoryCluster:
    cluster_id: str
    theme: str
    memory_ids: List[str]
    centroid_embedding: List[float]
    avg_score: float


@dataclass
class MemoryRecord:
    memory_id: str
    abstract: str
    overview: str
    content: str
    context_type: str
    category: str
    meta: Dict[str, Any] = field(default_factory=dict)


class SourceAdapter(Protocol):
    """Protocol for reading memories. Implementation bridges to Qdrant."""

    async def scan_memories(
        self, tenant_id: str, user_id: str,
        context_types: Optional[List[str]] = None,
        categories: Optional[List[str]] = None,
        min_count: int = 3,
    ) -> List[MemoryCluster]: ...

    async def get_cluster_memories(
        self, cluster: MemoryCluster,
    ) -> List[MemoryRecord]: ...
