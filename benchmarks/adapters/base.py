"""
EvalAdapter abstract base class and common dataclasses.

Each adapter handles one ingestion mode (memory/conversation/document)
and provides methods for dataset loading, ingestion, QA extraction,
baseline context, and retrieval.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple


@dataclass
class QAItem:
    """A single QA evaluation item."""

    question: str
    answer: str
    category: str = ""
    difficulty: str = ""
    expected_ids: List[str] = field(default_factory=list)
    expected_uris: List[str] = field(default_factory=list)
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class IngestResult:
    """Result of ingesting a dataset into OpenCortex."""

    total_items: int
    ingested_items: int
    errors: List[str] = field(default_factory=list)
    meta: Dict[str, Any] = field(default_factory=dict)


class EvalAdapter(ABC):
    """Abstract base class for mode-specific evaluation adapters.

    Lifecycle:
        1. load_dataset(path) — load and cache dataset
        2. ingest(oc) — write data to OpenCortex
        3. build_qa_items() — extract QA pairs for evaluation
        4. For each QA item:
           a. retrieve(oc, item, top_k) — search OpenCortex
           b. get_baseline_context(item) — get full context for baseline
    """

    def __init__(self):
        self._dataset: Any = None

    def load_dataset(self, dataset_path: str, **kwargs) -> None:
        """Load and cache the dataset. Called once before ingest/build_qa_items.

        Subclasses store parsed data in self._dataset for use by all methods.
        """
        ...

    @abstractmethod
    async def ingest(self, oc: Any, **kwargs) -> IngestResult:
        """Ingest loaded dataset into OpenCortex using mode-appropriate API calls."""
        ...

    @abstractmethod
    def build_qa_items(self, **kwargs) -> List[QAItem]:
        """Return QA items from loaded dataset for evaluation."""
        ...

    @abstractmethod
    def get_baseline_context(self, qa_item: QAItem) -> str:
        """Return full context for baseline LLM evaluation (no retrieval).

        Uses self._dataset to look up source documents/conversations.
        """
        ...

    @abstractmethod
    async def retrieve(self, oc: Any, qa_item: QAItem, top_k: int) -> Tuple[List[Dict], float]:
        """Retrieve relevant memories/chunks. Returns (results, latency_ms).

        Each result dict must contain 'uri' for retrieval quality measurement.
        """
        ...
