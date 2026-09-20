from __future__ import annotations

from unittest.mock import patch

from tests.support import RepositoryTest, injected_stats

# isort: split
from agent_session_relay.core.git import Git
from agent_session_relay.core.session import Relay


class ProtocolTests(RepositoryTest):
    def test_full_introduction_once_per_conversation_and_session(self):
        self.run_relay("start", "-m", "Review this feature")
        introductions = []
        for owner, full in (("a", True), ("a", False), ("b", True), ("a", False)):
            output = self.hook("prompt-submit", {"session_id": owner}).stdout
            self.assertEqual("Relay coordinates code review" in output, full)
            self.assertEqual("relay agent diff reviewed" in output, full)
            self.assertIn("[Agent-Session-Relay active]", output)
            self.assertIn("Do not run Git directly", output)
            self.assertIn("Leave your changes unstaged", output)
            self.assertNotIn("normal turn", output.lower())
            if full:
                introductions.append(output)
            else:
                self.assertLess(len(output), len(introductions[0]) // 4)
                self.assertLessEqual(len(output.splitlines()), 3)
                self.assertIn("Leave Relay controls to the human", output)
            self.hook("agent-stop", {"session_id": owner})
        self.run_relay("finish")
        self.run_relay("start")
        self.assertIn("Relay coordinates code review",
                      self.hook("prompt-submit", {"session_id": "a"}).stdout)

    def test_brief_turns_keep_new_stats_and_do_not_reannounce_old_changes(self):
        self.run_relay("start")
        self.hook("prompt-submit")
        self.hook("agent-stop")
        self.write("Token.kt", "approved token\n")
        self.git("add", "Token.kt")
        self.write("Config.kt", "human config\nextra configuration\n")
        second = self.hook("prompt-submit").stdout
        self.assertNotIn("Relay coordinates code review", second)
        self.assertEqual(injected_stats(second, "Approved"), {"Token.kt": (1, 1)})
        self.assertEqual(injected_stats(second, "Human"), {
            "Token.kt": (1, 1), "Config.kt": (2, 1),
        })
        self.assertNotIn("extra configuration", second)
        retry = self.hook("prompt-submit").stdout
        self.assertIn("Continuing the open turn", retry)
        self.assertIn("reviewed/human diffs still refer to its original handoff", retry)
        self.assertFalse(injected_stats(retry, "Human"))
        self.assertFalse(injected_stats(retry, "Approved"))
        self.hook("agent-stop")
        # Discard is a new human delta in the next handoff, with its own line counts.
        self.git("restore", "Config.kt")
        third = self.hook("prompt-submit").stdout
        self.assertNotIn("Relay coordinates code review", third)
        self.assertEqual(injected_stats(third, "Human"), {"Config.kt": (1, 2)})
        self.assertFalse(injected_stats(third, "Approved"))
        self.assertNotIn("Token.kt", third)

    def test_first_btw_introduces_normal_review_before_switching_to_brief_normal(self):
        self.run_relay("start")
        self.write("Config.kt", "human edit\n")
        self.run_relay("btw")
        first = self.hook("prompt-submit").stdout
        self.assertIn("Relay coordinates code review", first)
        self.assertIn("For normal turns:", first)
        self.assertIn("Reviewed code and human edits are soft", first)
        self.assertIn("read-only BTW turn", first)
        self.assertFalse(injected_stats(first, "Human"))
        self.hook("agent-stop")
        normal = self.hook("prompt-submit").stdout
        self.assertNotIn("Relay coordinates code review", normal)
        self.assertNotIn("read-only BTW turn", normal)
        self.assertIn("Leave your changes unstaged", normal)
        self.assertEqual(injected_stats(normal, "Human"), {"Config.kt": (1, 1)})
        self.hook("agent-stop")
        self.run_relay("btw")
        aside = self.hook("prompt-submit").stdout
        self.assertNotIn("Relay coordinates code review", aside)
        self.assertIn("do not modify files or Git state", aside)
        self.assertIn("Do not run Git directly", aside)
        self.assertFalse(injected_stats(aside, "Human"))

    def test_suspend_resume_preserves_introduction_history(self):
        self.run_relay("start")
        owner = {"session_id": "original"}
        self.hook("prompt-submit", owner)
        self.hook("agent-stop", owner)
        self.run_relay("suspend")
        self.run_relay("resume")
        self.assertNotIn("Relay coordinates code review", self.hook("prompt-submit", owner).stdout)
        self.hook("agent-stop", owner)
        new = self.hook("prompt-submit", {"session_id": "new-conversation"}).stdout
        self.assertIn("Relay coordinates code review", new)

    def test_stat_failure_rolls_back_handoff_so_retry_gets_initial_context(self):
        self.run_relay("start")
        self.write("Token.kt", "approved\n")
        self.git("add", "Token.kt")
        self.write("Config.kt", "human proposal\n")
        before = self.metadata()
        index = self.git("diff", "--cached")
        relay = Relay(Git(self.repo))
        with patch.object(relay.git, "diff_stats", side_effect=OSError("stat unavailable")):
            with self.assertRaisesRegex(OSError, "stat unavailable"):
                relay.handoff("owner")
        self.assertEqual(self.metadata(), before)
        self.assertEqual(self.git("diff", "--cached"), index)
        self.assertEqual(self.read("Config.kt"), "human proposal\n")
        retry = self.hook("prompt-submit", {"session_id": "owner"}).stdout
        self.assertIn("Relay coordinates code review", retry)
        self.assertEqual(injected_stats(retry, "Approved"), {"Token.kt": (1, 1)})
        self.assertEqual(self.state()["turn"], 1)
