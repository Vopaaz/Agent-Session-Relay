from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from agent_session_relay.core.errors import RelayError
from agent_session_relay.core.session import Relay
from agent_session_relay.core.workspace import Workspace
from tests.support import SOURCE, RepositoryTest


class WorkspaceTest(RepositoryTest):
    def setUp(self):
        super().setUp()
        self.parent = Path(self.temporary.name)
        renamed = self.parent / "r1"
        self.repo.rename(renamed)
        self.repo = renamed
        self.other = self.parent / "r2"
        self.git("clone", "--no-local", str(self.repo), str(self.other), cwd=self.parent)
        self.git("config", "user.name", "Relay Test", cwd=self.other)
        self.git("config", "user.email", "relay-test@example.invalid", cwd=self.other)
        (self.repo / "subdir").mkdir()
        (self.other / "subdir").mkdir()

    def ws(self, *args, **kwargs):
        kwargs.setdefault("cwd", self.parent)
        return self.run_relay(*args, **kwargs)

    def start(self, message="Implement a feature across packages"):
        args = ["start", "--repo", "./r1", "--repo", "./r2"]
        if message is not None:
            args += ["-m", message]
        return self.ws(*args)

    def workspace(self):
        return Workspace(self.parent, self.parent)

    def group(self):
        workspace = self.workspace()
        return workspace.active(workspace.store.load())

    def crash(self, code):
        script = (
            "import os,sys\n"
            "sys.path.insert(0,sys.argv[1])\n"
            "from unittest.mock import patch\n"
            "from agent_session_relay.core.session import Relay\n"
            "from agent_session_relay.core.storage import Store\n"
            "from agent_session_relay.core.workspace import Workspace\n" + code
        )
        result = subprocess.run(
            [sys.executable, "-c", script, str(SOURCE / "src")],
            cwd=self.parent,
            env=self.env,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 77, result.stderr)


