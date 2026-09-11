from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from tests.support import SOURCE, RepositoryTest

# isort: split
from agent_session_relay.core.git import Git
from agent_session_relay.core.session import Relay


class WorkflowTests(RepositoryTest):
    def test_full_multi_turn_partial_hunk_review(self):
        self.run_relay("start")
        session = self.state()["session"]
        self.assertEqual(self.state()["base_commit"], self.base)
        self.assertEqual(self.git("rev-parse", "main").strip(), self.base)
        self.assertEqual(self.git("symbolic-ref", "-q", "HEAD", check=False), "")
        injection = self.hook("prompt-submit").stdout
        self.assertIn("[Agent-Session-Relay active]", injection)
        original = self.read("Parser.kt")
        proposed = original.replace("line 2\n", "accepted parser hunk\n").replace(
            "line 30\n", "agent direction\n"
        )
        self.write("Parser.kt", proposed)
        self.write("Token.kt", "TokenDefinition refactored\n")
        self.write("Config.kt", "agent config proposal\n")
        self.write("README.md", "unwanted agent docs\n")
        self.write("new.bin", b"\x00new binary\xff")
        self.assertIn("new.bin", self.names("pending"))
        self.hook("agent-stop")
        self.assertEqual(self.git("diff", "--cached"), "")
        self.git("add", "Token.kt", "new.bin")
        self.stage_content("Parser.kt", original.replace("line 2\n", "accepted parser hunk\n"))
        self.write("Parser.kt", proposed.replace("agent direction\n", "human direction\n"))
        self.git("restore", "README.md")
        before_index = (self.repo / ".git/index").read_bytes()
        self.run_relay("agent", "status")
        self.names("pending")
        self.assertEqual((self.repo / ".git/index").read_bytes(), before_index)
        self.assertEqual(self.hook("prompt-submit").stdout, injection)
        self.assertEqual(self.git("diff", "--cached"), "")
        self.assertEqual(self.names("reviewed"), {"Parser.kt", "Token.kt", "new.bin"})
        # A discard is a workspace change made during the human interval too.
        self.assertEqual(self.names("human"), {"Parser.kt", "README.md"})
        self.assertEqual(self.names("pending"), {"Parser.kt", "Config.kt"})
        reviewed = self.run_relay("agent", "diff", "reviewed", "--", "Parser.kt").stdout
        self.assertIn("+accepted parser hunk", reviewed)
        self.assertNotIn("human direction", reviewed)
        human = self.run_relay("agent", "diff", "human", "--", "Parser.kt").stdout
        self.assertIn("-agent direction", human)
        self.assertIn("+human direction", human)
        pending = self.run_relay("agent", "diff", "pending", "--", "Parser.kt").stdout
        self.assertNotIn("+accepted parser hunk", pending)
        self.assertIn("+human direction", pending)
        # Duplicate hook execution must not erase the accepted-hunk/human provenance.
        self.hook("prompt-submit")
        self.assertEqual(self.state()["turn"], 2)
        self.assertEqual(self.run_relay("agent", "diff", "human", "--", "Parser.kt").stdout, human)
        self.write("Token.kt", "ParsedToken refactored again\n")
        self.write(
            "Parser.kt", self.read("Parser.kt").replace("human direction", "simplified direction")
        )
        self.write("Config.kt", "existing abstraction\n")
        self.assertIn("Token.kt", self.names("pending"))
        self.assertEqual(self.run_relay("agent", "diff", "human", "--", "Parser.kt").stdout, human)
        self.hook("agent-stop")
        self.git("add", "-A")
        final_tree = self.git("write-tree").strip()
        self.run_relay("finish", "-m", "Refactor parser and configuration")
        branch = f"relay/result/{session}"
        self.assert_one_commit(branch, final_tree)
        self.assertEqual(self.git("symbolic-ref", "--short", "HEAD").strip(), branch)
        self.assertEqual(
            self.git("log", "-1", "--format=%s"), "Refactor parser and configuration\n"
        )
        self.assertEqual(self.git("rev-parse", "main").strip(), self.base)
        self.assertEqual(self.git("for-each-ref", "refs/relay/"), "")
        self.assertFalse(self.state()["active"])
        self.assert_clean()

    def test_start_rejects_dirty_workspace_and_unfinished_operations(self):
        self.write("scratch.txt", "new")
        self.run_relay("start", ok=False)
        (self.repo / "scratch.txt").unlink()
        self.write("Token.kt", "changed")
        self.run_relay("start", ok=False)
        self.git("add", "Token.kt")
        self.run_relay("start", ok=False)
        self.git("reset", "--hard", "HEAD")
        for marker in (
            "MERGE_HEAD",
            "CHERRY_PICK_HEAD",
            "REVERT_HEAD",
            "rebase-merge",
            "rebase-apply",
            "sequencer",
            "BISECT_START",
            "index.lock",
        ):
            with self.subTest(marker=marker):
                path = self.repo / ".git" / marker
                path.write_text(self.base)
                result = self.run_relay("start", ok=False)
                self.assertIn("in progress", result.stderr)
                path.unlink()
        self.write("cache.ignored", "ignored files do not dirty the workspace")
        self.run_relay("start")
        self.run_relay("start", ok=False)

    def test_start_requires_a_commit(self):
        empty = Path(self.temporary.name) / "empty"
        empty.mkdir()
        self.git("init", "-b", "main", cwd=empty)
        self.assertIn("initial Git commit", self.run_relay("start", cwd=empty, ok=False).stderr)

    def test_finish_refuses_pending_and_new_files(self):
        self.run_relay("start", "-m", "Add a reviewed project file")
        self.write("new.txt", "not staged")
        self.assertIn("Pending changes remain", self.run_relay("finish", ok=False).stderr)
        self.git("add", "new.txt")
        self.write("new.txt", "a later unreviewed edit")
        self.run_relay("finish", ok=False)
        self.assertEqual(self.git("show", ":new.txt"), "not staged")
        self.git("add", "new.txt")
        self.run_relay("finish")
        self.assert_clean()

    def test_empty_result_still_has_exactly_one_commit(self):
        self.run_relay("start", "-m", "Verify the parser needs no changes")
        sid = self.state()["session"]
        self.run_relay("finish")
        self.assert_one_commit(
            "relay/result/" + sid, self.git("rev-parse", self.base + "^{tree}").strip()
        )

    def test_direct_index_changes_by_agent_are_unstaged_at_stop(self):
        self.run_relay("start")
        self.hook("prompt-submit")
        self.write("Token.kt", "agent")
        self.git("add", "Token.kt")
        self.hook("agent-stop")
        self.assertEqual(self.git("diff", "--cached"), "")
        self.assertEqual(self.names("pending"), {"Token.kt"})
        # A duplicate Stop after the human starts staging must leave those approvals intact.
        self.git("add", "Token.kt")
        self.hook("agent-stop")
        self.assertNotEqual(self.git("diff", "--cached"), "")

    def test_subdirectory_and_special_path_filtering(self):
        self.run_relay("start")
        self.write("subdir/space name.txt", "spaces\n")
        self.write("subdir/[literal].txt", "literal pathspec\n")
        self.write("--name-only", "a filename\n")
        self.write("subdir/new\nline.txt", "newline filename\n")
        result = self.run_relay(
            "agent", "diff", "pending", "--", "[literal].txt", cwd=self.repo / "subdir"
        )
        self.assertIn("+literal pathspec", result.stdout)
        self.assertNotIn("spaces", result.stdout)
        self.assertIn(
            "+a filename", self.run_relay("agent", "diff", "pending", "--", "--name-only").stdout
        )
        names = self.run_relay("agent", "diff", "pending", "--name-only", "-z").stdout.split("\0")
        self.assertIn("subdir/new\nline.txt", names)
        self.run_relay("agent", "diff", "pending", "--", "../outside", ok=False)

    def test_detached_origin_is_restored(self):
        self.git("checkout", "--detach")
        self.run_relay("start")
        self.write("Token.kt", "proposal")
        self.run_relay("suspend")
        self.assertEqual(self.git("symbolic-ref", "-q", "HEAD", check=False), "")
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), self.base)
        self.run_relay("resume")
        self.assertEqual(self.read("Token.kt"), "proposal")

    def test_outside_head_change_is_detected_without_discarding(self):
        self.run_relay("start")
        self.git("switch", "main")
        self.write("Token.kt", "keep this work")
        self.assertIn("HEAD changed outside Relay", self.run_relay("suspend", ok=False).stderr)
        self.assertEqual(self.read("Token.kt"), "keep this work")

    def test_flags_sparse_checkout_and_embedded_repositories_are_rejected(self):
        self.git("update-index", "--assume-unchanged", "Token.kt")
        self.assertIn("assume-unchanged", self.run_relay("start", ok=False).stderr)
        self.git("update-index", "--no-assume-unchanged", "Token.kt")
        self.git("config", "core.sparseCheckout", "true")
        self.assertIn("Sparse", self.run_relay("start", ok=False).stderr)
        self.git("config", "core.sparseCheckout", "false")
        self.run_relay("start")
        nested = self.repo / "nested"
        nested.mkdir()
        self.git("init", "-b", "main", cwd=nested)
        self.git(
            "-c",
            "user.name=T",
            "-c",
            "user.email=t@example.invalid",
            "commit",
            "--allow-empty",
            "-m",
            "inner",
            cwd=nested,
        )
        self.assertIn("embedded repositories", self.run_relay("suspend", ok=False).stderr)
        self.assertTrue((nested / ".git").exists())

    def test_linked_worktrees_have_independent_active_sessions(self):
        linked = Path(self.temporary.name) / "linked"
        self.git("worktree", "add", "-b", "other", str(linked))
        self.run_relay("start", "-m", "Review the main worktree")
        self.run_relay("start", "-m", "Review the linked worktree", cwd=linked)
        self.run_relay("message", "-m", "Refine the linked worktree", cwd=linked)
        self.assertEqual(self.state()["message"], "Review the main worktree")
        self.assertEqual(
            json.loads(self.run_relay("list", "--json", cwd=linked).stdout)[0]["message"],
            "Refine the linked worktree",
        )
        self.write("Token.kt", "main worktree proposal")
        (linked / "Token.kt").write_text("linked proposal")
        self.run_relay("suspend", cwd=linked)
        self.assertTrue(self.state()["active"])
        self.assertEqual(self.read("Token.kt"), "main worktree proposal")
        self.run_relay("resume", cwd=linked)
        self.assertEqual((linked / "Token.kt").read_text(), "linked proposal")


