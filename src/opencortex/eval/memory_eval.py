# SPDX-License-Identifier: Apache-2.0
"""Memory retrieval evaluation utilities.

Supports:
- Loading labeled query datasets
- Calling OpenCortex HTTP search API
- Computing Recall@k / Precision@k / HitRate@k / MRR
- Group-level breakdowns (category, difficulty)
"""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple


def normalize_uri(uri: str) -> str:
    """Normalize URI for comparison."""
    return uri.strip().rstrip("/")


def parse_ks(raw: str) -> List[int]:
    """Parse comma-separated K values."""
    values: List[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        k = int(part)
        if k <= 0:
            raise ValueError(f"k must be positive, got {k}")
        values.append(k)
    if not values:
        raise ValueError("no valid k values found")
    return sorted(set(values))


def load_dataset(path: str) -> List[Dict[str, Any]]:
    """Load dataset from JSON or JSONL.

    Supported formats:
    - JSON list
    - JSON object with key "queries" as list
    - JSONL (one object per line)
    """
    p = Path(path)
    text = p.read_text(encoding="utf-8")

    # Try JSON first
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None

    if isinstance(parsed, list):
        return [item for item in parsed if isinstance(item, dict)]
    if isinstance(parsed, dict):
        queries = parsed.get("queries", [])
        if isinstance(queries, list):
            return [item for item in queries if isinstance(item, dict)]
        raise ValueError("JSON object must contain list field 'queries'")

    # Fallback: JSONL
    rows: List[Dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        obj = json.loads(line)
        if isinstance(obj, dict):
            rows.append(obj)
    return rows


def search_via_http(
    base_url: str,
    query: str,
    limit: int,
    timeout: int = 30,
    context_type: Optional[str] = None,
    category: Optional[str] = None,
    detail_level: str = "l1",
) -> List[str]:
    """Search OpenCortex HTTP API and return ranked URIs."""
    url = base_url.rstrip("/") + "/api/v1/memory/search"
    payload: Dict[str, Any] = {
        "query": query,
        "limit": limit,
        "detail_level": detail_level,
    }
    if context_type:
        payload["context_type"] = context_type
    if category:
        payload["category"] = category

    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"connection failed: {exc.reason}") from exc

    items = data.get("results", [])
    uris: List[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        uri = item.get("uri", "")
        if uri:
            uris.append(normalize_uri(str(uri)))
    return uris


def _query_metrics(predicted: Sequence[str], expected: Sequence[str], ks: Sequence[int]) -> Dict[str, float]:
    expected_set = {normalize_uri(u) for u in expected if str(u).strip()}
    predicted_norm = [normalize_uri(u) for u in predicted if str(u).strip()]

    if not expected_set:
        raise ValueError("expected_uris is empty")

    out: Dict[str, float] = {}
    for k in ks:
        topk = predicted_norm[:k]
        hit_count = len(expected_set.intersection(topk))
        out[f"recall@{k}"] = hit_count / len(expected_set)
        out[f"precision@{k}"] = hit_count / k
        out[f"hit_rate@{k}"] = 1.0 if hit_count > 0 else 0.0
        # Accuracy@k in this retrieval setting means "whether top-k contains
        # at least one relevant memory", equivalent to hit_rate@k.
        out[f"accuracy@{k}"] = out[f"hit_rate@{k}"]

    rank: Optional[int] = None
    for idx, uri in enumerate(predicted_norm, start=1):
        if uri in expected_set:
            rank = idx
            break
    out["mrr"] = 1.0 / rank if rank else 0.0
    return out


def _aggregate(scored_rows: Sequence[Dict[str, float]], ks: Sequence[int]) -> Dict[str, float]:
    if not scored_rows:
        result: Dict[str, float] = {"count": 0.0}
        for k in ks:
            result[f"recall@{k}"] = 0.0
            result[f"precision@{k}"] = 0.0
            result[f"hit_rate@{k}"] = 0.0
            result[f"accuracy@{k}"] = 0.0
        result["mrr"] = 0.0
        return result

    total = float(len(scored_rows))
    result = {"count": total}
    keys = [f"recall@{k}" for k in ks] + [f"precision@{k}" for k in ks] + [
        f"hit_rate@{k}" for k in ks
    ] + [
        f"accuracy@{k}" for k in ks
    ] + ["mrr"]
    for key in keys:
        result[key] = sum(row.get(key, 0.0) for row in scored_rows) / total
    return result


def _to_float_or_none(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _token_comparison(rows: Sequence[Dict[str, Any]]) -> Dict[str, float]:
    """Compute token reduction between with-memory and without-memory runs.

    Uses optional row fields:
    - tokens_with_memory
    - tokens_without_memory
    """
    pairs: List[Tuple[float, float]] = []
    for row in rows:
        with_mem = _to_float_or_none(row.get("tokens_with_memory"))
        without_mem = _to_float_or_none(row.get("tokens_without_memory"))
        if with_mem is None or without_mem is None:
            continue
        if with_mem < 0 or without_mem <= 0:
            continue
        pairs.append((with_mem, without_mem))

    if not pairs:
        return {
            "count": 0.0,
            "avg_tokens_with_memory": 0.0,
            "avg_tokens_without_memory": 0.0,
            "total_tokens_with_memory": 0.0,
            "total_tokens_without_memory": 0.0,
            "token_reduction": 0.0,
            "token_reduction_ratio": 0.0,
        }

    total_with = sum(p[0] for p in pairs)
    total_without = sum(p[1] for p in pairs)
    reduction = total_without - total_with
    ratio = reduction / total_without if total_without > 0 else 0.0
    count = float(len(pairs))

    return {
        "count": count,
        "avg_tokens_with_memory": total_with / count,
        "avg_tokens_without_memory": total_without / count,
        "total_tokens_with_memory": total_with,
        "total_tokens_without_memory": total_without,
        "token_reduction": reduction,
        "token_reduction_ratio": ratio,
    }


def compute_report(rows: Sequence[Dict[str, Any]], ks: Sequence[int]) -> Dict[str, Any]:
    """Compute overall and grouped metrics from query-level rows.

    Each row should contain:
    - expected_uris: List[str]
    - predicted_uris: List[str]
    - optional category/difficulty
    """
    scored: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []

    for row in rows:
        query = str(row.get("query", "")).strip()
        expected = row.get("expected_uris", [])
        predicted = row.get("predicted_uris", [])
        if not isinstance(expected, list) or not expected:
            skipped.append({"query": query, "reason": "missing expected_uris"})
            continue
        if not isinstance(predicted, list):
            predicted = []
        metrics = _query_metrics(predicted=predicted, expected=expected, ks=ks)
        scored.append({
            **row,
            **metrics,
        })

    summary = _aggregate(scored, ks=ks)

    def _group_by(field: str) -> Dict[str, Dict[str, float]]:
        groups: Dict[str, List[Dict[str, float]]] = {}
        for row in scored:
            key = str(row.get(field, "unknown")).strip() or "unknown"
            groups.setdefault(key, []).append(row)
        return {k: _aggregate(v, ks=ks) for k, v in groups.items()}

    return {
        "summary": summary,
        "token_comparison": _token_comparison(rows),
        "by_category": _group_by("category"),
        "by_difficulty": _group_by("difficulty"),
        "scored_count": len(scored),
        "skipped_count": len(skipped),
        "skipped": skipped,
        "query_results": scored,
    }


def evaluate_dataset(
    dataset: Sequence[Dict[str, Any]],
    ks: Sequence[int],
    search_fn: Callable[[Dict[str, Any], int], List[str]],
) -> Dict[str, Any]:
    """Evaluate dataset by calling search_fn for each query."""
    max_k = max(ks)
    rows: List[Dict[str, Any]] = []
    for item in dataset:
        query = str(item.get("query", "")).strip()
        if not query:
            rows.append({
                **item,
                "predicted_uris": [],
            })
            continue

        predicted = search_fn(item, max_k)
        rows.append({
            **item,
            "predicted_uris": predicted,
        })
    return compute_report(rows, ks=ks)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Evaluate OpenCortex memory retrieval quality")
    parser.add_argument("--dataset", required=True, help="Path to JSON/JSONL eval dataset")
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:8921",
        help="OpenCortex HTTP server base URL",
    )
    parser.add_argument(
        "--k",
        default="1,3,5",
        help="Comma-separated k values, e.g. 1,3,5",
    )
    parser.add_argument("--timeout", type=int, default=30, help="HTTP timeout seconds")
    parser.add_argument("--output", default="", help="Optional report output path (.json)")
    return parser


def run_cli(argv: Optional[Sequence[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    dataset = load_dataset(args.dataset)
    ks = parse_ks(args.k)

    def _search(item: Dict[str, Any], max_k: int) -> List[str]:
        return search_via_http(
            base_url=args.base_url,
            query=str(item.get("query", "")),
            limit=max_k,
            timeout=args.timeout,
            context_type=item.get("context_type"),
            category=item.get("category_filter"),
            detail_level=item.get("detail_level", "l1"),
        )

    report = evaluate_dataset(dataset=dataset, ks=ks, search_fn=_search)

    output = json.dumps(report, indent=2, ensure_ascii=False)
    print(output)

    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(output + "\n", encoding="utf-8")

    return 0


if __name__ == "__main__":
    raise SystemExit(run_cli())