class WorkspaceWorkflowTests(WorkspaceTest):
    def test_every_entry_directory_sees_one_session_and_one_turn(self):
        self.start()
        locations = [
            self.parent,
            self.repo,
            self.other,
            self.repo / "subdir",
            self.other / "subdir",
        ]
        expected = json.loads(self.ws("status", "--json").stdout)
        for location in locations:
            with self.subTest(location=location):
                self.assertEqual(
                    json.loads(self.ws("status", "--json", cwd=location).stdout), expected
                )
        for location in locations:
            with self.subTest(location=location):
                context = self.ws(
                    "kiro", "hook", "prompt-submit", input='{"session_id":"a"}', cwd=location
                ).stdout
                self.assertIn('"r1"', context)
                self.assertIn('"r2"', context)
                status = json.loads(self.ws("agent", "status", cwd=location).stdout)
                self.assertEqual(status["turn"], 1)
                self.assertEqual(set(status["repositories"]), {"r1", "r2"})
        self.assertIn("No new human-to-agent handoff", context)
        self.ws("kiro", "hook", "agent-stop", input='{"session_id":"a"}', cwd=self.other)
        for location in locations:
            status = json.loads(self.ws("status", "--json", cwd=location).stdout)
            self.assertEqual(status["phase"], "human")
            self.assertEqual(status["turn"], 1)
        # Hooks can also find the workspace solely through the event cwd.
        self.ws(
            "kiro",
            "hook",
            "prompt-submit",
            cwd=SOURCE,
            input=json.dumps({"cwd": str(self.parent), "session_id": "a"}),
        )
        self.assertEqual(self.state()["turn"], 2)

    def test_review_provenance_aggregates_and_paths_stay_cwd_relative(self):
        self.start()
        self.hook("prompt-submit")
        self.write("Token.kt", "agent token\n")
        (self.other / "Config.kt").write_text("agent config\n")
        self.hook("agent-stop")
        self.git("add", "Token.kt")
        (self.other / "Config.kt").write_text("human config\n")
        context = self.ws("kiro", "hook", "prompt-submit", cwd=self.other).stdout
        self.assertIn('"r1/Token.kt"', context)
        self.assertIn('"r2/Config.kt"', context)
        self.assertEqual(
            self.ws("agent", "diff", "reviewed", "--name-only").stdout, "r1/Token.kt\n"
        )
        self.assertEqual(self.ws("agent", "diff", "human", "--name-only").stdout, "r2/Config.kt\n")
        for location in (self.parent, self.repo / "subdir", self.other):
            self.assertEqual(
                self.ws("agent", "diff", "pending", "--name-only", cwd=location).stdout,
                "r2/Config.kt\n",
            )
        patch_text = self.ws(
            "agent",
            "diff",
            "pending",
            "--repo",
            "r2",
            "--",
            "../Config.kt",
            cwd=self.other / "subdir",
        ).stdout
        self.assertIn("b/r2/Config.kt", patch_text)
        self.assertIn("+human config", patch_text)
        self.assertEqual(self.ws("agent", "diff", "pending", "--repo", "r1").stdout, "")
        self.assertEqual(
            self.ws("agent", "diff", "pending", "--", "r1", cwd=self.parent).stdout, ""
        )
        self.ws("agent", "diff", "pending", "--", "../outside", ok=False)
        self.ws("agent", "diff", "pending", "--repo", "missing", ok=False)
        self.assertIn("r2", self.ws("agent", "diff", "pending", "--stat").stdout)
        self.assertIn(
            "base", self.ws("agent", "git", "--repo", "r2", "log", "--oneline", "main").stdout
        )
        self.ws("agent", "git", "--repo", "r2", "reset", "--hard", ok=False)

    def test_finish_requires_all_reviews_and_preserves_per_repo_bases(self):
        # Give the second member a different immutable base.
        (self.other / "independent.txt").write_text("before session\n")
        self.git("add", ".", cwd=self.other)
        self.git("commit", "-m", "independent base", cwd=self.other)
        second_base = self.git("rev-parse", "HEAD", cwd=self.other).strip()
        self.start()
        group = self.group()
        self.write("Token.kt", "reviewed token\n")
        (self.other / "Config.kt").write_text("pending config\n")
        self.git("add", "Token.kt")
        self.assertIn("r2", self.ws("finish", cwd=self.repo, ok=False).stderr)
        self.assertEqual(self.git("branch", "--list", "relay/result/*"), "")
        self.assertTrue(self.state()["active"])
        self.git("add", "Config.kt", cwd=self.other)
        self.ws("message", "--repo", "r2", "-m", "Update the infrastructure")
        self.ws("finish", cwd=self.other / "subdir")
        for name, repo, base in (("r1", self.repo, self.base), ("r2", self.other, second_base)):
            branch = "relay/result/" + group["members"][name]
            self.assertEqual(self.git("symbolic-ref", "--short", "HEAD", cwd=repo).strip(), branch)
            self.assertEqual(
                self.git("rev-list", "--parents", "-n", "1", branch, cwd=repo).split()[1:], [base]
            )
            self.assertEqual(self.git("rev-parse", "main", cwd=repo).strip(), base)
            self.assertEqual(self.git("for-each-ref", "refs/relay/", cwd=repo), "")
            self.assertEqual(self.git("status", "--porcelain", cwd=repo), "")
        self.assertEqual(
            self.git("log", "-1", "--format=%B", cwd=self.other).strip(),
            "Update the infrastructure",
        )
        self.assertFalse(self.state()["active"])

    def test_unchanged_members_do_not_get_empty_commits(self):
        self.start()
        self.write("Token.kt", "reviewed\n")
        self.git("add", "Token.kt")
        self.ws("finish")
        self.assertEqual(self.git("branch", "--list", "relay/result/*", cwd=self.other), "")
        self.assertEqual(
            self.git("symbolic-ref", "--short", "HEAD", cwd=self.other).strip(), "main"
        )
        self.assertEqual(self.git("for-each-ref", "refs/relay/", cwd=self.other), "")

    def test_suspend_resume_and_multiple_sessions_preserve_each_index_through_gc(self):
        self.start()
        first = self.group()["id"]
        before = {}
        for repo in (self.repo, self.other):
            (repo / "Token.kt").write_text("staged\n")
            self.git("add", "Token.kt", cwd=repo)
            (repo / "Token.kt").write_text("unstaged\n")
            (repo / "new.bin").write_bytes(b"\0\xffdata")
            (repo / "intent.txt").write_text("intent\n")
            self.git("add", "-N", "intent.txt", cwd=repo)
            before[repo] = self.git("status", "--porcelain", "-z", cwd=repo)
        self.ws("btw", cwd=self.repo)
        self.ws("suspend", cwd=self.other)
        for repo in (self.repo, self.other):
            self.assertEqual(self.git("status", "--porcelain", cwd=repo), "")
            self.git("gc", "--prune=now", cwd=repo)
        self.assertEqual(self.ws("kiro", "hook", "prompt-submit").stdout, "")
        self.ws("start", "-m", "Another feature", cwd=self.repo)
        second = self.group()["id"]
        self.ws("suspend")
        self.assertIn("Multiple suspended", self.ws("resume", ok=False).stderr)
        self.assertEqual(len(json.loads(self.ws("list", "--json").stdout)), 2)
        self.ws("resume", first, cwd=self.other / "subdir")
        self.assertEqual(self.state()["next_turn"], "btw")
        for repo in (self.repo, self.other):
            self.assertEqual(self.git("status", "--porcelain", "-z", cwd=repo), before[repo])
            self.assertEqual(self.git("show", ":Token.kt", cwd=repo), "staged\n")
            self.assertEqual((repo / "new.bin").read_bytes(), b"\0\xffdata")
        self.ws("abort", input="abort\n")
        self.ws("resume", second)
        self.assertEqual(self.group()["id"], second)

    def test_btw_restores_all_members_and_does_not_consume_approvals(self):
        self.start()
        for repo in (self.repo, self.other):
            (repo / "Token.kt").write_text("approved\n")
            self.git("add", "Token.kt", cwd=repo)
            (repo / "Config.kt").write_text("human proposal\n")
        self.ws("btw", cwd=self.other)
        self.hook("prompt-submit")
        self.assertEqual(self.state()["turn_kind"], "btw")
        self.assertEqual(self.ws("agent", "diff", "reviewed").stdout, "")
        self.hook(
            "pre-tool-use",
            {"tool_name": "shell", "tool_input": {"command": "touch r2/x"}},
            ok=False,
        )
        for repo in (self.repo, self.other):
            (repo / "Config.kt").write_text("unexpected agent write\n")
            self.git("add", "Config.kt", cwd=repo)
        notice = self.ws("kiro", "hook", "agent-stop", cwd=self.other).stdout
        self.assertIn("r1", notice.lower())
        self.assertIn("r2", notice.lower())
        for repo in (self.repo, self.other):
            self.assertEqual((repo / "Config.kt").read_text(), "human proposal\n")
            self.assertEqual(self.git("show", ":Config.kt", cwd=repo), "old config\n")
            self.assertEqual(self.git("show", ":Token.kt", cwd=repo), "approved\n")
        self.hook("prompt-submit")
        self.assertEqual(
            self.ws("agent", "diff", "reviewed", "--name-only").stdout, "r1/Token.kt\nr2/Token.kt\n"
        )

    def test_abort_confirms_once_and_preserves_every_repo(self):
        self.start()
        group = self.group()
        self.write("new.txt", "r1 pending\n")
        (self.other / "new.txt").write_text("r2 pending\n")
        self.ws("abort", input="no\n", ok=False)
        self.assertTrue(self.state()["active"])
        output = self.ws("abort", input="abort\n", cwd=self.other).stdout
        self.assertEqual(output.count("Type 'abort'"), 1)
        for name, repo in (("r1", self.repo), ("r2", self.other)):
            branch = "relay/aborted/" + group["members"][name]
            self.assertIn(branch, output)
            self.assertEqual(self.git("show", branch + ":new.txt", cwd=repo), f"{name} pending\n")
            self.assertEqual(self.git("status", "--porcelain", cwd=repo), "")

    def test_owner_and_interrupted_turn_boundary_are_shared(self):
        self.start()
        self.hook("prompt-submit", {"session_id": "owner"})
        before = self.state()
        self.ws(
            "kiro",
            "hook",
            "prompt-submit",
            input='{"session_id":"other"}',
            cwd=self.other,
            ok=False,
        )
        self.ws(
            "kiro", "hook", "agent-stop", input='{"session_id":"other"}', cwd=self.other, ok=False
        )
        self.assertEqual(self.state(), before)
        (self.other / "Token.kt").write_text("partial output\n")
        self.ws("end-interrupted-agent-turn", cwd=self.repo / "subdir")
        self.assertEqual(self.state()["phase"], "human")
        self.assertEqual((self.other / "Token.kt").read_text(), "partial output\n")
        self.ws("end-interrupted-agent-turn", ok=False)

    def test_unselected_and_nested_repositories_do_not_join(self):
        third = self.parent / "r3"
        self.git("clone", str(self.repo), str(third), cwd=self.parent)
        self.start()
        self.assertEqual(
            json.loads(self.ws("agent", "status", cwd=third).stdout), {"active": False}
        )
        self.assertEqual(self.ws("kiro", "hook", "prompt-submit", cwd=third).stdout, "")
        nested = self.repo / "nested.ignored"
        self.git("clone", str(self.other), str(nested), cwd=self.parent)
        self.assertEqual(self.ws("kiro", "hook", "prompt-submit", cwd=nested).stdout, "")

    def test_invalid_members_and_dirty_start_leave_every_repo_untouched(self):
        for paths in (("r1", "r1"), ("r1/subdir", "r2"), ("r1", ".")):
            self.ws("start", "--repo", paths[0], "--repo", paths[1], ok=False)
            self.assertEqual(self.git("symbolic-ref", "--short", "HEAD").strip(), "main")
            self.assertEqual(self.git("for-each-ref", "refs/relay/"), "")
        (self.other / "dirty.txt").write_text("preserve\n")
        self.ws("start", "--repo", "r1", "--repo", "r2", ok=False)
        self.assertEqual(self.git("symbolic-ref", "--short", "HEAD").strip(), "main")
        self.assertFalse((self.parent / ".relay/state.json").exists())
        self.assertEqual((self.other / "dirty.txt").read_text(), "preserve\n")

    def test_existing_independent_session_cannot_be_enrolled(self):
        self.run_relay("start")
        self.ws("start", "--repo", "r1", "--repo", "r2", ok=False)
        self.assertEqual(
            self.git("symbolic-ref", "--short", "HEAD", cwd=self.other).strip(), "main"
        )
        self.assertFalse((self.parent / ".relay/state.json").exists())

    def test_linked_worktrees_have_distinct_namespaces_and_results(self):
        linked = self.parent / "linked"
        self.git("worktree", "add", "-b", "linked-base", str(linked))
        self.ws("start", "--repo", "r1", "--repo", "linked", "-m", "Both worktrees")
        for repo in (self.repo, linked):
            (repo / "Token.kt").write_text(repo.name + "\n")
            self.git("add", "Token.kt", cwd=repo)
        self.ws("finish", cwd=linked)
        branches = self.git("for-each-ref", "--format=%(refname)", "refs/heads/relay/result/")
        self.assertEqual(len(branches.splitlines()), 2)
        self.assertEqual(self.git("for-each-ref", "refs/relay/"), "")

    def test_nul_names_binary_patches_and_literal_path_filters(self):
        self.start()
        (self.other / "new\nline.txt").write_text("new\n")
        (self.other / "[literal].txt").write_text("literal\n")
        (self.other / "binary.dat").write_bytes(b"\0\xffnew")
        names = self.ws("agent", "diff", "pending", "--name-only", "-z").stdout.split("\0")
        self.assertIn("r2/new\nline.txt", names)
        self.assertIn("GIT binary patch", self.ws("agent", "diff", "pending").stdout)
        selected = self.ws("agent", "diff", "pending", "--", "r2/[literal].txt").stdout
        self.assertIn("+literal", selected)
        self.assertNotIn("binary.dat", selected)


