from __future__ import annotations

import json
import os
import subprocess
import sys
from unittest.mock import patch

from tests.support import SOURCE, RepositoryTest

# isort: split
from agent_session_relay.core.git import Git
from agent_session_relay.core.session import Relay


class SessionMessageTests(RepositoryTest):
    def test_finish_without_a_message_preserves_the_review_state(self):
        self.run_relay("start")
        self.assertIsNone(self.state()["message"])
        self.write("Token.kt", "reviewed change\n")
        self.git("add", "Token.kt")
        before = self.metadata()
        index = (self.repo / ".git/index").read_bytes()
        refs = self.git("for-each-ref")
        result = self.run_relay("finish", ok=False)
        self.assertIn("Message editing cancelled", result.stderr)
        self.assertEqual(self.metadata(), before)
        self.assertEqual((self.repo / ".git/index").read_bytes(), index)
        self.assertEqual(self.git("for-each-ref"), refs)
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), self.base)
        self.assertEqual(self.read("Token.kt"), "reviewed change\n")
        self.run_relay("finish", "-m", "Clarify token definitions")
        self.assert_one_commit("HEAD")
        self.assertEqual(self.git("log", "-1", "--format=%s"), "Clarify token definitions\n")

    def test_start_message_survives_turns_and_becomes_the_full_result_message(self):
        # Relay writes UTF-8, even if ordinary user commits use a different encoding.
        self.git("config", "i18n.commitEncoding", "ISO-8859-1")
        self.git("config", "i18n.logOutputEncoding", "UTF-8")
        message = "重构 token 配置\n\nReuse the parser abstraction.\nPreserve existing behavior."
        self.run_relay("start", "--message", message)
        sid = self.state()["session"]
        ref = f"refs/relay/sessions/{sid}/message"
        self.assertEqual(self.git("cat-file", "-t", ref), "commit\n")
        self.assertEqual(self.git("log", "-1", "--format=%B", ref).rstrip("\n"), message)
        self.assert_one_commit(ref, self.git("rev-parse", self.base + "^{tree}").strip())
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), self.base)
        self.assertNotIn("message", self.metadata()["sessions"][sid])
        self.assertEqual(self.state()["message"], message)
        self.assertEqual(json.loads(self.run_relay("list", "--json").stdout)[0]["message"], message)
        self.assertIn("重构 token 配置", self.run_relay("list").stdout)
        self.assertIn("重构 token 配置", self.run_relay("status").stdout)
        self.hook("prompt-submit")
        self.write("Token.kt", "refactored token\n")
        self.hook("agent-stop")
        self.git("add", "Token.kt")
        self.hook("prompt-submit")
        self.hook("agent-stop")
        self.git("gc", "--prune=now")
        self.assertEqual(self.state()["message"], message)
        self.run_relay("finish")
        self.assert_one_commit("HEAD")
        self.assertEqual(self.git("log", "-1", "--format=%B").rstrip("\n"), message)
        self.assertEqual(
            self.git("log", "-1", "--format=%an <%ae>"),
            "Relay Test <relay-test@example.invalid>\n",
        )
        self.assertEqual(self.git("for-each-ref", "refs/relay/"), "")
        self.assert_clean()

    def test_message_updates_preserve_index_workspace_and_provenance(self):
        self.run_relay("start", "-m", "Initial token refactor")
        self.hook("prompt-submit")
        self.write("Token.kt", "agent proposal\n")
        self.hook("agent-stop")
        self.git("add", "Token.kt")
        self.write("Token.kt", "human refinement\n")
        self.write("new.bin", b"\0\xffproposal")
        index = (self.repo / ".git/index").read_bytes()
        metadata = self.metadata()
        head = self.git("rev-parse", "HEAD")
        diffs = {
            kind: self.run_relay("agent", "diff", kind).stdout
            for kind in ("reviewed", "human", "pending")
        }
        message = "Refine tokens\n\nPreserve the human's proposed behavior."
        self.run_relay("message", "--message", message)
        self.assertEqual((self.repo / ".git/index").read_bytes(), index)
        self.assertEqual(self.metadata(), metadata)
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertEqual(self.read("Token.kt"), "human refinement\n")
        self.assertEqual((self.repo / "new.bin").read_bytes(), b"\0\xffproposal")
        for kind, expected in diffs.items():
            self.assertEqual(self.run_relay("agent", "diff", kind).stdout, expected)
        self.hook("prompt-submit")
        self.run_relay("message", "-m", "Refine token parsing")
        self.assertEqual(self.state()["phase"], "agent")
        self.hook("agent-stop")
        self.git("add", "-A")
        self.run_relay("finish")
        self.assertEqual(self.git("log", "-1", "--format=%s"), "Refine token parsing\n")
        self.assert_one_commit("HEAD")

    def test_finish_can_override_a_saved_message(self):
        self.run_relay("start", "-m", "Investigate parser behavior")
        self.run_relay("message", "-m", "Document parser behavior")
        message = "Confirm existing parser behavior\n\nNo code changes were needed."
        self.run_relay("finish", "--message", message)
        self.assertEqual(self.git("log", "-1", "--format=%B").rstrip("\n"), message)
        self.assert_one_commit("HEAD")

    def test_blank_start_messages_do_not_start_a_session(self):
        for message in ("", " \n\t "):
            with self.subTest(message=message):
                self.assertIn(
                    "non-empty, custom message",
                    self.run_relay("start", "-m", message, ok=False).stderr,
                )
                self.assertEqual(self.git("symbolic-ref", "--short", "HEAD"), "main\n")
                self.assertEqual(self.git("for-each-ref", "refs/relay/"), "")
                self.assertFalse(self.state()["active"])

    def test_invalid_messages_do_not_replace_a_saved_message_or_finish(self):
        self.run_relay("start", "-m", "Keep token behavior stable")
        refs = self.git("for-each-ref")
        for message in ("", " \n\t "):
            for command in ("message", "finish"):
                with self.subTest(message=message, command=command):
                    result = self.run_relay(command, "-m", message, ok=False)
                    self.assertIn("non-empty, custom message", result.stderr)
                    self.assertEqual(self.state()["message"], "Keep token behavior stable")
                    self.assertEqual(self.git("for-each-ref"), refs)
        self.run_relay("finish")
        self.assertEqual(self.git("log", "-1", "--format=%s"), "Keep token behavior stable\n")

    def test_suspended_messages_can_be_selected_and_survive_resume_and_gc(self):
        self.run_relay("start")
        first = self.state()["session"]
        self.run_relay("suspend")
        self.run_relay("message", "-m", "Refactor parser configuration")
        self.run_relay("start", "-m", "Clarify token naming")
        second = self.state()["session"]
        self.run_relay("message", first[:-2], "-m", "Extract parser configuration")
        self.assertEqual(self.state()["message"], "Clarify token naming")
        self.run_relay("suspend")
        self.run_relay("message", "-m", "Ambiguous target", ok=False)
        self.run_relay("message", first[:8], "-m", "Ambiguous prefix", ok=False)
        self.run_relay("message", "missing-session", "-m", "Unknown target", ok=False)
        self.write("Token.kt", "unrelated normal work\n")
        self.run_relay("message", second, "-m", "Clarify public token naming")
        self.assertEqual(self.read("Token.kt"), "unrelated normal work\n")
        self.git("restore", "Token.kt")
        self.git("gc", "--prune=now")
        messages = {
            s["id"]: s["message"] for s in json.loads(self.run_relay("list", "--json").stdout)
        }
        self.assertEqual(
            messages,
            {first: "Extract parser configuration", second: "Clarify public token naming"},
        )
        self.assertIn("Extract parser configuration", self.run_relay("status").stdout)
        self.run_relay("resume", first)
        self.run_relay("finish")
        self.assertEqual(self.git("log", "-1", "--format=%s"), "Extract parser configuration\n")
        self.run_relay("resume", second)
        self.assertEqual(self.state()["message"], "Clarify public token naming")

    def test_failed_message_ref_update_keeps_the_previous_message(self):
        self.run_relay("start", "-m", "Preserve original token behavior")
        before = self.metadata()
        refs = self.git("for-each-ref")
        with patch.dict(os.environ, self.env, clear=True):
            relay = Relay(Git(self.repo))
            with patch.object(relay.git, "pin", side_effect=OSError("simulated ref write failure")):
                with self.assertRaisesRegex(OSError, "simulated"):
                    relay.set_message("Change token behavior")
        self.assertEqual(self.metadata(), before)
        self.assertEqual(self.git("for-each-ref"), refs)
        self.assertEqual(self.state()["message"], "Preserve original token behavior")
        self.run_relay("finish")

    def test_failed_start_removes_the_message_and_restores_the_origin(self):
        relay = Relay(Git(self.repo))
        save = relay.store.save

        def fail_once(state):
            if state["active"]:
                save(state)
                raise OSError("simulated start failure")
            save(state)

        with patch.dict(os.environ, self.env, clear=True):
            with patch.object(relay.store, "save", side_effect=fail_once):
                with self.assertRaisesRegex(OSError, "simulated"):
                    relay.start("Investigate parser configuration")
        self.assertFalse(self.state()["active"])
        self.assertEqual(self.git("symbolic-ref", "--short", "HEAD"), "main\n")
        self.assertEqual(self.git("for-each-ref", "refs/relay/"), "")
        self.assert_clean()

    def test_failed_finish_keeps_the_saved_message_and_session(self):
        self.run_relay("start", "-m", "Save the parser review outcome")
        sid = self.state()["session"]
        before = self.metadata()
        relay = Relay(Git(self.repo))
        remove_session = relay.remove_session

        def fail_after_cleanup(state, session):
            remove_session(state, session)
            raise OSError("simulated cleanup failure")

        with patch.dict(os.environ, self.env, clear=True):
            with patch.object(relay, "remove_session", side_effect=fail_after_cleanup):
                with self.assertRaisesRegex(OSError, "simulated"):
                    relay.finish("An override that was not published")
        self.assertEqual(self.metadata(), before)
        self.assertEqual(self.state()["message"], "Save the parser review outcome")
        self.assertEqual(self.git("branch", "--list", "relay/result/" + sid), "")
        self.run_relay("finish")
        self.assertEqual(self.git("log", "-1", "--format=%s"), "Save the parser review outcome\n")

    def test_message_storage_works_without_a_public_git_identity(self):
        self.git("config", "--unset", "user.name")
        self.git("config", "--unset", "user.email")
        self.git("config", "user.useConfigOnly", "true")
        self.run_relay("start", "-m", "Inspect parser configuration")
        self.run_relay("message", "-m", "Clarify parser configuration")
        self.assertEqual(self.state()["message"], "Clarify parser configuration")
        self.run_relay("abort", input="abort\n")
        self.assertEqual(self.git("for-each-ref", "refs/relay/"), "")

    def test_finish_crash_and_gc_do_not_lose_the_saved_message(self):
        self.run_relay("start", "-m", "Refine token parsing after review")
        sid = self.state()["session"]
        self.write("Token.kt", "reviewed token changes\n")
        self.git("add", "Token.kt")
        script = """
import os
import sys
sys.path.insert(0, sys.argv[1])
from agent_session_relay.core.git import Git
from agent_session_relay.core.session import Relay
relay = Relay(Git())
remove = relay.remove_session
def crash_after_cleanup(state, session):
    remove(state, session)
    os._exit(77)
relay.remove_session = crash_after_cleanup
relay.finish()
"""
        result = subprocess.run(
            [sys.executable, "-c", script, str(SOURCE / "src")],
            cwd=self.repo,
            env=self.env,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 77, result.stderr)
        self.git("gc", "--prune=now")
        self.run_relay("recover")
        self.assertEqual(self.state()["session"], sid)
        self.assertEqual(self.state()["message"], "Refine token parsing after review")
        self.assertEqual(self.git("show", ":Token.kt"), "reviewed token changes\n")
        self.run_relay("finish")
        self.assert_one_commit("HEAD")
        self.assertEqual(
            self.git("log", "-1", "--format=%s"), "Refine token parsing after review\n"
        )
