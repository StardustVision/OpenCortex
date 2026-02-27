# SPDX-License-Identifier: Apache-2.0
"""
OpenAI-compatible Dense Embedder for OpenCortex.

Works with any OpenAI-compatible embedding API: OpenAI, vLLM, Ollama,
DeepSeek, Together, or any self-hosted service exposing POST /v1/embeddings.

Zero extra SDK dependencies — uses httpx directly.
"""

import logging
from typing import List, Optional

import httpx

from opencortex.models.embedder.base import (
    DenseEmbedderBase,
    EmbedResult,
    truncate_and_normalize,
)

logger = logging.getLogger(__name__)


class OpenAIDenseEmbedder(DenseEmbedderBase):
    """Dense embedder using any OpenAI-compatible /v1/embeddings endpoint.

    Args:
        model_name: Model identifier (e.g. "text-embedding-3-small").
        api_key: Bearer token for authentication.
        api_base: Base URL ending with /v1 (default: https://api.openai.com/v1).
        dimension: Target dimension for truncation. If None, auto-detected on
            first embed call.
    """

    def __init__(
        self,
        model_name: str,
        api_key: str,
        api_base: str = "https://api.openai.com/v1",
        dimension: Optional[int] = None,
    ):
        super().__init__(model_name)
        self.api_key = api_key
        self.api_base = api_base.rstrip("/")
        self.dimension = dimension
        self._dimension: Optional[int] = dimension

        if not self.api_key:
            raise ValueError("api_key is required for OpenAIDenseEmbedder")

        self._client = httpx.Client(timeout=60.0)

    def _post_embeddings(self, input_data) -> dict:
        """POST to /embeddings and return the parsed JSON response."""
        url = f"{self.api_base}/embeddings"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model_name,
            "input": input_data,
        }
        resp = self._client.post(url, headers=headers, json=payload)
        if resp.status_code >= 400:
            raise RuntimeError(
                f"OpenAI embedding API error {resp.status_code}: {resp.text}"
            )
        return resp.json()

    def embed(self, text: str) -> EmbedResult:
        """Embed a single text string.

        Args:
            text: Input text.

        Returns:
            EmbedResult with dense_vector.
        """
        data = self._post_embeddings(text)
        vector = data["data"][0]["embedding"]

        # Auto-detect dimension on first successful call
        if self._dimension is None:
            self._dimension = len(vector)
            logger.info(
                "[OpenAIDenseEmbedder] Auto-detected dimension: %d",
                self._dimension,
            )

        vector = truncate_and_normalize(vector, self.dimension)
        return EmbedResult(dense_vector=vector)

    def embed_batch(self, texts: List[str]) -> List[EmbedResult]:
        """Batch-embed multiple texts in a single API call.

        Args:
            texts: List of input texts.

        Returns:
            List of EmbedResult in the same order as inputs.
        """
        if not texts:
            return []

        data = self._post_embeddings(texts)

        # API may return items out of order — sort by index
        items = sorted(data["data"], key=lambda x: x["index"])
        results = []
        for item in items:
            vector = item["embedding"]

            if self._dimension is None:
                self._dimension = len(vector)
                logger.info(
                    "[OpenAIDenseEmbedder] Auto-detected dimension: %d",
                    self._dimension,
                )

            vector = truncate_and_normalize(vector, self.dimension)
            results.append(EmbedResult(dense_vector=vector))

        return results

    def get_dimension(self) -> int:
        """Return embedding dimension.

        If dimension was not provided at init time and no embed call has been
        made yet, performs a probe embed to auto-detect.
        """
        if self._dimension is None:
            self.embed("dimension detection probe")
        return self._dimension  # type: ignore[return-value]

    def close(self):
        """Close the underlying httpx client."""
        self._client.close()
