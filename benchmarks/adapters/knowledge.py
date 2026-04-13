"""
Knowledge quality evaluation adapter for OpenCortex Alpha pipeline.

Evaluates the Archivist's ability to extract correct knowledge from
conversation traces. Compares extracted knowledge against a gold standard
dataset using LLM-as-Judge for semantic matching.

Two evaluation modes:
  1. Direct mode (default): Calls Archivist.extract_from_cluster() directly
     via Python imports — no server required.
  2. Server mode: Uses HTTP endpoints to trigger the full pipeline and
     retrieve results — requires running server with archivist_enabled=True.

Metrics:
  - Knowledge Recall: fraction of expected items that were extracted
  - Knowledge Precision: fraction of extracted items that match expected
  - Type Accuracy: correct knowledge_type classification rate
  - Hallucination Rate: fraction of extracted items with no expected match
"""

import json
import time
from typing import Any, Dict, List, Optional, Tuple

from benchmarks.adapters.base import EvalAdapter, IngestResult, QAItem


# LLM prompt for matching extracted knowledge to expected knowledge
MATCH_PROMPT = (
    "You are an evaluation judge. Determine if the EXTRACTED knowledge item "
    "semantically matches any of the EXPECTED knowledge items.\n\n"
    "EXTRACTED:\n{extracted}\n\n"
    "EXPECTED:\n{expected}\n\n"
    "Does the extracted item match any expected item (same core knowledge, "
    "even if phrased differently)?\n"
    'Output JSON: {{"match": true/false, "expected_index": N, "reason": "..."}}\n'
    "If no match, set expected_index to -1.\n"
    "Output only JSON."
)

# LLM prompt for type accuracy evaluation
TYPE_PROMPT = (
    "You are an evaluation judge. Given a knowledge statement, classify it "
    "into one of these types:\n"
    "  - belief: A user preference, habit, opinion, or personal fact\n"
    "  - sop: A standard operating procedure, workflow, or best practice\n"
    "  - negative_rule: Something to avoid, an anti-pattern, or warning\n"
    "  - root_cause: An error pattern with its cause and fix suggestion\n\n"
    "KNOWLEDGE: {knowledge}\n\n"
    'Output JSON: {{"type": "belief|sop|negative_rule|root_cause"}}\n'
    "Output only JSON."
)


