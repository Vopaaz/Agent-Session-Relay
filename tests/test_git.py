from __future__ import annotations

import os
import time
from unittest.mock import patch

from tests.support import RepositoryTest, injected_stats

# isort: split
from agent_session_relay.core.git import Git


class GitIndexTests(RepositoryTest):
    def prepare_racy_index(self, *, split=False):
        # Simulate a same-size edit within the filesystem's timestamp resolution.
        # Disabling ctime checks also makes the reproduction portable across Git builds.
        self.git("config", "core.checkStat", "minimal")
        self.git("config", "core.trustctime", "false")
        timestamp = (int(time.time()) - 10) * 1_000_000_000
        path = self.repo / "Config.kt"
        self.write("Config.kt", "old config\n")
        os.utime(path, ns=(timestamp, timestamp))
        self.git("add", "Config.kt")
        if split:
            self.git("update-index", "--split-index")
        os.utime(self.repo / ".git/index", ns=(timestamp, timestamp))
        return timestamp

    def edit_with_same_stat(self, timestamp):
        self.write("Config.kt", "human edit\n")
        os.utime(self.repo / "Config.kt", ns=(timestamp, timestamp))

    def test_workspace_snapshot_detects_same_stat_edits_without_changing_the_index(self):
        for version, split in ((2, False), (4, False), (2, True)):
            with self.subTest(version=version, split=split):
                self.git("update-index", f"--index-version={version}")
                timestamp = self.prepare_racy_index(split=split)
                index = self.repo / ".git/index"
                before = index.read_bytes()
                self.edit_with_same_stat(timestamp)
                self.assertEqual(self.git("diff-files", "--name-only"), "Config.kt\n")
                with patch.dict(os.environ, self.env, clear=True):
                    git = Git(self.repo)
                    tree = git.workspace_tree()
                self.assertEqual(self.git("show", tree + ":Config.kt"), "human edit\n")
                self.assertEqual(index.read_bytes(), before)
                self.assertEqual(index.stat().st_mtime_ns, timestamp)
                self.assertEqual(self.git("show", ":Config.kt"), "old config\n")

    def test_restored_index_does_not_hide_same_stat_edits(self):
        timestamp = self.prepare_racy_index()
        index = (self.repo / ".git/index").read_bytes()
        self.edit_with_same_stat(timestamp)
        with patch.dict(os.environ, self.env, clear=True):
            git = Git(self.repo)
            git.restore_index(index)
            self.assertEqual(git.index_bytes(), index)
            self.assertEqual(git.text("diff-files", "--name-only"), "Config.kt")
            tree = git.workspace_tree()
        self.assertEqual(self.git("show", tree + ":Config.kt"), "human edit\n")
        self.assertEqual(self.git("show", ":Config.kt"), "old config\n")

    def test_btw_preserves_same_stat_human_edits_for_the_next_normal_turn(self):
        self.run_relay("start")
        timestamp = self.prepare_racy_index()
        index = (self.repo / ".git/index").read_bytes()
        self.edit_with_same_stat(timestamp)
        self.run_relay("btw")
        self.hook("prompt-submit")
        self.assertIn("+human edit", self.run_relay("agent", "diff", "human").stdout)
        self.assertEqual(self.hook("agent-stop").stdout, "")
        self.assertEqual(self.read("Config.kt"), "human edit\n")
        self.assertEqual((self.repo / ".git/index").read_bytes(), index)
        normal = self.hook("prompt-submit").stdout
        self.assertEqual(injected_stats(normal, "Human"), {"Config.kt": (1, 1)})
        self.assertEqual(self.read("Config.kt"), "human edit\n")
