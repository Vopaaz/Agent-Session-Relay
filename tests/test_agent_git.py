from __future__ import annotations

import subprocess
import sys
from unittest.mock import patch

from tests.support import CLI, RepositoryTest

# isort: split
from agent_session_relay.core.errors import RelayError
from agent_session_relay.core.readonly_git import run


class AgentGitTests(RepositoryTest):
    def snapshot(self, repo=None):
        repo = repo or self.repo
        return {
            str(path.relative_to(repo)): (path.read_bytes(), path.stat().st_mtime_ns)
            for path in repo.rglob("*") if path.is_file()
        }

    def query(self, *args, **kwargs):
        return self.run_relay("agent", "git", *args, **kwargs)

    def test_queries_match_git_without_creating_relay_state(self):
        cases = [
            ("log", "--oneline", "--graph", "--all", "-5"),
            ("log", "-n1", "--format=%H%n%P%n%s", "--", "Token.kt"),
            ("log", "--follow", "--", "Token.kt"),
            ("show", "--format=fuller", "--stat", self.base),
            ("show", self.base + ":Token.kt"),
            ("diff", "--name-status", self.base, "main", "--", "Token.kt"),
            ("status", "--porcelain=v2", "-b", "--untracked-files=all"),
            ("blame", "-L", "1,3", self.base, "--", "Parser.kt"),
            ("grep", "-n", "-e", "TokenDefinition", self.base, "--", "Token.kt"),
            ("rev-list", "--count", "--left-right", self.base + "...main"),
            ("rev-parse", "--verify", "--short=12", "main^{commit}"),
            ("merge-base", "--is-ancestor", self.base, "main"),
            ("ls-tree", "-r", "--name-only", self.base),
            ("ls-files", "--stage", "--", "Token.kt"),
            ("cat-file", "-p", self.base),
            ("show-ref", "--verify", "refs/heads/main"),
            ("for-each-ref", "--format=%(refname) %(objectname)", "refs/heads/"),
            ("describe", "--always", self.base),
            ("shortlog", "-sn", "main"),
            ("branch", "-avv"),
            ("branch", "--list", "--contains=" + self.base, "main"),
            ("tag", "--list", "v*"),
            ("config", "--get", "user.name"),
            ("config", "--get-regexp", "^user\\."),
            ("config", "--list"),
            ("symbolic-ref", "--short", "HEAD"),
            ("reflog", "show", "--format=%H", "main"),
            ("reflog", "-1", "--format=%H"),
            ("reflog", "exists", "refs/heads/main"),
            ("worktree", "list", "--porcelain"),
            ("remote", "-v"),
            ("count-objects", "-v"),
        ]
        before = self.snapshot()
        for args in cases:
            with self.subTest(args=args):
                self.assertEqual(self.query(*args).stdout, self.git(*args))
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.repo / ".git/agent-session-relay").exists())

    def test_write_commands_and_ambiguous_options_fail_before_git_runs(self):
        cases = [
            (), ("add", "."), ("commit", "-m", "oops"), ("reset", "--hard"),
            ("checkout", "main"), ("switch", "main"), ("stash", "list"),
            ("fetch", "--dry-run"), ("pull",), ("push", "--dry-run"), ("gc",),
            ("update-ref", "refs/heads/main", self.base), ("hash-object", "-w", "Token.kt"),
            ("alias-name",), ("/usr/bin/git", "status"),
            ("-c", "alias.read=!touch sentinel", "read"),
            ("--config-env=core.pager=PAGER", "log"), ("-C", str(self.repo), "status"),
            ("--git-dir=.git", "status"), ("--paginate", "log"),
            ("log", "--output=sentinel"), ("log", "--out", "sentinel"),
            ("diff", "--output", "sentinel"), ("show", "--ext-diff"),
            ("diff", "--textconv"), ("log", "--show-signature"), ("log", "--help"),
            ("log", "--stdin"), ("rev-list", "--stdin"), ("log", "--unknown-future-flag"),
            ("log", "--diff-merges=remerge"), ("diff", "--submodule=diff"),
            ("log", "--max-count", "--output=sentinel"), ("log", "--max-count"),
            ("branch", "new-branch"), ("branch", "-D", "main"),
            ("branch", "--list", "--delete", "main"), ("branch", "-ld", "main"),
            ("branch", "--edit-description"), ("branch", "--set-upstream-to=main", "other"),
            ("tag", "new-tag"), ("tag", "--list", "--delete", "v1"),
            ("tag", "-lv", "v1"), ("config", "user.name", "changed"),
            ("config", "--get", "--unset", "user.name"),
            ("config", "--list", "--edit"), ("config", "set", "user.name", "changed"),
            ("config", "get", "--unset", "user.name"),
            ("symbolic-ref", "HEAD", "refs/heads/main"), ("symbolic-ref", "--delete", "HEAD"),
            ("reflog", "expire", "--all"), ("reflog", "delete", "HEAD@{0}"),
            ("reflog", "show", "--rewrite"), ("worktree", "prune"),
            ("remote", "add", "origin", "."), ("remote", "set-url", "origin", "."),
            ("cat-file", "--filters", self.base + ":Token.kt"),
            ("describe", "--dirty"), ("describe", "--broken"),
            ("grep", "--open-files-in-pager=touch sentinel", "Token"),
            ("log", "--format=%G?", "--format=%h"), ("log", "--pretty=custom"),
            ("log", "--format=custom"), ("branch", "--format=%(signature:grade)"),
            ("for-each-ref", "--sort=signature:grade", "--sort=refname"),
        ]
        with patch("agent_session_relay.core.readonly_git.subprocess.run") as process:
            for args in cases:
                with self.subTest(args=args), self.assertRaises(RelayError):
                    run(list(args))
            process.assert_not_called()

    def test_cli_rejections_preserve_repository_and_explain_boundary(self):
        self.run_relay("start")
        before = self.snapshot()
        for args in [("branch", "oops"), ("diff", "--output=.git/config"),
                     ("config", "--unset", "user.name"), ("commit", "-m", "oops")]:
            result = self.query(*args, ok=False)
            self.assertEqual(result.stdout, "")
            self.assertIn("Only supported read-only Git queries", result.stderr)
        self.assertEqual(self.snapshot(), before)

    def test_managed_refs_commits_and_branches_are_allowed_in_normal_and_btw_turns(self):
        self.git("branch", "relay/result/example", self.base)
        self.run_relay("start")
        session = self.state()["session"]
        self.write("Token.kt", "reviewed checkpoint\n")
        self.git("add", "Token.kt")
        for btw in (False, True):
            if btw:
                self.run_relay("btw")
            injection = self.hook("prompt-submit").stdout
            self.assertIn("relay agent git <args...>", injection)
            self.assertIn("internal bookkeeping", injection)
            checkpoint = self.git("rev-parse", "HEAD").strip()
            self.assertNotEqual(checkpoint, self.base)
            before = self.snapshot()
            queries = [
                ("show", f"refs/relay/sessions/{session}/base:Token.kt"),
                ("log", "--format=%H", "HEAD"),
                ("show", "--no-patch", checkpoint),
                ("diff", self.base, "HEAD"),
                ("branch", "--list", "relay/*"),
                ("show", "--no-patch", "relay/result/example"),
                ("for-each-ref", "refs/relay/"),
            ]
            for args in queries:
                self.query(*args)
            self.hook("pre-tool-use", {"tool_name": "shell", "tool_input": {
                "command": "relay agent git log --oneline --all",
            }})
            self.assertEqual(self.snapshot(), before)
            self.hook("agent-stop")

    def test_paths_stdin_binary_output_and_exit_codes_are_preserved(self):
        self.write("folder/space name.txt", "needle\n")
        self.write("folder/--output=sentinel", "literal path\n")
        self.write("binary.dat", b"\x00\xff\r\n")
        self.git("add", ".")
        self.git("commit", "-m", "files")
        self.write("folder/--output=sentinel", "changed path\n")
        args = ("diff", "--name-only", "--", "--output=sentinel")
        subdir = self.repo / "folder"
        self.assertEqual(self.query(*args, cwd=subdir).stdout, self.git(*args, cwd=subdir))
        self.assertEqual(self.query("show", "HEAD:folder/space name.txt").stdout, "needle\n")
        for args in [("show", "HEAD:binary.dat"), ("ls-files", "-z")]:
            actual = subprocess.run([sys.executable, str(CLI), "agent", "git", *args],
                                    cwd=self.repo, env=self.env, capture_output=True)
            self.assertEqual(actual.returncode, 0, actual.stderr)
            self.assertEqual(actual.stdout, self.git(*args, data=b""))
        self.assertEqual(self.query("cat-file", "--batch-check", input=self.base + "\n").stdout,
                         self.git("cat-file", "--batch-check", data=(self.base + "\n").encode())
                         .decode())
        for args in [("diff", "--exit-code"), ("show", "does-not-exist"),
                     ("config", "--get", "absent.key"), ("grep", "missing-pattern")]:
            actual = self.query(*args, ok=False)
            expected = subprocess.run(["git", *args], cwd=self.repo, env=self.env,
                                      capture_output=True, text=True)
            self.assertEqual(actual.returncode, expected.returncode)
            self.assertEqual(actual.stdout, expected.stdout)
            self.assertEqual(actual.stderr, expected.stderr)

    def test_config_queries_and_local_remote_queries(self):
        self.git("remote", "add", "origin", "https://example.invalid/repo.git")
        self.assertEqual(self.query("remote", "get-url", "origin").stdout,
                         "https://example.invalid/repo.git\n")
        self.query("config", "--list", "--local")
        # Modern query subcommands are forwarded even on older Git versions, which
        # may reject their syntax; Relay must preserve that native exit status.
        for args in [("config", "get", "user.name"), ("config", "list", "--local")]:
            expected = subprocess.run(["git", *args], cwd=self.repo, env=self.env,
                                      capture_output=True, text=True)
            actual = self.query(*args, ok=expected.returncode == 0)
            self.assertEqual((actual.returncode, actual.stdout, actual.stderr),
                             (expected.returncode, expected.stdout, expected.stderr))

    def test_inspection_suppresses_helpers_and_optional_index_writes(self):
        sentinel = self.repo / "helper-ran.ignored"
        helper = f"touch {sentinel}; exit 1"
        for key in ("core.pager", "pager.log", "diff.external", "core.fsmonitor", "gpg.program"):
            self.git("config", key, helper)
        self.git("config", "diff.custom.textconv", helper)
        self.git("config", "diff.custom.cachetextconv", "true")
        self.git("config", "log.showSignature", "true")
        self.git("config", "alias.sneaky", "!" + helper)
        self.write(".gitattributes", "*.kt diff=custom\n")
        self.write("Token.kt", "modified\n")
        self.env.update({
            "GIT_INDEX_FILE": str(self.repo / "wrong-index"),
            "GIT_DIR": str(self.repo / "wrong-dir"),
            "GIT_PAGER": helper, "GIT_EXTERNAL_DIFF": helper,
            "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.fsmonitor",
            "GIT_CONFIG_VALUE_0": helper,
            "GIT_CONFIG_PARAMETERS": "'diff.external=" + helper + "'",
            "GIT_TRACE": str(sentinel), "GIT_OPTIONAL_LOCKS": "1",
        })
        before = self.snapshot()
        for args in [("status", "--porcelain"), ("diff",), ("show", "HEAD"),
                     ("log", "-p", "-1"), ("describe", "--always")]:
            self.query(*args)
            after = self.snapshot()
            self.assertEqual([name for name in before.keys() | after.keys()
                              if before.get(name) != after.get(name)], [], args)
        self.query("sneaky", ok=False)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(sentinel.exists())

    def test_help_is_available_without_a_repository(self):
        result = self.query("--help", cwd=self.temporary.name)
        self.assertIn("worktree list", result.stdout)
        self.assertIn("Unknown options", result.stdout)

    def test_missing_partial_clone_objects_are_not_fetched(self):
        self.git("config", "uploadpack.allowFilter", "true")
        partial = self.repo.parent / "partial"
        self.git("clone", "--filter=blob:none", "--no-checkout", self.repo.as_uri(), str(partial))
        before = self.snapshot(partial)
        self.query("show", "HEAD:Token.kt", cwd=partial, ok=False)
        self.assertEqual(self.snapshot(partial), before)
