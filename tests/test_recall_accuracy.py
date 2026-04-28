"""
Memory Recall Accuracy Test
============================
Extracts questions from project docs, queries the memory system,
and evaluates recall accuracy at 3 difficulty levels.

Usage:
    python tests/test_recall_accuracy.py [--base-url http://127.0.0.1:8921]
"""

import asyncio
import json
import sys
import time
from dataclasses import dataclass, field
from typing import Optional

# ---------------------------------------------------------------------------
# Test Questions (extracted from docs)
# ---------------------------------------------------------------------------


@dataclass
class RecallQuestion:
    query: str
    expected_keywords: list[str]  # at least one must appear in top results
    difficulty: str  # easy / medium / hard
    source_doc: str
    description: str


TEST_QUESTIONS: list[RecallQuestion] = [
    # ========== EASY (60%) - Direct fact recall ==========
    RecallQuestion(
        query="OpenCortex HTTP Server 用的什么框架和端口？",
        expected_keywords=["FastAPI", "8921"],
        difficulty="easy",
        source_doc="architecture.md",
        description="HTTP server framework and port",
    ),
    RecallQuestion(
        query="OpenCortex 默认 HTTP API 端口是什么？",
        expected_keywords=["8921"],
        difficulty="easy",
        source_doc="architecture.md",
        description="HTTP API default port",
    ),
    RecallQuestion(
        query="三层文件系统 L0 L1 L2 分别存什么？",
        expected_keywords=["abstract", "overview", "content"],
        difficulty="easy",
        source_doc="architecture.md",
        description="Three-layer filesystem contents",
    ),
    RecallQuestion(
        query="反馈排序的 reward_weight 参数是多少？",
        expected_keywords=["0.05"],
        difficulty="easy",
        source_doc="architecture.md",
        description="RL weight parameter value",
    ),
    RecallQuestion(
        query="QdrantStorageAdapter 的 decay_rate 默认值？",
        expected_keywords=["0.95"],
        difficulty="easy",
        source_doc="architecture.md",
        description="Decay rate default value",
    ),
    RecallQuestion(
        query="ACE v2 有哪三个角色？",
        expected_keywords=["Reflector", "SkillManager", "Skillbook"],
        difficulty="easy",
        source_doc="ace-design.md",
        description="ACE v2 three roles",
    ),
    RecallQuestion(
        query="OpenCortex 上下文生命周期端点支持哪三个阶段？",
        expected_keywords=["prepare", "commit", "end"],
        difficulty="easy",
        source_doc="architecture.md",
        description="Context lifecycle phases",
    ),
    RecallQuestion(
        query="记忆检索质量的 Recall@5 通过标准是多少？",
        expected_keywords=["0.80", "80"],
        difficulty="easy",
        source_doc="memory-test-plan.md",
        description="Recall@5 pass criterion",
    ),
    RecallQuestion(
        query="Plugin Hook 的四个生命周期阶段是什么？",
        expected_keywords=["SessionStart", "UserPromptSubmit", "Stop", "SessionEnd"],
        difficulty="easy",
        source_doc="architecture.md",
        description="Hook lifecycle stages",
    ),
    RecallQuestion(
        query="OpenCortex 的 URI 格式是怎样的？",
        expected_keywords=["opencortex://", "team", "user", "node_id"],
        difficulty="easy",
        source_doc="architecture.md",
        description="URI format",
    ),
    RecallQuestion(
        query="分数融合公式中 beta 参数代表什么？值是多少？",
        expected_keywords=["rerank", "0.7"],
        difficulty="easy",
        source_doc="architecture.md",
        description="Beta parameter meaning and value",
    ),
    RecallQuestion(
        query="OpenFang 用什么数据库做持久化？",
        expected_keywords=["SQLite", "WAL"],
        difficulty="easy",
        source_doc="openfang-memory-system.md",
        description="OpenFang storage backend",
    ),
    # ========== MEDIUM (30%) - Paraphrased / conceptual ==========
    RecallQuestion(
        query="系统如何防止无向量时检索质量下降？",
        expected_keywords=["词法", "lexical", "后备", "fallback", "关键词"],
        difficulty="medium",
        source_doc="memory-enhancement-rationale.md",
        description="Lexical fallback for no-vector scenario",
    ),
    RecallQuestion(
        query="怎么让常用的记忆不被衰减清理掉？",
        expected_keywords=["access", "访问", "遗忘", "decay", "保鲜", "protected"],
        difficulty="medium",
        source_doc="memory-enhancement-rationale.md",
        description="Access-driven forgetting mechanism",
    ),
    RecallQuestion(
        query="文档批量导入是在服务端还是客户端执行扫描？",
        expected_keywords=["agent", "客户端", "client", "Claude"],
        difficulty="medium",
        source_doc="document-scan-design.md",
        description="Document scan execution location",
    ),
    RecallQuestion(
        query="如何避免技能库无限膨胀？",
        expected_keywords=["UPDATE", "优先", "ADD", "合并"],
        difficulty="medium",
        source_doc="ace-design.md",
        description="Skillbook growth prevention",
    ),
    RecallQuestion(
        query="用户身份信息是怎么传递到服务端的？",
        expected_keywords=["header", "X-Tenant", "X-User", "middleware", "contextvars"],
        difficulty="medium",
        source_doc="architecture.md",
        description="Identity propagation mechanism",
    ),
    RecallQuestion(
        query="重复扫描同一目录会产生重复记忆吗？",
        expected_keywords=["URI", "upsert", "去重", "幂等", "确定性"],
        difficulty="medium",
        source_doc="document-scan-design.md",
        description="Idempotent scan deduplication",
    ),
    # ========== HARD (10%) - Cross-doc reasoning ==========
    RecallQuestion(
        query="OpenCortex 和 OpenFang 在遗忘机制上有什么区别？",
        expected_keywords=["reward", "access", "decay", "confidence", "7天"],
        difficulty="hard",
        source_doc="architecture.md + openfang-memory-system.md",
        description="Compare decay mechanisms across systems",
    ),
    RecallQuestion(
        query="如果检索指标下降了，应该按什么顺序排查问题？",
        expected_keywords=["Router", "Retriever", "rerank", "排序", "召回", "归因"],
        difficulty="hard",
        source_doc="memory-enhancement-rationale.md + memory-test-plan.md",
        description="Retrieval debugging methodology",
    ),
]