class SuspendAbortTests(RepositoryTest):
    def test_suspend_resume_preserves_full_review_ui_and_provenance_through_gc(self):
        self.run_relay("start")
        sid = self.state()["session"]
        self.hook("prompt-submit")
        self.write("Token.kt", "agent version\n")
        self.hook("agent-stop")
        self.git("add", "Token.kt")
        self.hook("prompt-submit")
        self.hook("agent-stop")
        self.write("Token.kt", "staged version never present in workspace snapshot\n")
        self.git("add", "Token.kt")
        self.write("Token.kt", "unstaged version\n")
        self.write("new.bin", b"\x00\xff\x01\n")
        self.write("intent.txt", "intent to add, not approved\n")
        self.git("add", "-N", "intent.txt")
        (self.repo / "Config.kt").unlink()
        self.git("mv", "README.md", "renamed.md")
        self.write("cache.ignored", "leave this ignored file in place")
        os.symlink("Token.kt", self.repo / "token-link")
        self.write("run.sh", "#!/bin/sh\nexit 0\n")
        (self.repo / "run.sh").chmod(0o755)
        before_status = self.git("status", "--porcelain=v1", "-z", "--untracked-files=all")
        before_index = self.git("diff", "--cached", "--binary")
        reviewed_diff = self.run_relay("agent", "diff", "reviewed").stdout
        self.run_relay("suspend")
        self.assert_clean()
        self.assertFalse((self.repo / "intent.txt").exists())
        self.assertFalse((self.repo / "new.bin").exists())
        self.assertTrue((self.repo / "cache.ignored").exists())
        self.git("switch", "-c", "urgent-fix")
        self.write("urgent.txt", "independent work\n")
        self.git("add", "urgent.txt")
        self.git("commit", "-m", "urgent fix")
        self.git("gc", "--prune=now")
        self.run_relay("resume")
        self.assertEqual(self.state()["session"], sid)
        self.assertEqual(self.state()["base_commit"], self.base)
        self.assertEqual(
            self.git("status", "--porcelain=v1", "-z", "--untracked-files=all"), before_status
        )
        self.assertEqual(self.git("diff", "--cached", "--binary"), before_index)
        self.assertEqual(self.run_relay("agent", "diff", "reviewed").stdout, reviewed_diff)
        self.assertEqual((self.repo / "new.bin").read_bytes(), b"\x00\xff\x01\n")
        self.assertEqual(os.readlink(self.repo / "token-link"), "Token.kt")
        self.assertTrue((self.repo / "run.sh").stat().st_mode & 0o111)
        self.assertFalse((self.repo / "urgent.txt").exists())

    def test_multiple_suspended_sessions_and_immutable_base(self):
        self.run_relay("start", "-m", "Update token definitions")
        first = self.state()["session"]
        self.write("Token.kt", "first proposal")
        self.run_relay("suspend")
        self.write("mainline.txt", "new mainline commit")
        self.git("add", "mainline.txt")
        self.git("commit", "-m", "mainline advanced")
        advanced = self.git("rev-parse", "HEAD").strip()
        self.run_relay("start")
        second = self.state()["session"]
        self.run_relay("suspend")
        self.assertIn("Multiple suspended", self.run_relay("resume", ok=False).stderr)
        self.assertEqual(len(json.loads(self.run_relay("list", "--json").stdout)), 2)
        self.run_relay("resume", first)
        self.assertEqual(self.state()["base_commit"], self.base)
        self.assertEqual(self.read("Token.kt"), "first proposal")
        self.git("add", "-A")
        self.run_relay("finish")
        self.assert_one_commit("relay/result/" + first)
        self.assertEqual(self.git("rev-parse", "main").strip(), advanced)
        self.run_relay("resume", second)
        self.assertEqual(self.state()["base_commit"], advanced)

    def test_dirty_resume_does_not_overwrite_normal_work(self):
        self.run_relay("start")
        self.write("Token.kt", "proposal")
        self.run_relay("suspend")
        self.write("Token.kt", "urgent unfinished work")
        self.run_relay("resume", ok=False)
        self.assertEqual(self.read("Token.kt"), "urgent unfinished work")

    def test_abort_double_confirmation_and_complete_recovery(self):
        self.run_relay("start")
        sid = self.state()["session"]
        self.write("Token.kt", "reviewed version\n")
        self.git("add", "Token.kt")
        self.hook("prompt-submit")
        self.write("Parser.kt", "agent proposal\n")
        self.hook("agent-stop")
        self.write("Config.kt", "staged version\n")
        self.git("add", "Config.kt")
        self.write("Config.kt", "final unstaged version\n")
        self.write("new project.txt", "untracked human code\n")
        self.write("binary.dat", b"\0\xffsaved")
        (self.repo / "README.md").unlink()
        before = self.git("status", "--porcelain", "-z")
        for answer in ("", "no\n", "abort\n", "abort\nno\n"):
            with self.subTest(answer=answer):
                self.run_relay("abort", input=answer, ok=False)
                self.assertTrue(self.state()["active"])
                self.assertEqual(self.git("status", "--porcelain", "-z"), before)
                self.assertEqual(self.git("branch", "--list", "relay/aborted/*"), "")
        result = self.run_relay("abort", input="abort\npreserve and abort\n")
        branch = f"relay/aborted/{sid}"
        self.assertIn("WARNING 1/2", result.stdout)
        self.assertIn("WARNING 2/2", result.stdout)
        self.assertIn(branch, result.stdout)
        self.assert_one_commit(branch)
        self.assertEqual(self.git("show", branch + ":Token.kt"), "reviewed version\n")
        self.assertEqual(self.git("show", branch + ":Config.kt"), "final unstaged version\n")
        self.assertEqual(self.git("show", branch + ":new project.txt"), "untracked human code\n")
        self.assertEqual(self.git("show", branch + ":binary.dat", data=b""), b"\0\xffsaved")
        self.assertNotIn("README.md", self.git("ls-tree", "--name-only", branch))
        self.assertEqual(self.git("symbolic-ref", "--short", "HEAD").strip(), "main")
        self.assertEqual(self.git("for-each-ref", "refs/relay/"), "")
        self.assert_clean()

    def test_result_and_recovery_branch_collisions_never_overwrite(self):
        self.run_relay("start", "-m", "Preserve staged token changes")
        sid = self.state()["session"]
        for prefix in ("result", "aborted"):
            self.git("branch", f"relay/{prefix}/{sid}", self.base)
        self.write("Token.kt", "keep staged code")
        self.git("add", "Token.kt")
        self.run_relay("finish", ok=False)
        self.run_relay("abort", input="abort\npreserve and abort\n", ok=False)
        self.assertTrue(self.state()["active"])
        self.assertEqual(self.read("Token.kt"), "keep staged code")
        self.assertEqual(self.git("show", ":Token.kt"), "keep staged code")
        for prefix in ("result", "aborted"):
            self.assertEqual(self.git("rev-parse", f"relay/{prefix}/{sid}").strip(), self.base)

    def test_resume_refuses_to_overwrite_ignored_files(self):
        self.run_relay("start")
        self.write("generated.ignored", "staged project file")
        self.git("add", "-f", "generated.ignored")
        self.run_relay("suspend")
        self.write("generated.ignored", "independent ignored contents")
        self.assertIn(
            "ignored file would be overwritten", self.run_relay("resume", ok=False).stderr
        )
        self.assertEqual(self.read("generated.ignored"), "independent ignored contents")
        self.assertFalse(self.state()["active"])
        (self.repo / "generated.ignored").unlink()
        self.run_relay("resume")
        self.assertEqual(self.read("generated.ignored"), "staged project file")

    def test_staged_deletion_then_ignored_recreation_is_preserved(self):
        self.write("kept.ignored", "original tracked content")
        self.git("add", "-f", "kept.ignored")
        self.git("commit", "-m", "track ignored file")
        self.run_relay("start")
        sid = self.state()["session"]
        self.git("rm", "--cached", "kept.ignored")
        self.write("kept.ignored", "recreated code must survive")
        self.run_relay("abort", input="abort\npreserve and abort\n")
        self.assertEqual(
            self.git("show", f"relay/aborted/{sid}:kept.ignored"), "recreated code must survive"
        )


