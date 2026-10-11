from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from tests.support import RepositoryTest, injected_stats
from tests.test_workspace import WorkspaceTest

# isort: split
from agent_session_relay.integrations.claude_code import adapter as claude_code
from agent_session_relay.integrations.codex import adapter as codex


class NativeHooks:
    def hook(self, event, payload=None, ok=True, **kwargs):
        body = {"session_id": "conversation-a", "cwd": str(self.repo), **(payload or {})}
        return self.run_relay(
            self.harness, "hook", event, input=json.dumps(body), ok=ok, **kwargs,
        )

    def context(self, result):
        output = json.loads(result.stdout)["hookSpecificOutput"]
        self.assertEqual(output["hookEventName"], "UserPromptSubmit")
        return output["additionalContext"]

    def git_files(self):
        return {
            str(path.relative_to(self.repo)): (path.read_bytes(), path.stat().st_mtime_ns)
            for path in (self.repo / ".git").rglob("*") if path.is_file()
        }

    def test_complete_review_cycle_and_duplicate_hooks(self):
        self.run_relay("start", "-m", "Update native token handling")
        initial = self.context(self.hook("prompt-submit"))
        self.assertIn("Relay coordinates code review", initial)
        duplicate = self.context(self.hook("prompt-submit"))
        self.assertIn("Continuing the open turn", duplicate)
        self.assertNotIn("Relay coordinates code review", duplicate)
        self.assertEqual(self.state()["turn"], 1)
        self.write("Token.kt", "agent proposal\n")
        self.git("add", "Token.kt")  # Even accidental agent staging remains pending.
        self.assertEqual(self.hook("agent-stop").stdout, "")
        self.assertEqual(self.git("diff", "--cached"), "")
        self.assertEqual(self.hook("agent-stop").stdout, "")
        self.write("Token.kt", "human reviewed\n")
        self.git("add", "Token.kt")
        context = self.context(self.hook("prompt-submit"))
        self.assertNotIn("Relay coordinates code review", context)
        self.assertEqual(injected_stats(context, "Approved"), {"Token.kt": (1, 1)})
        self.assertEqual(injected_stats(context, "Human"), {"Token.kt": (1, 1)})
        self.hook("agent-stop")
        sid = self.state()["session"]
        self.run_relay("finish")
        self.assert_one_commit("relay/result/" + sid)
        self.assert_clean()

    def test_btw_blocks_native_writes_and_reports_restoration_as_json(self):
        self.run_relay("start")
        self.write("Token.kt", "staged before btw\n")
        self.git("add", "Token.kt")
        self.write("Token.kt", "human pending\n")
        self.run_relay("btw")
        context = self.context(self.hook("prompt-submit"))
        self.assertIn("read-only BTW", context)
        before = self.git_files()
        for tool, tool_input in (
            ("Write", {"file_path": "Token.kt", "content": "x"}),
            ("Edit", {"file_path": "Token.kt", "old_string": "x", "new_string": "y"}),
            ("MultiEdit", {"file_path": "Token.kt"}),
            ("NotebookEdit", {"notebook_path": "test.ipynb"}),
            ("apply_patch", "*** Begin Patch\n*** End Patch"),
            ("functions.apply_patch", "*** Begin Patch\n*** End Patch"),
            ("Bash", {"command": "echo x > Token.kt"}),
        ):
            denied = self.hook("pre-tool-use", {"tool_name": tool, "tool_input": tool_input},
                               ok=False)
            self.assertEqual(denied.returncode, 2)
            self.assertIn("read-only btw", denied.stderr)
        self.assertEqual(self.git_files(), before)
        self.hook("pre-tool-use", {
            "tool_name": "Bash", "tool_input": {"command": "relay agent diff pending"},
        })
        # An unrecognized script can write; stop must preserve the write before restoring review.
        self.write("Token.kt", "unexpected write\n")
        self.git("add", "Token.kt")
        output = json.loads(self.hook("agent-stop").stdout)
        self.assertEqual(set(output), {"systemMessage"})
        self.assertIn("were saved in snapshot", output["systemMessage"])
        self.assertEqual(self.read("Token.kt"), "human pending\n")
        self.assertEqual(self.git("show", ":Token.kt"), "staged before btw\n")
        self.assertEqual(self.hook("agent-stop").stdout, "")
        self.hook("prompt-submit")
        self.hook("pre-tool-use", {"tool_name": "Write", "tool_input": {}})

    def test_shell_tool_names_and_external_working_directory_cannot_bypass_guard(self):
        self.run_relay("start")
        for tool, key in (
            ("Bash", "command"), ("exec_command", "cmd"), ("shell_command", "command"),
            ("functions.exec_command", "cmd"), ("mcp__git__git_status", "unused"),
        ):
            with self.subTest(tool=tool):
                denied = self.hook("pre-tool-use", {
                    "cwd": "/tmp", "tool_name": tool, "tool_input": {key: "git status"},
                }, ok=False)
                self.assertEqual(denied.returncode, 2)
                self.assertIn("relay agent", denied.stderr)
        self.hook("pre-tool-use", {
            "tool_name": "Bash", "tool_input": {"command": "relay agent git log -1"},
        })

    def test_owners_are_distinct_across_harnesses_and_subagent_stop_is_inert(self):
        self.run_relay("start")
        self.hook("prompt-submit")
        other = "codex" if self.harness == "claude-code" else "claude-code"
        for event in ("prompt-submit", "agent-stop"):
            denied = self.run_relay(other, "hook", event,
                                    input='{"session_id":"conversation-a"}', ok=False)
            self.assertEqual(denied.returncode, 2)
            self.assertIn("conversation", denied.stderr)
        self.assertEqual(self.hook("agent-stop", {"agent_id": "child"}).stdout, "")
        self.assertEqual(self.state()["phase"], "agent")
        self.hook("pre-tool-use", {
            "agent_id": "child", "tool_name": "Bash", "tool_input": {"command": "git add ."},
        }, ok=False)
        self.hook("agent-stop")
        takeover = self.context(self.hook("prompt-submit", {"session_id": "conversation-b"}))
        self.assertIn("You are taking over", takeover)

    def test_payload_cwd_discovers_session_from_an_external_launcher(self):
        self.run_relay("start")
        with tempfile.TemporaryDirectory(prefix="relay-launcher-") as outside:
            self.assertIn("Agent-Session-Relay active", self.context(
                self.hook("prompt-submit", cwd=outside),
            ))
            self.hook("agent-stop", cwd=outside)
        self.assertEqual(self.state()["phase"], "human")

    def test_inactive_and_suspended_hooks_are_silent_without_filesystem_changes(self):
        for suspended in (False, True):
            if suspended:
                self.run_relay("start")
                self.run_relay("suspend")
            before = self.git_files()
            for event in ("prompt-submit", "agent-stop", "pre-tool-use"):
                for body in ("", "bad JSON", "[]", '{"tool_name":"Bash"}'):
                    result = self.run_relay(self.harness, "hook", event, input=body)
                    self.assertEqual(result.stdout + result.stderr, "")
            self.assertEqual(self.git_files(), before)

    def test_invalid_active_payloads_fail_without_mutating_the_session(self):
        self.run_relay("start")
        before = self.git_files()
        for body in ("invalid", "[]", "null", "{" + " " * (4 * 1024 * 1024) + "}"):
            result = self.run_relay(self.harness, "hook", "prompt-submit", input=body, ok=False)
            self.assertEqual(result.returncode, 2)
            self.assertIn("Invalid", result.stderr)
        self.assertEqual(self.git_files(), before)


