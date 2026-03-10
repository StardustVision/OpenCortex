import unittest
from opencortex.config import CortexConfig, CortexAlphaConfig


class TestAlphaConfig(unittest.TestCase):

    def test_default_alpha_config(self):
        cfg = CortexConfig()
        self.assertIsNotNone(cfg.cortex_alpha)
        self.assertTrue(cfg.cortex_alpha.observer_enabled)
        self.assertTrue(cfg.cortex_alpha.trace_splitter_enabled)
        self.assertEqual(cfg.cortex_alpha.archivist_trigger_threshold, 20)
        self.assertEqual(cfg.cortex_alpha.archivist_max_delay_hours, 24)
        self.assertEqual(cfg.cortex_alpha.sandbox_min_traces, 3)
        self.assertEqual(cfg.cortex_alpha.sandbox_min_success_rate, 0.7)
        self.assertTrue(cfg.cortex_alpha.sandbox_require_human_approval)

    def test_alpha_config_from_dict(self):
        cfg = CortexAlphaConfig(
            observer_enabled=False,
            archivist_trigger_threshold=10,
            sandbox_min_traces=5,
        )
        self.assertFalse(cfg.observer_enabled)
        self.assertEqual(cfg.archivist_trigger_threshold, 10)

    def test_cortex_config_serialization_includes_alpha(self):
        cfg = CortexConfig()
        d = cfg.to_dict()
        self.assertIn("cortex_alpha", d)
        self.assertIsInstance(d["cortex_alpha"], dict)

    def test_max_context_tokens_default(self):
        cfg = CortexAlphaConfig()
        self.assertEqual(cfg.trace_splitter_max_context_tokens, 128000)


if __name__ == "__main__":
    unittest.main()
