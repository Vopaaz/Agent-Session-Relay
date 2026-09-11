from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.support import RepositoryTest

# isort: split
from agent_session_relay.integrations.kiro.adapter import hook_config, install
from agent_session_relay.integrations.kiro.guard import shell_violation, violation


class ShellGuardTests(unittest.TestCase):
    def test_direct_git_commands_and_common_wrappers_are_blocked(self):
        blocked = [
            "git status",
            "git diff --cached",
            "git log -5",
            "git add .",
            "git reset --hard",
            "git checkout other",
            "git stash",
            "git rebase main",
            "git merge main",
            "git -C /another/repo status",
            "/usr/bin/git status",
            "'/usr/bin/git' diff",
            '"git" diff',
            "g''it status",
            "\\git status",
            "git.exe status",
            "git-lfs status",
            "cd src && git status",
            "echo harmless; git diff",
            "echo before\ngit diff",
            "git diff | cat",
            "false || git log",
            "(git status)",
            "if git diff; then echo x; fi",
            "for p in a b; do git status; done",
            "X=1 git status",
            "env X=1 git status",
            "env -u GIT_DIR git status",
            "env -C /tmp git status",
            "command git diff",
            "command -- git diff",
            "exec git diff",
            "nohup git status",
            "sudo -u someone git log",
            "sudo --user someone git log",
            "xargs -I '{}' git show '{}'",
            "find . -exec git status \\;",
            "bash -lc 'git status'",
            "bash --norc -c 'git status'",
            'sh -c "git diff"',
            "eval 'git status'",
            'echo "$(git status)"',
            "echo `git log`",
            "echo $(command git diff)",
            'printf "%s" "$(echo $(git status))"',
            "G=git; $G status",
            'python3 -c \'import subprocess; subprocess.run(["git", "status"])\'',
            "python3 -c 'import os; os.system(\"git status\")'",
            "node -e \"require('child_process').execSync('git status')\"",
            "relay finish",
            "relay abort",
            "relay suspend",
            'relay message -m "Change the session description"',
            "relay message",
            "relay kiro hook agent-stop",
            "python3 -m agent_session_relay finish",
            "bash <<'EOF'\ngit status\nEOF\n",
            "cat <<EOF | bash\ngit status\nEOF\n",
            "cat <<EOF\n$(git status)\nEOF\n",
            "cat <<EOF\n'$(git status)'\nEOF\n",
            "python3 <<'EOF'\nimport subprocess\nsubprocess.run(['git', 'status'])\nEOF\n",
        ]
        for command in blocked:
            with self.subTest(command=command):
                self.assertTrue(shell_violation(command), command)

    def test_normal_shell_work_and_literal_git_mentions_are_allowed(self):
        allowed = [
            "python3 -m unittest discover -s tests",
            "npm test",
            "cargo check",
            "cat file.txt",
            "echo git status",
            "printf '%s' 'git diff'",
            "rg 'git status' README.md",
            'echo "git commit is blocked"',
            "echo '$(git status)'",
            "echo '`git status`'",
            "cat .gitignore",
            "echo github",
            "echo do-it",
            "ls; pwd",
            "echo x\ncat file.txt",
            "env FOO=1 python3 -m unittest",
            "sudo -u someone echo git",
            "sh -c 'echo git status'",
            "command python3 --version",
            "echo fine # git status",
            "relay agent status",
            "relay agent diff reviewed --name-only",
            "relay agent diff human -- Parser.kt Config.kt",
            "relay agent diff pending -- Parser.kt",
            "relay --help",
            "relay --version",
            "python3 -m agent_session_relay agent status",
            "python3 -c 'print(\"git status\")'",
            "cat >guide.md <<'EOF'\ngit status\necho $(git diff)\nEOF\n",
            "cat <<EOF\ngit status\nEOF\n",
            "cat <<-EOF\n\tgit status\n\tEOF\n",
            "cat <<'FIRST' <<'SECOND'\ngit status\nFIRST\ngit diff\nSECOND\n",
        ]
        for command in allowed:
            with self.subTest(command=command):
                self.assertFalse(shell_violation(command), command)

    def test_payload_variations_and_git_mcp_tools(self):
        for name in ("execute_bash", "shell", "execute_command", "runShellCommand"):
            self.assertIsNotNone(
                violation({"tool_name": name, "tool_input": {"command": "git status"}})
            )
        self.assertIsNotNone(violation({"toolName": "shell", "toolInput": {"cmd": "git status"}}))
        self.assertIsNotNone(
            violation({"tool_name": "shell", "tool_input": {"commands": ["pwd", "git status"]}})
        )
        for name in (
            "@git/status",
            "mcp__git__git_diff",
            "git_status",
            "@sourcecontrol/git_commit",
        ):
            self.assertIsNotNone(violation({"tool_name": name, "tool_input": {}}))
        self.assertIsNone(
            violation({"tool_name": "fs_write", "tool_input": {"content": "git status"}})
        )
        self.assertIsNotNone(violation({"tool_name": "shell", "tool_input": {}}))
        self.assertIsNotNone(violation({}))


