# tests/test_perf_fixes.py
import asyncio
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch


def _make_oc(provider: str, model: str = "text-embedding-3-small"):
    """Return a MemoryOrchestrator instance bypassing __init__ for unit tests."""
    from opencortex.orchestrator import MemoryOrchestrator
    from opencortex.config import CortexConfig
    oc = MemoryOrchestrator.__new__(MemoryOrchestrator)
    oc._config = CortexConfig(
        embedding_provider=provider,
        embedding_model=model,
        embedding_api_key="test-key",
    )
    return oc


class TestEmbedderCache(unittest.TestCase):
    def _run_with_mocked_provider(self, oc, module_name: str, class_name: str):
        """
        Inject a mock module for `module_name` and spy on _wrap_with_cache.
        Returns the MagicMock assigned to `oc._wrap_with_cache`.
        """
        mock_wrap = MagicMock(side_effect=lambda e: e)
        oc._wrap_with_cache = mock_wrap  # instance override shadows class method

        mock_mod = MagicMock()
        setattr(mock_mod, class_name, MagicMock(return_value=MagicMock()))
        with patch.dict("sys.modules", {module_name: mock_mod}):
            oc._create_default_embedder()
        return mock_wrap

    def test_volcengine_embedder_wrapped_with_cache(self):
        oc = _make_oc("volcengine", "ep-test-model")
        mock_wrap = self._run_with_mocked_provider(
            oc,
            "opencortex.models.embedder.volcengine_embedders",
            "VolcengineDenseEmbedder",
        )
        mock_wrap.assert_called_once()

    def test_openai_embedder_wrapped_with_cache(self):
        oc = _make_oc("openai")
        mock_wrap = self._run_with_mocked_provider(
            oc,
            "opencortex.models.embedder.openai_embedder",
            "OpenAIDenseEmbedder",
        )
        mock_wrap.assert_called_once()
