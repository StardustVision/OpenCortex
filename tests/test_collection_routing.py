# SPDX-License-Identifier: Apache-2.0
"""Tests for X-Collection header routing (collection_name contextvar)."""

import unittest


class TestCollectionRouting(unittest.TestCase):
    def test_default_collection_name_is_none(self):
        from opencortex.http.request_context import get_collection_name
        self.assertIsNone(get_collection_name())

    def test_set_and_get_collection_name(self):
        from opencortex.http.request_context import get_collection_name, set_collection_name, _collection_name
        token = set_collection_name("bench_qasper_abc123")
        self.addCleanup(_collection_name.reset, token)
        self.assertEqual(get_collection_name(), "bench_qasper_abc123")
