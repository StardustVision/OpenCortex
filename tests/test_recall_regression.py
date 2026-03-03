"""
Recall regression test suite for OpenCortex.

Fixed evaluation set of 10 queries against SASE project documents.
Baseline established 2026-03-02 with bge-m3 + bge-reranker-v2-m3.

Requires a running OpenCortex HTTP server with SASE documents loaded.
Run:  uv run python3 -m pytest tests/test_recall_regression.py -v
Or:   uv run python3 -m unittest tests.test_recall_regression -v
"""

import asyncio
import json
import os
import sys
import unittest
from dataclasses import dataclass, field
from typing import List, Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


# =============================================================================
# Test Case Definitions
# =============================================================================

@dataclass
class RecallTestCase:
    """A single recall regression test case."""
    id: int
    query: str
    category: str               # "semantic" | "hard_keyword" | "hierarchical"
    expected_hit: str            # "exact" | "partial" | "miss"
    expected_keywords: List[str]
    baseline_score: int          # 1-10, from initial evaluation


# The 10 gold-standard regression queries (baseline 2026-03-02)
REGRESSION_CASES: List[RecallTestCase] = [
    RecallTestCase(
        id=1,
        query="服务全景与端口",
        category="semantic",
        expected_hit="exact",
        expected_keywords=["服务列表", "端口"],
        baseline_score=10,
    ),
    RecallTestCase(
        id=2,
        query="Stream Gateway 内部组件",
        category="semantic",
        expected_hit="exact",
        expected_keywords=["Dispatcher", "MessageRouter"],
        baseline_score=9,
    ),
    RecallTestCase(
        id=3,
        query="DDD 领域边界",
        category="semantic",
        expected_hit="exact",
        expected_keywords=["核心域", "支撑域", "通用域"],
        baseline_score=9,
    ),
    RecallTestCase(
        id=4,
        query="Authentik SCIM 集成",
        category="semantic",
        expected_hit="exact",
        expected_keywords=["SCIM 同步", "Webhook"],
        baseline_score=8,
    ),
    RecallTestCase(
        id=5,
        query="TrafficRule 出站类型",
        category="hard_keyword",
        expected_hit="partial",       # baseline: partial, target: exact
        expected_keywords=["traffic_rule.proto", "OutboundType"],
        baseline_score=5,
    ),
    RecallTestCase(
        id=6,
        query="Agent TUN/DNS 设计",
        category="hard_keyword",
        expected_hit="miss",          # baseline: miss, target: exact
        expected_keywords=["Agent Service", "TUN", "DNS"],
        baseline_score=3,
    ),
    RecallTestCase(
        id=7,
        query="策略变更推送链路",
        category="semantic",
        expected_hit="exact",
        expected_keywords=["端到端推送"],
        baseline_score=10,
    ),
    RecallTestCase(
        id=8,
        query="BFF API 路由",
        category="semantic",
        expected_hit="exact",
        expected_keywords=["Config", "BFF", "路由"],
        baseline_score=8,
    ),
    RecallTestCase(
        id=9,
        query="Device Manager DUG 推送",
        category="semantic",
        expected_hit="exact",
        expected_keywords=["DUG", "Device Manager"],
        baseline_score=10,
    ),
    RecallTestCase(
        id=10,
        query="Sovereign SASE 主权架构",
        category="hard_keyword",
        expected_hit="partial",       # baseline: partial, target: exact
        expected_keywords=["pPOP", "vPOP", "PRD"],
        baseline_score=6,
    ),
]


# =============================================================================
# Helpers
# =============================================================================

def _check_keywords_in_results(results: list, keywords: List[str]) -> dict:
    """Check which expected keywords appear in search results.

    Returns dict with:
      - found: list of matched keywords
      - missing: list of unmatched keywords
      - hit_ratio: float 0..1
    """
    combined_text = ""
    for r in results:
        combined_text += " " + json.dumps(r, ensure_ascii=False, default=str)

    found = [kw for kw in keywords if kw in combined_text]
    missing = [kw for kw in keywords if kw not in combined_text]

    return {
        "found": found,
        "missing": missing,
        "hit_ratio": len(found) / len(keywords) if keywords else 1.0,
    }


def _classify_hit(hit_ratio: float) -> str:
    """Classify result as exact/partial/miss based on keyword hit ratio."""
    if hit_ratio >= 0.8:
        return "exact"
    elif hit_ratio >= 0.3:
        return "partial"
    else:
        return "miss"


