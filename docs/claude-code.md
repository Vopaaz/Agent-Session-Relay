# Claude Code integration

Relay supports Claude Code's `UserPromptSubmit`, `Stop`, and `PreToolUse` command hooks, following
the [official hook reference](https://code.claude.com/docs/en/hooks) and
[settings scopes](https://code.claude.com/docs/en/settings). Use a current Claude Code version with
these three events enabled.

## Install

Install Relay first, then choose one scope:

```bash
relay claude-code install --global
# Or, from the target Git repository:
relay claude-code install --project
```

`relay claude` is an alias for `relay claude-code`. Global installation merges handlers into
`~/.claude/settings.json`, or `$CLAUDE_CONFIG_DIR/settings.json` when that variable is set.
Project installation uses `<repository>/.claude/settings.json`. It preserves permissions, environment
settings, and unrelated hooks. Identical entries are not rewritten; `--force` replaces only customized
Relay entries. Invalid JSON and configuration symlinks are rejected even with `--force`.

Ensure `relay` is on Claude Code's PATH and start a new Claude Code session. Review the loaded hooks
with `/hooks` and accept the harness's workspace trust flow when required. Commit project settings
before `relay start`. Start Claude Code from the configured project directory, or install globally
when launching from a workspace parent or subdirectory. A multi-repository workspace parent does
not need to be a Git repository when hooks are installed globally.

## Lifecycle and output

| Event | Installed command | Behavior |
| --- | --- | --- |
| `UserPromptSubmit` | `relay claude-code hook prompt-submit` | Handoff; inject Relay context and normal-turn review statistics |
| `Stop` | `relay claude-code hook agent-stop` | Close the turn; keep ordinary changes pending; restore unexpected BTW writes |
| `PreToolUse` | `relay claude-code hook pre-tool-use` | Guard direct Git/human-only Relay commands and BTW writes |

The matcher is omitted so the guard sees all tool metadata, including Bash, Write, Edit, MultiEdit,
NotebookEdit, and Git MCP tools. Prompt output is JSON with
`hookSpecificOutput.hookEventName: "UserPromptSubmit"` and `additionalContext`. Denials exit 2 with
stderr guidance. Ordinary Stop has no output; BTW restoration reports snapshot IDs in a JSON
`systemMessage` without blocking Stop or requesting further work.

IDs are prefixed internally with `claude-code:` to distinguish conversations across harnesses.
Duplicate prompt hooks preserve the open handoff; duplicate Stop hooks are inert. Child tool calls
are guarded, but child lifecycle events do not end the parent's turn, and no `SubagentStop` hook
is installed. Inactive and suspended sessions are silent and do not change Git or the filesystem.

After manually interrupting Claude Code, wait for it to stop and run
`relay end-interrupted-agent-turn` before human review. Stop is not a process lock: wait for
background tasks and any continuation caused by other hooks before editing or staging.
The guard is a best-effort workflow aid; use Claude Code permissions for execution boundaries.

## Smoke test

In a disposable repository:

1. Install the integration and start a new Claude Code session with its hooks enabled.
2. In the human terminal run `relay start -m "Exercise Claude Code integration"`.
3. Ask Claude Code to edit a tracked file. After Stop, check `relay status` reports the human phase,
   with changes unstaged for review.
4. Stage selected edits and send another prompt. Check the injected approval/human statistics and
   `relay agent diff reviewed`, `human`, and `pending`.
5. Run `relay btw` and ask a question. Known writing tools should be denied. Any captured unexpected
   writes are saved before the original index/workspace are restored at Stop.
6. Stage all intended changes and `relay finish`; verify one result commit with the session base
   as its parent.

Automated tests cover native payloads, settings preservation, turn ownership, BTW restoration,
workspace coordination, and standalone packaging. They do not launch a paid/live Claude Code session.

To uninstall, remove only handlers beginning `relay claude-code hook ` from the `hooks` object,
preserving the other settings and hooks. Check both scopes if you installed both.
