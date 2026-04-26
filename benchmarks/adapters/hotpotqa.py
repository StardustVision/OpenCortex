"""
HotPotQA adapter for document-mode evaluation (multi-hop reasoning).

Dataset: https://hotpotqa.github.io/
Format: hotpot_dev_distractor_v1.json — 7405 questions, 10 context
paragraphs each (2 gold + 8 distractors).

Ingest: Each unique paragraph title → one memory record (memory mode,
        no LLM chunking overhead).
QA: question/answer + supporting_facts → expected_uris.
Baseline: All 10 distractor paragraphs per question.
Retrieve: oc.search(context_type="resource") for document chunks.
"""

import asyncio
import json
import logging
from hashlib import md5
from typing import Any, Dict, List, Set, Tuple

from benchmarks.adapters.base import EvalAdapter, IngestResult, QAItem

logger = logging.getLogger(__name__)

# Flag for HotPotQA-specific scoring in eval_one
is_hotpotqa = True


class HotPotQAAdapter(EvalAdapter):
    """HotPotQA distractor-setting evaluation adapter."""

    is_hotpotqa = True

    def __init__(self):
        super().__init__()
        self._title_to_sentences: Dict[str, List[str]] = {}
        self._title_to_uri: Dict[str, str] = {}
        self._questions: List[Dict] = []
        self._qid_to_context: Dict[int, List[Tuple[str, List[str]]]] = {}

    def _validate_dataset(self, raw: Any) -> None:
        if not isinstance(raw, list) or not raw:
            raise ValueError("HotPotQA dataset must be a non-empty JSON array")
        if "supporting_facts" not in raw[0]:
            raise ValueError("HotPotQA dataset must contain 'supporting_facts' field")

        title_to_sentences: Dict[str, List[str]] = {}
        collision_count = 0

        for i, q in enumerate(raw):
            self._qid_to_context[i] = []
            for title, sentences in q.get("context", []):
                self._qid_to_context[i].append((title, sentences))
                if title not in title_to_sentences:
                    title_to_sentences[title] = sentences
                elif title_to_sentences[title] != sentences:
                    collision_count += 1

        if collision_count > 0:
            logger.warning(
                f"[HotPotQA] {collision_count} title collisions (different content, "
                "first-occurrence kept)"
            )

        self._title_to_sentences = title_to_sentences
        self._questions = raw
        logger.info(
            f"[HotPotQA] Loaded {len(raw)} questions, "
            f"{len(title_to_sentences)} unique paragraphs"
        )
        title_to_sentences: Dict[str, List[str]] = {}
        collision_count = 0

        for i, q in enumerate(raw):
            self._qid_to_context[i] = []
            for title, sentences in q.get("context", []):
                self._qid_to_context[i].append((title, sentences))
                if title not in title_to_sentences:
                    title_to_sentences[title] = sentences
                elif title_to_sentences[title] != sentences:
                    collision_count += 1

        if collision_count > 0:
            logger.warning(
                f"[HotPotQA] {collision_count} title collisions (different content, "
                "first-occurrence kept)"
            )

        self._title_to_sentences = title_to_sentences
        self._questions = raw
        self._dataset = raw
        logger.info(
            f"[HotPotQA] Loaded {len(raw)} questions, "
            f"{len(title_to_sentences)} unique paragraphs"
        )

    async def ingest(self, oc: Any, **kwargs) -> IngestResult:
        """Ingest unique paragraphs as memory-mode records (no LLM overhead).

        If max_qa is passed, only ingests paragraphs from the first N questions
        to avoid ingesting 66k+ paragraphs for a quick test.
        """
        max_qa = kwargs.get("max_qa", 0)

        if max_qa > 0 and max_qa < len(self._questions):
            # Only ingest paragraphs referenced by the first max_qa questions
            needed_titles: Dict[str, List[str]] = {}
            for q in self._questions[:max_qa]:
                for title, sentences in q.get("context", []):
                    if title not in needed_titles:
                        needed_titles[title] = self._title_to_sentences.get(title, sentences)
            titles = list(needed_titles.items())
            logger.info(
                f"[HotPotQA] Quick mode: ingesting {len(titles)} paragraphs "
                f"for {max_qa} questions (vs {len(self._title_to_sentences)} total)"
            )
        else:
            titles = list(self._title_to_sentences.items())

        errors: List[str] = []
        title_to_uri: Dict[str, str] = {}
        sem = asyncio.Semaphore(20)

        async def _store_one(title: str, sentences: List[str]):
            async with sem:
                content = " ".join(sentences)
                try:
                    result = await oc.store(
                        abstract=title,
                        content=content,
                        context_type="resource",
                    )
                    uri = result.get("uri", "")
                    if uri:
                        title_to_uri[title] = uri
                except Exception as e:
                    errors.append(f"title={title[:50]}: {e}")

        # Concurrent ingestion
        batch_size = 100
        for start in range(0, len(titles), batch_size):
            batch = titles[start : start + batch_size]
            await asyncio.gather(
                *[_store_one(t, s) for t, s in batch],
                return_exceptions=True,
            )
            if (start + batch_size) % 1000 == 0 or start + batch_size >= len(titles):
                logger.info(
                    f"[HotPotQA] Ingested {min(start + batch_size, len(titles))}"
                    f"/{len(titles)} paragraphs"
                )

        self._title_to_uri = title_to_uri
        return IngestResult(
            total_items=len(titles),
            ingested_items=len(title_to_uri),
            errors=errors,
        )

    def build_qa_items(self, **kwargs) -> List[QAItem]:
        max_qa = kwargs.get("max_qa", 0)
        items: List[QAItem] = []

        for i, q in enumerate(self._questions):
            # Gold titles from supporting_facts
            gold_titles: Set[str] = {sf[0] for sf in q.get("supporting_facts", [])}
            expected_uris = [
                self._title_to_uri[t]
                for t in gold_titles
                if t in self._title_to_uri
            ]

            items.append(QAItem(
                question=q["question"],
                answer=q["answer"],
                category=q.get("type", ""),
                difficulty=q.get("level", ""),
                expected_ids=list(gold_titles),
                expected_uris=expected_uris,
                meta={
                    "question_id": i,
                    "gold_titles": list(gold_titles),
                    "hotpotqa_id": q.get("_id", ""),
                },
            ))

            if max_qa > 0 and len(items) >= max_qa:
                break

        return items

    def get_baseline_context(self, qa_item: QAItem) -> str:
        """Return all 10 context paragraphs for the distractor setting."""
        qid = qa_item.meta.get("question_id", -1)
        context_pairs = self._qid_to_context.get(qid, [])
        parts = []
        for title, sentences in context_pairs:
            text = " ".join(sentences)
            parts.append(f"## {title}\n\n{text}")
        return "\n\n".join(parts)

    def _get_retrieval_session_id(self, qa_item: QAItem) -> str:
        return "ev-hp-" + md5(qa_item.question.encode()).hexdigest()[:12]

    def _get_retrieval_context_type(self) -> str:
        return "resource"

    def _get_retrieval_detail_level(self) -> str:
        return "l0"
