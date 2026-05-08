# SPDX-License-Identifier: Apache-2.0
"""Tests for opencortex_app LLM client helpers."""

from __future__ import annotations

import unittest

from opencortex_app.llm.client import (
    LAYER_DERIVATION_SYSTEM_PROMPT,
    LLMCompletion,
    LLMConfig,
    resolve_api_style,
)


class TestLLMClient(unittest.IsolatedAsyncioTestCase):
    """Verify required LLM configuration and hard-coded system prompt."""

    async def test_llm_config_requires_api_key(self) -> None:
        """LLM config fails fast without a key."""
        with self.assertRaises(ValueError):
            LLMConfig(api_key="", system_prompt="prompt")

    async def test_openai_payload_uses_system_message(self) -> None:
        """OpenAI-compatible payloads include the built-in system prompt."""
        completion = LLMCompletion(
            LLMConfig(
                api_key="key",
            )
        )
        try:
            payload = completion.payload("user prompt", temperature=0.0)
        finally:
            await completion.close()

        self.assertEqual(
            payload["messages"],
            [
                {"role": "system", "content": LAYER_DERIVATION_SYSTEM_PROMPT},
                {"role": "user", "content": "user prompt"},
            ],
        )

    async def test_auto_style_detects_anthropic(self) -> None:
        """Auto style follows Anthropic hostnames."""
        self.assertEqual(
            resolve_api_style("auto", "https://api.anthropic.com/v1"),
            "anthropic",
        )