class CodexHookTests(NativeHooks, RepositoryTest):
    harness = "codex"


class ClaudeCodeHookTests(NativeHooks, RepositoryTest):
    harness = "claude-code"


class NativeHookInstallTests(RepositoryTest):
    adapters = (("codex", codex, ".codex", "hooks.json", "CODEX_HOME"),
                ("claude-code", claude_code, ".claude", "settings.json", "CLAUDE_CONFIG_DIR"))

    def test_project_install_merges_settings_and_is_idempotent(self):
        for harness, adapter, directory, filename, _ in self.adapters:
            with self.subTest(harness=harness):
                path = self.repo / directory / filename
                path.parent.mkdir()
                existing = {"env": {"KEEP": "yes"}, "permissions": {"allow": ["Read"]},
                            "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
                                {"type": "command", "command": "echo custom"},
                            ]}]}}
                path.write_text(json.dumps(existing))
                self.run_relay(harness, "install", "--project")
                installed = json.loads(path.read_text())
                self.assertEqual(installed["permissions"], existing["permissions"])
                self.assertEqual(installed["env"], existing["env"])
                self.assertEqual(installed["hooks"]["PreToolUse"][0],
                                 existing["hooks"]["PreToolUse"][0])
                for event, groups in adapter.hook_config()["hooks"].items():
                    self.assertEqual(installed["hooks"][event][-1], groups[0])
                before = (path.read_bytes(), path.stat().st_mtime_ns)
                self.run_relay(harness, "install", "--project")
                self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)

    def test_force_replaces_only_customized_relay_handlers_in_a_mixed_group(self):
        for harness, adapter, directory, filename, _ in self.adapters:
            path = self.repo / directory / filename
            self.run_relay(harness, "install", "--project")
            current = json.loads(path.read_text())
            custom = {"type": "command", "command": "echo keep me", "timeout": 7}
            group = current["hooks"]["PreToolUse"][0]
            group["matcher"] = "Bash"
            group["hooks"][0]["timeout"] = 1
            group["hooks"].append(custom)
            path.write_text(json.dumps(current))
            before = path.read_bytes()
            self.assertIn("--force", self.run_relay(harness, "install", "--project",
                                                    ok=False).stderr)
            self.assertEqual(path.read_bytes(), before)
            self.run_relay(harness, "install", "--project", "--force")
            result = json.loads(path.read_text())
            self.assertEqual(result["hooks"]["PreToolUse"][0],
                             {"matcher": "Bash", "hooks": [custom]})
            self.assertEqual(result["hooks"]["PreToolUse"][1:],
                             adapter.hook_config()["hooks"]["PreToolUse"])

    def test_invalid_configurations_are_preserved_even_with_force(self):
        for harness, _, directory, filename, _ in self.adapters:
            path = self.repo / directory / filename
            path.parent.mkdir()
            for body in ("broken JSON", "[]", '{"hooks":[]}',
                         '{"hooks":{"Stop":[{"hooks":"invalid"}]}}'):
                path.write_text(body)
                result = self.run_relay(harness, "install", "--project", "--force", ok=False)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(path.read_text(), body)

    def test_global_install_honors_home_and_custom_config_directories(self):
        home = self.repo.parent / "fake-home"
        for harness, adapter, directory, filename, variable in self.adapters:
            with (
                patch.dict(os.environ, {variable: ""}),
                patch.object(Path, "home", return_value=home),
            ):
                path = adapter.install(global_scope=True)
            self.assertEqual(path, home / directory / filename)
            self.assertEqual(json.loads(path.read_text()), adapter.hook_config())
            override = home / (harness + "-config")
            with patch.dict(os.environ, {variable: str(override)}):
                path = adapter.install(global_scope=True)
            self.assertEqual(path, override / filename)
            self.assertEqual(json.loads(path.read_text()), adapter.hook_config())

    def test_symlink_directories_and_files_are_rejected(self):
        target = self.repo.parent / "elsewhere"
        target.mkdir()
        for harness, _, directory, filename, _ in self.adapters:
            config = self.repo / directory
            config.symlink_to(target, target_is_directory=True)
            self.assertIn("symlink", self.run_relay(harness, "install", "--project",
                                                   ok=False).stderr)
            config.unlink()
            config.mkdir()
            path = config / filename
            path.symlink_to(target / filename)
            self.assertIn("symlink", self.run_relay(harness, "install", "--project", "--force",
                                                   ok=False).stderr)
        self.assertEqual(list(target.iterdir()), [])

    def test_claude_alias_installs_canonical_commands(self):
        self.run_relay("claude", "install", "--project")
        config = json.loads((self.repo / ".claude/settings.json").read_text())
        self.assertEqual(config, claude_code.hook_config())


