import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


class MemCortexCliFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.home = Path(self.temp_dir.name) / ".memcortex-test"
        self.env = os.environ.copy()
        self.env["PYTHONDONTWRITEBYTECODE"] = "1"
        self.env["PYTHONPATH"] = str(REPO_ROOT / "src")
        self.env["MEMCORTEX_HOME"] = str(self.home)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _run_cli(self, *args):
        cmd = [sys.executable, "-m", "memcortex.cli", *args]
        proc = subprocess.run(
            cmd,
            cwd=REPO_ROOT,
            env=self.env,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            proc.returncode,
            0,
            msg="CLI failed: {}\\nstdout={}\\nstderr={}".format(" ".join(cmd), proc.stdout, proc.stderr),
        )
        try:
            return json.loads(proc.stdout.strip())
        except json.JSONDecodeError as exc:
            self.fail("Invalid JSON output: {}\\n{}".format(proc.stdout, exc))

    def test_capture_and_status(self):
        init = self._run_cli("init")
        self.assertTrue(init["ok"])

        cap = self._run_cli(
            "capture",
            "--source-tool",
            "claude_code",
            "--session-id",
            "sess_001",
            "--event-type",
            "tool_use_end",
            "--content",
            "word to markdown conversion succeeded",
            "--meta-json",
            "{\"project\":\"memcortex\"}",
        )
        self.assertTrue(cap["ok"])
        self.assertIn("event_id", cap)

        status = self._run_cli("status")
        self.assertEqual(status["backlog_count"], 1)
        self.assertEqual(status["pending_sync_count"], 0)

    def test_maybe_flush_respects_thresholds(self):
        self._run_cli("init")
        self._run_cli(
            "capture",
            "--source-tool",
            "cursor",
            "--session-id",
            "sess_002",
            "--event-type",
            "assistant_response",
            "--content",
            "short response",
            "--meta-json",
            "{\"project\":\"memcortex\"}",
        )

        result = self._run_cli("maybe-flush", "--local-only")
        self.assertFalse(result["flushed"])
        self.assertEqual(result["reason"], "threshold_not_met")
        self.assertEqual(result["stats"]["backlog_count"], 1)

    def test_force_flush_processes_and_syncs(self):
        self._run_cli("init")
        self._run_cli(
            "capture",
            "--source-tool",
            "claude_code",
            "--session-id",
            "sess_003",
            "--event-type",
            "error",
            "--content",
            "traceback sample",
            "--meta-json",
            "{\"project\":\"memcortex\",\"severity\":\"high\"}",
        )

        flush_result = self._run_cli("flush", "--force")
        self.assertEqual(flush_result["processed"], 1)
        self.assertEqual(flush_result["failed"], 0)
        self.assertGreaterEqual(flush_result["synced"], 1)

        status = self._run_cli("status")
        self.assertEqual(status["backlog_count"], 0)
        self.assertEqual(status["pending_sync_count"], 0)

        outbox = self.home / "state" / "sync-outbox.jsonl"
        self.assertTrue(outbox.exists(), "sync outbox should be created")
        content = outbox.read_text(encoding="utf-8").strip()
        self.assertTrue(content, "sync outbox should contain at least one batch")


if __name__ == "__main__":
    unittest.main()
