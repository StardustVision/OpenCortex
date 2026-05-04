# SPDX-License-Identifier: Apache-2.0
"""Shared storage-filter helpers for memory visibility and scope."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Tuple


@dataclass(frozen=True)
class FilterExpr:
    """Small typed builder for the storage filter DSL."""

    op: str
    field_name: str = ""
    values: Tuple[Any, ...] = ()
    children: Tuple["FilterExpr", ...] = field(default_factory=tuple)
    prefix_value: str = ""

    @classmethod
    def eq(cls, field_name: str, *values: Any) -> "FilterExpr":
        """Build an equality/membership filter."""
        return cls(op="must", field_name=field_name, values=tuple(values))

    @classmethod
    def neq(cls, field_name: str, *values: Any) -> "FilterExpr":
        """Build a negated equality/membership filter."""
        return cls(op="must_not", field_name=field_name, values=tuple(values))

    @classmethod
    def prefix(cls, field_name: str, value: str) -> "FilterExpr":
        """Build a prefix filter."""
        return cls(op="prefix", field_name=field_name, prefix_value=value)

    @classmethod
    def all(cls, *children: "FilterExpr | None") -> "FilterExpr":
        """Build an AND expression."""
        return cls(op="and", children=tuple(child for child in children if child))

    @classmethod
    def any(cls, *children: "FilterExpr | None") -> "FilterExpr":
        """Build an OR expression."""
        return cls(op="or", children=tuple(child for child in children if child))

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to the storage filter DSL."""
        if self.op in {"must", "must_not"}:
            return {
                "op": self.op,
                "field": self.field_name,
                "conds": list(self.values),
            }
        if self.op == "prefix":
            return {
                "op": "prefix",
                "field": self.field_name,
                "prefix": self.prefix_value,
            }
        return {"op": self.op, "conds": [child.to_dict() for child in self.children]}


def and_filter(*children: FilterExpr | None) -> Dict[str, Any]:
    """Return an AND filter dict from non-empty expressions."""
    return FilterExpr.all(*children).to_dict()


def scope_visibility_filter(user_id: str) -> FilterExpr:
    """Return private-own-or-shared visibility filter."""
    return FilterExpr.any(
        FilterExpr.eq("scope", "shared", ""),
        FilterExpr.all(
            FilterExpr.eq("scope", "private"),
            FilterExpr.eq("source_user_id", user_id),
        ),
    )


def tenant_filter(tenant_id: str, *, include_legacy_empty: bool = True) -> FilterExpr | None:
    """Return tenant filter when an effective tenant is available."""
    if not tenant_id:
        return None
    values: Iterable[str] = (tenant_id, "") if include_legacy_empty else (tenant_id,)
    return FilterExpr.eq("source_tenant_id", *values)


def project_visibility_filter(project_id: str) -> FilterExpr | None:
    """Return project visibility filter matching recall/list behavior."""
    if not project_id or project_id == "public":
        return None
    return FilterExpr.eq("project_id", project_id, "public", "")


def memory_visibility_filter(
    *,
    tenant_id: str,
    user_id: str,
    project_id: str,
    exclude_staging: bool = True,
    exclude_superseded: bool = False,
) -> FilterExpr:
    """Build the common user-visible memory ACL filter."""
    clauses = []
    if exclude_staging:
        clauses.append(FilterExpr.neq("context_type", "staging"))
    clauses.append(scope_visibility_filter(user_id))
    clauses.append(tenant_filter(tenant_id))
    clauses.append(project_visibility_filter(project_id))
    if exclude_superseded:
        clauses.append(FilterExpr.neq("meta.superseded", True))
    return FilterExpr.all(*clauses)
