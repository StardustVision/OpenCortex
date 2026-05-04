# SPDX-License-Identifier: Apache-2.0
"""Tests for shared memory filter helpers."""

from __future__ import annotations

import unittest

from opencortex.services.memory_filters import (
    FilterExpr,
    memory_visibility_filter,
    project_visibility_filter,
)


class TestMemoryFilters(unittest.TestCase):
    """Verify shared memory filter DSL helpers."""

    def test_memory_visibility_non_public_project(self) -> None:
        """Visibility includes staging exclusion, scope, tenant, and project."""
        self.assertEqual(
            memory_visibility_filter(
                tenant_id="tenant-1",
                user_id="user-1",
                project_id="project-1",
                exclude_staging=True,
                exclude_superseded=True,
            ).to_dict(),
            {
                "op": "and",
                "conds": [
                    {
                        "op": "must_not",
                        "field": "context_type",
                        "conds": ["staging"],
                    },
                    {
                        "op": "or",
                        "conds": [
                            {"op": "must", "field": "scope", "conds": ["shared", ""]},
                            {
                                "op": "and",
                                "conds": [
                                    {
                                        "op": "must",
                                        "field": "scope",
                                        "conds": ["private"],
                                    },
                                    {
                                        "op": "must",
                                        "field": "source_user_id",
                                        "conds": ["user-1"],
                                    },
                                ],
                            },
                        ],
                    },
                    {
                        "op": "must",
                        "field": "source_tenant_id",
                        "conds": ["tenant-1", ""],
                    },
                    {
                        "op": "must",
                        "field": "project_id",
                        "conds": ["project-1", "public", ""],
                    },
                    {
                        "op": "must_not",
                        "field": "meta.superseded",
                        "conds": [True],
                    },
                ],
            },
        )

    def test_project_visibility_skips_public_project(self) -> None:
        """Public project scope does not add an extra project clause."""
        self.assertIsNone(project_visibility_filter("public"))
        self.assertIsNone(project_visibility_filter(""))

    def test_prefix_expr(self) -> None:
        """Prefix filters serialize to the existing storage DSL."""
        self.assertEqual(
            FilterExpr.prefix("uri", "opencortex://tenant/user").to_dict(),
            {
                "op": "prefix",
                "field": "uri",
                "prefix": "opencortex://tenant/user",
            },
        )
