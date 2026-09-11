from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

from tests.support import SOURCE, RepositoryTest


class DistributionTests(RepositoryTest):
    @classmethod
    def setUpClass(cls):
        cls.build_directory = tempfile.TemporaryDirectory(prefix="relay-distribution-")
        cls.addClassCleanup(cls.build_directory.cleanup)
        cls.archive = Path(cls.build_directory.name) / "relay executable.pyz"
        subprocess.run(
            [sys.executable, str(SOURCE / "scripts/build_zipapp.py"), "--output", str(cls.archive)],
            check=True,
            capture_output=True,
        )

    def packed(self, *args, input=None, ok=True):
        result = subprocess.run(
            [sys.executable, str(self.archive), *args],
            cwd=self.repo,
            env=self.env,
            capture_output=True,
            text=True,
            input=input,
        )
        self.assertEqual(result.returncode == 0, ok, result.stdout + result.stderr)
        return result

    def test_standalone_archive_runs_a_complete_session(self):
        self.packed("start", "-m", "Update standalone token handling")
        status = json.loads(self.packed("agent", "status").stdout)
        self.packed("kiro", "hook", "prompt-submit", input="{}")
        self.write("Token.kt", "standalone proposal")
        self.packed("kiro", "hook", "agent-stop", input="{}")
        self.assertEqual(
            self.packed("agent", "diff", "pending", "--name-only").stdout, "Token.kt\n"
        )
        self.packed("finish", ok=False)
        self.git("add", "Token.kt")
        self.packed("message", "-m", "Refine standalone token handling")
        self.packed("finish")
        self.assert_one_commit("relay/result/" + status["session"])
        self.assertEqual(self.git("log", "-1", "--format=%s"), "Refine standalone token handling\n")
        self.assert_clean()

    def test_standalone_archive_preserves_guard_exit_code(self):
        self.packed("start")
        result = self.packed(
            "kiro",
            "hook",
            "pre-tool-use",
            input=json.dumps({"tool_name": "shell", "tool_input": {"command": "git status"}}),
            ok=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
