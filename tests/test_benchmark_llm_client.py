import asyncio
import os
import sys
import unittest

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from benchmarks.llm_client import LLMClient


class _StubClient:
    def __init__(self, response: httpx.Response):
        self._response = response

    async def post(self, *args, **kwargs):
        return self._response

    async def aclose(self):
        return None


class TestBenchmarkLLMClient(unittest.TestCase):
    def test_complete_raises_runtime_error_with_http_body(self):
        async def run():
            client = LLMClient(
                base="https://api.example.com",
                key="test-key",
                model="test-model",
            )
            client._client = _StubClient(
                httpx.Response(
                    500,
                    text="backend exploded",
                    request=httpx.Request("POST", "https://api.example.com/chat/completions"),
                )
            )
            with self.assertRaisesRegex(RuntimeError, "LLM HTTP 500: backend exploded"):
                await client.complete("hello", retries=1)

        asyncio.run(run())

    def test_complete_raises_runtime_error_on_non_json_body(self):
        async def run():
            client = LLMClient(
                base="https://api.example.com",
                key="test-key",
                model="test-model",
            )
            client._client = _StubClient(
                httpx.Response(
                    200,
                    text="not json",
                    request=httpx.Request("POST", "https://api.example.com/chat/completions"),
                )
            )
            with self.assertRaisesRegex(RuntimeError, "LLM returned non-JSON"):
                await client.complete("hello", retries=1)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
