from __future__ import annotations

import copy
import json
import shlex
import sys
from pathlib import Path
from unittest.mock import patch

from agent_session_relay.core.errors import RelayError
from agent_session_relay.core.session import Relay
from tests.test_workspace import WorkspaceTest


class RepositoryFinishTests(WorkspaceTest):
    def finish(self, *paths, **kwargs):
        return self.ws("repo", "finish", *paths, **kwargs)

    def approve(self, repo, content="approved\n"):
        (repo / "Token.kt").write_text(content)
        self.git("add", "Token.kt", cwd=repo)

    def member_state(self, repo):
        directory = Path(self.git("rev-parse", "--absolute-git-dir", cwd=repo).strip())
        return {
            "state": (directory / "agent-session-relay/state.json").read_bytes(),
            "index": (directory / "index").read_bytes(),
            "head": self.git("rev-parse", "HEAD", cwd=repo),
            "refs": self.git("for-each-ref", cwd=repo),
            "workspace": self.git("diff-files", "--binary", cwd=repo),
        }

    def assert_result(self, repo, base, message):
        branch = self.git("symbolic-ref", "--short", "HEAD", cwd=repo).strip()
        self.assertTrue(branch.startswith("relay/result/"), branch)
        self.assertEqual(
            self.git("rev-list", "--parents", "-n", "1", "HEAD", cwd=repo).split()[1:], [base],
        )
        self.assertEqual(self.git("log", "-1", "--format=%B", cwd=repo).strip(), message)
        self.assertEqual(self.git("for-each-ref", "refs/relay/", cwd=repo), "")
        self.assertEqual(self.git("status", "--porcelain", cwd=repo), "")
        return branch

    def test_partial_finish_preserves_remaining_review_and_lifecycle(self):
        self.start()
        self.hook("prompt-submit")
        self.hook("agent-stop")
        self.ws("message", "--repo", "r1", "-m", "First package")
        self.ws("message", "--repo", "r2", "-m", "Second package")
        self.ws("btw")
        self.approve(self.repo)
        self.approve(self.other)
        (self.other / "Token.kt").write_text("pending\n")
        (self.other / "new.txt").write_text("pending addition\n")
        before = copy.deepcopy(self.group())
        remaining = self.member_state(self.other)
        output = self.finish("../../r1", cwd=self.other / "subdir").stdout
        branch = self.assert_result(self.repo, self.base, "First package")
        self.assertIn("Remaining repositories: r2", output)
        self.assertNotIn("Start a new agent session", output)
        del before["members"]["r1"]
        del before["messages"]["r1"]
        self.assertEqual(self.group(), before)
        self.assertEqual(self.member_state(self.other), remaining)
        self.assertEqual((self.other / "new.txt").read_text(), "pending addition\n")
        self.assertEqual(set(self.state_from_parent()["repositories"]), {"r2"})
        self.assertEqual(
            set(self.ws("agent", "diff", "pending", "--name-only").stdout.splitlines()),
            {"r2/Token.kt", "r2/new.txt"},
        )
        self.ws("suspend")
        self.ws("resume", cwd=self.other)
        self.assertEqual(self.group(), before)
        context = self.ws("kiro", "hook", "prompt-submit").stdout
        self.assertIn(str(self.other), context)
        self.assertNotIn(str(self.repo), context)
        self.ws("kiro", "hook", "agent-stop")
        self.git("add", ".", cwd=self.other)
        self.ws("finish")
        self.assertEqual(self.git("symbolic-ref", "--short", "HEAD").strip(), branch)
        self.assert_result(self.other, self.base, "Second package")

    def state_from_parent(self):
        return json.loads(self.ws("status", "--json").stdout)

    def test_multiple_targets_and_last_member_finish_the_session(self):
        third = self.parent / "r3"
        self.git("clone", str(self.other), str(third))
        self.ws("start", "--repo", "r1", "--repo", "r2", "--repo", "r3", "-m", "Feature")
        sid = self.group()["id"]
        self.approve(self.repo)
        self.approve(self.other)
        self.finish("r2", "r1")
        self.assert_result(self.repo, self.base, "Feature")
        self.assert_result(self.other, self.base, "Feature")
        self.assertEqual(set(self.group()["members"]), {"r3"})
        self.assertEqual(self.group()["id"], sid)
        output = self.finish("r3").stdout
        self.assertIn("no net changes", output)
        self.assertIn("no repositories remain", output)
        self.assertFalse(self.state_from_parent()["active"])
        self.assertEqual(self.workspace().store.load()["sessions"], {})
        self.assertEqual(self.git("symbolic-ref", "--short", "HEAD", cwd=third), "main\n")
        self.assertEqual(self.git("branch", "--list", "relay/result/*", cwd=third), "")

    def test_invalid_or_pending_targets_do_not_finish_any_members(self):
        self.start()
        self.approve(self.repo)
        (self.other / "Token.kt").write_text("pending\n")
        before = copy.deepcopy(self.group())
        snapshots = {repo: self.member_state(repo) for repo in (self.repo, self.other)}
        for targets in ((), ("r1", "missing"), ("r1", "r1"),
                        ("r1", str(self.repo)), ("r1/subdir",), ("r1", "r2")):
            with self.subTest(targets=targets):
                self.finish(*targets, ok=False)
                self.assertEqual(self.group(), before)
                for repo, snapshot in snapshots.items():
                    self.assertEqual(self.member_state(repo), snapshot)

    def test_requires_an_active_human_phase_and_remains_guarded(self):
        self.finish("r1", ok=False)
        self.start()
        self.hook("prompt-submit")
        before = copy.deepcopy(self.group())
        result = self.finish("r1", ok=False)
        self.assertIn("human phase", result.stderr)
        self.assertEqual(self.group(), before)
        self.hook("pre-tool-use", {
            "tool_name": "shell", "tool_input": {"command": "relay repo finish ../r2"},
        }, ok=False)
        self.hook("agent-stop")
        self.ws("suspend")
        self.finish("r1", ok=False)

    def test_only_selected_messages_are_required_and_readding_starts_from_the_result(self):
        self.start(message=None)
        self.ws("message", "--repo", "r1", "-m", "First package")
        self.approve(self.repo)
        (self.other / "Token.kt").write_text("pending\n")
        # The remaining member has no message and must not trigger the editor.
        self.finish("r1")
        first = self.assert_result(self.repo, self.base, "First package")
        base = self.git("rev-parse", "HEAD").strip()
        self.ws("repo", "add", "r1")
        self.assertNotIn("r1", self.group()["messages"])
        self.approve(self.repo, "second approved change\n")
        self.finish("r1", "-m", "Second phase", "-m", "Independent result.")
        second = self.assert_result(self.repo, base, "Second phase\n\nIndependent result.")
        self.assertNotEqual(first, second)
        self.assertIsNone(self.group()["message"])
        self.assertEqual(self.group()["messages"], {})
        self.assertIsNone(self.state_from_parent()["repositories"]["r2"]["message"])
        refs = self.git("for-each-ref", "refs/heads/relay/result/")
        self.ws("abort", input="abort\n")
        self.assertEqual(self.git("for-each-ref", "refs/heads/relay/result/"), refs)

    def test_editor_uses_selected_repository_configuration(self):
        self.start(message=None)
        self.approve(self.other)
        (self.repo / "Token.kt").write_text("pending\n")
        script = self.parent / "editor.py"
        script.write_text(
            "import sys\nfrom pathlib import Path\n"
            "assert Path.cwd().name == 'r2'\n"
            "Path(sys.argv[-1]).write_text('Selected repository message\\n')\n"
        )
        self.env.pop("GIT_EDITOR")
        self.git("config", "core.editor", "false")
        self.git("config", "core.editor", shlex.join([sys.executable, str(script)]), cwd=self.other)
        self.finish("r2")
        self.assert_result(self.other, self.base, "Selected repository message")
        self.assertIsNone(self.group()["message"])

    def test_editor_rechecks_membership_without_holding_locks(self):
        third = self.parent / "r3"
        self.git("clone", str(self.other), str(third))
        self.start(message=None)
        self.approve(self.repo)

        def editor(initial):
            self.ws("status")
            self.ws("repo", "add", "r3")
            return "Final message"

        with self.assertRaisesRegex(RelayError, "changed while editing"):
            self.workspace().finish(editor=editor, repos=["r1"])
        self.assertEqual(set(self.group()["members"]), {"r1", "r2", "r3"})
        self.assertEqual(self.git("branch", "--list", "relay/result/*"), "")

    def test_editor_rechecks_selected_pending_changes(self):
        self.start(message=None)
        self.approve(self.repo)
        before = copy.deepcopy(self.group())

        def editor(initial):
            self.write("Token.kt", "new pending changes\n")
            return "Final message"

        with self.assertRaisesRegex(RelayError, "Pending changes remain in r1"):
            self.workspace().finish(editor=editor, repos=["r1"])
        self.assertEqual(self.group(), before)
        self.assertEqual(self.read("Token.kt"), "new pending changes\n")
        self.assertEqual(self.git("branch", "--list", "relay/result/*"), "")

    def test_failure_on_second_target_restores_all_members_and_result_refs(self):
        third = self.parent / "r3"
        self.git("clone", str(self.other), str(third))
        self.ws("start", "--repo", "r1", "--repo", "r2", "--repo", "r3", "-m", "Feature")
        self.approve(self.repo)
        self.approve(self.other)
        self.approve(third)
        (third / "Token.kt").write_text("pending\n")
        before = copy.deepcopy(self.group())
        snapshots = {repo: self.member_state(repo) for repo in (self.repo, self.other, third)}
        finish = Relay.finish

        def fail_second(relay, *args, **kwargs):
            branch = finish(relay, *args, **kwargs)
            if relay.git.root == self.other:
                raise OSError("second finish failed")
            return branch

        with patch.object(Relay, "finish", fail_second):
            with self.assertRaisesRegex(OSError, "second finish failed"):
                self.workspace().finish(repos=["r1", "r2"])
        self.assertEqual(self.group(), before)
        for repo, snapshot in snapshots.items():
            self.assertEqual(self.member_state(repo), snapshot)
        self.finish("r1", "r2")

    def test_crash_after_member_removal_rolls_back_from_the_removed_repository(self):
        self.start()
        self.approve(self.repo)
        before = copy.deepcopy(self.group())
        self.crash(
            "from agent_session_relay.core.workspace import WorkspaceStore\n"
            "original=WorkspaceStore.save\n"
            "def crash_after_save(self,state):\n"
            "    original(self,state)\n"
            "    os._exit(77)\n"
            "with patch.object(WorkspaceStore,'save',crash_after_save):\n"
            "    Workspace('.').finish(repos=['r1'])\n"
        )
        for repo in (self.repo, self.other):
            self.git("gc", "--prune=now", cwd=repo)
            (repo / "late.txt").write_text(repo.name + " later work\n")
            self.ws("kiro", "hook", "prompt-submit", cwd=repo, ok=False)
        self.ws("recover", cwd=self.repo / "subdir")
        self.assertEqual(self.group(), before)
        self.assertEqual(self.git("branch", "--list", "relay/result/*"), "")
        for repo in (self.repo, self.other):
            branch = self.git(
                "for-each-ref", "--format=%(refname)", "refs/heads/relay/recovered/", cwd=repo,
            ).strip()
            self.assertEqual(
                self.git("show", branch + ":late.txt", cwd=repo), repo.name + " later work\n",
            )
        self.finish("r1")

    def test_recovery_after_commit_keeps_removed_members_finished(self):
        self.start()
        self.approve(self.repo)
        self.crash(
            "original=Store.complete\n"
            "def crash_during_cleanup(self,journal):\n"
            "    original(self,journal)\n"
            "    os._exit(77)\n"
            "with patch.object(Store,'complete',crash_during_cleanup):\n"
            "    Workspace('.').finish(repos=['r1'])\n"
        )
        for repo in (self.repo, self.other):
            (repo / "late.txt").write_text("keep in place\n")
        self.assertIn("cleanup completed", self.ws("recover", cwd=self.repo).stdout)
        self.assertEqual(set(self.group()["members"]), {"r2"})
        self.assertFalse(json.loads(self.run_relay("status", "--json").stdout)["active"])
        self.assertTrue(self.git("branch", "--list", "relay/result/*"))
        for repo in (self.repo, self.other):
            self.assertEqual((repo / "late.txt").read_text(), "keep in place\n")
            self.assertEqual(self.git("for-each-ref", "refs/relay/transactions/", cwd=repo), "")

    def test_finishing_promoted_original_leaves_discovery_and_guard_unchanged(self):
        self.run_relay("start", "-m", "Feature")
        self.hook("prompt-submit", {"session_id": "old"})
        self.hook("agent-stop", {"session_id": "old"})
        sid = self.state()["session"]
        self.run_relay("repo", "add", "../r2")
        self.approve(self.repo)
        output = self.finish(".", cwd=self.repo).stdout
        self.assertIn("Start a new agent session", output)
        self.assertFalse(self.state()["active"])
        self.assertEqual(self.group()["id"], sid)
        context = self.ws(
            "kiro", "hook", "prompt-submit", cwd=self.other, input='{"session_id":"new"}',
        ).stdout
        self.assertIn(str(self.other), context)
        self.assertNotIn(str(self.repo), context)
        self.assertEqual(self.hook("prompt-submit").stdout, "")
        self.ws("kiro", "hook", "pre-tool-use", ok=False, input=json.dumps({
            "tool_name": "shell", "tool_input": {"command": f"git -C {self.repo} status"},
        }))

    def test_finishing_added_member_keeps_original_working_directory_active(self):
        self.run_relay("start", "-m", "Feature")
        self.run_relay("repo", "add", "../r2")
        output = self.finish("../r2", cwd=self.repo).stdout
        self.assertNotIn("Start a new agent session", output)
        self.assertEqual(set(self.state()["repositories"]), {"r1"})
        self.assertIn(str(self.repo), self.hook("prompt-submit").stdout)

    def test_other_suspended_sessions_keep_their_original_membership(self):
        self.start()
        old = copy.deepcopy(self.group())
        self.ws("suspend")
        self.start()
        self.finish("r1")
        old["state"] = "suspended"
        self.assertEqual(self.workspace().store.load()["sessions"][old["id"]], old)
        self.ws("suspend")
        self.ws("resume", old["id"])
        self.assertEqual(set(self.group()["members"]), {"r1", "r2"})

    def test_linked_worktree_finish_preserves_the_remaining_members_refs(self):
        linked = self.parent / "linked"
        self.git("worktree", "add", "-b", "linked-base", str(linked))
        self.ws("start", "--repo", "r1", "--repo", "linked", "-m", "Feature")
        self.approve(self.repo)
        self.approve(linked)
        before = self.metadata()
        members = self.group()["members"]
        self.finish("linked")
        self.assertEqual(self.metadata(), before)
        self.assertEqual(set(self.group()["members"]), {"r1"})
        self.assertEqual(self.git(
            "for-each-ref", f"refs/relay/sessions/{members['linked']['id']}/",
        ), "")
        self.assertTrue(self.git(
            "for-each-ref", f"refs/relay/sessions/{members['r1']['id']}/",
        ))
        self.finish("r1")
        self.assert_result(self.repo, self.base, "Feature")
        self.assert_result(linked, self.base, "Feature")

    def test_standalone_repo_finish_is_human_only_and_requires_its_root(self):
        self.run_relay("start", "-m", "Feature")
        self.hook("prompt-submit")
        self.assertIn("human phase", self.finish(".", cwd=self.repo, ok=False).stderr)
        self.hook("agent-stop")
        self.approve(self.repo)
        for targets in (("../r2",), (".", "."), ("subdir",)):
            self.finish(*targets, cwd=self.repo, ok=False)
        self.finish("..", cwd=self.repo / "subdir")
        self.assert_result(self.repo, self.base, "Feature")
        self.assertFalse(self.state()["active"])
