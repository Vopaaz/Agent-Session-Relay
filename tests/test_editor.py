from __future__ import annotations

import shlex
import sys
from pathlib import Path

from tests.support import CLI, RepositoryTest


class MessageEditorTests(RepositoryTest):
    def configure_editor(self, body: str, *, via: str = "core.editor") -> Path:
        directory = Path(self.temporary.name) / "editor tools"
        directory.mkdir(exist_ok=True)
        number = len(list(directory.iterdir()))
        script = directory / f"message editor {number}.py"
        capture = directory / f"initial message {number}.txt"
        script.write_text(
            "import sys\nfrom pathlib import Path\n"
            "path = Path(sys.argv[-1])\n"
            "assert path.name == 'COMMIT_EDITMSG'\n"
            f"Path({str(capture)!r}).write_bytes(path.read_bytes())\n" + body,
            encoding="utf-8",
        )
        command = shlex.join([sys.executable, str(script), "--wait"])
        if via == "core.editor":
            self.env.pop("GIT_EDITOR", None)
            self.git("config", "core.editor", command)
        else:
            self.env[via] = command
        return capture

    def assert_no_edit_buffers(self):
        self.assertEqual(list((self.repo / ".git").glob("relay-message-*")), [])

    def test_start_and_explicit_messages_never_open_an_editor_or_read_a_template(self):
        capture = self.configure_editor("raise RuntimeError('editor must not run')\n")
        self.git("config", "commit.template", "missing-template.txt")
        self.run_relay("start")
        self.assertIsNone(self.state()["message"])
        self.assertFalse(capture.exists())
        self.run_relay(
            "message", "-m", "Review parser behavior", "-m", "Keep the public API stable."
        )
        self.assertEqual(
            self.state()["message"], "Review parser behavior\n\nKeep the public API stable."
        )
        self.run_relay("finish", "-m", "Confirm parser behavior", "-m", "No changes were needed.")
        self.assertEqual(
            self.git("log", "-1", "--format=%B").rstrip("\n"),
            "Confirm parser behavior\n\nNo changes were needed.",
        )
        self.run_relay("start", "-m", "Review token behavior", "-m", "Check naming consistency.")
        self.run_relay("finish")
        self.assertFalse(capture.exists())
        self.assert_no_edit_buffers()

    def test_message_opens_configured_editor_with_existing_message_and_preserves_review(self):
        self.run_relay("start", "-m", "Review tokens", "-m", "Check the parser contract.")
        self.git("config", "commit.template", "missing-template.txt")
        self.write("Token.kt", "staged changes\n")
        self.git("add", "Token.kt")
        self.write("Token.kt", "further pending changes\n")
        self.write("new.txt", "pending addition\n")
        before = self.metadata()
        index = (self.repo / ".git/index").read_bytes()
        capture = self.configure_editor(
            "path.write_text('重构 token\\n\\n保留 parser 接口。\\n# editor comment\\n', "
            "encoding='utf-8')\n"
        )
        self.run_relay("message")
        self.assertIn("Review tokens\n\nCheck the parser contract.", capture.read_text())
        self.assertEqual(self.state()["message"], "重构 token\n\n保留 parser 接口。")
        self.assertEqual(self.metadata(), before)
        self.assertEqual((self.repo / ".git/index").read_bytes(), index)
        self.assertEqual(self.read("Token.kt"), "further pending changes\n")
        self.assertEqual(self.read("new.txt"), "pending addition\n")
        self.assert_no_edit_buffers()

    def test_finish_uses_template_and_editor_to_create_the_single_commit(self):
        self.run_relay("start")
        self.write("Token.kt", "reviewed token changes\n")
        self.git("add", "Token.kt")
        template = Path(self.temporary.name) / "commit template.txt"
        template.write_text("Subject goes here\n\n# Explain why\n", encoding="utf-8")
        # A relative configured path is resolved from the repository root, even in a subdirectory.
        self.git("config", "commit.template", "../commit template.txt")
        capture = self.configure_editor(
            "path.write_text('Refine token parsing  \\n\\n\\n"
            "Preserve the public API.\\n# note\\n')\n"
        )
        (self.repo / "subdir").mkdir()
        original_editmsg = self.repo / ".git/COMMIT_EDITMSG"
        original_editmsg.write_text("an unrelated Git editing buffer\n")
        self.run_relay("finish", cwd=self.repo / "subdir")
        self.assertTrue(capture.read_text().startswith(template.read_text()))
        self.assertEqual(
            self.git("log", "-1", "--format=%B").rstrip("\n"),
            "Refine token parsing\n\nPreserve the public API.",
        )
        self.assert_one_commit("HEAD")
        self.assertEqual(original_editmsg.read_text(), "an unrelated Git editing buffer\n")
        self.assert_clean()
        self.assert_no_edit_buffers()

    def test_unmodified_template_comments_and_empty_messages_cancel_finish(self):
        self.run_relay("start")
        template = Path(self.temporary.name) / "template.txt"
        template.write_text("Describe the work here\n\n# Instructions\n")
        self.git("config", "commit.template", str(template))
        before = self.metadata()
        refs = self.git("for-each-ref")
        index = (self.repo / ".git/index").read_bytes()
        cases = [
            ("", "template was not changed"),
            ("path.write_text(path.read_text() + '\\n# another comment\\n')\n", "template"),
            ("path.write_text(' \\n\\t\\n')\n", "message is empty"),
            ("path.write_text('# comment only\\n')\n", "message is empty"),
            ("path.write_text('Draft that must not be accepted\\n'); sys.exit(1)\n", "cancelled"),
        ]
        for body, error in cases:
            with self.subTest(body=body):
                self.configure_editor(body)
                self.assertIn(error, self.run_relay("finish", ok=False).stderr)
                self.assertEqual(self.metadata(), before)
                self.assertEqual(self.git("for-each-ref"), refs)
                self.assertEqual((self.repo / ".git/index").read_bytes(), index)
                self.assertIsNone(self.state()["message"])
                self.assert_no_edit_buffers()

    def test_cancelling_message_edit_keeps_the_previous_message(self):
        self.run_relay("start", "-m", "Existing useful description")
        ref = f"refs/relay/sessions/{self.state()['session']}/message"
        before = self.git("rev-parse", ref)
        for body in ("path.write_text('')\n", "path.write_text('New draft'); sys.exit(2)\n"):
            self.configure_editor(body)
            self.assertIn("cancelled", self.run_relay("message", ok=False).stderr)
            self.assertEqual(self.git("rev-parse", ref), before)
            self.assertEqual(self.state()["message"], "Existing useful description")
        self.configure_editor("")
        self.run_relay("message")
        self.assertEqual(self.state()["message"], "Existing useful description")

    def test_editor_precedence_and_inherited_terminal_streams(self):
        self.run_relay("start")
        configured = self.configure_editor("path.write_text('Configured editor')\n")
        visual = self.configure_editor("path.write_text('Visual editor')\n", via="VISUAL")
        fallback = self.configure_editor("path.write_text('Fallback editor')\n", via="EDITOR")
        self.env["TERM"] = "xterm"
        override = self.configure_editor(
            "print('Enter a message:', flush=True)\npath.write_text(sys.stdin.readline())\n",
            via="GIT_EDITOR",
        )
        result = self.run_relay("message", input="Environment editor message\n")
        self.assertIn("Enter a message:", result.stdout)
        self.assertEqual(self.state()["message"], "Environment editor message")
        self.assertTrue(override.exists())
        self.assertFalse(configured.exists())
        self.assertFalse(visual.exists())
        self.env.pop("GIT_EDITOR")
        self.run_relay("message")
        self.assertEqual(self.state()["message"], "Configured editor")
        self.git("config", "--unset", "core.editor")
        self.run_relay("message")
        self.assertEqual(self.state()["message"], "Visual editor")
        self.env.pop("VISUAL")
        self.run_relay("message")
        self.assertEqual(self.state()["message"], "Fallback editor")
        self.assertTrue(fallback.exists())

    def test_git_comment_character_cleanup_and_auto_selection(self):
        self.run_relay("start", "-m", "# Preserve this heading")
        self.git("config", "core.commentChar", "auto")
        capture = self.configure_editor("")
        self.run_relay("message")
        self.assertEqual(self.state()["message"], "# Preserve this heading")
        self.assertIn("; Describe this Relay session", capture.read_text())
        self.git("config", "core.commentChar", ";")
        self.configure_editor("path.write_text('Explain parsing\\n; ignored comment\\n')\n")
        self.run_relay("message")
        self.assertEqual(self.state()["message"], "Explain parsing")

    def test_commit_cleanup_modes(self):
        self.run_relay("start")
        cases = [
            ("strip", "Subject  \n\n# comment\n\nBody\n", "Subject\n\nBody"),
            ("whitespace", "Subject  \n\n# keep this\n", "Subject\n\n# keep this"),
            ("verbatim", "Subject  \n\n\n# keep this\n", "Subject  \n\n\n# keep this"),
            (
                "scissors",
                "Subject\n\n# keep this\n# ------------------------ >8 "
                "------------------------\nignored\n",
                "Subject\n\n# keep this",
            ),
        ]
        for mode, contents, expected in cases:
            with self.subTest(mode=mode):
                self.git("config", "commit.cleanup", mode)
                self.configure_editor(f"path.write_text({contents!r})\n")
                self.run_relay("message")
                self.assertEqual(self.state()["message"], expected)

    def test_suspended_session_can_be_edited_without_resuming(self):
        self.run_relay("start", "-m", "First description")
        sid = self.state()["session"]
        self.run_relay("suspend")
        capture = self.configure_editor("path.write_text('Updated paused session\\n')\n")
        self.run_relay("message", sid)
        self.assertIn("First description", capture.read_text())
        self.assertFalse(self.state()["active"])
        self.assertEqual(self.git("symbolic-ref", "--short", "HEAD"), "main\n")
        self.run_relay("resume")
        self.assertEqual(self.state()["message"], "Updated paused session")

    def test_finish_checks_pending_changes_before_and_after_editing(self):
        self.run_relay("start")
        self.write("Token.kt", "pending\n")
        capture = self.configure_editor("path.write_text('Explain token changes\\n')\n")
        self.assertIn("Pending changes remain", self.run_relay("finish", ok=False).stderr)
        self.assertFalse(capture.exists())
        self.git("add", "Token.kt")
        self.configure_editor(
            "path.write_text('Explain token changes\\n')\n"
            "Path('Token.kt').write_text('new pending work\\n')\n"
        )
        result = self.run_relay("finish", ok=False)
        self.assertIn("Finish cancelled: pending changes appeared", result.stderr)
        self.assertEqual(self.read("Token.kt"), "new pending work\n")
        self.assertEqual(self.git("show", ":Token.kt"), "pending\n")
        self.assertTrue(self.state()["active"])

    def test_missing_template_editor_failure_and_invalid_utf8_leave_session_intact(self):
        self.run_relay("start")
        capture = self.configure_editor("path.write_bytes(b'\\xff')\n")
        self.git("config", "commit.template", "missing.txt")
        self.run_relay("finish", ok=False)
        self.assertFalse(capture.exists())
        self.git("config", "--unset", "commit.template")
        self.assertIn("cannot read the UTF-8 message", self.run_relay("finish", ok=False).stderr)
        self.git("config", "core.editor", "nonexistent-relay-test-editor")
        self.assertIn("cancelled", self.run_relay("finish", ok=False).stderr)
        self.assertTrue(self.state()["active"])
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), self.base)
        self.assert_no_edit_buffers()

    def test_editor_does_not_hold_the_lock_or_overwrite_another_message_update(self):
        self.run_relay("start", "-m", "Original description")
        self.configure_editor(
            "import subprocess\n"
            f"cli = [sys.executable, {str(CLI)!r}]\n"
            "subprocess.run(cli + ['status', '--json'], check=True, capture_output=True)\n"
            "subprocess.run(cli + ['message', '-m', 'Another terminal updated this'], "
            "check=True, capture_output=True)\n"
            "path.write_text('A draft based on the original description\\n')\n"
        )
        self.assertIn("message changed", self.run_relay("message", ok=False).stderr)
        self.assertEqual(self.state()["message"], "Another terminal updated this")
        self.assert_no_edit_buffers()

    def test_finish_does_not_switch_to_a_different_session_after_editing(self):
        self.run_relay("start")
        original = self.state()["session"]
        self.configure_editor(
            "import subprocess\n"
            f"cli = [sys.executable, {str(CLI)!r}]\n"
            "subprocess.run(cli + ['status', '--json'], check=True, capture_output=True)\n"
            "subprocess.run(cli + ['suspend'], check=True, capture_output=True)\n"
            "subprocess.run(cli + ['start', '-m', 'A different session'], "
            "check=True, capture_output=True)\n"
            "path.write_text('A description for the original session\\n')\n"
        )
        self.assertIn("session or its message changed", self.run_relay("finish", ok=False).stderr)
        self.assertNotEqual(self.state()["session"], original)
        self.assertEqual(self.state()["message"], "A different session")
        self.assertEqual(self.git("branch", "--list", "relay/result/*"), "")
        self.assertEqual(self.metadata()["sessions"][original]["state"], "suspended")

    def test_finish_does_not_override_a_message_saved_during_editing(self):
        self.run_relay("start")
        self.configure_editor(
            "import subprocess\n"
            f"cli = [sys.executable, {str(CLI)!r}]\n"
            "subprocess.run(cli + ['message', '-m', 'New description from another terminal'], "
            "check=True, capture_output=True)\n"
            "path.write_text('An outdated description\\n')\n"
        )
        self.assertIn("session or its message changed", self.run_relay("finish", ok=False).stderr)
        self.assertEqual(self.state()["message"], "New description from another terminal")
        self.assertEqual(self.git("branch", "--list", "relay/result/*"), "")
