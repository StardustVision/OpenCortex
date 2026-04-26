"""EvalAdapter abstract base class and common dataclasses.

Each adapter handles one ingestion mode (memory/conversation/document)
and provides methods for dataset loading, ingestion, QA extraction,
baseline context, and retrieval.
"""

import json
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


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

    def __init__(self) -> None:
        self._dataset: Any = None
        self._last_retrieval_meta: Dict[str, Any] = {}
        self._retrieve_method: str = "search"
        self._ingest_method: str = ""
        self._ingest_concurrency: int = 4

    # ------------------------------------------------------------------
    # Retrieval metadata tracking
    # ------------------------------------------------------------------

    def _set_last_retrieval_meta(
        self,
        payload: Any,
        *,
        endpoint: str = "",
        session_scope: bool = False,
    ) -> None:
        """Persist raw retrieval attribution for the last adapter call."""
        if not isinstance(payload, dict):
            self._last_retrieval_meta = {}
            return

        intent = payload.get("intent")
        if not isinstance(intent, dict):
            intent = {}

        memory_pipeline = payload.get("memory_pipeline")
        if not isinstance(memory_pipeline, dict):
            memory_pipeline = intent.get("memory_pipeline")
        if not isinstance(memory_pipeline, dict):
            memory_pipeline = {}

        meta: Dict[str, Any] = {}
        if intent:
            meta["intent"] = intent
        if memory_pipeline:
            meta["memory_pipeline"] = memory_pipeline
        meta["retrieval_contract"] = {
            "method": str(getattr(self, "_retrieve_method", "") or ""),
            "endpoint": endpoint,
            "session_scope": bool(session_scope),
        }
        self._last_retrieval_meta = meta

    def pop_last_retrieval_meta(self) -> Dict[str, Any]:
        """Return and clear the last raw retrieval attribution payload."""
        meta = dict(self._last_retrieval_meta)
        self._last_retrieval_meta = {}
        return meta

    # ------------------------------------------------------------------
    # Dataset loading
    # ------------------------------------------------------------------

    def load_dataset(self, dataset_path: str, **kwargs: Any) -> None:
        """Load and cache the dataset from a JSON file.

        Subclasses may override ``_validate_dataset`` to add dataset-
        specific validation after the JSON is parsed and stored in
        ``self._dataset``.
        """
        with open(dataset_path, encoding="utf-8") as f:
            raw = json.load(f)
        self._dataset = raw
        self._validate_dataset(raw)

    def _validate_dataset(self, raw: Any) -> None:
        """Hook for subclasses to validate loaded dataset. No-op by default."""

    # ------------------------------------------------------------------
    # Retrieval dispatch (template method)
    # ------------------------------------------------------------------

    async def retrieve(
        self, oc: Any, qa_item: QAItem, top_k: int,
    ) -> Tuple[List[Dict[str, Any]], float]:
        """Retrieve relevant memories via recall or search.

        Dispatches based on ``self._retrieve_method``:
        - ``"recall"`` → ``oc.context_recall()`` with ``session_scope``
          and ``session_id`` from hooks.
        - ``"search"`` (default) → ``oc.search_payload()`` with optional
          ``metadata_filter`` from hooks.

        Subclasses override hook methods instead of this method directly.
        """
        started = time.perf_counter()
        session_id = self._get_retrieval_session_id(qa_item)
        session_scope = self._get_retrieval_session_scope()
        metadata_filter = self._get_retrieval_metadata_filter(session_id)
        context_type = self._get_retrieval_context_type()

        if self._retrieve_method == "recall":
            recall_kwargs: Dict[str, Any] = {
                "session_id": session_id or "",
                "query": qa_item.question,
                "limit": top_k,
            }
            detail = self._get_retrieval_detail_level()
            if detail:
                recall_kwargs["detail_level"] = detail
            turn_id = self._get_retrieval_turn_id(qa_item)
            if turn_id:
                recall_kwargs["turn_id"] = turn_id
            if session_scope:
                recall_kwargs["session_scope"] = True

            result = await oc.context_recall(**recall_kwargs)
            self._set_last_retrieval_meta(
                result, endpoint="context_recall", session_scope=session_scope,
            )
            raw_results = result.get("memory", [])
        else:
            search_kwargs: Dict[str, Any] = {
                "query": qa_item.question,
                "limit": top_k,
            }
            if context_type:
                search_kwargs["context_type"] = context_type
            detail = self._get_retrieval_detail_level()
            if detail:
                search_kwargs["detail_level"] = detail
            if metadata_filter:
                search_kwargs["metadata_filter"] = metadata_filter

            result = await oc.search_payload(**search_kwargs)
            self._set_last_retrieval_meta(
                result, endpoint="memory_search", session_scope=session_scope,
            )
            raw_results = result.get("results", [])

        results = self._post_process_retrieval(raw_results)
        latency_ms = (time.perf_counter() - started) * 1000
        return results, latency_ms

    def _get_retrieval_session_id(self, qa_item: QAItem) -> Optional[str]:
        """Return the session_id for retrieval, or None for non-scoped."""
        return None

    def _get_retrieval_session_scope(self) -> bool:
        """Return whether to set session_scope on the retrieval call."""
        return False

    def _get_retrieval_metadata_filter(
        self, session_id: Optional[str],
    ) -> Optional[Dict[str, Any]]:
        """Return metadata_filter dict, or None for unfiltered search."""
        if session_id:
            return {
                "op": "must",
                "field": "session_id",
                "conds": [session_id],
            }
        return None

    def _get_retrieval_context_type(self) -> Optional[str]:
        """Return context_type for search_payload, or None for default."""
        return None

    def _get_retrieval_detail_level(self) -> Optional[str]:
        """Return detail_level for the retrieval call, or None to omit."""
        return None

    def _get_retrieval_turn_id(self, qa_item: QAItem) -> Optional[str]:
        """Return turn_id for context_recall, or None to omit."""
        return None

    def _post_process_retrieval(
        self, results: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Post-process raw retrieval results. Default returns as-is."""
        return results

    # ------------------------------------------------------------------
    # Abstract methods
    # ------------------------------------------------------------------

    @abstractmethod
    async def ingest(self, oc: Any, **kwargs: Any) -> IngestResult:
        """Ingest loaded dataset into OpenCortex using mode-appropriate API calls."""
        ...

    @abstractmethod
    def build_qa_items(self, **kwargs: Any) -> List[QAItem]:
        """Return QA items from loaded dataset for evaluation."""
        ...

    @abstractmethod
    def get_baseline_context(self, qa_item: QAItem) -> str:
        """Return full context for baseline LLM evaluation (no retrieval).

        Uses self._dataset to look up source documents/conversations.
        """
        ...