class WorkspaceRecoveryTests(WorkspaceTest):
    def test_failure_in_second_member_restores_first_member_and_all_staging(self):
        self.start()
        before = self.state()
        for repo in (self.repo, self.other):
            (repo / "Token.kt").write_text("staged\n")
            self.git("add", "Token.kt", cwd=repo)
            (repo / "Token.kt").write_text("unstaged\n")
        original = Relay.suspend

        def fail_second(relay):
            if relay.git.root == self.other:
                raise OSError("second member failed")
            return original(relay)

        with patch.object(Relay, "suspend", fail_second):
            with self.assertRaisesRegex(OSError, "second member"):
                self.workspace().suspend()
        for name, repo in (("r1", self.repo), ("r2", self.other)):
            self.assertEqual(
                self.git("rev-parse", "HEAD", cwd=repo).strip(),
                before["repositories"][name]["reviewed_checkpoint"],
            )
            self.assertEqual(self.git("show", ":Token.kt", cwd=repo), "staged\n")
            self.assertEqual((repo / "Token.kt").read_text(), "unstaged\n")
        self.assertFalse((self.parent / ".relay/transaction.json").exists())
        self.ws("suspend")

    def test_crash_mid_finish_preserves_later_edits_and_all_provenance_through_gc(self):
        self.start()
        self.hook("prompt-submit")
        for repo in (self.repo, self.other):
            (repo / "Token.kt").write_text("agent output\n")
        self.hook("agent-stop")
        for repo in (self.repo, self.other):
            (repo / "Token.kt").write_text("human reviewed\n")
            self.git("add", "Token.kt", cwd=repo)
        self.hook("prompt-submit")
        self.hook("agent-stop")
        before = self.state()
        human = self.ws("agent", "diff", "human").stdout
        self.crash(
            "original=Relay.finish\n"
            "def crash_first(self,*args,**kwargs):\n"
            "    result=original(self,*args,**kwargs)\n"
            "    if self.git.root.name=='r1': os._exit(77)\n"
            "    return result\n"
            "with patch.object(Relay,'finish',crash_first): Workspace('.').finish()\n"
        )
        for repo in (self.repo, self.other):
            self.git("reflog", "expire", "--expire=now", "--all", cwd=repo)
            self.git("gc", "--prune=now", cwd=repo)
            (repo / "late.txt").write_text(repo.name + " later edits\n")
        for location in (self.parent, self.repo, self.other / "subdir"):
            self.assertIn("relay recover", self.ws("status", cwd=location, ok=False).stderr)
            self.ws("kiro", "hook", "prompt-submit", cwd=location, ok=False)
        output = self.ws("recover", cwd=self.other).stdout
        self.assertEqual(self.state(), before)
        self.assertEqual(self.ws("agent", "diff", "human").stdout, human)
        for repo in (self.repo, self.other):
            branch = self.git(
                "for-each-ref", "--format=%(refname)", "refs/heads/relay/recovered/", cwd=repo
            ).strip()
            self.assertIn(branch.removeprefix("refs/heads/"), output)
            self.assertEqual(
                self.git("show", branch + ":late.txt", cwd=repo), repo.name + " later edits\n"
            )
            self.assertFalse((repo / "late.txt").exists())
            self.assertEqual(self.git("branch", "--list", "relay/result/*", cwd=repo), "")
        self.ws("finish")

    def test_crash_after_commit_completes_cleanup_without_undoing_results(self):
        self.start()
        for repo in (self.repo, self.other):
            (repo / "Token.kt").write_text("approved\n")
            self.git("add", "Token.kt", cwd=repo)
        self.crash(
            "original=Store.complete\n"
            "def crash_during_cleanup(self,journal):\n"
            "    original(self,journal)\n"
            "    if self.git.root.name=='r1': os._exit(77)\n"
            "with patch.object(Store,'complete',crash_during_cleanup):\n"
            "    Workspace('.').finish()\n"
        )
        for repo in (self.repo, self.other):
            self.git("gc", "--prune=now", cwd=repo)
        (self.other / "late.txt").write_text("keep in place\n")
        self.assertIn("cleanup completed", self.ws("recover", cwd=self.repo).stdout)
        self.assertFalse(self.state()["active"])
        self.assertEqual((self.other / "late.txt").read_text(), "keep in place\n")
        for repo in (self.repo, self.other):
            self.assertTrue(self.git("branch", "--list", "relay/result/*", cwd=repo))
            self.assertEqual(self.git("for-each-ref", "refs/relay/", cwd=repo), "")

    def test_crash_during_start_is_discoverable_from_all_members(self):
        self.crash(
            "original=Relay.start\n"
            "def crash_first(self,*args,**kwargs):\n"
            "    result=original(self,*args,**kwargs)\n"
            "    os._exit(77)\n"
            "with patch.object(Relay,'start',crash_first):\n"
            "    Workspace('.').start(repos=['r1','r2'])\n"
        )
        self.ws("kiro", "hook", "prompt-submit", cwd=self.other, ok=False)
        self.ws("recover", cwd=self.other / "subdir")
        for repo in (self.repo, self.other):
            self.assertEqual(self.git("symbolic-ref", "--short", "HEAD", cwd=repo).strip(), "main")
            self.assertEqual(self.git("for-each-ref", "refs/relay/sessions/", cwd=repo), "")
        self.start()

    def test_dirty_resume_and_missing_member_leave_other_members_untouched(self):
        self.start()
        self.write("Token.kt", "saved proposal\n")
        self.ws("suspend")
        (self.other / "Token.kt").write_text("outside work\n")
        self.ws("resume", ok=False)
        self.assertEqual(self.read("Token.kt"), "TokenDefinition\n")
        self.assertEqual((self.other / "Token.kt").read_text(), "outside work\n")
        self.git("restore", "Token.kt", cwd=self.other)
        moved = self.parent / "temporarily-moved"
        self.other.rename(moved)
        self.ws("resume", cwd=self.repo, ok=False)
        self.assertEqual(self.read("Token.kt"), "TokenDefinition\n")
        moved.rename(self.other)
        self.ws("resume")
        self.assertEqual(self.read("Token.kt"), "saved proposal\n")

    def test_editor_does_not_hold_locks_and_revalidates_group(self):
        self.start(message=None)
        self.write("Token.kt", "approved\n")
        self.git("add", "Token.kt")
        workspace = self.workspace()

        def editor(initial):
            self.ws("status")
            self.ws("message", "-m", "Changed while editing", cwd=self.other)
            return "Editor message"

        with self.assertRaisesRegex(RelayError, "changed while editing"):
            workspace.finish(editor=editor)
        self.assertTrue(self.state()["active"])
        self.assertEqual(self.git("branch", "--list", "relay/result/*"), "")
        before = copy.deepcopy(self.group())
        self.ws("finish")
        self.assertEqual(self.git("log", "-1", "--format=%B").strip(), before["message"])
