from __future__ import annotations

from unittest.mock import patch

from tests.support import RepositoryTest, injected_stats

# isort: split
from agent_session_relay.core.git import Git
from agent_session_relay.core.session import Relay

COMMAND = "end-interrupted-agent-turn"


class InterruptedTurnTests(RepositoryTest):
    def review_state(self):
        return {
            "metadata": self.metadata(),
            "head": self.git("rev-parse", "HEAD"),
            "head_ref": self.git("symbolic-ref", "-q", "HEAD", check=False),
            "refs": self.git("for-each-ref", "refs/relay/"),
            "index": (self.repo / ".git/index").read_bytes(),
            "workspace": self.git("diff-files", "--patch", "--binary"),
            "untracked": self.git("ls-files", "--others", "--exclude-standard"),
        }

    def test_continuation_preserves_original_handoff_and_does_not_reannounce_it(self):
        self.run_relay("start")
        owner = {"session_id": "conversation-a"}
        self.hook("prompt-submit", owner)
        self.write("Token.kt", "approved work\n")
        self.hook("agent-stop", owner)
        self.git("add", "Token.kt")
        self.write("Config.kt", "human direction\n")
        initial = self.hook("prompt-submit", owner).stdout
        self.assertEqual(injected_stats(initial, "Approved"), {"Token.kt": (1, 1)})
        self.assertIn("Human edits at turn start (", initial)
        self.assertNotIn("Continuing the open turn", initial)
        self.write("Parser.kt", "partial agent work\n")
        self.stage_content("Parser.kt", "agent staging\n")
        before = self.review_state()
        provenance = {
            kind: self.run_relay("agent", "diff", kind).stdout
            for kind in ("human", "reviewed")
        }

        continued = self.hook("prompt-submit", {**owner, "prompt": "Try another approach"}).stdout
        self.assertIn("Continuing the open turn", continued)
        self.assertIn("reviewed/human diffs still refer to its original handoff", continued)
        self.assertIn("Leave your changes unstaged", continued)
        self.assertIn("Do not run Git directly", continued)
        self.assertNotIn("relay agent diff human", continued)
        self.assertNotIn("Approvals are sealed", continued)
        self.assertFalse(injected_stats(continued, "Human"))
        self.assertFalse(injected_stats(continued, "Approved"))
        self.assertEqual(self.hook("prompt-submit", owner).stdout, continued)
        self.assertEqual(self.review_state(), before)
        for kind, expected in provenance.items():
            self.assertEqual(self.run_relay("agent", "diff", kind).stdout, expected)
        self.assertEqual(self.read("Parser.kt"), "partial agent work\n")

        self.assertIn("Another agent conversation", self.hook(
            "prompt-submit", {"session_id": "conversation-b"}, ok=False,
        ).stderr)
        self.assertEqual(self.review_state(), before)

    def test_manual_end_preserves_code_and_establishes_the_next_human_boundary(self):
        self.run_relay("start")
        owner = {"session_id": "conversation-a"}
        self.hook("prompt-submit", owner)
        self.write("Token.kt", "partial agent work\n")
        self.write("new.bin", b"\x00partial output\xff")
        self.git("add", "Token.kt")
        result = self.run_relay(COMMAND)
        self.assertIn("Relay is now in the human phase", result.stdout)
        state = self.state()
        self.assertEqual((state["phase"], state["turn"]), ("human", 1))
        self.assertEqual(state["reviewed_checkpoint"], self.base)
        self.assertEqual(self.git("diff", "--cached"), "")
        self.assertEqual(self.read("Token.kt"), "partial agent work\n")
        self.assertEqual((self.repo / "new.bin").read_bytes(), b"\x00partial output\xff")
        session = self.metadata()["sessions"][state["session"]]
        self.assertEqual(session["last_post"], session["turns"][-1]["post"])
        self.assertEqual(self.git("show", session["last_post"] + ":Token.kt"),
                         "partial agent work\n")

        self.git("add", "Token.kt")
        self.write("Config.kt", "human changes after manual end\n")
        before = self.review_state()
        self.assertIn("already in the human phase", self.run_relay(COMMAND, ok=False).stderr)
        self.assertEqual(self.review_state(), before)
        # A delayed or duplicate Stop cannot erase human edits or approvals.
        self.assertEqual(self.hook("agent-stop", owner).stdout, "")
        self.assertEqual(self.review_state(), before)

        injection = self.hook("prompt-submit", {"session_id": "conversation-b"}).stdout
        self.assertNotIn("Continuing the open turn", injection)
        self.assertEqual(injected_stats(injection, "Approved"), {"Token.kt": (1, 1)})
        self.assertEqual(injected_stats(injection, "Human"), {"Config.kt": (1, 1)})
        self.assertEqual(self.state()["turn"], 2)
        self.assertEqual(self.names("human"), {"Config.kt"})
        self.assertEqual(self.names("reviewed"), {"Token.kt"})
        self.assertEqual(self.names("pending"), {"Config.kt", "new.bin"})

    def test_command_rejects_inactive_human_and_suspended_sessions_without_changes(self):
        result = self.run_relay(COMMAND, ok=False)
        self.assertIn("No active Relay session", result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertFalse((self.repo / ".git/agent-session-relay").exists())
        self.run_relay("start")
        self.run_relay("btw")
        self.write("Token.kt", "human staged work\n")
        self.git("add", "Token.kt")
        self.write("Token.kt", "human unstaged work\n")
        before = self.review_state()
        self.assertIn("already in the human phase", self.run_relay(COMMAND, ok=False).stderr)
        self.assertEqual(self.review_state(), before)
        self.assertEqual(self.state()["next_turn"], "btw")
        self.run_relay("btw", "--cancel")
        self.hook("prompt-submit")
        self.run_relay("suspend")
        before = self.review_state()
        self.assertIn("No active Relay session", self.run_relay(COMMAND, ok=False).stderr)
        self.assertEqual(self.review_state(), before)
        self.run_relay("resume")
        self.run_relay(COMMAND)
        self.assertEqual(self.state()["phase"], "human")

    def test_empty_turns_can_be_ended_in_both_modes(self):
        for kind in ("normal", "btw"):
            with self.subTest(kind=kind):
                self.run_relay("start", "-m", "Complete empty turn")
                if kind == "btw":
                    self.run_relay("btw")
                self.hook("prompt-submit")
                result = self.run_relay(COMMAND)
                self.assertIn("Relay is now in the human phase", result.stdout)
                self.assertNotIn("snapshot", result.stdout)
                self.assertEqual(self.state()["phase"], "human")
                self.assertFalse(self.state()["pending_changes"])
                self.run_relay("finish")
                self.run_relay(COMMAND, ok=False)

    def test_agent_cannot_invoke_the_human_command_in_either_mode(self):
        self.run_relay("start")
        for kind in ("normal", "btw"):
            with self.subTest(kind=kind):
                if kind == "btw":
                    self.run_relay("btw")
                self.hook("prompt-submit")
                before = self.review_state()
                denied = self.hook("pre-tool-use", {
                    "tool_name": "execute_bash", "tool_input": {"command": "relay " + COMMAND},
                }, ok=False)
                self.assertEqual(denied.returncode, 2)
                self.assertIn("human-only", denied.stderr)
                self.assertEqual(self.review_state(), before)
                self.hook("agent-stop")

    def test_manual_end_of_btw_uses_existing_backup_and_restoration(self):
        self.run_relay("start")
        self.write("Config.kt", "human staged work\n")
        self.git("add", "Config.kt")
        self.write("Config.kt", "human unstaged work\n")
        self.run_relay("btw")
        self.hook("prompt-submit", {"session_id": "conversation-a"})
        before_index = (self.repo / ".git/index").read_bytes()
        self.write("Token.kt", "accidental agent write\n")
        self.stage_content("Config.kt", "accidental staged write\n")
        result = self.run_relay(COMMAND)
        prefix = f"refs/relay/sessions/{self.state()['session']}/turn-000001-post"
        for area, suffix in (("Workspace", ""), ("Staging", "-staged")):
            commit = self.git("rev-parse", prefix + suffix).strip()
            self.assertIn(f"{area} changes were saved in snapshot {commit}", result.stdout)
        self.assertEqual(self.read("Token.kt"), "TokenDefinition\n")
        self.assertEqual(self.read("Config.kt"), "human unstaged work\n")
        self.assertEqual((self.repo / ".git/index").read_bytes(), before_index)
        self.assertEqual(self.state()["phase"], "human")
        self.assertNotIn("btw_recoveries", self.state())

    def test_command_rejects_unstable_head_locks_and_unresolved_transactions(self):
        self.run_relay("start")
        self.hook("prompt-submit")
        self.write("Token.kt", "partial agent work\n")
        self.git("symbolic-ref", "HEAD", "refs/heads/main")
        before = self.review_state()
        self.assertIn("HEAD changed outside Relay", self.run_relay(COMMAND, ok=False).stderr)
        self.assertEqual(self.review_state(), before)
        self.git("update-ref", "--no-deref", "HEAD", self.base)
        for name, message in (
            ("index.lock", "Git operation or lock in progress"),
            ("agent-session-relay/transaction.json", "relay recover"),
        ):
            with self.subTest(name=name):
                blocker = self.repo / ".git" / name
                blocker.write_text("{}")
                try:
                    before = self.review_state()
                    self.assertIn(message, self.run_relay(COMMAND, ok=False).stderr)
                    self.assertEqual(self.review_state(), before)
                finally:
                    blocker.unlink()

    def test_failure_to_finish_manual_end_rolls_back_code_index_and_turn(self):
        self.run_relay("start")
        self.hook("prompt-submit")
        self.write("Token.kt", "partial agent work\n")
        self.git("add", "Token.kt")
        self.write("Token.kt", "unstaged partial work\n")
        self.write("new.txt", "untracked partial work\n")
        before = self.review_state()
        relay = Relay(Git(self.repo))
        save = relay.store.save
        calls = 0

        def fail_once(state):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("simulated state save failure")
            return save(state)

        with patch.object(relay.store, "save", side_effect=fail_once):
            with self.assertRaisesRegex(OSError, "simulated state save failure"):
                relay.stop(require_open=True)
        self.assertEqual(self.review_state(), before)
        self.assertEqual(self.read("Token.kt"), "unstaged partial work\n")
        self.assertEqual(self.read("new.txt"), "untracked partial work\n")
        self.assertFalse(relay.store.journal_path.exists())
        self.run_relay(COMMAND)
        self.assertEqual(self.state()["phase"], "human")
