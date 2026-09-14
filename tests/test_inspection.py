from __future__ import annotations

import json
import os

from tests.support import RepositoryTest


class InspectionTests(RepositoryTest):
    def agent_status(self):
        return json.loads(self.run_relay("agent", "status").stdout)

    def test_session_includes_approved_pending_and_human_changes_as_a_net_delta(self):
        self.run_relay("start")
        self.assertEqual(self.names("session"), set())
        self.hook("prompt-submit")
        token = self.read("Token.kt")
        parser = self.read("Parser.kt")
        self.write("Token.kt", "accepted token\n")
        self.write("Parser.kt", parser.replace("line 2\n", "accepted hunk\n").replace(
            "line 30\n", "pending hunk\n"
        ))
        self.write("new.txt", "pending addition\n")
        self.hook("agent-stop")
        self.git("add", "Token.kt")
        self.stage_content("Parser.kt", parser.replace("line 2\n", "accepted hunk\n"))
        self.write("Config.kt", "human direction\n")
        before = self.run_relay("agent", "diff", "session").stdout
        self.hook("prompt-submit")
        self.assertEqual(self.run_relay("agent", "diff", "session").stdout, before)
        self.assertEqual(self.names("session"), {"Token.kt", "Parser.kt", "Config.kt", "new.txt"})
        self.assertEqual(self.names("pending"), {"Parser.kt", "Config.kt", "new.txt"})
        self.assertEqual(self.names("reviewed"), {"Token.kt", "Parser.kt"})
        self.assertEqual(self.names("human"), {"Config.kt"})
        self.assertIn("+accepted hunk", before)
        self.assertIn("+pending hunk", before)
        self.assertIn("+human direction", before)
        # Session is live and net, not a concatenation of approval and pending patches.
        self.write("Token.kt", token)
        self.assertNotIn("Token.kt", self.names("session"))
        self.assertIn("Token.kt", self.names("pending"))
        self.assertIn("Token.kt", self.names("reviewed"))
        self.hook("agent-stop")
        snapshot = self.run_relay("agent", "diff", "session").stdout
        self.run_relay("suspend")
        self.git("gc", "--prune=now")
        self.run_relay("resume")
        self.assertEqual(self.run_relay("agent", "diff", "session").stdout, snapshot)

    def test_session_preserves_file_types_literal_paths_and_real_index(self):
        self.run_relay("start")
        self.write("subdir/--stat", "literal option filename\n")
        self.write("subdir/[literal].txt", "literal path\n")
        self.write("subdir/new\nline.txt", "newline path\n")
        self.write("new.bin", b"\x00binary\xff")
        self.write("run.sh", "#!/bin/sh\nexit 0\n")
        (self.repo / "run.sh").chmod(0o755)
        os.symlink("Token.kt", self.repo / "link")
        (self.repo / "Config.kt").unlink()
        self.git("add", "run.sh")
        index = (self.repo / ".git/index").read_bytes()
        patch = self.run_relay("agent", "diff", "session").stdout
        self.assertIn("GIT binary patch", patch)
        self.assertIn("new file mode 100755", patch)
        self.assertIn("new file mode 120000", patch)
        self.assertIn("deleted file mode", patch)
        for path in ("--stat", "[literal].txt", "new\nline.txt"):
            with self.subTest(path=path):
                result = self.run_relay(
                    "agent", "diff", "session", "--name-only", "-z", "--", path,
                    cwd=self.repo / "subdir",
                )
                self.assertEqual(result.stdout, "subdir/" + path + "\0")
        stat = self.run_relay(
            "agent", "diff", "session", "--stat", "--", "--stat", cwd=self.repo / "subdir"
        ).stdout
        self.assertIn("1 file changed, 1 insertion(+)", stat)
        self.assertNotIn("new.bin", stat)
        self.assertIn("Bin", self.run_relay(
            "agent", "diff", "session", "--stat", "--", "new.bin"
        ).stdout)
        self.agent_status()
        self.assertEqual((self.repo / ".git/index").read_bytes(), index)
        self.run_relay("agent", "diff", "session", "--", "../outside", ok=False)

    def test_stat_uses_each_views_baseline_and_path_filter(self):
        self.run_relay("start")
        self.hook("prompt-submit")
        self.write("Token.kt", "approved\n")
        self.write("new.txt", "pending\n")
        self.hook("agent-stop")
        self.git("add", "Token.kt")
        self.write("Config.kt", "human\n")
        self.hook("prompt-submit")
        for kind, path, summary in (
            ("session", "Token.kt", "1 file changed, 1 insertion(+), 1 deletion(-)"),
            ("reviewed", "Token.kt", "1 file changed, 1 insertion(+), 1 deletion(-)"),
            ("human", "Config.kt", "1 file changed, 1 insertion(+), 1 deletion(-)"),
            ("pending", "new.txt", "1 file changed, 1 insertion(+)"),
        ):
            with self.subTest(kind=kind):
                result = self.run_relay("agent", "diff", kind, "--stat", "--", path)
                self.assertIn(path, result.stdout)
                self.assertIn(summary, result.stdout)
                self.assertNotIn("diff --git", result.stdout)
                self.assertEqual(result.stderr, "")
        self.assertEqual(self.run_relay(
            "agent", "diff", "pending", "--stat", "--", "Token.kt"
        ).stdout, "")

    def test_empty_diffs_are_successful_and_invalid_options_fail_explicitly(self):
        self.run_relay("start")
        for kind in ("session", "reviewed", "human", "pending"):
            for options in ((), ("--stat",), ("--name-only",), ("--name-only", "-z")):
                with self.subTest(kind=kind, options=options):
                    result = self.run_relay("agent", "diff", kind, *options)
                    self.assertEqual(result.stdout + result.stderr, "")
        for options in (("--unknown",), ("--stat", "--name-only"), ("--stat", "-z")):
            with self.subTest(options=options):
                result = self.run_relay("agent", "diff", "pending", *options, ok=False)
                self.assertEqual(result.stdout, "")
                self.assertTrue(result.stderr)
        for options in (("--turn", "1"), ("--staged",)):
            self.assertIn("require `relay agent diff btw`", self.run_relay(
                "agent", "diff", "session", *options, ok=False
            ).stderr)

    def test_agent_status_distinguishes_session_pending_and_handoff_state(self):
        self.assertEqual(self.agent_status(), {"active": False})
        self.run_relay("start", "-m", "Human commit description")
        initial = self.agent_status()
        self.assertEqual(initial, {
            "active": True, "phase": "human", "turn": 0, "turn_kind": None,
            "session_changes": False, "pending_changes": False,
            "provenance": {"available": False, "newly_reviewed": False, "human_edits": False},
            "btw_recoveries": [],
        })
        self.hook("prompt-submit")
        token = self.read("Token.kt")
        self.write("Token.kt", "approved\n")
        self.hook("agent-stop")
        self.git("add", "Token.kt")
        self.hook("prompt-submit")
        approved = self.agent_status()
        self.assertEqual(set(approved), set(initial))
        self.assertEqual((approved["phase"], approved["turn"], approved["turn_kind"]),
                         ("agent", 2, "normal"))
        self.assertTrue(approved["session_changes"])
        self.assertFalse(approved["pending_changes"])
        self.assertEqual(approved["provenance"], {
            "available": True, "newly_reviewed": True, "human_edits": False,
        })
        self.assertFalse(self.state()["staged_approvals"])
        self.assertEqual(self.state()["base_commit"], self.base)
        self.assertEqual(self.state()["message"], "Human commit description")
        self.write("Token.kt", token)
        reverted = self.agent_status()
        self.assertFalse(reverted["session_changes"])
        self.assertTrue(reverted["pending_changes"])
        self.assertEqual(reverted["provenance"], approved["provenance"])
        self.hook("agent-stop")
        self.write("Config.kt", "human edit\n")
        self.hook("prompt-submit")
        self.assertEqual(self.agent_status()["provenance"], {
            "available": True, "newly_reviewed": False, "human_edits": True,
        })
        self.hook("agent-stop")
        self.run_relay("suspend")
        self.assertEqual(self.agent_status(), {"active": False})

    def test_approval_notice_is_conditional_partial_and_repeatable(self):
        self.run_relay("start")
        first = self.hook("prompt-submit").stdout
        notice = "Approvals are sealed into the reviewed checkpoint this turn."
        self.assertNotIn(notice, first)
        self.assertNotIn("Staged approvals have been sealed", first)
        self.assertNotIn("edits/discards", first)
        original = self.read("Parser.kt")
        self.write("Parser.kt", original.replace("line 2\n", "approved\n").replace(
            "line 30\n", "pending\n"
        ))
        unusual = 'new\n"file.txt'
        self.write(unusual, "approved addition\n")
        self.hook("agent-stop")
        self.stage_content("Parser.kt", original.replace("line 2\n", "approved\n"))
        self.git("add", unusual)
        injection = self.hook("prompt-submit").stdout
        self.assertIn(notice, injection)
        self.assertIn(
            "the rest of the file they are in may still contain pending changes", injection
        )
        self.assertNotIn("Human changes (", injection)
        encoded = injection.split("Approved changes (repository-relative paths, JSON):\n")[1]
        self.assertEqual(json.loads(encoded), ["Parser.kt", unusual])
        self.assertEqual(self.names("pending"), {"Parser.kt"})
        self.assertEqual(self.hook("prompt-submit").stdout, injection)
        self.hook("agent-stop")
        self.assertNotIn(notice, self.hook("prompt-submit").stdout)
