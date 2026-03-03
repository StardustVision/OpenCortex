"""
OpenCortex Project Recall Test — 10 queries across 3 difficulty levels.

Tests recall accuracy against OpenCortex's own architecture documents
stored in the remote memory system.

Usage:
    uv run python3 tests/test_recall_opencortex.py [--base-url http://10.46.35.24:18921]
"""

import asyncio
import json
import sys
import time
from dataclasses import dataclass

import httpx


@dataclass
class TestCase:
    id: int
    query: str
    difficulty: str          # easy / medium / hard
    description: str
    expected_keywords: list[str]  # at least 1 must appear in top-5 results


TEST_CASES: list[TestCase] = [
    # ========== EASY (5) — direct fact recall ==========
    TestCase(
        id=1,
        query="Hook 四个生命周期阶段分别在什么时候触发？",
        difficulty="easy",
        description="Hook lifecycle trigger timing",
        expected_keywords=["SessionStart", "UserPromptSubmit", "Stop", "SessionEnd"],
    ),
    TestCase(
        id=2,
        query="Score Fusion 公式中 beta 和 rl_weight 分别是多少？",
        difficulty="easy",
        description="Score fusion parameter values",
        expected_keywords=["0.7", "0.05"],
    ),
    TestCase(
        id=3,
        query="MCP Server 默认端口和支持的传输协议？",
        difficulty="easy",
        description="MCP server port and transports",
        expected_keywords=["8920", "stdio", "SSE"],
    ),
    TestCase(
        id=4,
        query="三层存储 L0 L1 L2 各自的文件名是什么？",
        difficulty="easy",
        description="Three-layer file naming",
        expected_keywords=["abstract", "overview", "content"],
    ),
    TestCase(
        id=5,
        query="ACE v2 的三个角色分别负责什么？",
        difficulty="easy",
        description="ACE v2 three roles and responsibilities",
        expected_keywords=["Reflector", "SkillManager", "Skillbook"],
    ),

    # ========== MEDIUM (3) — paraphrased / conceptual ==========
    TestCase(
        id=6,
        query="文档批量扫描为什么选择在客户端而不是服务端执行？",
        difficulty="medium",
        description="Document scan client-side rationale",
        expected_keywords=["agent", "本地文件", "Claude"],
    ),
    TestCase(
        id=7,
        query="HierarchicalRetriever 用的是什么检索策略？",
        difficulty="medium",
        description="Retriever search strategy",
        expected_keywords=["wave", "frontier", "batching"],
    ),
    TestCase(
        id=8,
        query="HTTP Server 在系统架构中扮演什么角色？",
        difficulty="medium",
        description="HTTP Server architectural role",
        expected_keywords=["入口", "Orchestrator", "Qdrant"],
    ),

    # ========== HARD (2) — cross-doc reasoning ==========
    TestCase(
        id=9,
        query="OpenCortex 和 OpenFang 在记忆存储引擎上有什么区别？",
        difficulty="hard",
        description="Compare storage engines across systems",
        expected_keywords=["Qdrant", "SQLite", "SemanticStor"],
    ),
    TestCase(
        id=10,
        query="SkillManager 的增量操作有哪些类型？如何防止技能膨胀？",
        difficulty="hard",
        description="Skill operations and anti-bloat",
        expected_keywords=["ADD", "UPDATE", "TAG", "REMOVE"],
    ),
]


BASE_URL = "http://10.46.35.24:18921"


async def search(query: str, limit: int = 5) -> dict:
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{BASE_URL}/api/v1/memory/search",
            json={"query": query, "limit": limit},
            headers={"X-Tenant-ID": "netops", "X-User-ID": "liaowh4"},
        )
        resp.raise_for_status()
        return resp.json()


async def main():
    global BASE_URL
    for i, arg in enumerate(sys.argv):
        if arg == "--base-url" and i + 1 < len(sys.argv):
            BASE_URL = sys.argv[i + 1]

    print("=" * 70)
    print("  OpenCortex Project Recall Test (10 queries)")
    print("=" * 70)
    print(f"  Server: {BASE_URL}")
    easy = sum(1 for t in TEST_CASES if t.difficulty == "easy")
    med = sum(1 for t in TEST_CASES if t.difficulty == "medium")
    hard = sum(1 for t in TEST_CASES if t.difficulty == "hard")
    print(f"  Easy: {easy}  Medium: {med}  Hard: {hard}")
    print("=" * 70)
    print()

    hits = 0
    misses = []
    latencies = []

    for tc in TEST_CASES:
        t0 = time.monotonic()
        try:
            resp = await search(tc.query, limit=5)
        except Exception as e:
            elapsed = (time.monotonic() - t0) * 1000
            print(f"  [{tc.id:2d}/10] ERROR [{tc.difficulty:6s}] {tc.description}")
            print(f"         {e}")
            print(f"         Latency: {elapsed:.0f}ms\n")
            misses.append(tc)
            latencies.append(elapsed)
            continue

        elapsed = (time.monotonic() - t0) * 1000
        latencies.append(elapsed)

        results = resp.get("results", [])
        combined = " ".join(
            json.dumps(r, ensure_ascii=False, default=str) for r in results
        )

        matched = [kw for kw in tc.expected_keywords if kw in combined]
        hit = len(matched) > 0

        if hit:
            hits += 1
            print(f"  [{tc.id:2d}/10] HIT  [{tc.difficulty:6s}] {tc.description}")
            print(f"         Matched: {matched}")
        else:
            misses.append(tc)
            print(f"  [{tc.id:2d}/10] MISS [{tc.difficulty:6s}] {tc.description}")
            print(f"         Expected: {tc.expected_keywords}")
            if results:
                abstract = results[0].get("abstract", "")[:100]
                print(f"         Top result: {abstract}...")

        print(f"         Latency: {elapsed:.0f}ms\n")

    # --- Summary ---
    total = len(TEST_CASES)
    avg_lat = sum(latencies) / len(latencies) if latencies else 0

    print("=" * 70)
    print("  RESULTS SUMMARY")
    print("=" * 70)
    print(f"  Overall: {hits}/{total} = {hits/total*100:.1f}%")

    for diff in ["easy", "medium", "hard"]:
        subset = [tc for tc in TEST_CASES if tc.difficulty == diff]
        diff_hits = sum(
            1 for tc in subset
            if tc not in misses
        )
        print(f"  {diff:8s}: {diff_hits}/{len(subset)}")

    print(f"  Avg Latency: {avg_lat:.0f}ms")

    if misses:
        print(f"\n  MISSED ({len(misses)}):")
        for tc in misses:
            print(f"    - [{tc.difficulty}] {tc.query}")

    print("\n" + "=" * 70)
    rate = hits / total
    if rate >= 0.80:
        print("  VERDICT: PASS")
    elif rate >= 0.60:
        print("  VERDICT: MARGINAL")
    else:
        print("  VERDICT: FAIL")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
