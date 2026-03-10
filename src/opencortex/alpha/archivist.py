"""
Archivist — clusters traces and extracts knowledge via LLM.

Pipeline:
  1. Group traces by source + task_type
  2. Within each group, cluster by embedding similarity
  3. For each cluster, call LLM to extract knowledge
  4. Save knowledge candidates to KnowledgeStore

Design doc §5.3, §10.1.
"""

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine, Dict, List, Optional, Tuple

from opencortex.alpha.types import Knowledge, KnowledgeType, KnowledgeScope, KnowledgeStatus

logger = logging.getLogger(__name__)


_EXTRACT_PROMPT = """Given these related task traces, extract reusable knowledge.

Traces ({count} total):
{traces_text}

For each piece of knowledge you identify, classify it as one of:
- belief: A judgment rule or best practice
- sop: A standard operating procedure with ordered steps
- negative_rule: Something that should never be done
- root_cause: A recurring error pattern with its cause and fix

Return a JSON array of knowledge items:
[{{"type": "belief|sop|negative_rule|root_cause", "statement": "...", "objective": "...", "action_steps": ["step1", "step2"] (for sop only), "error_pattern": "..." (for root_cause), "cause": "..." (for root_cause), "fix_suggestion": "..." (for root_cause), "severity": "low|medium|high" (for negative_rule), "trigger_keywords": ["kw1", "kw2"]}}]

Return ONLY the JSON array."""


def cluster_traces(
    traces: List[Dict[str, Any]],
    embedder=None,
    similarity_threshold: float = 0.8,
) -> List[List[Dict[str, Any]]]:
    """
    Group and cluster traces.

    1. Group by source + task_type
    2. Within groups, use greedy clustering if embedder available,
       otherwise treat each group as one cluster.
    """
    # Step 1: Group by source + task_type
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for trace in traces:
        key = f"{trace.get('source', 'unknown')}:{trace.get('task_type', 'unknown')}"
        groups.setdefault(key, []).append(trace)

    clusters = []

    for group_key, group_traces in groups.items():
        if not embedder or len(group_traces) <= 2:
            # Without embedder or small groups, treat as single cluster
            clusters.append(group_traces)
            continue

        # Step 2: Greedy clustering by abstract similarity
        assigned = [False] * len(group_traces)
        for i in range(len(group_traces)):
            if assigned[i]:
                continue
            cluster = [group_traces[i]]
            assigned[i] = True

            text_i = group_traces[i].get("abstract", "")
            if not text_i:
                continue

            try:
                vec_i = embedder.embed(text_i).dense
            except Exception:
                continue

            for j in range(i + 1, len(group_traces)):
                if assigned[j]:
                    continue
                text_j = group_traces[j].get("abstract", "")
                if not text_j:
                    continue
                try:
                    vec_j = embedder.embed(text_j).dense
                    sim = _cosine_similarity(vec_i, vec_j)
                    if sim >= similarity_threshold:
                        cluster.append(group_traces[j])
                        assigned[j] = True
                except Exception:
                    continue

            clusters.append(cluster)

        # Add unassigned as singletons
        for i in range(len(group_traces)):
            if not assigned[i]:
                clusters.append([group_traces[i]])

    return clusters


def _cosine_similarity(a: List[float], b: List[float]) -> float:
    """Compute cosine similarity between two vectors."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


class Archivist:
    """Clusters traces and extracts knowledge candidates via LLM."""

    def __init__(
        self,
        llm_fn: Callable[..., Coroutine],
        embedder=None,
        similarity_threshold: float = 0.8,
        trigger_threshold: int = 20,
        trigger_mode: str = "auto",
    ):
        self._llm_fn = llm_fn
        self._embedder = embedder
        self._similarity_threshold = similarity_threshold
        self._trigger_threshold = trigger_threshold
        self._trigger_mode = trigger_mode
        self._last_run_at: Optional[str] = None
        self._running = False

    async def extract_from_cluster(
        self,
        cluster: List[Dict[str, Any]],
        tenant_id: str,
        user_id: str,
        scope: KnowledgeScope = KnowledgeScope.USER,
    ) -> List[Knowledge]:
        """Extract knowledge from a cluster of traces via LLM."""
        traces_text = "\n\n".join(
            f"Trace: {t.get('abstract', 'no summary')}\n"
            f"Outcome: {t.get('outcome', 'unknown')}\n"
            f"Type: {t.get('task_type', 'unknown')}"
            for t in cluster
        )

        prompt = _EXTRACT_PROMPT.format(
            count=len(cluster),
            traces_text=traces_text,
        )

        try:
            response = await self._llm_fn(prompt)
            items = json.loads(response)
        except (json.JSONDecodeError, Exception) as e:
            logger.warning(f"Archivist LLM parse error: {e}")
            return []

        knowledge_items = []
        source_trace_ids = [t.get("trace_id", "") for t in cluster if t.get("trace_id")]

        for item in items:
            ktype = _parse_knowledge_type(item.get("type", ""))
            if ktype is None:
                continue

            k = Knowledge(
                knowledge_id=f"k-{uuid.uuid4().hex[:12]}",
                knowledge_type=ktype,
                tenant_id=tenant_id,
                user_id=user_id,
                scope=scope,
                status=KnowledgeStatus.CANDIDATE,
                statement=item.get("statement"),
                objective=item.get("objective"),
                action_steps=item.get("action_steps"),
                trigger_keywords=item.get("trigger_keywords"),
                error_pattern=item.get("error_pattern"),
                cause=item.get("cause"),
                fix_suggestion=item.get("fix_suggestion"),
                severity=item.get("severity"),
                source_trace_ids=source_trace_ids,
                abstract=item.get("statement") or item.get("objective") or "",
            )
            knowledge_items.append(k)

        return knowledge_items

    async def run(
        self,
        traces: List[Dict[str, Any]],
        tenant_id: str,
        user_id: str,
        scope: KnowledgeScope = KnowledgeScope.USER,
    ) -> List[Knowledge]:
        """Full Archivist pipeline: cluster + extract."""
        if self._running:
            logger.warning("Archivist already running, skipping")
            return []

        self._running = True
        try:
            clusters = cluster_traces(
                traces, self._embedder, self._similarity_threshold,
            )

            all_knowledge = []
            for cluster in clusters:
                if len(cluster) < 2:
                    continue  # Skip singletons
                items = await self.extract_from_cluster(
                    cluster, tenant_id, user_id, scope,
                )
                all_knowledge.extend(items)

            self._last_run_at = datetime.now(timezone.utc).isoformat()
            return all_knowledge
        finally:
            self._running = False

    def should_trigger(self, new_trace_count: int) -> bool:
        """Check if Archivist should run based on trigger mode."""
        if self._trigger_mode == "manual":
            return False
        return new_trace_count >= self._trigger_threshold

    @property
    def status(self) -> Dict[str, Any]:
        return {
            "running": self._running,
            "last_run_at": self._last_run_at,
            "trigger_mode": self._trigger_mode,
            "trigger_threshold": self._trigger_threshold,
        }


def _parse_knowledge_type(type_str: str) -> Optional[KnowledgeType]:
    mapping = {
        "belief": KnowledgeType.BELIEF,
        "sop": KnowledgeType.SOP,
        "negative_rule": KnowledgeType.NEGATIVE_RULE,
        "root_cause": KnowledgeType.ROOT_CAUSE,
    }
    return mapping.get(type_str)