def _parse_json_response(text: str) -> Optional[Dict]:
    """Parse JSON from LLM response, handling markdown fences."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [l for l in lines if not l.startswith("```")]
        text = "\n".join(lines)
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None


class KnowledgeAdapter(EvalAdapter):
    """Knowledge quality evaluation adapter.

    Evaluates knowledge extraction quality by comparing Archivist output
    against gold standard expected knowledge items.
    """

    def __init__(self):
        super().__init__()
        self._clusters: List[Dict] = []
        self._results_by_cluster: Dict[str, List[Dict]] = {}
        self._eval_method = "direct"  # or "server"

    def load_dataset(self, dataset_path: str, **kwargs) -> None:
        with open(dataset_path, encoding="utf-8") as f:
            data = json.load(f)
        self._clusters = data.get("clusters", [])
        self._results_by_cluster = {}
        self._dataset = data

    async def ingest(self, oc: Any, **kwargs) -> IngestResult:
        """Ingest: no-op for direct mode. For server mode, triggers pipeline."""
        self._eval_method = kwargs.get("eval_method", "direct")

        if self._eval_method == "server":
            return await self._ingest_server(oc)
        return IngestResult(
            total_items=len(self._clusters),
            ingested_items=len(self._clusters),
            errors=[],
            meta={"eval_method": "direct"},
        )

    async def _ingest_server(self, oc: Any) -> IngestResult:
        """Server mode: store conversation traces via context_commit/end,
        then trigger Archivist and wait for results."""
        total = 0
        ingested = 0
        errors: List[str] = []

        for cluster in self._clusters:
            session_id = f"knowledge-eval-{cluster['cluster_id']}"
            trace_idx = 0

            for trace in cluster.get("traces", []):
                total += 1
                trace_idx += 1
                try:
                    await oc.context_commit(
                        session_id=session_id,
                        turn_id=f"t{trace_idx}",
                        messages=[
                            {"role": "user", "content": trace["abstract"]},
                            {"role": "assistant", "content": trace.get("overview", trace["abstract"])},
                        ],
                    )
                    ingested += 1
                except Exception as e:
                    errors.append(f"cluster={cluster['cluster_id']} trace={trace['trace_id']}: {e}")

            try:
                await oc.context_end(session_id)
            except Exception as e:
                errors.append(f"end session={session_id}: {e}")

        # Trigger archivist
        try:
            await oc.archivist_trigger()
        except Exception as e:
            errors.append(f"archivist trigger: {e}")

        return IngestResult(
            total_items=total,
            ingested_items=ingested,
            errors=errors,
            meta={"eval_method": "server"},
        )

    def build_qa_items(self, **kwargs) -> List[QAItem]:
        """Each cluster becomes a QA item for evaluation."""
        max_qa = kwargs.get("max_qa", 0)
        items: List[QAItem] = []

        for cluster in self._clusters:
            expected = cluster.get("expected_knowledge", [])
            items.append(QAItem(
                question=f"Extract knowledge from cluster: {cluster['cluster_id']}",
                answer=json.dumps(expected),
                category=cluster["cluster_id"],
                difficulty="",
                expected_ids=[k.get("type", "unknown") for k in expected],
                expected_uris=[],
                meta={
                    "cluster_id": cluster["cluster_id"],
                    "description": cluster.get("description", ""),
                    "traces": cluster.get("traces", []),
                    "expected_knowledge": expected,
                    "notes": cluster.get("notes", ""),
                    "dataset": "knowledge",
                },
            ))

        if max_qa > 0:
            items = items[:max_qa]
        return items

    def get_baseline_context(self, qa_item: QAItem) -> str:
        """Return trace abstracts as baseline context."""
        traces = qa_item.meta.get("traces", [])
        parts = []
        for t in traces:
            parts.append(f"[{t.get('task_type', 'unknown')}/{t.get('outcome', 'unknown')}] {t.get('abstract', '')}")
            if t.get("overview"):
                parts.append(f"  Details: {t['overview']}")
        return "\n".join(parts)

    async def retrieve(self, oc: Any, qa_item: QAItem, top_k: int) -> Tuple[List[Dict], float]:
        """For knowledge eval, this triggers the Archivist extraction directly."""
        t0 = time.perf_counter()

        if self._eval_method == "server":
            return await self._retrieve_server(oc, qa_item, top_k)

        # Direct mode: import and call Archivist directly
        return await self._retrieve_direct(qa_item, top_k, oc)

    async def _retrieve_direct(
        self, qa_item: QAItem, top_k: int, oc: Any
    ) -> Tuple[List[Dict], float]:
        """Direct Python call to Archivist.extract_from_cluster()."""
        t0 = time.perf_counter()
        traces_data = qa_item.meta.get("traces", [])
        cluster_id = qa_item.meta.get("cluster_id", "")

        try:
            from opencortex.alpha.archivist import Archivist
            from opencortex.alpha.types import Knowledge, KnowledgeScope, Trace, Turn

            # Build Trace objects from gold standard data
            traces = []
            for td in traces_data:
                turns = [
                    Turn(
                        turn_id=f"{td['trace_id']}_0",
                        prompt_text=td.get("abstract", ""),
                        final_text=td.get("overview", ""),
                    )
                ]
                trace = Trace(
                    trace_id=td["trace_id"],
                    session_id=f"eval_{cluster_id}",
                    tenant_id="eval_tenant",
                    user_id="eval_user",
                    source="eval",
                    turns=turns,
                    abstract=td.get("abstract", ""),
                    overview=td.get("overview", ""),
                    task_type=td.get("task_type", ""),
                    outcome=td.get("outcome", "success"),
                )
                traces.append(trace)

            # Use LLM from oc for extraction (via the llm_client we have)
            # We need to get the llm_fn somehow — store it during ingest
            archivist = Archivist(
                llm_fn=self._get_llm_fn(),
                similarity_threshold=0.5,  # Lower threshold for eval traces
            )

            knowledge_items = await archivist.extract_from_cluster(
                cluster=traces,
                tenant_id="eval_tenant",
                user_id="eval_user",
                scope=KnowledgeScope.USER,
            )

            results = [k.to_dict() for k in knowledge_items]
            self._results_by_cluster[cluster_id] = results

        except ImportError as e:
            return [], (time.perf_counter() - t0) * 1000
        except Exception as e:
            return [], (time.perf_counter() - t0) * 1000

        latency_ms = (time.perf_counter() - t0) * 1000
        return results, latency_ms

    async def _retrieve_server(
        self, oc: Any, qa_item: QAItem, top_k: int
    ) -> Tuple[List[Dict], float]:
        """Server mode: retrieve knowledge candidates."""
        t0 = time.perf_counter()
        cluster_id = qa_item.meta.get("cluster_id", "")

        try:
            candidates = await oc.knowledge_candidates()
            # Filter to candidates from this cluster's session
            session_id = f"knowledge-eval-{cluster_id}"
            results = [
                c for c in candidates
                if c.get("source_trace_ids", []) and
                any(session_id in sid for sid in c.get("source_trace_ids", []))
            ]
            self._results_by_cluster[cluster_id] = results
        except Exception:
            results = []

        latency_ms = (time.perf_counter() - t0) * 1000
        return results, latency_ms

    def set_llm_fn(self, llm_fn):
        """Store the LLM function for direct-mode extraction."""
        self._llm_fn = llm_fn

    def _get_llm_fn(self):
        """Get the stored LLM function."""
        if not hasattr(self, "_llm_fn") or self._llm_fn is None:
            raise RuntimeError(
                "LLM function not set. Call adapter.set_llm_fn() before retrieve()."
            )
        return self._llm_fn

    def get_extracted_results(self, cluster_id: str) -> List[Dict]:
        """Get extraction results for a cluster."""
        return self._results_by_cluster.get(cluster_id, [])

    async def evaluate_extraction(
        self,
        extracted: List[Dict],
        expected: List[Dict],
        llm_fn,
    ) -> Dict[str, Any]:
        """Evaluate extracted knowledge against expected gold standard.

        Returns dict with:
          - recall: fraction of expected items matched
          - precision: fraction of extracted items matched
          - type_accuracy: correct type classification rate
          - hallucination_rate: fraction of extracted with no match
          - matches: list of match details
        """
        if not expected:
            return {
                "recall": 0.0,
                "precision": 0.0,
                "type_accuracy": 0.0,
                "hallucination_rate": 1.0 if extracted else 0.0,
                "matches": [],
                "n_expected": 0,
                "n_extracted": len(extracted),
            }

        matches: List[Dict] = []
        matched_expected = set()

        for ext in extracted:
            ext_summary = json.dumps({
                "type": ext.get("knowledge_type", ext.get("type", "")),
                "statement": ext.get("statement", ""),
                "objective": ext.get("objective", ""),
                "action_steps": ext.get("action_steps", []),
                "severity": ext.get("severity", ""),
                "cause": ext.get("cause", ""),
            }, ensure_ascii=False)

            expected_summaries = []
            for i, exp in enumerate(expected):
                expected_summaries.append(
                    f"[{i}] type={exp.get('type','')}: {exp.get('statement','')}"
                )

            prompt = MATCH_PROMPT.format(
                extracted=ext_summary,
                expected="\n".join(expected_summaries),
            )

            try:
                response = await llm_fn(prompt, 256, temperature=0)
                result = _parse_json_response(response)
                if result and result.get("match"):
                    idx = result.get("expected_index", -1)
                    if 0 <= idx < len(expected):
                        matched_expected.add(idx)
                        ext_type = ext.get("knowledge_type", ext.get("type", ""))
                        exp_type = expected[idx].get("type", "")
                        type_match = ext_type == exp_type
                        matches.append({
                            "extracted": ext.get("statement", "")[:100],
                            "expected_idx": idx,
                            "expected": expected[idx].get("statement", "")[:100],
                            "type_match": type_match,
                            "reason": result.get("reason", ""),
                        })
                    else:
                        matches.append({
                            "extracted": ext.get("statement", "")[:100],
                            "expected_idx": -1,
                            "expected": "",
                            "type_match": False,
                            "reason": "Invalid index",
                        })
                else:
                    matches.append({
                        "extracted": ext.get("statement", "")[:100],
                        "expected_idx": -1,
                        "expected": "",
                        "type_match": False,
                        "reason": result.get("reason", "No match") if result else "Parse error",
                    })
            except Exception as e:
                matches.append({
                    "extracted": ext.get("statement", "")[:100],
                    "expected_idx": -1,
                    "expected": "",
                    "type_match": False,
                    "reason": f"Error: {e}",
                })

        # Compute metrics
        n_expected = len(expected)
        n_extracted = len(extracted)
        n_matched = len(matched_expected)

        recall = n_matched / n_expected if n_expected > 0 else 0.0
        precision = n_matched / n_extracted if n_extracted > 0 else 0.0

        # Type accuracy: among matched items, how many have correct type
        type_correct = sum(1 for m in matches if m.get("type_match"))
        type_accuracy = type_correct / len(matches) if matches else 0.0

        # Hallucination rate: extracted items with no expected match
        n_hallucinated = sum(1 for m in matches if m.get("expected_idx", -1) == -1)
        hallucination_rate = n_hallucinated / n_extracted if n_extracted > 0 else 0.0

        return {
            "recall": round(recall, 4),
            "precision": round(precision, 4),
            "type_accuracy": round(type_accuracy, 4),
            "hallucination_rate": round(hallucination_rate, 4),
            "matches": matches,
            "n_expected": n_expected,
            "n_extracted": n_extracted,
        }