# =============================================================================
# Test Suite
# =============================================================================

class TestRecallRegression(unittest.TestCase):
    """Recall regression tests against a live OpenCortex server.

    Requires:
      - OpenCortex HTTP server running at OPENCORTEX_URL (default: http://127.0.0.1:8921)
      - SASE project documents loaded
      - Set RECALL_REGRESSION=1 to enable (skipped by default)
    """

    @classmethod
    def setUpClass(cls):
        if not os.environ.get("RECALL_REGRESSION"):
            raise unittest.SkipTest(
                "Set RECALL_REGRESSION=1 to run recall regression tests"
            )

        cls.base_url = os.environ.get("OPENCORTEX_URL", "http://127.0.0.1:8921")
        cls.results_summary: List[dict] = []

    @classmethod
    def tearDownClass(cls):
        if not hasattr(cls, "results_summary") or not cls.results_summary:
            return

        # Print summary report
        print("\n" + "=" * 72)
        print("RECALL REGRESSION REPORT")
        print("=" * 72)

        total_score = 0
        exact_count = 0
        for r in cls.results_summary:
            status_icon = {"exact": "✓", "partial": "△", "miss": "✗"}.get(
                r["actual_hit"], "?"
            )
            improved = ""
            if r["baseline_hit"] in ("miss", "partial") and r["actual_hit"] == "exact":
                improved = " [IMPROVED]"
            elif r["baseline_hit"] == "exact" and r["actual_hit"] != "exact":
                improved = " [REGRESSED]"

            print(
                f"  [{status_icon}] #{r['id']:2d} {r['query']:<28s}  "
                f"{r['actual_hit']:<8s} (baseline: {r['baseline_hit']}){improved}"
            )
            if r["actual_hit"] == "exact":
                exact_count += 1
                total_score += 10
            elif r["actual_hit"] == "partial":
                total_score += 5
            # miss = 0

        avg = total_score / len(cls.results_summary) if cls.results_summary else 0
        print("-" * 72)
        print(
            f"  Exact: {exact_count}/{len(cls.results_summary)}  "
            f"Avg Score: {avg:.1f}/10  "
            f"Baseline: 7.8/10"
        )
        print("=" * 72)


    def _run_search(self, query: str, limit: int = 5) -> list:
        """Execute a search query against the server."""
        import httpx

        async def _do():
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    f"{self.base_url}/api/v1/memory/search",
                    json={"query": query, "limit": limit},
                    headers={
                        "X-Tenant-ID": "netops",
                        "X-User-ID": "liaowh4",
                    },
                )
                resp.raise_for_status()
                return resp.json().get("results", [])

        return asyncio.run(_do())

    def _run_case(self, case: RecallTestCase):
        """Run a single recall test case and record results."""
        results = self._run_search(case.query, limit=5)

        kw_check = _check_keywords_in_results(results, case.expected_keywords)
        actual_hit = _classify_hit(kw_check["hit_ratio"])

        self.__class__.results_summary.append({
            "id": case.id,
            "query": case.query,
            "category": case.category,
            "baseline_hit": case.expected_hit,
            "actual_hit": actual_hit,
            "found_keywords": kw_check["found"],
            "missing_keywords": kw_check["missing"],
            "hit_ratio": kw_check["hit_ratio"],
            "result_count": len(results),
        })

        return actual_hit, kw_check

    # --- Individual test methods (one per query for granular reporting) ---

    def test_01_service_overview_ports(self):
        """#1 服务全景与端口 (semantic, baseline: exact)"""
        case = REGRESSION_CASES[0]
        actual_hit, kw = self._run_case(case)
        self.assertGreaterEqual(
            kw["hit_ratio"], 0.5,
            f"Query '{case.query}': expected keywords {kw['missing']} not found"
        )

    def test_02_stream_gateway_components(self):
        """#2 Stream Gateway 内部组件 (semantic, baseline: exact)"""
        case = REGRESSION_CASES[1]
        actual_hit, kw = self._run_case(case)
        self.assertGreaterEqual(
            kw["hit_ratio"], 0.5,
            f"Query '{case.query}': expected keywords {kw['missing']} not found"
        )

    def test_03_ddd_domain_boundaries(self):
        """#3 DDD 领域边界 (semantic, baseline: exact)"""
        case = REGRESSION_CASES[2]
        actual_hit, kw = self._run_case(case)
        self.assertGreaterEqual(
            kw["hit_ratio"], 0.5,
            f"Query '{case.query}': expected keywords {kw['missing']} not found"
        )

    def test_04_authentik_scim_integration(self):
        """#4 Authentik SCIM 集成 (semantic, baseline: exact)"""
        case = REGRESSION_CASES[3]
        actual_hit, kw = self._run_case(case)
        self.assertGreaterEqual(
            kw["hit_ratio"], 0.5,
            f"Query '{case.query}': expected keywords {kw['missing']} not found"
        )

    def test_05_trafficrule_outbound_type(self):
        """#5 TrafficRule 出站类型 (hard_keyword, baseline: partial)"""
        case = REGRESSION_CASES[4]
        actual_hit, kw = self._run_case(case)
        # Baseline is partial — any result is acceptable, exact is improvement
        self.assertGreater(
            len(kw["found"]), 0,
            f"Query '{case.query}': no expected keywords found at all"
        )

    def test_06_agent_tun_dns_design(self):
        """#6 Agent TUN/DNS 设计 (hard_keyword, baseline: miss)"""
        case = REGRESSION_CASES[5]
        actual_hit, kw = self._run_case(case)
        # Baseline is miss — record result but don't fail (improvement target)
        # After Phase 1 optimization, this should become exact
        self.__class__.results_summary[-1]["note"] = (
            "baseline=miss, improvement target for Phase 1"
        )

    def test_07_policy_push_chain(self):
        """#7 策略变更推送链路 (semantic, baseline: exact)"""
        case = REGRESSION_CASES[6]
        actual_hit, kw = self._run_case(case)
        self.assertGreaterEqual(
            kw["hit_ratio"], 0.5,
            f"Query '{case.query}': expected keywords {kw['missing']} not found"
        )

    def test_08_bff_api_routing(self):
        """#8 BFF API 路由 (semantic, baseline: exact)"""
        case = REGRESSION_CASES[7]
        actual_hit, kw = self._run_case(case)
        self.assertGreaterEqual(
            kw["hit_ratio"], 0.5,
            f"Query '{case.query}': expected keywords {kw['missing']} not found"
        )

    def test_09_device_manager_dug_push(self):
        """#9 Device Manager DUG 推送 (semantic, baseline: exact)"""
        case = REGRESSION_CASES[8]
        actual_hit, kw = self._run_case(case)
        self.assertGreaterEqual(
            kw["hit_ratio"], 0.5,
            f"Query '{case.query}': expected keywords {kw['missing']} not found"
        )

    def test_10_sovereign_sase_architecture(self):
        """#10 Sovereign SASE 主权架构 (hard_keyword, baseline: partial)"""
        case = REGRESSION_CASES[9]
        actual_hit, kw = self._run_case(case)
        # Baseline is partial — any result is acceptable
        self.assertGreater(
            len(kw["found"]), 0,
            f"Query '{case.query}': no expected keywords found at all"
        )

    # --- Aggregate tests ---

    def test_aggregate_no_regression(self):
        """Ensure no previously-exact queries regress to miss."""
        # Run any cases not yet executed
        executed_ids = {r["id"] for r in self.__class__.results_summary}
        for case in REGRESSION_CASES:
            if case.id not in executed_ids:
                self._run_case(case)

        regressions = []
        for r in self.__class__.results_summary:
            if r["baseline_hit"] == "exact" and r["actual_hit"] == "miss":
                regressions.append(f"#{r['id']} {r['query']}")

        self.assertEqual(
            len(regressions), 0,
            f"Regressions detected (exact→miss): {regressions}"
        )

    def test_aggregate_exact_hit_rate(self):
        """Track exact hit rate against baseline (70%)."""
        executed_ids = {r["id"] for r in self.__class__.results_summary}
        for case in REGRESSION_CASES:
            if case.id not in executed_ids:
                self._run_case(case)

        exact_count = sum(
            1 for r in self.__class__.results_summary
            if r["actual_hit"] == "exact"
        )
        total = len(self.__class__.results_summary)
        rate = exact_count / total if total else 0

        # Should not drop below baseline of 70%
        self.assertGreaterEqual(
            rate, 0.7,
            f"Exact hit rate {rate:.0%} below baseline 70% "
            f"({exact_count}/{total})"
        )


if __name__ == "__main__":
    unittest.main()
