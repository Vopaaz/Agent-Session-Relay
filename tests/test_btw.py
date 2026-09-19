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
    def backup_ref(self, number, suffix="post"):
        return f"refs/relay/sessions/{self.state()['session']}/turn-{number:06d}-{suffix}"

    def saved_diff(self, number, *, staged=False):
        suffix = "-staged" if staged else ""
        return self.run_relay(
            "agent", "git", "diff", "--binary",
            self.backup_ref(number, "pre" + suffix), self.backup_ref(number, "post" + suffix),
        ).stdout

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
            self.assertNotIn("Approvals are sealed", injection)
            self.assertNotIn("Human changes (", injection)
            self.assertNotIn("Token.kt", injection)
            self.assertEqual(self.state()["turn_kind"], "btw")
            self.assertEqual(self.state()["next_turn"], "normal")
            self.assertEqual(self.state()["reviewed_checkpoint"], reviewed)
            self.assertEqual(self.git("rev-parse", "HEAD").strip(), reviewed)
            self.assertEqual(self.names("reviewed"), set())
            status = json.loads(self.run_relay("agent", "status").stdout)
            self.assertEqual(status["turn_kind"], "btw")
            self.assertFalse(status["provenance"]["newly_reviewed"])
            self.assertTrue(status["provenance"]["human_edits"])
            self.assertEqual(self.names("session"), {"Parser.kt", "Token.kt", "Config.kt"})
            self.assertEqual(self.names("human"), {"Parser.kt", "Token.kt", "Config.kt"})
            self.assertIn("+human direction", self.run_relay("agent", "diff", "human").stdout)
            continued = self.hook("prompt-submit").stdout
            self.assertIn("No new human-to-agent handoff has occurred", continued)
            self.assertIn("read-only BTW turn", continued)
            self.assertEqual(self.hook("prompt-submit").stdout, continued)
            self.assertEqual(self.hook("agent-stop").stdout, "")
            self.assertNotIn("btw_recoveries", self.state())
            self.assertEqual(self.git("for-each-ref", self.backup_ref(iteration + 2)), "")
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

    def test_unexpected_writes_are_saved_then_excluded_from_relay_state(self):
        self.prepare_review()
        self.write("new-human.txt", "human addition\n")
        self.write("intent.txt", "human intent-to-add\n")
        self.git("add", "-N", "intent.txt")
        index = (self.repo / ".git/index").read_bytes()
        parser = self.read("Parser.kt")
        self.run_relay("btw")
        self.hook("prompt-submit")
        views = {kind: self.run_relay("agent", "diff", kind).stdout for kind in (
            "session", "reviewed", "human", "pending",
        )}
        status = json.loads(self.run_relay("agent", "status").stdout)
        last_post = self.metadata()["sessions"][self.state()["session"]]["last_post"]
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
        workspace_commit = self.git("rev-parse", self.backup_ref(2)).strip()
        staging_commit = self.git("rev-parse", self.backup_ref(2, "post-staged")).strip()
        self.assertEqual(stop, (
            "Project file writes or staging changes detected during a read-only BTW turn.\n"
            f"Workspace changes were saved in snapshot {workspace_commit}.\n"
            f"Staging changes were saved in snapshot {staging_commit}.\n"
            "The original workspace and staging state from the last human-to-agent handoff "
            "were restored.\n"
        ))
        self.assertNotIn("relay agent", stop)
        self.assertEqual(self.read("Token.kt"), "human direction\n")
        self.assertEqual(self.read("new-human.txt"), "human addition\n")
        self.assertEqual(self.read("Parser.kt"), parser)
        self.assertEqual(self.read("Config.kt"), "old config\n")
        self.assertEqual(self.read(".gitignore"), "*.ignored\n")
        self.assertEqual((self.repo / ".git/index").read_bytes(), index)
        for name in ("new.bin", "run.sh", "link"):
            self.assertFalse((self.repo / name).exists())
        self.assertEqual(json.loads(self.run_relay("agent", "status").stdout), {
            **status, "phase": "human",
        })
        self.assertNotIn("btw_recoveries", self.state())
        self.assertNotIn("Saved", self.run_relay("status").stdout)
        session = self.metadata()["sessions"][self.state()["session"]]
        self.assertEqual(session["last_post"], last_post)
        self.assertIsNone(session["turns"][-1]["post"])
        self.assertNotIn("recovery", session["turns"][-1])
        self.assertNotIn("post_index", session["turns"][-1])
        # Generic Git can discover and inspect the backups, even after suspend/resume and GC.
        backup = self.backup_ref(2)
        self.assertIn(backup, self.run_relay(
            "agent", "git", "for-each-ref", "refs/relay/",
        ).stdout)
        self.run_relay("suspend")
        self.git("gc", "--prune=now")
        self.run_relay("resume")
        for kind, expected in views.items():
            self.assertEqual(self.run_relay("agent", "diff", kind).stdout, expected)
        saved = self.saved_diff(2)
        self.assertIn("-human direction", saved)
        self.assertIn("+unexpected agent work", saved)
        self.assertIn("GIT binary patch", saved)
        self.assertIn("new file mode 100755", saved)
        self.assertIn("new file mode 120000", saved)
        self.assertIn("+staged-only btw work", self.saved_diff(2, staged=True))
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
        for kind in views:
            current = self.run_relay("agent", "diff", kind).stdout
            self.assertNotIn("unexpected agent work", current)
            self.assertNotIn("staged-only btw work", current)
            self.assertNotIn("new.bin", current)
        self.assertEqual(self.read("Token.kt"), "human direction\n")
        self.assertEqual(self.read("README.md"), "later human edit\n")
        self.assertFalse((self.repo / "new.bin").exists())
        self.assertEqual(self.run_relay("agent", "diff", "human").stdout, human)
        self.assertEqual(self.git("diff", "--cached"), "")
        self.hook("agent-stop")
        self.hook("prompt-submit")
        self.assertEqual(self.names("human"), set())

    def test_staged_only_backup_is_saved_without_affecting_review(self):
        self.run_relay("start")
        self.run_relay("btw")
        self.hook("prompt-submit")
        self.stage_content("Token.kt", "saved only in index\n")
        stop = self.hook("agent-stop").stdout
        staging_commit = self.git("rev-parse", self.backup_ref(1, "post-staged")).strip()
        self.assertIn(f"Staging changes were saved in snapshot {staging_commit}.", stop)
        self.assertNotIn("Workspace changes were saved", stop)
        self.assertEqual(self.run_relay(
            "agent", "git", "show", staging_commit + ":Token.kt",
        ).stdout, "saved only in index\n")
        self.assertEqual(self.git("diff", "--cached"), "")
        self.assertEqual(self.saved_diff(1), "")
        self.git("gc", "--prune=now")
        self.assertIn("+saved only in index", self.saved_diff(1, staged=True))
        self.hook("prompt-submit")
        self.assertEqual(self.read("Token.kt"), "TokenDefinition\n")
        self.assertEqual(self.git("diff", "--cached"), "")
        for kind in ("session", "reviewed", "human", "pending"):
            self.assertEqual(self.names(kind), set())

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
        continued = self.hook("prompt-submit", owner).stdout
        self.assertIn("No new human-to-agent handoff has occurred", continued)
        self.assertIn("read-only BTW turn", continued)
        self.assertEqual(self.hook("prompt-submit", owner).stdout, continued)
        self.run_relay("btw", "--cancel", ok=False)
        self.hook("prompt-submit", {"session_id": "conversation-b"}, ok=False)
        self.hook("agent-stop", {"session_id": "conversation-b"}, ok=False)
        self.hook("agent-stop", owner)
        normal_injection = self.hook("prompt-submit", owner).stdout
        self.assertEqual(self.state()["turn_kind"], "normal")
        self.assertIn("Leave your changes unstaged", normal_injection)
        self.assertNotIn("read-only BTW turn", normal_injection)

    def test_finish_and_abort_remove_btw_snapshots_without_including_their_output(self):
        for command in ("finish", "abort"):
            with self.subTest(command=command):
                self.run_relay("start", "-m", "Complete review")
                self.run_relay("btw")
                self.hook("prompt-submit")
                self.write("Token.kt", "unwanted btw work\n")
                self.hook("agent-stop")
                self.assertIn("unwanted btw work", self.saved_diff(1))
                sid = self.state()["session"]
                self.run_relay(command, input="abort\n" if command == "abort" else None)
                self.assertEqual(self.read("Token.kt"), "TokenDefinition\n")
                branch = f"relay/{'result' if command == 'finish' else 'aborted'}/{sid}"
                self.assertEqual(self.git("show", branch + ":Token.kt"), "TokenDefinition\n")
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
        stop = self.hook("agent-stop").stdout
        workspace_commit = self.git("rev-parse", self.backup_ref(1)).strip()
        self.assertIn(f"Workspace changes were saved in snapshot {workspace_commit}.", stop)
        self.assertNotIn("Staging changes were saved", stop)
        self.assertEqual(self.read("Token.kt"), "TokenDefinition\n")
        self.assertIn("valuable accidental work", self.saved_diff(1))

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

    def test_btw_has_only_mode_selection_and_cancellation_commands(self):
        self.run_relay("start")
        self.run_relay("btw")
        self.hook("prompt-submit")
        self.write("Token.kt", "valuable accidental work\n")
        before = self.metadata()
        for args in (
            ("agent", "restore-btw", "1"),
            ("agent", "restore-btw", "1", "--staged"),
            ("agent", "diff", "btw"),
            ("agent", "diff", "btw", "--turn", "1"),
            ("agent", "diff", "human", "--turn", "1"),
            ("agent", "diff", "pending", "--staged"),
        ):
            with self.subTest(args=args):
                result = self.run_relay(*args, ok=False)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
        self.assertEqual(self.metadata(), before)
        self.assertEqual(self.read("Token.kt"), "valuable accidental work\n")
        for args in (("agent", "--help"), ("agent", "diff", "--help")):
            help_text = self.run_relay(*args).stdout
            for removed in ("btw", "--turn", "--staged"):
                self.assertNotIn(removed, help_text)
