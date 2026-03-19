import unittest
from datetime import datetime, timedelta


class TestTimeFilter(unittest.TestCase):
    def test_time_filter_builds_range_condition(self):
        from opencortex.storage.qdrant.filter_translator import translate_filter
        now = datetime.utcnow()
        week_ago = (now - timedelta(days=7)).isoformat() + "Z"
        dsl = {"op": "range", "field": "created_at", "gte": week_ago}
        f = translate_filter(dsl)
        self.assertIsNotNone(f)
        self.assertEqual(len(f.must), 1)
        self.assertEqual(f.must[0].key, "created_at")

    def test_session_filter_builds_match(self):
        from opencortex.storage.qdrant.filter_translator import translate_filter
        dsl = {"op": "match", "field": "session_id", "value": "sess_123"}
        f = translate_filter(dsl)
        self.assertIsNotNone(f)
