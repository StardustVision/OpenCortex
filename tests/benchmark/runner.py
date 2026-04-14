#!/usr/bin/env python3
"""
OpenCortex Phase 1 Benchmark Runner.

Seeds test memories into a running server, runs queries, computes metrics,
and saves a structured report.

Each run creates an isolated tenant (bench_<uuid>) to prevent cross-run pollution.
JWT is auto-generated from the server's auth_secret.key via --data-root.

Usage:
    # Against running server (real embeddings)
    python tests/benchmark/runner.py --base-url http://127.0.0.1:8921 --data-root ~/.opencortex

    # Save report
    python tests/benchmark/runner.py --data-root /path/to/data --output tests/benchmark/baseline/report.json
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from opencortex.auth.token import ensure_secret, generate_token
from opencortex.eval.memory_eval import evaluate_dataset

DATASET_PATH = Path(__file__).resolve().parent / "dataset.json"
DEFAULT_KS = [1, 3, 5]


def _auth_headers(jwt_token: str, collection: str = "") -> Dict[str, str]:
    """Build request headers with JWT Bearer auth."""
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {jwt_token}",
    }
    if collection:
        headers["X-Collection"] = collection
    return headers


def _http_post(base_url: str, path: str, payload: Dict, jwt_token: str,
               timeout: int = 30, collection: str = "") -> Dict:
    """POST JSON to server, return parsed response."""
    url = base_url.rstrip("/") + path
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=_auth_headers(jwt_token, collection), method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def seed_memories(base_url: str, memories: List[Dict], jwt_token: str,
                  timeout: int = 30, collection: str = "") -> Dict[str, str]:
    """Write benchmark memories to server. Returns {memory_id: uri}."""
    id_to_uri: Dict[str, str] = {}
    for mem in memories:
        payload = {
            "content": mem["content"],
            "abstract": mem.get("abstract", ""),
            "overview": mem.get("overview", mem.get("abstract", "")),
            "category": mem.get("category", ""),
            "context_type": mem.get("context_type", "memory"),
            "dedup": False,
        }
        try:
            result = _http_post(base_url, "/api/v1/memory/store", payload, jwt_token, timeout,
                                collection=collection)
            uri = result.get("uri", "")
            if uri:
                id_to_uri[mem["id"]] = uri
        except Exception as exc:
            print(f"  WARN: Failed to seed {mem['id']}: {exc}", file=sys.stderr)
    return id_to_uri


def search_via_http(
    base_url: str, query: str, limit: int, jwt_token: str,
    timeout: int = 30, collection: str = ""
) -> tuple[List[str], Dict[str, Any]]:
    """Search and return ranked URIs plus the raw payload."""
    payload = {"query": query, "limit": limit, "detail_level": "l1"}
    result = _http_post(base_url, "/api/v1/memory/search", payload, jwt_token, timeout,
                        collection=collection)
    items = result.get("results", [])
    uris = [item.get("uri", "") for item in items if isinstance(item, dict) and item.get("uri")]
    return uris, result


def _extract_search_attribution(result: Dict[str, Any]) -> Dict[str, Any]:
    """Extract phase attribution from a search response without affecting metrics."""
    memory_pipeline = result.get("memory_pipeline", {})
    probe = memory_pipeline.get("probe") or {}
    planner = memory_pipeline.get("planner") or {}
    runtime = memory_pipeline.get("runtime") or {}
    runtime_trace = runtime.get("trace") or {}
    runtime_degrade = runtime.get("degrade") or {}
    return {
        "probe": probe,
        "planner": planner,
        "runtime": {
            "trace": runtime_trace,
            "degrade": runtime_degrade,
        },
    }


def run_benchmark(
    base_url: str,
    data_root: str,
    dataset_path: Optional[str] = None,
    ks: Optional[List[int]] = None,
    timeout: int = 30,
) -> Dict[str, Any]:
    """Run full benchmark: generate isolated tenant → seed → query → metrics."""
    ds_path = dataset_path or str(DATASET_PATH)
    ks = ks or DEFAULT_KS

    with open(ds_path, encoding="utf-8") as f:
        dataset = json.load(f)

    memories = dataset.get("memories", [])
    queries = dataset.get("queries", [])

    # Isolation: unique tenant + isolated collection per run
    run_id = f"bench_{uuid4().hex[:8]}"
    user_id = "runner"
    jwt_token = generate_token(run_id, user_id, ensure_secret(data_root))
    collection_name = run_id  # e.g. bench_a1b2c3d4

    # Create isolated collection (physically separate from main "context" collection)
    try:
        _http_post(base_url, "/api/v1/admin/collection",
                   {"name": collection_name}, jwt_token, timeout)
        print(f"Run ID: {run_id} (isolated collection)", file=sys.stderr)
    except Exception as exc:
        print(f"WARN: Failed to create collection {collection_name}: {exc}. "
              f"Falling back to shared collection.", file=sys.stderr)
        collection_name = ""  # fallback to default collection
        print(f"Run ID: {run_id} (tenant isolation only)", file=sys.stderr)

    try:
        # Phase 1: Seed memories (always — no skip-seed)
        print(f"Seeding {len(memories)} memories...", file=sys.stderr)
        id_to_uri = seed_memories(base_url, memories, jwt_token, timeout,
                                  collection=collection_name)
        print(f"  Seeded {len(id_to_uri)}/{len(memories)} memories", file=sys.stderr)
        # Brief pause for indexing
        time.sleep(1)

        # Phase 2: Build eval rows with URI-based ground truth
        eval_rows: List[Dict[str, Any]] = []
        for q in queries:
            expected_ids = q.get("expected_ids", [])
            expected_uris = [id_to_uri[mid] for mid in expected_ids if mid in id_to_uri]

            eval_rows.append({
                "query": q["query"],
                "expected_uris": expected_uris,
                "category": q.get("category", "unknown"),
                "difficulty": q.get("difficulty", "unknown"),
                "query_id": q["id"],
            })

        # Phase 3: Run search + compute metrics
        print(f"Running {len(eval_rows)} queries (k={ks})...", file=sys.stderr)
        attribution_by_query_id: Dict[str, Dict[str, Any]] = {}

        def _search(item: Dict[str, Any], k: int) -> List[str]:
            uris, raw_result = search_via_http(
                base_url,
                item["query"],
                k,
                jwt_token,
                timeout,
                collection=collection_name,
            )
            attribution_by_query_id[item["query_id"]] = _extract_search_attribution(raw_result)
            return uris

        report = evaluate_dataset(dataset=eval_rows, ks=ks, search_fn=_search)

        query_attribution: List[Dict[str, Any]] = []
        for item in eval_rows:
            query_attribution.append(
                {
                    "query_id": item["query_id"],
                    "query": item["query"],
                    "attribution": attribution_by_query_id.get(item["query_id"], {}),
                }
            )

        # Add metadata
        report["metadata"] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "run_id": run_id,
            "base_url": base_url,
            "dataset": ds_path,
            "memories_seeded": len(id_to_uri),
            "queries_total": len(queries),
            "ks": ks,
            "collection_isolated": bool(collection_name),
        }
        report["attribution"] = query_attribution

        return report

    finally:
        # Drop isolated collection to prevent data accumulation
        if collection_name:
            try:
                url = f"{base_url.rstrip('/')}/api/v1/admin/collection/{collection_name}"
                req = urllib.request.Request(url, headers=_auth_headers(jwt_token), method="DELETE")
                urllib.request.urlopen(req, timeout=10)
                print(f"Cleaned up collection {collection_name}", file=sys.stderr)
            except Exception:
                pass


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="OpenCortex Phase 1 Benchmark Runner")
    parser.add_argument("--base-url", default="http://127.0.0.1:8921", help="Server URL")
    parser.add_argument("--data-root", default=None,
                        help="Server data_root for JWT generation (default: ~/.opencortex)")
    parser.add_argument("--dataset", default=None, help="Dataset JSON path (default: tests/benchmark/dataset.json)")
    parser.add_argument("--output", default=None, help="Save report to JSON file")
    parser.add_argument("--k", default="1,3,5", help="Comma-separated k values")
    parser.add_argument("--timeout", type=int, default=30, help="HTTP timeout")
    args = parser.parse_args(argv)

    data_root = args.data_root or str(Path.home() / ".opencortex")
    ks = [int(x.strip()) for x in args.k.split(",")]

    report = run_benchmark(
        base_url=args.base_url,
        data_root=data_root,
        dataset_path=args.dataset,
        ks=ks,
        timeout=args.timeout,
    )

    output_str = json.dumps(report, indent=2, ensure_ascii=False)
    print(output_str)

    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(output_str + "\n", encoding="utf-8")
        print(f"\nReport saved to {args.output}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
