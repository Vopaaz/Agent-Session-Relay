from __future__ import annotations

import copy
import json
import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from agent_session_relay.core.errors import RelayError
from agent_session_relay.core.git import Git
from agent_session_relay.core.session import Relay
from agent_session_relay.core.storage import atomic_json
from agent_session_relay.core.workspace import Workspace, add_repository
from tests.support import injected_stats
from tests.test_workspace import WorkspaceTest


class RepositoryAddTests(WorkspaceTest):
    def add(self, path="../r2", *paths, **kwargs):
        return self.run_relay("repo", "add", path, *paths, **kwargs)

    def test_batch_promotion_uses_all_paths_and_preserves_the_original_review_state(self):
        packages = self.parent / "packages"
        packages.mkdir()
        self.repo.rename(packages / "r1")
        self.repo = packages / "r1"
        self.other.rename(packages / "r2")
        self.other = packages / "r2"
        third = self.parent / "r3"
        self.git("clone", str(self.other), str(third))
        self.run_relay("start", "-m", "Shared feature")
        self.hook("prompt-submit")
        self.hook("agent-stop")
        self.write("Token.kt", "approved\n")
        self.git("add", "Token.kt")
        self.write("Token.kt", "unstaged\n")
        before = self.metadata()
        index = self.git("write-tree")
        refs = self.git("for-each-ref", "refs/relay/")
        output = self.add("../../r2", "../../../r3", cwd=self.repo / "subdir").stdout
        self.assertIn("Repositories added: packages/r2, r3", output)
        group = self.group()
        self.assertEqual(set(group["members"]), {"packages/r1", "packages/r2", "r3"})
        expected = copy.deepcopy(before)
        expected["sessions"][group["id"]]["coordinator"] = str(self.parent / ".relay/state.json")
        self.assertEqual(self.metadata(), expected)
        self.assertEqual(self.git("write-tree"), index)
        self.assertEqual(self.git("for-each-ref", "refs/relay/"), refs)
        self.assertEqual(self.read("Token.kt"), "unstaged\n")
        for path in (self.other, third):
            status = json.loads(self.run_relay("status", "--json", cwd=path).stdout)
            self.assertEqual(status["session"], group["id"])
            self.assertEqual(status["turn"], 1)
            self.assertEqual(status["message"], "Shared feature")

    def test_batch_add_to_workspace_preserves_existing_members_and_next_turn_mode(self):
        third = self.parent / "r3"
        self.git("clone", str(self.other), str(third))
        self.ws("start", "--repo", "r1", "-m", "Shared feature")
        self.hook("prompt-submit")
        self.hook("agent-stop")
        self.run_relay("btw")
        before = copy.deepcopy(self.group())
        self.add("../r2", "../r3")
        group = self.group()
        expected = copy.deepcopy(before)
        expected["members"].update({
            name: group["members"][name] for name in ("r2", "r3")
        })
        self.assertEqual(group, expected)
        self.assertEqual(len({member["id"] for member in group["members"].values()}), 3)
        for path in (self.other, third):
            local = Relay(Git(path), coordinator=str(self.workspace().store.path))
            session = local.active(local.store.load())
            self.assertEqual(session["turn"], 1)
            self.assertEqual(session["next_turn"], "btw")
            self.assertEqual(session["base_commit"], self.base)
        self.run_relay("suspend")
        self.run_relay("resume", cwd=third)
        self.assertEqual(self.group(), group)

    def test_invalid_batch_does_not_enroll_any_target(self):
        third = self.parent / "r3"
        self.git("clone", str(self.other), str(third))
        (third / "dirty.txt").write_text("preserve\n")
        for workspace in (False, True):
            with self.subTest(workspace=workspace):
                if workspace:
                    self.ws("start", "--repo", "r1", "-m", "Feature")
                else:
                    self.run_relay("start", "-m", "Feature")
                before = self.metadata()
                group = copy.deepcopy(self.group()) if workspace else None
                for path in ("../missing", "../r2", "../r2/../r2", ".", "../r2/subdir", "../r3"):
                    with self.subTest(path=path):
                        self.add("../r2", path, ok=False)
                        self.assertEqual(self.metadata(), before)
                        if workspace:
                            self.assertEqual(self.group(), group)
                        else:
                            self.assertFalse((self.parent / ".relay/state.json").exists())
                        self.assertEqual(
                            self.git("symbolic-ref", "--short", "HEAD", cwd=self.other), "main\n",
                        )
                        self.assertEqual(
                            self.git("for-each-ref", "refs/relay/", cwd=self.other), "",
                        )
                self.run_relay("finish")

    def test_failure_on_second_batch_member_rolls_back_all_enrollments(self):
        third = self.parent / "r3"
        self.git("clone", str(self.other), str(third))
        self.run_relay("start", "-m", "Feature")
        self.write("Token.kt", "approved\n")
        self.git("add", "Token.kt")
        self.write("Token.kt", "unstaged\n")
        before = self.metadata()
        start = Relay.start
        started = []

        def fail_second(relay, *args, **kwargs):
            started.append(relay.git.root)
            if relay.git.root == third:
                raise OSError("second enrollment failed")
            return start(relay, *args, **kwargs)

        with patch.object(Relay, "start", fail_second):
            with self.assertRaisesRegex(OSError, "second enrollment failed"):
                add_repository(["../r2", "../r3"], self.repo)
        self.assertEqual(started, [self.other, third])
        self.assertEqual(self.metadata(), before)
        self.assertNotIn("repositories", self.state())
        self.assertEqual(self.git("show", ":Token.kt"), "approved\n")
        self.assertEqual(self.read("Token.kt"), "unstaged\n")
        for path in (self.other, third):
            self.assertEqual(self.git("symbolic-ref", "--short", "HEAD", cwd=path), "main\n")
            self.assertEqual(self.git("for-each-ref", "refs/relay/", cwd=path), "")
            self.assertEqual(self.git("status", "--porcelain", cwd=path), "")
        self.add("../r2", "../r3")

    def test_single_promotion_preserves_reviews_and_all_commands_use_session_members(self):
        self.run_relay("start", "-m", "Shared feature")
        sid = self.state()["session"]
        self.hook("prompt-submit", {"session_id": "owner"})
        self.write("Token.kt", "agent output\n")
        self.hook("agent-stop", {"session_id": "owner"})
        self.git("add", "Token.kt")
        self.hook("prompt-submit", {"session_id": "owner"})
        self.hook("agent-stop", {"session_id": "owner"})
        self.write("Config.kt", "staged\n")
        self.git("add", "Config.kt")
        self.write("Config.kt", "unstaged\n")
        before = self.metadata()
        refs = self.git("for-each-ref", "refs/relay/")
        index = self.git("write-tree")
        output = self.add("../../r2", cwd=self.repo / "subdir").stdout
        self.assertIn(sid, output)
        self.assertNotIn("harness", output.lower())
        self.assertEqual(self.git("write-tree"), index)
        self.assertEqual(self.git("for-each-ref", "refs/relay/"), refs)
        expected = copy.deepcopy(before)
        expected["sessions"][sid]["coordinator"] = str(self.parent / ".relay/state.json")
        self.assertEqual(self.metadata(), expected)
        group = self.group()
        self.assertEqual(group["id"], sid)
        self.assertEqual(set(group["members"]), {"r1", "r2"})
        self.assertNotIn("repositories", self.workspace().store.load())
        self.assertEqual(self.state()["turn_kind"], "normal")
        for location in (self.parent, self.repo / "subdir", self.other / "subdir"):
            status = json.loads(self.ws("status", "--json", cwd=location).stdout)
            self.assertEqual(status["session"], sid)
            self.assertEqual(set(status["repositories"]), {"r1", "r2"})
        self.assertEqual(self.names("reviewed"), {"r1/Token.kt"})
        self.assertEqual(self.names("pending"), {"r1/Config.kt"})
        (self.other / "Config.kt").write_text("new member change\n")
        self.assertEqual(self.names("session"), {
            "r1/Token.kt", "r1/Config.kt", "r2/Config.kt",
        })
        selected = self.run_relay(
            "agent", "diff", "pending", "--repo", "r2", "--", "../../r2/Config.kt",
            cwd=self.repo / "subdir",
        ).stdout
        self.assertIn("b/r2/Config.kt", selected)
        self.assertIn("+new member change", selected)
        self.git("add", ".")
        self.assertIn("r2", self.run_relay("finish", ok=False).stderr)
        self.git("add", ".", cwd=self.other)
        self.run_relay("message", "--repo", "r2", "-m", "Second package")
        self.run_relay("finish", cwd=self.other / "subdir")
        for repo in (self.repo, self.other):
            self.assertEqual(
                self.git("rev-list", "--parents", "-n", "1", "HEAD", cwd=repo).split()[1:],
                [self.base],
            )
            self.assertEqual(self.git("for-each-ref", "refs/relay/", cwd=repo), "")
        self.assertEqual(self.git("log", "-1", "--format=%s", cwd=self.other), "Second package\n")
        # There is no remembered workspace configuration after finish.
        self.run_relay("start")
        self.assertNotIn("repositories", self.state())

    def test_multi_add_has_the_same_member_shape_and_keeps_existing_suspended_scope(self):
        self.ws("start", "--repo", "r1", "-m", "Old scope")
        old = copy.deepcopy(self.group())
        self.ws("suspend")
        self.ws("start", "--repo", "r1", "-m", "New scope")
        self.hook("prompt-submit")
        self.hook("agent-stop")
        self.add()
        group = self.group()
        self.assertEqual(set(group["members"]["r1"]), set(group["members"]["r2"]))
        state = self.workspace().store.load()
        old["state"] = "suspended"
        self.assertEqual(state["sessions"][old["id"]], old)
        self.assertEqual(self.state()["turn"], 1)
        self.assertFalse(self.state()["repositories"]["r2"]["provenance"]["available"])
        self.ws("suspend")
        self.ws("resume", old["id"], cwd=self.repo)
        self.assertEqual(set(self.state()["repositories"]), {"r1"})
        self.assertEqual(self.git("symbolic-ref", "--short", "HEAD", cwd=self.other), "main\n")

    def test_local_suspended_sessions_remain_visible_and_independent_after_promotion(self):
        self.run_relay("start", "-m", "Original suspended session")
        old = self.state()["session"]
        self.write("Token.kt", "old proposal\n")
        self.run_relay("suspend")
        saved = copy.deepcopy(self.metadata()["sessions"][old])
        self.run_relay("start", "-m", "Current feature")
        self.add()
        current = self.group()["id"]
        self.assertEqual(self.metadata()["sessions"][old], saved)
        listed = json.loads(self.run_relay("list", "--json").stdout)
        self.assertEqual({s["id"] for s in listed}, {old, current})
        self.run_relay("message", old, "-m", "Edited old message")
        self.assertEqual(self.group()["message"], "Current feature")
        self.run_relay("suspend")
        self.run_relay("resume", ok=False)
        self.run_relay("resume", old)
        self.assertNotIn("repositories", self.state())
        self.assertEqual(self.read("Token.kt"), "old proposal\n")
        self.assertEqual(self.git("symbolic-ref", "--short", "HEAD", cwd=self.other), "main\n")
        self.run_relay("suspend")
        self.run_relay("resume", current)
        self.assertEqual(set(self.state()["repositories"]), {"r1", "r2"})

    def test_add_requires_an_active_human_turn_and_is_guarded_for_agents(self):
        self.add(ok=False)
        self.run_relay("start")
        self.hook("prompt-submit")
        before = self.metadata()
        result = self.add(ok=False)
        self.assertIn("human phase", result.stderr)
        self.assertEqual(self.metadata(), before)
        self.assertFalse((self.parent / ".relay").exists())
        self.hook("pre-tool-use", {
            "tool_name": "shell", "tool_input": {"command": "relay repo add ../r2"},
        }, ok=False)
        self.hook("agent-stop")
        self.run_relay("suspend")
        self.add(ok=False)
        self.assertFalse((self.parent / ".relay").exists())

    def test_invalid_or_dirty_targets_do_not_change_the_session(self):
        self.run_relay("start")
        before = self.metadata()
        for path in (".", "subdir", "../missing", "../r2/subdir"):
            with self.subTest(path=path):
                self.add(path, ok=False)
                self.assertEqual(self.metadata(), before)
                self.assertFalse((self.repo / ".relay").exists())
        for staged in (False, True):
            (self.other / "dirty.txt").write_text("preserve\n")
            if staged:
                self.git("add", ".", cwd=self.other)
            self.add(ok=False)
            self.assertEqual(self.metadata(), before)
        self.git("reset", "--hard", cwd=self.other)
        self.run_relay("start", cwd=self.other)
        self.add(ok=False)
        self.assertEqual(self.metadata(), before)

    def test_active_multi_root_stays_fixed(self):
        self.ws("start", "--repo", "r1", "-m", "Feature")
        self.add()
        before = copy.deepcopy(self.group())
        self.add("../r2", ok=False)
        self.add(str(self.parent.parent), ok=False)
        with tempfile.TemporaryDirectory(prefix="relay-outside-") as directory:
            outside = Path(directory) / "outside"
            self.git("clone", str(self.other), str(outside))
            result = self.add(str(outside), ok=False)
            self.assertIn("below the common workspace", result.stderr)
        self.assertEqual(self.group(), before)

    def test_new_member_uses_its_own_base_and_requires_an_initial_commit(self):
        self.run_relay("start", "-m", "Feature")
        empty = self.parent / "empty"
        self.git("init", "-b", "main", str(empty))
        result = self.add("../empty", ok=False)
        self.assertIn("initial Git commit", result.stderr)
        self.assertNotIn("repositories", self.state())
        (self.other / "base.txt").write_text("independent base\n")
        self.git("add", ".", cwd=self.other)
        self.git("commit", "-m", "Independent base", cwd=self.other)
        base = self.git("rev-parse", "HEAD", cwd=self.other).strip()
        self.add()
        (self.other / "Config.kt").write_text("reviewed\n")
        self.git("add", ".", cwd=self.other)
        self.run_relay("finish")
        self.assertEqual(
            self.git("rev-list", "--parents", "-n", "1", "HEAD", cwd=self.other).split()[1:],
            [base],
        )
        self.assertEqual(self.git("symbolic-ref", "--short", "HEAD"), "main\n")

    def test_scope_is_in_every_prompt_including_continuations_after_add(self):
        self.run_relay("start")
        owner = {"session_id": "same"}
        first = self.hook("prompt-submit", owner).stdout
        self.assertIn(str(self.repo), first)
        self.hook("agent-stop", owner)
        self.add()
        for continuing in (False, True):
            context = self.hook("prompt-submit", owner).stdout
            self.assertIn(str(self.repo), context)
            self.assertIn(str(self.other), context)
            self.assertNotIn("Relay coordinates code review", context)
            self.assertEqual("Continuing the open turn" in context, continuing)
        self.hook("agent-stop", owner)
        (self.other / "Config.kt").write_text("human edit\n")
        context = self.hook("prompt-submit", owner).stdout
        self.assertEqual(injected_stats(context, "Human"), {"r2/Config.kt": (1, 1)})
        self.assertEqual(self.state()["turn"], 3)

    def test_btw_selection_suspend_resume_and_abort_include_the_new_member(self):
        self.run_relay("start", "-m", "Feature")
        self.hook("prompt-submit")
        self.hook("agent-stop")
        self.run_relay("btw")
        self.add()
        self.write("Token.kt", "approved\n")
        self.git("add", "Token.kt")
        self.run_relay("suspend")
        self.run_relay("resume", cwd=self.other)
        self.assertEqual(self.state()["next_turn"], "btw")
        self.hook("prompt-submit")
        (self.other / "Config.kt").write_text("unexpected write\n")
        self.git("add", ".", cwd=self.other)
        self.hook("agent-stop")
        self.assertEqual((self.other / "Config.kt").read_text(), "old config\n")
        self.assertEqual(self.git("show", ":Token.kt"), "approved\n")
        (self.other / "new.txt").write_text("preserved\n")
        group = self.group()
        self.run_relay("abort", input="abort\n", cwd=self.other)
        branch = "relay/aborted/" + group["members"]["r2"]["id"]
        self.assertEqual(self.git("show", branch + ":new.txt", cwd=self.other), "preserved\n")

    def test_add_failure_rolls_back_promotion_and_preserves_staging(self):
        self.run_relay("start", "-m", "Feature")
        self.write("Token.kt", "approved\n")
        self.git("add", "Token.kt")
        self.write("Token.kt", "unstaged\n")
        before = self.metadata()
        with patch.object(Relay, "start", side_effect=OSError("enrollment failed")):
            with self.assertRaisesRegex(OSError, "enrollment failed"):
                add_repository("../r2", self.repo)
        self.assertEqual(self.metadata(), before)
        self.assertNotIn("repositories", self.state())
        self.assertEqual(self.git("show", ":Token.kt"), "approved\n")
        self.assertEqual(self.read("Token.kt"), "unstaged\n")
        self.assertEqual(self.git("status", "--porcelain", cwd=self.other), "")
        self.add()

    def test_promotion_reads_the_saved_message_under_the_member_lock(self):
        self.run_relay("start", "-m", "Original message")
        locks = Workspace._member_locks

        @contextmanager
        def change_before_lock(workspace, members):
            self.run_relay("message", "-m", "Updated before enrollment")
            with locks(workspace, members):
                yield

        with patch.object(Workspace, "_member_locks", change_before_lock):
            add_repository("../r2", self.repo)
        self.assertEqual(self.group()["message"], "Updated before enrollment")
        self.run_relay("finish")

    def test_crash_during_batch_promotion_is_recoverable_from_all_members(self):
        third = self.parent / "r3"
        self.git("clone", str(self.other), str(third))
        self.run_relay("start", "-m", "Feature")
        before = self.metadata()
        self.crash(
            "from agent_session_relay.core.workspace import add_repository\n"
            "original=Relay.start\n"
            "def crash_after_start(self,*args,**kwargs):\n"
            "    result=original(self,*args,**kwargs)\n"
            "    os._exit(77)\n"
            "with patch.object(Relay,'start',crash_after_start):\n"
            "    add_repository(['../r2','../r3'],'r1')\n"
        )
        for repo in (self.repo, self.other, third):
            self.ws("kiro", "hook", "prompt-submit", cwd=repo, ok=False)
            self.git("gc", "--prune=now", cwd=repo)
        (self.other / "late.txt").write_text("later work\n")
        self.ws("recover", cwd=third)
        self.assertEqual(self.metadata(), before)
        self.assertNotIn("repositories", self.state())
        self.assertFalse(json.loads(self.ws("agent", "status", cwd=self.other).stdout)["active"])
        branch = self.git(
            "for-each-ref", "--format=%(refname)", "refs/heads/relay/recovered/", cwd=self.other,
        ).strip()
        self.assertEqual(self.git("show", branch + ":late.txt", cwd=self.other), "later work\n")
        self.add("../r2", "../r3")

    def test_crash_after_add_commit_keeps_members_and_only_finishes_cleanup(self):
        third = self.parent / "r3"
        self.git("clone", str(self.other), str(third))
        self.run_relay("start", "-m", "Feature")
        self.crash(
            "from agent_session_relay.core.workspace import add_repository\n"
            "original=Store.complete\n"
            "def crash_during_cleanup(self,journal):\n"
            "    original(self,journal)\n"
            "    os._exit(77)\n"
            "with patch.object(Store,'complete',crash_during_cleanup):\n"
            "    add_repository(['../r2','../r3'],'r1')\n"
        )
        (self.other / "late.txt").write_text("keep in place\n")
        self.run_relay("recover", cwd=self.other)
        self.assertEqual(set(self.state()["repositories"]), {"r1", "r2", "r3"})
        self.assertEqual((self.other / "late.txt").read_text(), "keep in place\n")
        self.run_relay("abort", input="abort\n")

    def legacy_state(self, state):
        state = copy.deepcopy(state)
        repositories = {}
        for group in state["sessions"].values():
            group.pop("owners")
            group.pop("turn_kind")
            for name, member in group["members"].items():
                repositories[name] = {"name": name, "git_dir": member["git_dir"]}
            group["members"] = {name: member["id"] for name, member in group["members"].items()}
        state["schema"] = 1
        state["repositories"] = list(repositories.values())
        return state

    def test_legacy_workspace_can_add_without_losing_scope_or_conversation_history(self):
        self.ws("start", "--repo", "r1", "-m", "Feature")
        owner = {"session_id": "same"}
        self.hook("prompt-submit", owner)
        self.hook("agent-stop", owner)
        workspace = self.workspace()
        atomic_json(workspace.store.path, self.legacy_state(workspace.store.load()))
        path = self.repo / ".git/agent-session-relay/state.json"
        local = json.loads(path.read_text())
        for session in local["sessions"].values():
            session.pop("coordinator")
        atomic_json(path, local)
        before = workspace.store.path.read_bytes()
        self.assertEqual(self.state()["turn_kind"], "normal")
        self.assertEqual(len(json.loads(self.run_relay("list", "--json").stdout)), 1)
        self.assertEqual(workspace.store.path.read_bytes(), before)
        self.add()
        self.assertNotIn("repositories", workspace.store.load())
        context = self.hook("prompt-submit", owner).stdout
        self.assertNotIn("Relay coordinates code review", context)
        self.assertIn(str(self.other), context)
        self.hook("agent-stop", owner)
        self.run_relay("finish")

    def test_legacy_interrupted_operation_recovers_using_session_owned_members(self):
        self.start()
        before = self.state()
        self.crash(
            "original=Relay.suspend\n"
            "def crash_first(self,*args,**kwargs):\n"
            "    result=original(self,*args,**kwargs)\n"
            "    os._exit(77)\n"
            "with patch.object(Relay,'suspend',crash_first): Workspace('.').suspend()\n"
        )
        workspace = self.workspace()
        journal = json.loads(workspace.store.journal_path.read_text())
        journal["state"] = self.legacy_state(journal["state"])
        journal["repositories"] = journal["state"]["repositories"]
        journal["members"] = {name: m["id"] for name, m in journal["members"].items()}
        atomic_json(workspace.store.journal_path, journal)
        atomic_json(workspace.store.path, journal["state"])
        self.ws("recover", cwd=self.other)
        self.assertEqual(self.state(), before)

    def test_linked_worktree_add_has_distinct_refs_and_immutable_bases(self):
        linked = self.parent / "linked"
        self.git("worktree", "add", "-b", "linked-base", str(linked))
        self.run_relay("start", "-m", "Both worktrees")
        self.add("../r2", "../linked")
        for repo in (self.repo, self.other, linked):
            (repo / "Token.kt").write_text(repo.name + "\n")
            self.git("add", ".", cwd=repo)
        self.run_relay("finish", cwd=linked)
        self.assertEqual(len(self.git(
            "for-each-ref", "--format=%(refname)", "refs/heads/relay/result/",
        ).splitlines()), 2)
        self.assertEqual(self.git("for-each-ref", "refs/relay/"), "")

    def test_stale_standalone_engine_cannot_mutate_a_promoted_session(self):
        self.run_relay("start")
        stale = Relay(Git(self.repo))
        self.add()
        with self.assertRaisesRegex(RelayError, "coordination changed"):
            stale.handoff()
        self.assertEqual(self.state()["phase"], "human")
