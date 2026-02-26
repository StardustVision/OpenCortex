# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: Apache-2.0
"""
Translate VikingDB filter DSL to the RuVector metadata filter format.

VikingDB filter DSL example::

    {
        "op": "and",
        "conds": [
            {"op": "must",     "field": "name",   "conds": ["Alice"]},
            {"op": "range",    "field": "age",    "gte": 18},
            {"op": "prefix",   "field": "uri",    "prefix": "opencortex://"},
            {"op": "contains", "field": "desc",   "substring": "hello"},
        ]
    }

RuVector only supports simple equality filters via ``--filter``.  All other
conditions (range, prefix, contains, or) are handled by a post-filter callable
that is applied in Python after the CLI returns results.
"""

import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def translate_filter(
    viking_filter: Dict[str, Any],
) -> Tuple[Dict[str, Any], Optional[Callable[[Dict[str, Any]], bool]]]:
    """
    Translate a VikingDB filter dict to a RuVector-compatible representation.

    Args:
        viking_filter: VikingDB DSL filter dict.

    Returns:
        A two-element tuple ``(cli_filter, post_filter_fn)`` where:

        * ``cli_filter`` is a plain equality dict passed to rvf-cli via
          ``--filter``.  Only ``must`` (equality) conditions that can be
          expressed as ``{field: value}`` end up here.
        * ``post_filter_fn`` is a callable ``(record: dict) -> bool`` that
          evaluates all conditions that rvf-cli cannot handle natively
          (range, prefix, contains, or).  Returns ``None`` when no
          post-filtering is needed.
    """
    if not viking_filter:
        return {}, None

    cli_filter: Dict[str, Any] = {}
    post_conditions: List[Callable[[Dict[str, Any]], bool]] = []

    _extract_filters(viking_filter, cli_filter, post_conditions)

    post_fn: Optional[Callable[[Dict[str, Any]], bool]] = None
    if post_conditions:
        # Capture the list by reference so the closure picks up the final state.
        captured = list(post_conditions)

        def post_filter(record: Dict[str, Any], _conds: List = captured) -> bool:
            return all(cond(record) for cond in _conds)

        post_fn = post_filter

    return cli_filter, post_fn


def _extract_filters(
    node: Dict[str, Any],
    cli_filter: Dict[str, Any],
    post_conditions: List[Callable[[Dict[str, Any]], bool]],
) -> None:
    """
    Recursively walk a filter node and populate *cli_filter* and
    *post_conditions* in-place.

    Args:
        node: A single filter node (may be a compound ``and``/``or`` node or a
              leaf condition).
        cli_filter: Accumulator for simple equality conditions.
        post_conditions: Accumulator for callables that must be evaluated in
                         Python.
    """
    op = node.get("op", "")

    if op == "and":
        # AND: recurse into each child; all must pass.
        for cond in node.get("conds", []):
            if isinstance(cond, dict):
                _extract_filters(cond, cli_filter, post_conditions)

    elif op == "or":
        # OR: RuVector has no native OR — convert the entire branch to a
        # post-filter.
        or_conds: List[Dict[str, Any]] = node.get("conds", [])

        def or_filter(
            record: Dict[str, Any],
            conditions: List[Dict[str, Any]] = or_conds,
        ) -> bool:
            for child in conditions:
                sub_cli: Dict[str, Any] = {}
                sub_post: List[Callable] = []
                _extract_filters(child, sub_cli, sub_post)
                cli_match = all(record.get(k) == v for k, v in sub_cli.items())
                post_match = all(fn(record) for fn in sub_post)
                if cli_match and post_match:
                    return True
            return False

        post_conditions.append(or_filter)

    elif op == "must":
        # Equality check: push to rvf-cli filter when there is exactly one value.
        field = node.get("field", "")
        values = node.get("conds", [])
        if field and values:
            if len(values) == 1:
                # Single value: native equality filter.
                cli_filter[field] = values[0]
            else:
                # Multiple allowed values: treat as an OR of equalities.
                allowed = list(values)

                def in_filter(
                    record: Dict[str, Any],
                    f: str = field,
                    vals: List = allowed,
                ) -> bool:
                    return record.get(f) in vals

                post_conditions.append(in_filter)

    elif op == "prefix":
        field = node.get("field", "")
        prefix = node.get("prefix", "")

        def prefix_filter(
            record: Dict[str, Any],
            f: str = field,
            p: str = prefix,
        ) -> bool:
            return str(record.get(f, "")).startswith(p)

        post_conditions.append(prefix_filter)

    elif op == "range":
        field = node.get("field", "")
        # Capture the entire node for bound extraction.
        captured_node = dict(node)

        def range_filter(
            record: Dict[str, Any],
            f: str = field,
            n: Dict = captured_node,
        ) -> bool:
            val = record.get(f)
            if val is None:
                return False
            try:
                val = float(val)
            except (TypeError, ValueError):
                return False
            if "gte" in n and val < n["gte"]:
                return False
            if "gt" in n and val <= n["gt"]:
                return False
            if "lte" in n and val > n["lte"]:
                return False
            if "lt" in n and val >= n["lt"]:
                return False
            return True

        post_conditions.append(range_filter)

    elif op == "contains":
        field = node.get("field", "")
        substring = node.get("substring", "")

        def contains_filter(
            record: Dict[str, Any],
            f: str = field,
            s: str = substring,
        ) -> bool:
            return s in str(record.get(f, ""))

        post_conditions.append(contains_filter)

    else:
        logger.warning("filter_translator: unknown op '%s' — skipping node", op)
