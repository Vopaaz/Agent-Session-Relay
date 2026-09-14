# Kiro adapter

The installer targets Kiro IDE 1.x and CLI 3.x's standalone `v1` JSON hooks. It does not install the
older `.kiro.hook` format or modify embedded agent profiles. The reference contracts were checked
against Kiro's official documentation on 2026-09-08.

- [Schema and automatic discovery](https://kiro.dev/docs/hooks/)
- [Event names and migration mapping](https://kiro.dev/docs/cli/v3/hooks-migration/)
- [Shell action output and blocking](https://kiro.dev/docs/hooks/actions/)
- [Global and project paths](https://kiro.dev/docs/configuration/)
- [Tool context and lifecycle descriptions](https://kiro.dev/docs/hooks/types/)

The human-readable Prompt Submit / Agent Stop events map to `UserPromptSubmit` / `Stop` in the
standalone schema. All hooks use command actions. The PreToolUse hook has no matcher restriction so
that the adapter can recognize shell aliases as well as direct Git MCP tools. During normal turns
it leaves non-shell, non-Git tools alone. During btw it also checks known file write tools.

## Installation and discovery

`relay kiro install --global` writes `~/.kiro/hooks/agent-session-relay.json`.
`relay kiro install --project` writes `<repository>/.kiro/hooks/agent-session-relay.json`.
Choose either scope; both are user-controlled. A project hook file should be committed before start.
No agent model, system prompt, tool permissions, or other hooks are rewritten. Customizations to the
dedicated Relay file are preserved unless the installer is invoked with `--force`.

Kiro must resolve `relay` from PATH. GUI-launched IDEs can have a different PATH from your terminal;
restart Kiro from an environment where `relay --version` works. Start a new Kiro session after install.
Remove the dedicated file to uninstall. Duplicate lifecycle delivery (for example, both installation
scopes) is idempotent while a turn is open; it cannot reseal approvals or erase that turn's provenance.

## Hook protocol

| Entrypoint | Input | Output while active |
| --- | --- | --- |
| `relay kiro hook prompt-submit` | Optional JSON `cwd`, `session_id` | Mode-specific protocol; normal turns also include any newly approved and human-changed paths; exit 2 on error |
| `relay kiro hook agent-stop` | Optional JSON `cwd`, `session_id` | Usually silent; btw restoration notice and retrieval commands when needed; exit 2 on error |
| `relay kiro hook pre-tool-use` | JSON `tool_name` and `tool_input` | Silent success, or guidance on stderr with exit 2 to block |

The adapter reads JSON from stdin without evaluating it. `cwd` helps discover the active repository
when a launcher does not use the workspace as the process directory. Changing a shell tool's own
working-directory argument does not disable the current workspace's guard. Prompts and tool inputs
are not written into Relay metadata. A `session_id` identifies ownership of an open agent turn.

Normal prompts announce approvals only when they were sealed at that handoff. The notice lists
paths from `diff reviewed` and explains that the files may still contain pending hunks. Approvals
and human edits have separate complete JSON arrays of repository-relative paths, each omitted when
empty; no patch is injected. JSON escaping keeps unusual filenames on a single data line. Repeated
hooks for an open turn reproduce its original notice and lists. Btw prompts omit both lists, while
`relay agent diff human` still exposes the accumulated changes at btw entry.
The protocol explains the live `session`/`pending` views, fixed `reviewed`/`human` views, and
`--name-only`/`--stat`/path filtering. It calls human workspace changes "edits", without implying
a separately tracked discard/rejection state.
Mode selection comes from `relay btw`, not from interpreting the user's natural-language prompt.

When inactive or suspended, all three entrypoints return 0, emit nothing, and create no state files,
locks, Git objects, or context. Malformed input is also ignored while inactive. When active, malformed
shell context is rejected rather than silently disabling the guard. Relay's internal Git subprocesses
are not agent tool calls and therefore do not re-enter the guard.

The adapter recognizes common direct Git calls, absolute executable paths, shell quoting, compound
commands, command substitutions, wrappers (`env`, `command`, `sudo`, `xargs`, and shell `-c`), and
common literal subprocess invocations. It guides agents toward the semantic inspection interface.
It also blocks human-only `relay` lifecycle commands from agent shell calls. Dynamic programs,
pre-existing shell aliases, arbitrary scripts, and renamed binaries are outside this guard's scope;
harness sandboxing remains separate.

`relay agent git <args...>` is allowed through the hook in normal and btw turns. The command itself
enforces an explicit allowlist of read-only Git query forms, so bypassing the hook does not bypass
that validation. Direct `git` calls remain blocked. The injected protocol prefers semantic diffs
for current-session changes and reserves this entry point for inspection outside the session;
Relay-managed objects are accepted without target classification. See [query forms](agent-git.md).

Btw uses a separate small best-effort guard (`readonly.py`) for known write tools, output redirection,
common file-writing commands, and in-place sed/perl. It does not attempt to infer arbitrary scripts'
side effects or expand the existing Git parser. Writes that escape this guard are saved at Stop
before the original workspace and index are restored. The hook output tells the agent that the
changes were restored and provides inspection/retrieval commands; `relay status` retains those
commands for the human too. Read-only btw turns do not rewrite project files or the index.

## Live smoke test in Kiro

Use a disposable repository with the current Kiro version. Automated tests replay hook contracts
with real Git, but do not launch or impersonate a real Kiro agent service.

1. Install Relay and the chosen hook scope. Commit project configuration if applicable.
2. With no Relay session, ask Kiro to inspect Git status. Verify ordinary Git access and no Relay
   context. This checks that installation does not affect other conversations.
3. Run `relay start` in the human terminal. Ask Kiro to make two small changes. Verify Prompt Submit
   introduces Relay and that Agent Stop leaves the changes unstaged.
4. Ask the agent to run `git diff`. Verify PreToolUse blocks it with Relay command guidance. A
   `relay agent diff pending` tool call must succeed instead.
   Also verify `relay agent git log --oneline main` succeeds and `relay agent git branch unexpected`
   is rejected by the command without creating a branch.
5. Stage one hunk in your normal Git UI, edit another hunk manually, and send a second prompt. Verify
   staged approvals are sealed, `diff reviewed` shows that exact hunk, `diff human` shows the manual
   edit, and `diff pending` shows the unresolved remainder. The hook must list newly approved and
   human-changed paths separately, without their patch contents. `diff session --stat` must still
   include the approved work. On a later approval-only handoff, only the approval list appears;
   on a handoff without approvals, there is no approval notice.
6. After Agent Stop, stage one hunk and manually edit another. Run `relay btw`, then ask a code
   question. Verify no human file list is injected, human diff is still queryable, known writes are
   blocked, and staging survives Stop. Send a normal prompt and verify the full human list returns.
7. In a disposable test, allow a script to make a change during btw to exercise the fallback. Verify
   Stop reports a saved turn, restores the pre-btw state, and `diff btw --turn N` shows the script's
   changes. On a normal turn, retrieve them with `relay agent restore-btw N`; they remain unstaged.
8. After Agent Stop, suspend. Verify normal Git calls work again. Resume and verify staged/unstaged
   state returns unchanged.
9. Review and stage everything, then finish. Check the result's only parent is the start commit,
   and all session refs, including btw backups, have been removed.
10. In a separate disposable session, test abort cancellation and the single-confirmed recovery
    branch, including a newly created untracked file. Btw backups must also be cleaned up on abort.

If no protocol appears, check that the hooks are enabled and loaded, Kiro can find `relay`, and the
hook event's workspace corresponds to `relay status`. Old Kiro versions without the documented
stdin tool payload cannot provide this integration's complete Git guard and should be upgraded.

If an Agent Stop was missed, stop the agent before doing more human review. The human can explicitly
record the end boundary with `relay kiro hook agent-stop` (stdin can be `{}`), then continue. Relay
cannot reconstruct human/agent provenance for edits made across a missing boundary. Do not use
this manual entrypoint while an agent is still writing files.
