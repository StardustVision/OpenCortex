"""
Unit tests for per-request tenant/user identity via contextvars.

Tests:
- contextvar defaults fall back to config
- set/reset work correctly
- concurrent async tasks get isolated identities
"""

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from opencortex.config import CortexConfig, init_config
from opencortex.http.request_context import (
    get_effective_identity,
    reset_request_identity,
    set_request_identity,
)


class TestRequestContext(unittest.TestCase):
    """Test the request_context module."""

    def setUp(self):
        self.config = CortexConfig(
            tenant_id="cfg-tenant",
            user_id="cfg-user",
        )
        init_config(self.config)

    def _run(self, coro):
        return asyncio.run(coro)

    # -----------------------------------------------------------------
    # 1. Default fallback to config
    # -----------------------------------------------------------------

    def test_01_default_fallback_to_config(self):
        """Without contextvar set, get_effective_identity returns config values."""
        tid, uid = get_effective_identity()
        self.assertEqual(tid, "cfg-tenant")
        self.assertEqual(uid, "cfg-user")

    def test_02_default_fallback_to_explicit_args(self):
        """Without contextvar, explicit args take precedence over config."""
        tid, uid = get_effective_identity("arg-tenant", "arg-user")
        self.assertEqual(tid, "arg-tenant")
        self.assertEqual(uid, "arg-user")

    # -----------------------------------------------------------------
    # 2. Set / Reset
    # -----------------------------------------------------------------

    def test_03_set_overrides_config(self):
        """After set_request_identity, contextvar values are returned."""
        tokens = set_request_identity("req-tenant", "req-user")
        try:
            tid, uid = get_effective_identity()
            self.assertEqual(tid, "req-tenant")
            self.assertEqual(uid, "req-user")
        finally:
            reset_request_identity(tokens)

    def test_04_reset_restores_default(self):
        """After reset, we fall back to config again."""
        tokens = set_request_identity("req-tenant", "req-user")
        reset_request_identity(tokens)
        tid, uid = get_effective_identity()
        self.assertEqual(tid, "cfg-tenant")
        self.assertEqual(uid, "cfg-user")

    def test_05_contextvar_overrides_explicit_args(self):
        """Contextvar takes precedence over explicit config args."""
        tokens = set_request_identity("req-tenant", "req-user")
        try:
            tid, uid = get_effective_identity("arg-tenant", "arg-user")
            self.assertEqual(tid, "req-tenant")
            self.assertEqual(uid, "req-user")
        finally:
            reset_request_identity(tokens)

    # -----------------------------------------------------------------
    # 3. Async task isolation
    # -----------------------------------------------------------------

    def test_06_concurrent_async_isolation(self):
        """Different async tasks see their own contextvar values."""

        async def _run_concurrent():
            results = {}

            async def worker(name, tenant, user):
                tokens = set_request_identity(tenant, user)
                try:
                    # Yield to let other tasks run
                    await asyncio.sleep(0.01)
                    tid, uid = get_effective_identity()
                    results[name] = (tid, uid)
                finally:
                    reset_request_identity(tokens)

            # Run 3 concurrent tasks with different identities
            await asyncio.gather(
                worker("a", "tenant-a", "user-a"),
                worker("b", "tenant-b", "user-b"),
                worker("c", "tenant-c", "user-c"),
            )
            return results

        results = self._run(_run_concurrent())
        self.assertEqual(results["a"], ("tenant-a", "user-a"))
        self.assertEqual(results["b"], ("tenant-b", "user-b"))
        self.assertEqual(results["c"], ("tenant-c", "user-c"))

    def test_07_nested_set_reset(self):
        """Nested set/reset works correctly (inner restores outer)."""
        outer_tokens = set_request_identity("outer-t", "outer-u")
        try:
            inner_tokens = set_request_identity("inner-t", "inner-u")
            tid, uid = get_effective_identity()
            self.assertEqual(tid, "inner-t")
            self.assertEqual(uid, "inner-u")
            reset_request_identity(inner_tokens)

            tid, uid = get_effective_identity()
            self.assertEqual(tid, "outer-t")
            self.assertEqual(uid, "outer-u")
        finally:
            reset_request_identity(outer_tokens)


if __name__ == "__main__":
    unittest.main()