# ---------------------------------------------------------------------------
# HTTP Client
# ---------------------------------------------------------------------------


async def memory_search(
    query: str, limit: int = 5, base_url: str = "http://127.0.0.1:8921"
) -> dict:
    """Call memory_search via HTTP API."""
    import httpx

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{base_url}/api/v1/memory/search",
            json={"query": query, "limit": limit},
            headers={
                "X-Tenant-ID": "netops",
                "X-User-ID": "liaowh4",
            },
        )
        resp.raise_for_status()
        return resp.json()


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


@dataclass
class RecallResult:
    query: str
    difficulty: str
    description: str
    source_doc: str
    expected_keywords: list[str]
    hit: bool
    matched_keywords: list[str]
    top_results: list[str]  # abstracts
    latency_ms: float


async def evaluate_question(q: RecallQuestion, base_url: str) -> RecallResult:
    """Evaluate a single question's recall accuracy."""
    t0 = time.monotonic()
    try:
        resp = await memory_search(q.query, limit=5, base_url=base_url)
    except Exception as e:
        return RecallResult(
            query=q.query,
            difficulty=q.difficulty,
            description=q.description,
            source_doc=q.source_doc,
            expected_keywords=q.expected_keywords,
            hit=False,
            matched_keywords=[],
            top_results=[f"ERROR: {e}"],
            latency_ms=(time.monotonic() - t0) * 1000,
        )
    latency = (time.monotonic() - t0) * 1000

    results = resp.get("results", [])
    # Combine all result text for keyword matching
    combined_text = " ".join(
        r.get("abstract", "") + " " + r.get("overview", "") + " " + r.get("content", "")
        for r in results
    )
    combined_lower = combined_text.lower()

    matched = [kw for kw in q.expected_keywords if kw.lower() in combined_lower]
    hit = len(matched) > 0

    top_abstracts = [r.get("abstract", "")[:100] for r in results[:5]]

    return RecallResult(
        query=q.query,
        difficulty=q.difficulty,
        description=q.description,
        source_doc=q.source_doc,
        expected_keywords=q.expected_keywords,
        hit=hit,
        matched_keywords=matched,
        top_results=top_abstracts,
        latency_ms=latency,
    )


