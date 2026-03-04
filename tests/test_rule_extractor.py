# SPDX-License-Identifier: Apache-2.0
"""
Tests for RuleExtractor — zero-LLM pattern extraction.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from opencortex.ace.rule_extractor import ExtractedSkill, RuleExtractor


class TestRuleExtractor(unittest.TestCase):
    """Test RuleExtractor pattern extraction and granularity validation."""

    def setUp(self):
        self.extractor = RuleExtractor()

    # -----------------------------------------------------------------
    # Content filtering
    # -----------------------------------------------------------------

    def test_01_short_content_skipped(self):
        """Content shorter than MIN_CONTENT_LEN is skipped entirely."""
        skills = self.extractor.extract("short note", "too short")
        self.assertEqual(len(skills), 0)

    def test_02_empty_content_skipped(self):
        """Empty content returns no skills."""
        skills = self.extractor.extract("title", "")
        self.assertEqual(len(skills), 0)

    # -----------------------------------------------------------------
    # Error → Fix extraction
    # -----------------------------------------------------------------

    def test_03_error_fix_extraction(self):
        """Detects error→fix pattern with causal structure."""
        content = (
            "When running the data pipeline, we encountered an error:\n"
            "UnicodeDecodeError: 'utf-8' codec can't decode byte 0xff in position 0\n\n"
            "The fix was to use chardet to detect encoding before decoding. "
            "When encountering UTF-8 decode errors, first use chardet.detect() "
            "to identify the actual encoding, then decode with the detected codec."
        )
        # Pad to meet MIN_CONTENT_LEN
        content += "\n" * 50
        skills = self.extractor.extract("UTF-8 error fix", content)
        error_fixes = [s for s in skills if s.section == "error_fixes"]
        self.assertGreater(len(error_fixes), 0, "Should extract at least one error_fix skill")

    def test_04_error_without_fix_skipped(self):
        """Error without a fix pattern is not extracted."""
        content = (
            "The system crashed with an error message:\n"
            "RuntimeError: something went wrong\n\n"
            "We observed the logs and noted the timestamps.\n"
            "The issue was intermittent and hard to reproduce."
        )
        content += "\n" * 50
        skills = self.extractor.extract("Error observation", content)
        error_fixes = [s for s in skills if s.section == "error_fixes"]
        self.assertEqual(len(error_fixes), 0, "No fix = no error_fix skill")

    # -----------------------------------------------------------------
    # Preference extraction
    # -----------------------------------------------------------------

    def test_05_preference_always_detected(self):
        """'always' keyword triggers preference extraction."""
        content = (
            "Project configuration notes:\n"
            "The team decided that we should always use TypeScript for new modules.\n"
            "This applies to all frontend and backend code going forward.\n"
            "Additional context about the project setup and tooling.\n"
        )
        content += "\n" * 50
        skills = self.extractor.extract("Config notes", content)
        prefs = [s for s in skills if s.section == "preferences"]
        self.assertGreater(len(prefs), 0, "Should extract preference from 'always' signal")

    def test_06_preference_never_detected(self):
        """'never' keyword triggers preference extraction."""
        content = (
            "Code review guidelines:\n"
            "We should never use eval() in production code for security reasons.\n"
            "All dynamic code execution must use safe alternatives.\n"
            "The security team has confirmed this policy applies globally.\n"
        )
        content += "\n" * 50
        skills = self.extractor.extract("Code review", content)
        prefs = [s for s in skills if s.section == "preferences"]
        self.assertGreater(len(prefs), 0, "Should extract preference from 'never' signal")

    def test_07_preference_chinese_detected(self):
        """Chinese preference keywords (必须/不要) trigger extraction."""
        content = (
            "团队编码规范：\n"
            "必须使用 black 格式化所有 Python 代码，确保一致性。\n"
            "所有提交前必须运行 lint 检查。\n"
            "这是团队达成共识的标准流程。\n"
        )
        content += "\n" * 50
        skills = self.extractor.extract("Coding standard", content)
        prefs = [s for s in skills if s.section == "preferences"]
        self.assertGreater(len(prefs), 0, "Should extract Chinese preference")

    # -----------------------------------------------------------------
    # Workflow extraction
    # -----------------------------------------------------------------

    def test_08_workflow_extraction(self):
        """Multi-step workflow with causal structure and ≥3 steps is extracted."""
        content = (
            "When deploying to production, then follow this procedure:\n"
            "1. Run lint checks on the codebase\n"
            "2. Execute the full test suite\n"
            "3. Build the Docker image with version tag\n"
            "4. Push to container registry\n"
            "5. Deploy to staging environment\n"
            "This ensures quality before production release.\n"
        )
        content += "\n" * 30
        skills = self.extractor.extract("Deployment", content)
        workflows = [s for s in skills if s.section == "workflows"]
        self.assertGreater(len(workflows), 0, "Should extract workflow pattern")

    def test_08b_workflow_requires_causal_structure(self):
        """Multi-step workflow without causal connectors is rejected."""
        content = (
            "Project architecture overview:\n"
            "1. The HTTP server handles incoming requests\n"
            "2. The orchestrator coordinates all components\n"
            "3. The storage layer persists data to Qdrant\n"
            "4. The embedder generates vector representations\n"
            "5. The retriever searches across collections\n"
            "This is a standard layered architecture.\n"
        )
        content += "\n" * 30
        skills = self.extractor.extract("Architecture", content)
        workflows = [s for s in skills if s.section == "workflows"]
        self.assertEqual(len(workflows), 0,
                         "Workflow without causal structure should be rejected")

    def test_09_too_few_steps_not_workflow(self):
        """Fewer than 3 steps is not extracted as a workflow."""
        content = (
            "Simple setup:\n"
            "1. Install dependencies\n"
            "2. Run the app\n"
            "No further steps needed for local development.\n"
        )
        content += "\n" * 80
        skills = self.extractor.extract("Setup", content)
        workflows = [s for s in skills if s.section == "workflows"]
        self.assertEqual(len(workflows), 0, "Too few steps for workflow extraction")

    # -----------------------------------------------------------------
    # Granularity validation
    # -----------------------------------------------------------------

    def test_10_trivial_command_rejected(self):
        """Trivial single commands are rejected by granularity check."""
        skill = ExtractedSkill(content="ls -la /tmp", section="workflows")
        self.assertFalse(self.extractor._validate_granularity(skill))

    def test_11_too_short_rejected(self):
        """Content shorter than 10 chars is rejected."""
        skill = ExtractedSkill(content="use foo", section="preferences")
        self.assertFalse(self.extractor._validate_granularity(skill))

    def test_12_too_long_rejected(self):
        """Content longer than 300 chars is rejected."""
        skill = ExtractedSkill(content="use " + "x" * 300, section="preferences")
        self.assertFalse(self.extractor._validate_granularity(skill))

    def test_13_no_action_verb_rejected(self):
        """Content without action verbs is rejected."""
        skill = ExtractedSkill(content="the system has many components and layers", section="preferences")
        self.assertFalse(self.extractor._validate_granularity(skill))

    def test_14_valid_skill_accepted(self):
        """Well-formed skill passes granularity validation."""
        skill = ExtractedSkill(
            content="Always use TypeScript for new frontend modules in the project",
            section="preferences",
        )
        self.assertTrue(self.extractor._validate_granularity(skill))

    def test_15_error_fix_without_causal_rejected(self):
        """Error fix without causal structure is rejected."""
        skill = ExtractedSkill(
            content="Fix the encoding error by updating the config file",
            section="error_fixes",
        )
        self.assertFalse(self.extractor._validate_granularity(skill))

    def test_16_error_fix_with_causal_accepted(self):
        """Error fix with causal structure passes validation."""
        skill = ExtractedSkill(
            content="When encountering a UTF-8 decode error, use chardet to detect the actual encoding then decode",
            section="error_fixes",
        )
        self.assertTrue(self.extractor._validate_granularity(skill))

    # -----------------------------------------------------------------
    # Helper methods
    # -----------------------------------------------------------------

    def test_17_has_action_verb_english(self):
        """English action verbs are detected."""
        self.assertTrue(self.extractor._has_action_verb("always use pytest for testing"))
        self.assertFalse(self.extractor._has_action_verb("the sky is blue today"))

    def test_18_has_action_verb_chinese(self):
        """Chinese action verbs are detected."""
        self.assertTrue(self.extractor._has_action_verb("必须使用 black 格式化代码"))

    def test_19_is_trivial_command(self):
        """Trivial commands are detected."""
        self.assertTrue(self.extractor._is_trivial_command("ls -la"))
        self.assertTrue(self.extractor._is_trivial_command("git status"))
        self.assertFalse(self.extractor._is_trivial_command("deploy the staging server using docker compose"))

    def test_20_has_causal_structure(self):
        """Causal structure detection works."""
        self.assertTrue(self.extractor._has_causal_structure(
            "When the cache expires, use a fresh token to authenticate"
        ))
        self.assertFalse(self.extractor._has_causal_structure(
            "The server processes requests"
        ))

    # -----------------------------------------------------------------
    # Non-skill content blockers
    # -----------------------------------------------------------------

    def test_21_code_snippet_rejected(self):
        """Code snippets (shebang / import) are rejected."""
        cases = [
            ExtractedSkill(content="#!/usr/bin/env python\nimport sys\nuse run deploy", section="preferences"),
            ExtractedSkill(content="import os\nimport sys\nuse run deploy stuff", section="preferences"),
            ExtractedSkill(content="from opencortex.ace import rule_extractor\nuse this module", section="preferences"),
        ]
        for skill in cases:
            self.assertFalse(
                self.extractor._validate_granularity(skill),
                f"Code snippet should be rejected: {skill.content[:40]}",
            )

    def test_22_json_fragment_rejected(self):
        """JSON/config fragments are rejected."""
        skill = ExtractedSkill(
            content='{"name": "test", "version": "1.0"}\nuse this config to deploy',
            section="preferences",
        )
        self.assertFalse(self.extractor._validate_granularity(skill))

    def test_23_tool_use_record_rejected(self):
        """Tool-use records are rejected."""
        cases = [
            ExtractedSkill(content="[tool-use] Called mcp server to run deploy action", section="preferences"),
            ExtractedSkill(content="Called mcp__memory_store to add and use it", section="preferences"),
        ]
        for skill in cases:
            self.assertFalse(
                self.extractor._validate_granularity(skill),
                f"Tool-use record should be rejected: {skill.content[:40]}",
            )

    def test_24_markdown_table_rejected(self):
        """Markdown tables are rejected."""
        skill = ExtractedSkill(
            content="| Pattern | Use |\n| --- | --- |\n| always check | apply fix |\n| never skip | run test |",
            section="preferences",
        )
        self.assertFalse(self.extractor._validate_granularity(skill))

    def test_25_code_heavy_content_rejected(self):
        """Content with >30% code chars is rejected."""
        skill = ExtractedSkill(
            content="function() { use(a); apply(b); run(c); return [d]; }",
            section="preferences",
        )
        self.assertFalse(self.extractor._validate_granularity(skill))

    def test_26_line_numbered_code_rejected(self):
        """cat -n style line-numbered code is rejected."""
        skill = ExtractedSkill(
            content="1→  import os\n2→  use run deploy\n3→  apply config",
            section="preferences",
        )
        self.assertFalse(self.extractor._validate_granularity(skill))

    def test_27_normal_skill_not_blocked(self):
        """Normal skill content is not falsely blocked by content filters."""
        skill = ExtractedSkill(
            content="Always use TypeScript for new frontend modules in the project",
            section="preferences",
        )
        self.assertTrue(self.extractor._validate_granularity(skill))


if __name__ == "__main__":
    unittest.main()