class NativeWorkspaceHookTests(WorkspaceTest):
    def check_workspace(self, harness):
        self.start()
        payload = json.dumps({"cwd": str(self.parent), "session_id": "workspace-conversation"})
        first = self.ws(harness, "hook", "prompt-submit", input=payload)
        context = json.loads(first.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn('"r1"', context)
        self.assertIn('"r2"', context)
        for repo in (self.repo, self.other):
            (repo / "Token.kt").write_text("native workspace proposal\n")
        self.ws(harness, "hook", "agent-stop", input=payload, cwd=self.other / "subdir")
        for repo in (self.repo, self.other):
            self.git("add", "Token.kt", cwd=repo)
        self.ws(harness, "hook", "prompt-submit", input=payload)
        self.ws(harness, "hook", "agent-stop", input=payload)
        self.ws("finish")
        for repo in (self.repo, self.other):
            self.assertEqual(self.git("rev-list", "--parents", "-n", "1", "HEAD",
                                      cwd=repo).split()[1:], [self.base])
            self.assertEqual(self.git("status", "--porcelain", cwd=repo), "")

    def test_codex_coordinates_all_workspace_members(self):
        self.check_workspace("codex")

    def test_claude_code_coordinates_all_workspace_members(self):
        self.check_workspace("claude-code")