class KiroHookTests(RepositoryTest):
    def git_files(self):
        return {
            str(path.relative_to(self.repo)): (path.read_bytes(), path.stat().st_mtime_ns)
            for path in (self.repo / ".git").rglob("*")
            if path.is_file()
        }

    def test_inactive_hooks_are_silent_and_have_no_side_effects(self):
        before = self.git_files()
        for event in ("prompt-submit", "agent-stop", "pre-tool-use"):
            for body in (
                "",
                "invalid json",
                json.dumps({"tool_name": "shell", "tool_input": {"command": "git status"}}),
            ):
                result = self.run_relay("kiro", "hook", event, input=body)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr, "")
        self.assertEqual(self.git_files(), before)
        self.assertFalse((self.repo / ".git/agent-session-relay").exists())
        with tempfile.TemporaryDirectory(prefix="relay-outside-") as outside:
            result = self.run_relay("kiro", "hook", "prompt-submit", input="{}", cwd=outside)
            self.assertEqual(result.stdout + result.stderr, "")

    def test_suspended_hooks_are_silent_and_git_is_unguarded(self):
        self.run_relay("start")
        self.write("Token.kt", "proposal")
        self.run_relay("suspend")
        before = self.git_files()
        for event in ("prompt-submit", "agent-stop", "pre-tool-use"):
            result = self.hook(
                event, {"tool_name": "shell", "tool_input": {"command": "git status"}}
            )
            self.assertEqual(result.stdout + result.stderr, "")
        self.assertEqual(self.git_files(), before)

    def test_active_guard_blocks_git_and_allows_semantic_inspection(self):
        self.run_relay("start")
        before = self.git_files()
        denied = self.hook(
            "pre-tool-use",
            {"tool_name": "execute_bash", "tool_input": {"command": "git diff"}},
            ok=False,
        )
        self.assertEqual(denied.returncode, 2)
        self.assertEqual(denied.stdout, "")
        self.assertIn("relay agent diff pending", denied.stderr)
        self.assertEqual(self.git_files(), before)
        for command in (
            "relay agent status",
            "relay agent diff pending --name-only",
            "python3 -m unittest",
        ):
            self.assertEqual(
                self.hook(
                    "pre-tool-use", {"tool_name": "shell", "tool_input": {"command": command}}
                ).stdout,
                "",
            )
        self.assertEqual(
            self.run_relay("kiro", "hook", "pre-tool-use", input="bad json", ok=False).returncode, 2
        )

    def test_hook_workspace_discovery_and_conversation_ownership(self):
        self.run_relay("start")
        with tempfile.TemporaryDirectory(prefix="relay-hook-cwd-") as outside:
            payload = json.dumps({"cwd": str(self.repo), "session_id": "conversation-a"})
            result = self.run_relay("kiro", "hook", "prompt-submit", input=payload, cwd=outside)
            self.assertIn("Agent-Session-Relay active", result.stdout)
        self.assertIn(
            "Another Kiro conversation",
            self.hook("prompt-submit", {"session_id": "conversation-b"}, ok=False).stderr,
        )
        self.hook("agent-stop", {"session_id": "conversation-b"}, ok=False)
        self.assertEqual(self.state()["phase"], "agent")
        self.hook("agent-stop", {"session_id": "conversation-a"})
        self.hook("prompt-submit", {"session_id": "conversation-b"})
        self.assertEqual(self.state()["turn"], 2)

    def test_tool_working_directory_does_not_bypass_active_guard(self):
        self.run_relay("start")
        self.hook(
            "pre-tool-use",
            {
                "cwd": "/tmp",
                "tool_name": "shell",
                "tool_input": {"command": "git status", "cwd": "/tmp"},
            },
            ok=False,
        )

    def test_project_install_is_idempotent_and_preserves_other_hooks(self):
        other = self.repo / ".kiro/hooks/custom.json"
        other.parent.mkdir(parents=True)
        other.write_text('{"custom":true}')
        self.run_relay("kiro", "install", "--project")
        path = other.parent / "agent-session-relay.json"
        self.assertEqual(json.loads(path.read_text()), hook_config())
        before = path.stat().st_mtime_ns
        self.run_relay("kiro", "install", "--project")
        self.assertEqual(path.stat().st_mtime_ns, before)
        self.assertEqual(other.read_text(), '{"custom":true}')
        self.assertEqual(
            [h["trigger"] for h in hook_config()["hooks"]],
            ["UserPromptSubmit", "Stop", "PreToolUse"],
        )
        path.write_text('{"customized":true}')
        self.run_relay("kiro", "install", "--project", ok=False)
        self.assertEqual(path.read_text(), '{"customized":true}')
        self.run_relay("kiro", "install", "--project", "--force")
        self.assertEqual(json.loads(path.read_text()), hook_config())

    def test_global_install_uses_user_hooks_directory_without_changing_profiles(self):
        fake_home = Path(self.temporary.name) / "user"
        with patch(
            "agent_session_relay.integrations.kiro.adapter.Path.home", return_value=fake_home
        ):
            path = install(global_scope=True)
        self.assertEqual(path, fake_home / ".kiro/hooks/agent-session-relay.json")
        self.assertEqual(json.loads(path.read_text()), hook_config())
        self.assertFalse((fake_home / ".kiro/agents").exists())

    def test_installer_does_not_follow_configuration_symlinks(self):
        destination = Path(self.temporary.name) / "other-config"
        destination.mkdir()
        os.symlink(destination, self.repo / ".kiro")
        self.assertIn("symlink", self.run_relay("kiro", "install", "--project", ok=False).stderr)
        self.assertEqual(list(destination.iterdir()), [])
