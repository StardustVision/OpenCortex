# SPDX-License-Identifier: Apache-2.0
"""
Tests for multi-tenant security decorator (require_tenant_access).

Tests verify:
1. Same tenant allowed - users can access their own tenant/user data
2. Different tenant denied - users cannot access other tenant data
3. Admin can access any tenant - admin users bypass tenant checks
"""

import unittest
from functools import wraps
from typing import Any, Callable

# Import the decorator and identity functions
from opencortex.insights.security import require_tenant_access
from opencortex.http.request_context import (
    set_request_identity,
    reset_request_identity,
    set_request_role,
    reset_request_role,
)


class TestRequireTenantAccess(unittest.TestCase):
    """Test cases for require_tenant_access decorator."""

    def test_same_tenant_allowed(self) -> None:
        """Users should access their own tenant/user data."""
        # Set up identity: tenant="acme", user="alice"
        tokens = set_request_identity("acme", "alice")
        role_token = set_request_role("user")

        try:
            # Create a decorated function that checks access to tenant "acme" user "alice"
            @require_tenant_access(tenant_id="acme", user_id="alice")
            def get_user_data() -> str:
                return "secret data"

            # Should not raise PermissionError
            result = get_user_data()
            self.assertEqual(result, "secret data")
        finally:
            reset_request_identity(tokens)
            reset_request_role(role_token)

    def test_different_tenant_denied(self) -> None:
        """Users should NOT access other tenant data."""
        # Set up identity: tenant="acme", user="alice"
        tokens = set_request_identity("acme", "alice")
        role_token = set_request_role("user")

        try:
            # Create a decorated function that checks access to tenant "evil" user "bob"
            @require_tenant_access(tenant_id="evil", user_id="bob")
            def get_user_data() -> str:
                return "secret data"

            # Should raise PermissionError
            with self.assertRaises(PermissionError):
                get_user_data()
        finally:
            reset_request_identity(tokens)
            reset_request_role(role_token)

    def test_admin_can_access_any_tenant(self) -> None:
        """Admin users should access any tenant."""
        # Set up admin identity: tenant="acme", user="admin"
        tokens = set_request_identity("acme", "admin")
        role_token = set_request_role("admin")

        try:
            # Create a decorated function that checks access to tenant "evil" user "bob"
            @require_tenant_access(tenant_id="evil", user_id="bob")
            def get_user_data() -> str:
                return "secret data"

            # Admin should be able to access despite tenant mismatch
            result = get_user_data()
            self.assertEqual(result, "secret data")
        finally:
            reset_request_identity(tokens)
            reset_request_role(role_token)


if __name__ == "__main__":
    unittest.main()
