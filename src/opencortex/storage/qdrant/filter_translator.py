# SPDX-License-Identifier: Apache-2.0
"""
Filter DSL → Qdrant Filter model translator.

Translates the filter DSL used throughout OpenCortex into
Qdrant's native Filter/FieldCondition models.

Supported operators:
    must, must_not, range, prefix, contains, and, or, is_null
"""

from typing import Any, Dict, List

from qdrant_client import models


def translate_filter(dsl: Dict[str, Any]) -> models.Filter:
    """Translate a Filter DSL dict into a Qdrant Filter.

    Args:
        dsl: filter dictionary with "op" key.

    Returns:
        Qdrant Filter model.

    Examples:
        >>> translate_filter({"op": "must", "field": "uri", "conds": ["val"]})
        Filter(must=[FieldCondition(key="uri", match=MatchValue(value="val"))])
    """
    if not dsl:
        return models.Filter()

    op = dsl.get("op", "")

    if op == "and":
        children = [translate_filter(c) for c in dsl.get("conds", [])]
        # Flatten: collect all must conditions from children into a single list
        must_conditions: List[models.Condition] = []
        for child in children:
            if child.must:
                must_conditions.extend(child.must)
            elif child.should:
                # Wrap OR groups as a nested filter
                must_conditions.append(models.Filter(should=child.should))
            if child.must_not:
                must_conditions.extend(
                    [models.Filter(must_not=[c]) for c in child.must_not]
                )
        return models.Filter(must=must_conditions) if must_conditions else models.Filter()

    elif op == "or":
        children = [translate_filter(c) for c in dsl.get("conds", [])]
        should_conditions: List[models.Condition] = []
        for child in children:
            if child.must and len(child.must) == 1:
                should_conditions.append(child.must[0])
            elif child.must:
                should_conditions.append(models.Filter(must=child.must))
            elif child.should:
                should_conditions.extend(child.should)
        return models.Filter(should=should_conditions) if should_conditions else models.Filter()

    elif op == "must":
        field = dsl.get("field", "")
        conds = dsl.get("conds", [])
        condition = _must_condition(field, conds)
        return models.Filter(must=[condition])

    elif op == "must_not":
        field = dsl.get("field", "")
        conds = dsl.get("conds", [])
        condition = _must_condition(field, conds)
        return models.Filter(must_not=[condition])

    elif op == "range":
        condition = _range_condition(dsl)
        return models.Filter(must=[condition])

    elif op == "prefix":
        condition = _prefix_condition(dsl)
        return models.Filter(must=[condition])

    elif op == "contains":
        condition = _contains_condition(dsl)
        return models.Filter(must=[condition])

    elif op == "is_null":
        # Matches records where the field is missing, null, or empty array.
        # Qdrant's IsEmpty covers missing fields; IsNull only covers explicit nulls.
        field = dsl.get("field", "")
        return models.Filter(should=[
            models.IsNullCondition(is_null=models.PayloadField(key=field)),
            models.IsEmptyCondition(is_empty=models.PayloadField(key=field)),
        ])

    # Unknown op — return empty filter (match all)
    return models.Filter()


def _must_condition(field: str, conds: List[Any]) -> models.FieldCondition:
    """Create a FieldCondition for must/must_not ops."""
    if len(conds) == 1:
        value = conds[0]
        if isinstance(value, bool):
            return models.FieldCondition(
                key=field,
                match=models.MatchValue(value=value),
            )
        return models.FieldCondition(
            key=field,
            match=models.MatchValue(value=value),
        )
    else:
        # Multiple values → MatchAny
        return models.FieldCondition(
            key=field,
            match=models.MatchAny(any=list(conds)),
        )


def _range_condition(dsl: Dict[str, Any]) -> models.FieldCondition:
    """Create a FieldCondition for range op."""
    field = dsl.get("field", "")
    range_kwargs = {}
    if "gte" in dsl:
        range_kwargs["gte"] = dsl["gte"]
    if "gt" in dsl:
        range_kwargs["gt"] = dsl["gt"]
    if "lte" in dsl:
        range_kwargs["lte"] = dsl["lte"]
    if "lt" in dsl:
        range_kwargs["lt"] = dsl["lt"]

    return models.FieldCondition(
        key=field,
        range=models.Range(**range_kwargs),
    )


def _prefix_condition(dsl: Dict[str, Any]) -> models.FieldCondition:
    """Create a FieldCondition for prefix matching.

    Qdrant doesn't have a native prefix match on keyword fields,
    so we use MatchText for full-text indexed fields or a workaround
    by storing prefix-matchable fields with a text index.

    For keyword fields we use match with prefix=True (Qdrant >=1.10).
    """
    field = dsl.get("field", "")
    prefix = dsl.get("prefix", "")

    return models.FieldCondition(
        key=field,
        match=models.MatchText(text=prefix),
    )


def _contains_condition(dsl: Dict[str, Any]) -> models.FieldCondition:
    """Create a FieldCondition for substring contains."""
    field = dsl.get("field", "")
    substring = dsl.get("substring", "")

    return models.FieldCondition(
        key=field,
        match=models.MatchText(text=substring),
    )
