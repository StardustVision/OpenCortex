"""
PersonaMem v2 adapter for memory-mode evaluation.

Dataset: https://huggingface.co/datasets/bowen-upenn/PersonaMem-v2

Ingest: Stores pre-extracted persona_attributes via oc.store().
QA: Uses dataset's questions array.
Baseline: All persona attributes concatenated as a fact list.
Retrieve: Direct oc.search() (no session context).
"""

import json
import time
from typing import Any, Dict, List, Tuple

from benchmarks.adapters.base import EvalAdapter, IngestResult, QAItem


class MemoryAdapter(EvalAdapter):
    """PersonaMem v2 evaluation adapter."""

    def load_dataset(self, dataset_path: str, **kwargs) -> None:
        with open(dataset_path, encoding="utf-8") as f:
            self._dataset = json.load(f)

        # Validate required fields
        if not self._dataset.get("persona_attributes"):
            raise ValueError(
                "PersonaMem v2 dataset must contain non-empty 'persona_attributes'. "
                "The memory adapter requires pre-extracted structured attributes — "
                "no runtime extraction is performed."
            )

    async def ingest(self, oc: Any, **kwargs) -> IngestResult:
        """Store each persona attribute as a memory via oc.store().

        If max_qa is passed, only ingests attributes referenced by the first
        N questions to speed up quick tests.
        """
        max_qa = kwargs.get("max_qa", 0)
        attributes = self._dataset["persona_attributes"]

        if max_qa > 0:
            # Only ingest attributes needed by first max_qa questions
            questions = self._dataset.get("questions", [])[:max_qa]
            needed_ids = set()
            for q in questions:
                needed_ids.update(q.get("expected_ids", []))
            attributes = [a for a in attributes if a.get("id", "") in needed_ids]
        errors: List[str] = []
        id_to_uri: Dict[str, str] = {}

        for i, attr in enumerate(attributes):
            attr_text = attr.get("attribute", "")
            category = attr.get("category", "")
            attr_id = attr.get("id", str(i))
            try:
                result = await oc.store(
                    abstract=attr_text,
                    content=attr_text,
                    category=category,
                    context_type="memory",
                )
                uri = result.get("uri", "")
                if uri:
                    id_to_uri[attr_id] = uri
            except Exception as e:
                errors.append(f"attribute {attr_id}: {e}")

        # Store id→uri mapping for QA item URI resolution
        self._id_to_uri = id_to_uri

        return IngestResult(
            total_items=len(attributes),
            ingested_items=len(id_to_uri),
            errors=errors,
            meta={"id_to_uri": id_to_uri},
        )

    def build_qa_items(self, **kwargs) -> List[QAItem]:
        """Build QA items from dataset questions array."""
        questions = self._dataset.get("questions", [])
        max_qa = kwargs.get("max_qa", 0)
        if max_qa > 0:
            questions = questions[:max_qa]

        items: List[QAItem] = []
        id_to_uri = getattr(self, "_id_to_uri", {})

        for q in questions:
            expected_ids = q.get("expected_ids", [])
            expected_uris = [id_to_uri[eid] for eid in expected_ids if eid in id_to_uri]

            items.append(QAItem(
                question=q["question"],
                answer=str(q.get("answer", "")),
                category=q.get("category", ""),
                difficulty=q.get("difficulty", ""),
                expected_ids=expected_ids,
                expected_uris=expected_uris,
                meta=q.get("meta", {}),
            ))
        return items

    def get_baseline_context(self, qa_item: QAItem) -> str:
        """All persona attributes concatenated as a fact list."""
        attributes = self._dataset.get("persona_attributes", [])
        lines = [f"- {attr['attribute']}" for attr in attributes if attr.get("attribute")]
        return "Known facts about the user:\n" + "\n".join(lines)

    async def retrieve(self, oc: Any, qa_item: QAItem, top_k: int) -> Tuple[List[Dict], float]:
        """Direct memory search (no session context)."""
        t0 = time.perf_counter()
        results = await oc.search(query=qa_item.question, limit=top_k)
        latency_ms = (time.perf_counter() - t0) * 1000
        return results, latency_ms
