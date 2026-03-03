# tests/test_semantic_name.py
import unittest


class TestSemanticNodeName(unittest.TestCase):

    def test_ascii_passthrough(self):
        from opencortex.utils.semantic_name import semantic_node_name
        self.assertEqual(semantic_node_name("hello_world"), "hello_world")

    def test_chinese_preserved(self):
        from opencortex.utils.semantic_name import semantic_node_name
        result = semantic_node_name("用户偏好深色主题")
        self.assertEqual(result, "用户偏好深色主题")

    def test_special_chars_replaced(self):
        from opencortex.utils.semantic_name import semantic_node_name
        result = semantic_node_name("Fix: import error (PYTHONPATH)")
        self.assertNotIn(":", result)
        self.assertNotIn("(", result)
        self.assertNotIn(")", result)

    def test_consecutive_underscores_merged(self):
        from opencortex.utils.semantic_name import semantic_node_name
        result = semantic_node_name("a:::b")
        self.assertNotIn("__", result)

    def test_truncation_with_hash(self):
        from opencortex.utils.semantic_name import semantic_node_name
        long_text = "a" * 100
        result = semantic_node_name(long_text, max_length=50)
        self.assertLessEqual(len(result), 50)
        # Should end with _<8-char-hash>
        self.assertRegex(result, r"_[a-f0-9]{8}$")

    def test_empty_returns_unnamed(self):
        from opencortex.utils.semantic_name import semantic_node_name
        self.assertEqual(semantic_node_name(""), "unnamed")
        self.assertEqual(semantic_node_name("!!!"), "unnamed")

    def test_deterministic(self):
        from opencortex.utils.semantic_name import semantic_node_name
        a = semantic_node_name("同一输入每次结果相同")
        b = semantic_node_name("同一输入每次结果相同")
        self.assertEqual(a, b)

    def test_short_text_no_hash(self):
        from opencortex.utils.semantic_name import semantic_node_name
        result = semantic_node_name("short")
        # "short" is only 5 chars, well under limit - no hash suffix
        self.assertEqual(result, "short")


if __name__ == "__main__":
    unittest.main()
