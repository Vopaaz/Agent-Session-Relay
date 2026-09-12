from __future__ import annotations

import json
import os
from unittest.mock import patch

from tests.support import RepositoryTest

# isort: split
from agent_session_relay.core.errors import RelayError
from agent_session_relay.core.git import Git
from agent_session_relay.core.session import Relay


class BtwTests(RepositoryTest):
    def prepare_review(self):
        self.run_relay("start", "-m", "Review the changes")
        self.hook("prompt-submit")
        self.write("Token.kt", "agent proposal\n")
        self.write("Parser.kt", self.read("Parser.kt").replace("line 2\n", "accepted hunk\n"))
        self.hook("agent-stop")
        self.git("add", "Parser.kt")
        self.write("Parser.kt", self.read("Parser.kt").replace("line 30\n", "still pending\n"))
        self.write("Token.kt", "human direction\n")

    def test_readonly_btw_preserves_review_and_accumulates_human_changes(self):
        self.prepare_review()
        reviewed = self.state()["reviewed_checkpoint"]
        index = (self.repo / ".git/index").read_bytes()
        staged = self.git("diff", "--cached")
        for iteration in range(2):
            self.run_relay("btw")
            self.assertEqual(self.state()["next_turn"], "btw")
            # Arming does not snapshot: edits before submitting belong in the btw view.
            self.write("Config.kt", f"human config {iteration}\n")
            timestamps = {p: (self.repo / p).stat().st_mtime_ns for p in ("Token.kt", "Config.kt")}
            injection = self.hook("prompt-submit").stdout
            self.assertIn("BTW turn", injection)
            self.assertNotIn("Human changes (", injection)
            self.assertNotIn("Token.kt", injection)
            self.assertEqual(self.state()["turn_kind"], "btw")
            self.assertEqual(self.state()["next_turn"], "normal")
            self.assertEqual(self.state()["reviewed_checkpoint"], reviewed)
            self.assertEqual(self.git("rev-parse", "HEAD").strip(), reviewed)
            self.assertEqual(self.names("reviewed"), set())
            self.assertEqual(self.names("human"), {"Parser.kt", "Token.kt", "Config.kt"})
            self.assertIn("+human direction", self.run_relay("agent", "diff", "human").stdout)
            self.assertEqual(self.hook("prompt-submit").stdout, injection)
            self.assertEqual(self.hook("agent-stop").stdout, "")
            self.assertEqual(self.state()["btw_recoveries"], [])
            self.assertEqual((self.repo / ".git/index").read_bytes(), index)
            self.assertEqual(self.git("diff", "--cached"), staged)
            for name, timestamp in timestamps.items():
                self.assertEqual((self.repo / name).stat().st_mtime_ns, timestamp)
        injection = self.hook("prompt-submit").stdout
        self.assertIn('["Config.kt", "Parser.kt", "Token.kt"]', injection)
        self.assertNotIn("human direction", injection)
        self.assertEqual(self.state()["turn_kind"], "normal")
        self.assertEqual(self.names("reviewed"), {"Parser.kt"})
        self.assertEqual(self.git("diff", "--cached"), "")
        self.assertIn("+human direction", self.run_relay("agent", "diff", "human").stdout)

    def test_unexpected_writes_are_saved_then_restored_and_retrieved_as_agent_changes(self):
        self.prepare_review()
        self.write("new-human.txt", "human addition\n")
        self.write("intent.txt", "human intent-to-add\n")
        self.git("add", "-N", "intent.txt")
        index = (self.repo / ".git/index").read_bytes()
        parser = self.read("Parser.kt")
        self.run_relay("btw")
        self.hook("prompt-submit")
        self.write("Token.kt", "unexpected agent work\n")
        self.write("new-human.txt", "agent edit of human addition\n")
        self.write(".gitignore", "*.ignored\nnew-human.txt\n")
        self.write("new.bin", b"\x00btw binary\xff")
        self.write("run.sh", "#!/bin/sh\nexit 0\n")
        (self.repo / "run.sh").chmod(0o755)
        os.symlink("Token.kt", self.repo / "link")
        (self.repo / "Config.kt").unlink()
        self.stage_content("Parser.kt", "staged-only btw work\n")
        stop = self.hook("agent-stop").stdout
        self.assertIn("original workspace and staging were restored", stop)
        self.assertIn("relay agent diff btw --turn 2", stop)
        self.assertEqual(self.read("Token.kt"), "human direction\n")
        self.assertEqual(self.read("new-human.txt"), "human addition\n")
        self.assertEqual(self.read("Parser.kt"), parser)
        self.assertEqual(self.read("Config.kt"), "old config\n")
        self.assertEqual(self.read(".gitignore"), "*.ignored\n")
        self.assertEqual((self.repo / ".git/index").read_bytes(), index)
        for name in ("new.bin", "run.sh", "link"):
            self.assertFalse((self.repo / name).exists())
        recovery = self.state()["btw_recoveries"][0]
        self.assertEqual(recovery["turn"], 2)
        self.assertTrue(recovery["workspace_changes"])
        self.assertTrue(recovery["index_changes"])
        self.git("gc", "--prune=now")
        saved = self.run_relay("agent", "diff", "btw", "--turn", "2").stdout
        self.assertIn("-human direction", saved)
        self.assertIn("+unexpected agent work", saved)
        self.assertIn("GIT binary patch", saved)
        self.assertIn("new file mode 100755", saved)
        self.assertIn("new file mode 120000", saved)
        self.assertIn("+staged-only btw work", self.run_relay(
            "agent", "diff", "btw", "--turn", "2", "--staged",
        ).stdout)
        self.run_relay("agent", "restore-btw", "2", ok=False)
        # A duplicate Stop must leave subsequent human edits and approvals alone.
        self.write("README.md", "later human edit\n")
        self.git("add", "README.md")
        later_index = (self.repo / ".git/index").read_bytes()
        self.assertEqual(self.hook("agent-stop").stdout, "")
        self.assertEqual((self.repo / ".git/index").read_bytes(), later_index)
        self.assertEqual(self.read("README.md"), "later human edit\n")
        self.hook("prompt-submit")
        human = self.run_relay("agent", "diff", "human").stdout
        self.assertNotIn("unexpected agent work", human)
        self.assertIn("+human direction", human)
        self.assertIn("+later human edit", human)
        normal_index = (self.repo / ".git/index").read_bytes()
        self.run_relay("agent", "restore-btw", "2")
        self.assertEqual((self.repo / ".git/index").read_bytes(), normal_index)
        self.assertEqual(self.read("Token.kt"), "unexpected agent work\n")
        self.assertEqual(self.read("README.md"), "later human edit\n")
        self.assertEqual((self.repo / "new.bin").read_bytes(), b"\x00btw binary\xff")
        self.assertEqual(self.run_relay("agent", "diff", "human").stdout, human)
        self.assertEqual(self.git("diff", "--cached"), "")
        self.hook("agent-stop")
        self.hook("prompt-submit")
        self.assertEqual(self.names("human"), set())

    def test_staged_only_recovery_can_be_inspected_and_retrieved_unstaged(self):
        self.run_relay("start")
        self.run_relay("btw")
        self.hook("prompt-submit")
        self.stage_content("Token.kt", "saved only in index\n")
        self.hook("agent-stop")
        self.assertEqual(self.git("diff", "--cached"), "")
        self.assertEqual(self.run_relay("agent", "diff", "btw", "--turn", "1").stdout, "")
        self.git("gc", "--prune=now")
        self.assertIn("+saved only in index", self.run_relay(
            "agent", "diff", "btw", "--turn", "1", "--staged",
        ).stdout)
        self.hook("prompt-submit")
        self.run_relay("agent", "restore-btw", "1", "--staged")
        self.assertEqual(self.read("Token.kt"), "saved only in index\n")
        self.assertEqual(self.git("diff", "--cached"), "")

    def test_conflicting_retrieval_leaves_workspace_untouched(self):
        self.run_relay("start")
        self.run_relay("btw")
        self.hook("prompt-submit")
        self.write("Token.kt", "btw version\n")
        self.hook("agent-stop")
        self.write("Token.kt", "later human version\n")
        self.hook("prompt-submit")
        before = self.git("diff")
        error = self.run_relay("agent", "restore-btw", "1", ok=False).stderr
        self.assertIn("do not apply", error)
        self.assertEqual(self.git("diff"), before)

    def test_btw_can_be_cancelled_and_retains_mode_across_hook_retries_and_suspend(self):
        self.run_relay("btw", ok=False)
        self.run_relay("start")
        self.run_relay("btw")
        self.run_relay("btw", "--cancel")
        self.assertEqual(self.state()["next_turn"], "normal")
        self.run_relay("btw")
        self.run_relay("suspend")
        self.run_relay("resume")
        owner = {"session_id": "conversation-a"}
        injection = self.hook("prompt-submit", owner).stdout
        self.assertIn("BTW turn", injection)
        self.assertEqual(self.hook("prompt-submit", owner).stdout, injection)
        self.run_relay("btw", "--cancel", ok=False)
        self.hook("prompt-submit", {"session_id": "conversation-b"}, ok=False)
        self.hook("agent-stop", {"session_id": "conversation-b"}, ok=False)
        self.hook("agent-stop", owner)
        normal_injection = self.hook("prompt-submit", owner).stdout
        self.assertEqual(self.state()["turn_kind"], "normal")
        self.assertIn("Leave your changes unstaged", normal_injection)
        self.assertNotIn("read-only BTW turn", normal_injection)

    def test_finish_and_abort_remove_btw_snapshots_and_recovery_records(self):
        for command in ("finish", "abort"):
            with self.subTest(command=command):
                self.run_relay("start", "-m", "Complete review")
                self.run_relay("btw")
                self.hook("prompt-submit")
                self.write("Token.kt", "unwanted btw work\n")
                self.hook("agent-stop")
                self.assertTrue(self.state()["btw_recoveries"])
                self.run_relay(command, input="abort\n" if command == "abort" else None)
                self.assertEqual(self.git("for-each-ref", "refs/relay/"), "")
                self.assertEqual(self.metadata()["sessions"], {})
                self.assertNotIn("btw", self.git("branch", "--list"))

    def test_failure_to_save_btw_output_keeps_the_output_and_open_turn(self):
        self.run_relay("start")
        self.run_relay("btw")
        self.hook("prompt-submit")
        self.write("Token.kt", "valuable accidental work\n")
        relay = Relay(Git(self.repo))
        with patch.object(relay, "save_index", side_effect=RelayError("cannot save index")):
            with self.assertRaisesRegex(RelayError, "cannot save index"):
                relay.stop()
        self.assertEqual(self.read("Token.kt"), "valuable accidental work\n")
        self.assertEqual(self.state()["phase"], "agent")
        self.hook("agent-stop")
        self.assertEqual(self.read("Token.kt"), "TokenDefinition\n")
        self.assertIn("valuable accidental work", self.run_relay(
            "agent", "diff", "btw", "--turn", "1",
        ).stdout)

    def test_normal_human_path_injection_is_complete_quoted_and_has_no_patch(self):
        self.run_relay("start")
        names = ['new file.txt', 'line\nbreak.txt', '中文.txt']
        for name in names:
            self.write(name, "human body must stay out of hook\n")
        injection = self.hook("prompt-submit").stdout
        encoded = injection.split("Human changes (repository-relative paths, JSON):\n")[1]
        self.assertEqual(set(json.loads(encoded)), set(names))
        self.assertNotIn("human body must stay out of hook", injection)
        self.assertIn("line\\nbreak.txt", injection)

    def test_btw_diff_flags_and_literal_path_filtering(self):
        self.run_relay("start")
        self.run_relay("btw")
        self.hook("prompt-submit")
        self.write("dir/--name-only", "btw addition\n")
        self.write("Token.kt", "btw token\n")
        self.hook("agent-stop")
        (self.repo / "dir").mkdir(exist_ok=True)
        self.assertEqual(self.run_relay(
            "agent", "diff", "btw", "--turn", "1", "--name-only", "-z", "--", "--name-only",
            cwd=self.repo / "dir",
        ).stdout, "dir/--name-only\0")
        self.assertEqual(self.run_relay(
            "agent", "diff", "btw", "--turn", "1", "--name-only", "-z", "--", "Token.kt",
        ).stdout, "Token.kt\0")
        self.run_relay("agent", "diff", "btw", ok=False)
        self.run_relay("agent", "diff", "human", "--turn", "1", ok=False)
        self.run_relay("agent", "diff", "pending", "--staged", ok=False)
