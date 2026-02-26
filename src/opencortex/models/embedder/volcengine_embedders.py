# SPDX-License-Identifier: Apache-2.0
"""
Volcengine Embedding implementations for OpenCortex.

Supports doubao-embedding models via the Volcengine Ark SDK.
Reads configuration from ~/.openviking/ov.conf or CortexConfig.
"""

import logging
import math
from typing import Any, Dict, List, Optional

from opencortex.models.embedder.base import (
    DenseEmbedderBase,
    EmbedResult,
    truncate_and_normalize,
)

logger = logging.getLogger(__name__)


def _ensure_ark_sdk():
    """Lazily import volcenginesdkarkruntime."""
    try:
        import volcenginesdkarkruntime

        return volcenginesdkarkruntime
    except ImportError:
        raise ImportError(
            "volcenginesdkarkruntime is required for Volcengine embedding. "
            "Install with: uv pip install volcengine-python-sdk"
        )


class VolcengineDenseEmbedder(DenseEmbedderBase):
    """Volcengine Dense Embedder for OpenCortex.

    Uses the doubao-embedding models via the Volcengine Ark SDK.
    Supports both text-only and multimodal input modes.

    Args:
        model_name: Volcengine model name (e.g. "doubao-embedding-vision-250615")
        api_key: API key for authentication
        api_base: API base URL (default: https://ark.cn-beijing.volces.com/api/v3)
        dimension: Target dimension for truncation (auto-detected if None)
        input_type: "text" or "multimodal" (default: "multimodal")
    """

    def __init__(
        self,
        model_name: str,
        api_key: str,
        api_base: str = "https://ark.cn-beijing.volces.com/api/v3",
        dimension: Optional[int] = None,
        input_type: str = "multimodal",
    ):
        super().__init__(model_name)
        self.api_key = api_key
        self.api_base = api_base
        self.dimension = dimension
        self.input_type = input_type

        if not self.api_key:
            raise ValueError("api_key is required for VolcengineDenseEmbedder")

        sdk = _ensure_ark_sdk()
        self.client = sdk.Ark(api_key=self.api_key, base_url=self.api_base)

        # Auto-detect dimension if not provided
        self._dimension = dimension
        if self._dimension is None:
            self._dimension = self._detect_dimension()

    def _detect_dimension(self) -> int:
        """Detect dimension by making an actual API call."""
        try:
            result = self.embed("dimension detection probe")
            if result.dense_vector:
                dim = len(result.dense_vector)
                logger.info(
                    "[VolcengineDenseEmbedder] Auto-detected dimension: %d", dim
                )
                return dim
        except Exception as e:
            logger.warning(
                "[VolcengineDenseEmbedder] Dimension detection failed: %s, "
                "defaulting to 2048",
                e,
            )
        return 2048

    def embed(self, text: str) -> EmbedResult:
        """Embed single text.

        Args:
            text: Input text

        Returns:
            EmbedResult with dense_vector
        """
        try:
            if self.input_type == "multimodal":
                response = self.client.multimodal_embeddings.create(
                    input=[{"type": "text", "text": text}],
                    model=self.model_name,
                )
                vector = response.data.embedding
            else:
                response = self.client.embeddings.create(
                    input=text,
                    model=self.model_name,
                )
                vector = response.data[0].embedding

            vector = truncate_and_normalize(vector, self.dimension)
            return EmbedResult(dense_vector=vector)
        except Exception as e:
            raise RuntimeError(f"Volcengine embedding failed: {e}") from e

    def embed_batch(self, texts: List[str]) -> List[EmbedResult]:
        """Batch embedding.

        For multimodal models (doubao-embedding-vision), the API returns
        a single merged embedding per call, so we must call once per text.
        For text-only models, the standard batch API is used.

        Args:
            texts: List of input texts

        Returns:
            List of EmbedResult
        """
        if not texts:
            return []

        if self.input_type == "multimodal":
            # Multimodal API returns one embedding per call; loop individually
            return [self.embed(text) for text in texts]

        # Text-only batch API
        try:
            response = self.client.embeddings.create(
                input=texts,
                model=self.model_name,
            )
            return [
                EmbedResult(
                    dense_vector=truncate_and_normalize(
                        item.embedding, self.dimension
                    )
                )
                for item in response.data
            ]
        except Exception as e:
            raise RuntimeError(f"Volcengine batch embedding failed: {e}") from e

    def get_dimension(self) -> int:
        return self._dimension


def create_embedder_from_ov_conf(
    conf_path: Optional[str] = None,
) -> VolcengineDenseEmbedder:
    """Create a VolcengineDenseEmbedder from ~/.openviking/ov.conf.

    Args:
        conf_path: Path to ov.conf (default: ~/.openviking/ov.conf)

    Returns:
        Configured VolcengineDenseEmbedder
    """
    import json
    from pathlib import Path

    if conf_path is None:
        conf_path = str(Path.home() / ".openviking" / "ov.conf")

    with open(conf_path, "r") as f:
        conf = json.load(f)

    embedding_conf = conf.get("embedding", {}).get("dense", {})
    if not embedding_conf:
        raise ValueError(f"No embedding.dense config found in {conf_path}")

    return VolcengineDenseEmbedder(
        model_name=embedding_conf["model"],
        api_key=embedding_conf["api_key"],
        api_base=embedding_conf.get(
            "api_base", "https://ark.cn-beijing.volces.com/api/v3"
        ),
        dimension=embedding_conf.get("dimension"),
        input_type=embedding_conf.get("input", "multimodal"),
    )