async def run_evaluation(base_url: str = "http://127.0.0.1:8921"):
    """Run full evaluation and print report."""
    print("=" * 70)
    print("  OpenCortex Memory Recall Accuracy Test")
    print("=" * 70)
    print(f"  Base URL: {base_url}")
    print(f"  Total questions: {len(TEST_QUESTIONS)}")
    print(f"  Easy: {sum(1 for q in TEST_QUESTIONS if q.difficulty == 'easy')}")
    print(f"  Medium: {sum(1 for q in TEST_QUESTIONS if q.difficulty == 'medium')}")
    print(f"  Hard: {sum(1 for q in TEST_QUESTIONS if q.difficulty == 'hard')}")
    print("=" * 70)
    print()

    results: list[RecallResult] = []
    for i, q in enumerate(TEST_QUESTIONS, 1):
        r = await evaluate_question(q, base_url)
        results.append(r)
        status = "HIT" if r.hit else "MISS"
        icon = "\u2705" if r.hit else "\u274c"
        print(
            f"  [{i:2d}/{len(TEST_QUESTIONS)}] {icon} {status} [{r.difficulty:6s}] {r.description}"
        )
        if r.hit:
            print(f"         Matched: {r.matched_keywords}")
        else:
            print(f"         Expected: {r.expected_keywords}")
            if r.top_results and not r.top_results[0].startswith("ERROR"):
                print(f"         Got: {r.top_results[0][:80]}...")
        print(f"         Latency: {r.latency_ms:.0f}ms")
        print()

    # --- Aggregate metrics ---
    print("=" * 70)
    print("  RESULTS SUMMARY")
    print("=" * 70)

    total_hit = sum(1 for r in results if r.hit)
    total = len(results)
    print(f"\n  Overall Hit Rate: {total_hit}/{total} = {total_hit / total * 100:.1f}%")

    for diff in ["easy", "medium", "hard"]:
        subset = [r for r in results if r.difficulty == diff]
        if not subset:
            continue
        hits = sum(1 for r in subset if r.hit)
        avg_latency = sum(r.latency_ms for r in subset) / len(subset)
        print(
            f"  {diff:8s}: {hits}/{len(subset)} = {hits / len(subset) * 100:.1f}%  (avg latency: {avg_latency:.0f}ms)"
        )

    avg_latency_all = sum(r.latency_ms for r in results) / len(results)
    print(f"\n  Average Latency: {avg_latency_all:.0f}ms")

    # --- Miss analysis ---
    misses = [r for r in results if not r.hit]
    if misses:
        print(f"\n  MISSED QUESTIONS ({len(misses)}):")
        for r in misses:
            print(f"    - [{r.difficulty}] {r.query}")
            print(f"      Expected: {r.expected_keywords}")
            if r.top_results:
                print(f"      Top result: {r.top_results[0][:80]}")

    # --- Quality assessment ---
    print("\n" + "=" * 70)
    recall_rate = total_hit / total
    if recall_rate >= 0.80:
        print("  VERDICT: PASS - Recall rate meets the 80% threshold")
    elif recall_rate >= 0.60:
        print("  VERDICT: MARGINAL - Recall rate between 60-80%, needs improvement")
    else:
        print("  VERDICT: FAIL - Recall rate below 60%, significant issues")
    print("=" * 70)

    return results


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    base_url = "http://127.0.0.1:8921"
    for i, arg in enumerate(sys.argv):
        if arg == "--base-url" and i + 1 < len(sys.argv):
            base_url = sys.argv[i + 1]

    asyncio.run(run_evaluation(base_url))
