import unittest
from opencortex.skill_engine.patch import (
    detect_patch_type, PatchType, apply_patch, PatchResult,
    parse_multi_file_full, _find_anchor, _fuzzy_replace,
)


class TestDetectPatchType(unittest.TestCase):

    def test_detect_full(self):
        self.assertEqual(detect_patch_type("*** Begin Files\n*** File: SKILL.md"), PatchType.FULL)

    def test_detect_patch(self):
        self.assertEqual(detect_patch_type("*** Begin Patch\n@@ line"), PatchType.PATCH)

    def test_detect_diff(self):
        self.assertEqual(detect_patch_type("<<<<<<< SEARCH\nold\n=======\nnew\n>>>>>>> REPLACE"), PatchType.DIFF)

    def test_detect_default_full(self):
        self.assertEqual(detect_patch_type("just plain content"), PatchType.FULL)


class TestApplyFull(unittest.TestCase):

    def test_simple_replacement(self):
        result = apply_patch("old", "new content here", PatchType.FULL)
        self.assertTrue(result.ok)
        self.assertEqual(result.content, "new content here")

    def test_multi_file_markers(self):
        patch = "*** Begin Files\n*** File: SKILL.md\n# New Skill\n1. Step one\n*** End Files"
        result = apply_patch("old", patch, PatchType.FULL)
        self.assertTrue(result.ok)
        self.assertIn("# New Skill", result.content)

    def test_parse_multi_file(self):
        content = "*** File: SKILL.md\n# Main\n*** File: helper.sh\n#!/bin/bash"
        files = parse_multi_file_full(content)
        self.assertIn("SKILL.md", files)
        self.assertIn("helper.sh", files)
        self.assertIn("# Main", files["SKILL.md"])


class TestApplyDiff(unittest.TestCase):

    def test_simple_search_replace(self):
        original = "# Skill\n1. Old step\n2. Keep this"
        patch = "<<<<<<< SEARCH\n1. Old step\n=======\n1. New step\n>>>>>>> REPLACE"
        result = apply_patch(original, patch, PatchType.DIFF)
        self.assertTrue(result.ok)
        self.assertIn("1. New step", result.content)
        self.assertIn("2. Keep this", result.content)
        self.assertNotIn("1. Old step", result.content)

    def test_multiple_replacements(self):
        original = "A\nB\nC"
        patch = "<<<<<<< SEARCH\nA\n=======\nX\n>>>>>>> REPLACE\n<<<<<<< SEARCH\nC\n=======\nZ\n>>>>>>> REPLACE"
        result = apply_patch(original, patch, PatchType.DIFF)
        self.assertTrue(result.ok)
        self.assertIn("X", result.content)
        self.assertIn("Z", result.content)
        self.assertEqual(result.applied_count, 2)

    def test_no_match_fails(self):
        result = apply_patch("abc", "<<<<<<< SEARCH\nxyz\n=======\nnew\n>>>>>>> REPLACE", PatchType.DIFF)
        self.assertFalse(result.ok)

    def test_fuzzy_match_trailing_whitespace(self):
        original = "1. Step one   \n2. Step two"
        patch = "<<<<<<< SEARCH\n1. Step one\n=======\n1. Better step\n>>>>>>> REPLACE"
        result = apply_patch(original, patch, PatchType.DIFF)
        self.assertTrue(result.ok)
        self.assertIn("1. Better step", result.content)


class TestFindAnchor(unittest.TestCase):

    def test_exact_match(self):
        self.assertEqual(_find_anchor("a\nb\nc", "b"), 1)

    def test_strip_match(self):
        self.assertEqual(_find_anchor("  a  \n  b  \n  c  ", "b"), 1)

    def test_no_match(self):
        self.assertIsNone(_find_anchor("a\nb\nc", "z"))


class TestFuzzyReplace(unittest.TestCase):

    def test_exact(self):
        result = _fuzzy_replace("hello world", "hello", "hi")
        self.assertEqual(result, "hi world")

    def test_strip_match(self):
        result = _fuzzy_replace("  hello  \n  world  ", "hello\nworld", "hi\nearth")
        self.assertIsNotNone(result)
        self.assertIn("hi", result)

    def test_no_match(self):
        result = _fuzzy_replace("abc", "xyz", "new")
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