class RecoveryTests(RepositoryTest):
    def test_failed_abort_keeps_recovery_branch_and_can_retry(self):
        self.run_relay("start")
        sid = self.state()["session"]
        self.write("Token.kt", "recover this code")
        relay = Relay(Git(self.repo))
        save = relay.store.save
        calls = 0

        def fail_once(state):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("simulated abort cleanup failure")
            return save(state)

        with patch.object(relay.store, "save", side_effect=fail_once):
            with self.assertRaisesRegex(Exception, "Recovery code remains at"):
                relay.abort(sid)
        branch = "relay/aborted/" + sid
        recovery = self.git("rev-parse", branch).strip()
        self.assertEqual(self.git("show", branch + ":Token.kt"), "recover this code")
        self.assertTrue(self.state()["active"])
        self.run_relay("abort", input="abort\npreserve and abort\n")
        self.assertEqual(self.git("rev-parse", branch).strip(), recovery)
        self.assert_clean()

    def test_failure_after_workspace_switch_rolls_back_staging_and_metadata(self):
        self.run_relay("start")
        self.write("Token.kt", "staged\n")
        self.git("add", "Token.kt")
        self.write("Token.kt", "unstaged\n")
        self.write("new.txt", "untracked\n")
        before = self.git("status", "--porcelain", "-z")
        before_meta = self.metadata()
        relay = Relay(Git(self.repo))
        save = relay.store.save
        calls = 0

        def fail_once(state):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("simulated state write failure")
            return save(state)

        with patch.object(relay.store, "save", side_effect=fail_once):
            with self.assertRaisesRegex(OSError, "simulated"):
                relay.suspend()
        self.assertEqual(self.git("status", "--porcelain", "-z"), before)
        self.assertEqual(self.metadata(), before_meta)
        self.assertEqual(self.read("Token.kt"), "unstaged\n")
        self.assertEqual(self.git("show", ":Token.kt"), "staged\n")
        self.assertFalse(relay.store.journal_path.exists())

    def test_crash_recovery_preserves_later_edits_and_restores_review_state(self):
        self.run_relay("start", "-m", "Refine token handling")
        self.write("Token.kt", "staged\n")
        self.git("add", "Token.kt")
        self.write("Token.kt", "unstaged\n")
        before_meta = self.metadata()
        script = (
            "import os,sys; sys.path.insert(0,sys.argv[1]); "
            "from agent_session_relay.core.git import Git; "
            "from agent_session_relay.core.session import Relay; "
            "r=Relay(Git()); r.store.save=lambda state:os._exit(77); r.suspend()"
        )
        result = subprocess.run(
            [sys.executable, "-c", script, str(SOURCE / "src")],
            cwd=self.repo,
            env=self.env,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 77, result.stderr)
        self.git("gc", "--prune=now")
        self.assertIn("relay recover", self.run_relay("status", ok=False).stderr)
        self.write("late.txt", "human edits after interruption\n")
        result = self.run_relay("recover")
        branch = result.stdout.split("preserved at ")[1].strip().removesuffix(".")
        self.assertEqual(self.git("show", branch + ":late.txt"), "human edits after interruption\n")
        self.assertEqual(self.metadata(), before_meta)
        self.assertEqual(self.state()["message"], "Refine token handling")
        self.assertEqual(self.read("Token.kt"), "unstaged\n")
        self.assertEqual(self.git("show", ":Token.kt"), "staged\n")
        self.assertFalse((self.repo / "late.txt").exists())

    def test_process_lock_is_exclusive(self):
        relay = Relay(Git(self.repo))
        with relay.store.lock():
            self.assertIn("Another Relay command", self.run_relay("start", ok=False).stderr)

    def test_unavailable_git_identity_does_not_lose_finished_code(self):
        self.run_relay("start", "-m", "Update reviewed token definitions")
        self.write("Token.kt", "staged\n")
        self.git("add", "Token.kt")
        self.git("config", "--unset", "user.name")
        self.git("config", "--unset", "user.email")
        self.git("config", "user.useConfigOnly", "true")
        self.run_relay("finish", ok=False)
        self.assertTrue(self.state()["active"])
        self.assertEqual(self.git("show", ":Token.kt"), "staged\n")
        # Abort recovery uses an internal fallback identity.
        self.run_relay("abort", input="abort\npreserve and abort\n")
