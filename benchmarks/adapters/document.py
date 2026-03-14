"""
Document adapter for QASPER, LongBench, and CMRC 2018 datasets.

Ingest: Each document stored via document mode (meta.ingest_mode="document").
QA: Normalized to common QAItem format from dataset-specific structures.
Baseline: Source document text.
Retrieve: oc.search(context_type="resource") to filter document chunks only.
"""

import json
import time
from typing import Any, Dict, List, Tuple

from benchmarks.adapters.base import EvalAdapter, IngestResult, QAItem


def _detect_document_dataset(data: Any) -> str:
    """Detect dataset type from JSON structure."""
    if isinstance(data, dict):
        # QASPER: dict keyed by paper ID
        first_key = next(iter(data), "")
        first_val = data.get(first_key, {})
        if isinstance(first_val, dict) and "full_text" in first_val:
            return "qasper"
    if isinstance(data, list) and data:
        first = data[0]
        if isinstance(first, dict):
            if "context" in first and "answers" in first and "answer_start" in first.get("answers", {}):
                return "cmrc"
            if "input" in first and "answers" in first:
                return "longbench"
    raise ValueError(
        "Cannot detect document dataset type. Expected QASPER (dict with full_text), "
        "LongBench (list with input+answers), or CMRC (list with context+answers.answer_start)."
    )


class DocumentAdapter(EvalAdapter):
    """QASPER / LongBench / CMRC 2018 evaluation adapter."""

    def load_dataset(self, dataset_path: str, **kwargs) -> None:
        with open(dataset_path, encoding="utf-8") as f:
            raw = json.load(f)

        dataset_type = kwargs.get("dataset_type", "")
        if not dataset_type:
            dataset_type = _detect_document_dataset(raw)

        self._dataset_type = dataset_type
        self._raw = raw
        self._dataset = self._normalize_dataset(raw, dataset_type)

    def _normalize_dataset(self, raw: Any, dtype: str) -> List[Dict]:
        """Normalize dataset to common format: [{doc_id, title, full_text, qas}]."""
        if dtype == "qasper":
            return self._normalize_qasper(raw)
        elif dtype == "longbench":
            return self._normalize_longbench(raw)
        elif dtype == "cmrc":
            return self._normalize_cmrc(raw)
        raise ValueError(f"Unknown document dataset type: {dtype}")

    def _normalize_qasper(self, raw: Dict) -> List[Dict]:
        docs = []
        for paper_id, paper in raw.items():
            full_text = ""
            for section in paper.get("full_text", []):
                heading = section.get("section_name", "")
                paragraphs = section.get("paragraphs", [])
                if heading:
                    full_text += f"\n## {heading}\n\n"
                full_text += "\n".join(paragraphs) + "\n"

            qas = []
            for qa_entry in paper.get("qas", []):
                question = qa_entry.get("question", "")
                # QASPER answers: list of annotator answers
                for ans_obj in qa_entry.get("answers", []):
                    answer_obj = ans_obj.get("answer", {})
                    if answer_obj.get("unanswerable", False):
                        answer = "unanswerable"
                    elif answer_obj.get("yes_no") is not None:
                        answer = "yes" if answer_obj["yes_no"] else "no"
                    elif answer_obj.get("extractive_spans"):
                        answer = " ".join(answer_obj["extractive_spans"])
                    elif answer_obj.get("free_form_answer"):
                        answer = answer_obj["free_form_answer"]
                    else:
                        continue
                    qas.append({"question": question, "answer": answer, "category": "qasper"})
                    break  # Use first annotator answer

            docs.append({
                "doc_id": paper_id,
                "title": paper.get("title", paper_id),
                "full_text": full_text.strip(),
                "qas": qas,
            })
        return docs

    def _normalize_longbench(self, raw: List[Dict]) -> List[Dict]:
        docs = []
        for i, item in enumerate(raw):
            doc_id = item.get("id", str(i))
            qas = []
            answers = item.get("answers", [])
            if isinstance(answers, list):
                answer = answers[0] if answers else ""
            else:
                answer = str(answers)
            qas.append({
                "question": item.get("input", ""),
                "answer": str(answer),
                "category": item.get("type", "longbench"),
            })
            docs.append({
                "doc_id": doc_id,
                "title": item.get("title", f"doc_{doc_id}"),
                "full_text": item.get("context", ""),
                "qas": qas,
            })
        return docs

    def _normalize_cmrc(self, raw: Any) -> List[Dict]:
        # CMRC: {"data": [{"paragraphs": [{"context": ..., "qas": [...]}]}]}
        paragraphs = []
        if isinstance(raw, dict) and "data" in raw:
            for article in raw["data"]:
                for para in article.get("paragraphs", []):
                    paragraphs.append(para)
        elif isinstance(raw, list):
            paragraphs = raw

        docs = []
        for i, para in enumerate(paragraphs):
            context = para.get("context", "")
            qas = []
            for qa in para.get("qas", []):
                answers = qa.get("answers", [])
                answer = answers[0].get("text", "") if answers else ""
                qas.append({
                    "question": qa.get("question", ""),
                    "answer": answer,
                    "category": "cmrc",
                })
            docs.append({
                "doc_id": para.get("id", str(i)),
                "title": f"paragraph_{i}",
                "full_text": context,
                "qas": qas,
            })
        return docs

    async def ingest(self, oc: Any, **kwargs) -> IngestResult:
        """Ingest documents via document mode (meta.ingest_mode='document')."""
        errors: List[str] = []
        ingested = 0

        for doc in self._dataset:
            doc_id = doc["doc_id"]
            try:
                await oc.store(
                    abstract=doc["title"],
                    content=doc["full_text"],
                    context_type="resource",
                    meta={
                        "ingest_mode": "document",
                        "source_path": f"{doc_id}.md",
                    },
                )
                ingested += 1
            except Exception as e:
                errors.append(f"doc={doc_id}: {e}")

        return IngestResult(
            total_items=len(self._dataset),
            ingested_items=ingested,
            errors=errors,
        )

    def build_qa_items(self, **kwargs) -> List[QAItem]:
        max_qa = kwargs.get("max_qa", 0)
        items: List[QAItem] = []

        for doc in self._dataset:
            for qa in doc.get("qas", []):
                items.append(QAItem(
                    question=qa["question"],
                    answer=str(qa.get("answer", "")),
                    category=qa.get("category", ""),
                    meta={"doc_id": doc["doc_id"], "dataset": self._dataset_type},
                ))

        if max_qa > 0:
            items = items[:max_qa]
        return items

    def get_baseline_context(self, qa_item: QAItem) -> str:
        """Source document text for baseline evaluation."""
        doc_id = qa_item.meta.get("doc_id", "")
        for doc in self._dataset:
            if doc["doc_id"] == doc_id:
                return doc["full_text"]
        return ""

    async def retrieve(self, oc: Any, qa_item: QAItem, top_k: int) -> Tuple[List[Dict], float]:
        """Search document chunks with context_type='resource' filter."""
        t0 = time.perf_counter()
        results = await oc.search(
            query=qa_item.question,
            limit=top_k,
            context_type="resource",
        )
        latency_ms = (time.perf_counter() - t0) * 1000
        return results, latency_ms
