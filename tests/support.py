from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1]
CLI = SOURCE / "relay"


class RepositoryTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="relay-test-")
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name) / "repo"
        self.repo.mkdir()
        self.env = os.environ.copy()
        for key in list(self.env):
            if key.startswith("GIT_"):
                self.env.pop(key)
        self.env.update(
            {
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_SYSTEM": os.devnull,
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_OPTIONAL_LOCKS": "0",
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Relay Test")
        self.git("config", "user.email", "relay-test@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        self.write("Parser.kt", "".join(f"line {n}\n" for n in range(1, 41)))
        self.write("Token.kt", "TokenDefinition\n")
        self.write("Config.kt", "old config\n")
        self.write("README.md", "original documentation\n")
        self.write(".gitignore", "*.ignored\n")
        self.git("add", ".")
        self.git("commit", "-m", "base")
        self.base = self.git("rev-parse", "HEAD").strip()

    def git(self, *args, data=None, cwd=None, check=True):
        result = subprocess.run(
            ["git", *args], cwd=cwd or self.repo, env=self.env, input=data, capture_output=True
        )
        if check:
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        return result.stdout if data is not None else result.stdout.decode(errors="surrogateescape")

    def run_relay(self, *args, input=None, ok=True, cwd=None):
        result = subprocess.run(
            [sys.executable, str(CLI), *args],
            cwd=cwd or self.repo,
            env=self.env,
            input=input,
            capture_output=True,
            text=True,
        )
        if ok:
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout)
        return result

    def write(self, name, content):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")

    def read(self, name):
        return (self.repo / name).read_text(encoding="utf-8")

    def state(self):
        return json.loads(self.run_relay("status", "--json").stdout)

    def metadata(self):
        git_dir = Path(self.git("rev-parse", "--absolute-git-dir").strip())
        return json.loads((git_dir / "agent-session-relay/state.json").read_text())

    def hook(self, event, payload=None, ok=True):
        return self.run_relay("kiro", "hook", event, input=json.dumps(payload or {}), ok=ok)

    def stage_content(self, name, content):
        blob = self.git("hash-object", "-w", "--stdin", data=content.encode()).decode().strip()
        self.git("update-index", "--add", "--cacheinfo", "100644", blob, name)

    def names(self, kind, *paths):
        args = ["agent", "diff", kind, "--name-only"]
        if paths:
            args += ["--", *paths]
        return set(self.run_relay(*args).stdout.splitlines())

    def assert_clean(self):
        self.assertEqual(self.git("status", "--porcelain", "--untracked-files=all"), "")

    def assert_one_commit(self, branch, tree=None):
        self.assertEqual(
            self.git("rev-list", "--parents", "-n", "1", branch).split()[1:], [self.base]
        )
        self.assertEqual(self.git("rev-list", "--count", f"{self.base}..{branch}").strip(), "1")
        if tree:
            self.assertEqual(self.git("rev-parse", branch + "^{tree}").strip(), tree)
