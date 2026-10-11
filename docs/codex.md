# Codex integration

Relay supports Codex versions with native `UserPromptSubmit`, `Stop`, and `PreToolUse` command
hooks. Use a current Codex CLI; older versions exposing only `notify` cannot run this adapter.
The hook contract follows the [official OpenAI documentation](https://learn.chatgpt.com/docs/hooks).

## Install

Install the Relay CLI first, then choose one scope:

```bash
relay codex install --global
# Or, from the target Git repository:
relay codex install --project
```

Global installation merges entries into `~/.codex/hooks.json`, or `$CODEX_HOME/hooks.json` when
`CODEX_HOME` is set. Project installation uses `<repository>/.codex/hooks.json`. Other JSON settings
and hooks are preserved. Reinstalling identical entries does not rewrite the file. `--force` replaces
only customized Relay entries; malformed configuration and symlinks are rejected even with `--force`.
The installer does not edit `config.toml`, permissions, or hook trust records.

Ensure `relay` is on the Codex process's PATH. Start a new Codex session, open `/hooks`, and review
and trust the Relay definitions. Project configuration must also be trusted. Commit project hook
files before `relay start`, so the worktree starts clean. Choose global installation for a
multi-repository workspace whose parent is not itself a Git repository.

## Lifecycle and output

| Event | Installed command | Behavior |
| --- | --- | --- |
| `UserPromptSubmit` | `relay codex hook prompt-submit` | Handoff; inject the Relay protocol and normal-turn review statistics as JSON context |
| `Stop` | `relay codex hook agent-stop` | Close the turn, leave changes pending, restore unexpected BTW writes |
| `PreToolUse` | `relay codex hook pre-tool-use` | Guard direct Git and human-only Relay commands; guard known writes during BTW |

Prompt output uses `hookSpecificOutput.additionalContext`. Stop is silent on ordinary success;
BTW restoration emits JSON with `systemMessage`, showing the saved snapshot IDs without requesting
another agent turn. Guard denials exit 2 with a reason on stderr. Bash, `exec_command`,
`shell_command`, Git MCP metadata, and `apply_patch` are covered by the shared best-effort guard.
Arbitrary programs and dynamically constructed commands are outside its scope.

Conversation IDs have a `codex:` prefix internally. A Claude Code or Kiro conversation cannot take
over an identified open Codex turn. Duplicate prompt hooks preserve its original handoff, and duplicate
Stop hooks are inert. Child tool calls are guarded; Relay does not install a `SubagentStop` hook.
Inactive and suspended sessions produce no output and make no Git/filesystem changes.

After manually interrupting Codex, wait for it to stop and run `relay end-interrupted-agent-turn`
before reviewing, editing, or staging. No automatic `Interrupt` cleanup is installed. If another
hook or an automated background continuation keeps the agent working after Stop, wait for all work
to finish before review; Relay does not stop agent processes.

## Smoke test

Use a disposable repository and a Codex environment that can execute `relay`:

1. Install globally, start a fresh Codex session, and trust the Relay hooks in `/hooks`.
2. Run `relay start -m "Exercise Codex integration"` in the human terminal.
3. Ask Codex to change a tracked file. After it stops, verify `relay status` reports the human phase
   and the change remains unstaged.
4. Stage part of the change, submit another prompt, and verify the approval and human-change context
   refers to that handoff. Use `relay agent diff pending` to inspect remaining proposals.
5. Run `relay btw`, ask a read-only question, and verify the staged/unstaged state survives.
6. Stage the desired final result, run `relay finish`, and inspect the single result commit.

Automated tests exercise these commands with native payload fixtures, real Git repositories, workspace
coordination, and the standalone archive. They do not launch a paid/live Codex conversation.

To uninstall, remove only handlers whose commands begin `relay codex hook ` from the `hooks` object.
Keep unrelated handlers and settings. Check both scopes if you installed more than one.
