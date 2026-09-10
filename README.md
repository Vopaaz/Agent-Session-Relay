# Agent-Session-Relay

[简体中文](https://github.com/Vopaaz/Agent-Session-Relay/blob/master/README.zh-CN.md) ·
[Contributing](https://github.com/Vopaaz/Agent-Session-Relay/blob/master/CONTRIBUTING.md) ·
[Architecture](https://github.com/Vopaaz/Agent-Session-Relay/blob/master/docs/architecture.md)

**Use your normal Git staging UI to review an agent's work across multiple turns.**

An agent changes four files. You accept one, stage two good hunks in another, adjust a third by hand,
and send the rest back. Agent-Session-Relay keeps track of what you accepted, what you edited, and
what is still pending. You can keep using your preferred Git UI without explaining the origin of
every staged or unstaged change in each prompt.

The CLI is `relay`. Relay provides session state and semantic diffs; it does not provide a review UI.

## Mental model

| State | Meaning | What you do |
| --- | --- | --- |
| Reviewed checkpoint | The version accepted so far; still editable | Continue refining it when needed |
| Staged | Hunks accepted during this human turn | Stage files or selected hunks; unstage to revoke this turn's acceptance |
| Unstaged / new files | Proposals that still need review | Edit, request another agent pass, or discard using Git |
| Human edits | Directional proposals, with turn provenance | Leave them unstaged until you have reviewed them |

Reviewed code is **soft approval**, never a lock. If the agent renames or refactors accepted code,
the new delta returns to pending review. The same freedom applies to human-authored edits.

At each prompt handoff, Relay incorporates staged approvals into an internal reviewed checkpoint,
leaves the staging area clean relative to it, and preserves every remaining proposal. On Agent Stop,
Relay captures the agent's output and presents it as unstaged changes or untracked new files.

**The human uses Git. The agent uses Relay. Relay uses Git internally.**

## Install

Requires **Python 3.10+**, **Git 2.37+**, and Linux, macOS, or WSL. There are no Python runtime
dependencies. Kiro IDE 1.x / CLI 3.x is the first supported harness.

Install the published CLI with [pipx](https://pipx.pypa.io/):

```bash
pipx install agent-session-relay
relay --version
```

Upgrade it later with `pipx upgrade agent-session-relay`. If `relay` is not yet on PATH, run
`pipx ensurepath` once and open a new terminal.

Alternatively, build a standalone executable from this repository with the standard library:

```bash
git clone https://github.com/Vopaaz/Agent-Session-Relay.git
cd Agent-Session-Relay
python3 scripts/build_zipapp.py
mkdir -p ~/.local/bin
install -m 755 dist/relay.pyz ~/.local/bin/relay
export PATH="$HOME/.local/bin:$PATH"
relay --version
```

Add that PATH setting to your shell configuration if needed. The archive is portable between supported
platforms and still needs Python and Git on PATH. `dist/relay.pyz.sha256` contains its checksum.

To install from a checkout, use `pipx install .`, or run `python -m pip install .` inside a virtual
environment. Contributors can use `./relay` directly.

Configure Git's `user.name` and `user.email` before finishing a session; the final public commit uses
your normal Git identity. Internal snapshots and abort recovery also work without that identity.

## Kiro integration

Choose one installation scope:

```bash
relay kiro install --global
# or, inside the target repository:
relay kiro install --project
```

| Scope | File written | Applies to |
| --- | --- | --- |
| Global | `~/.kiro/hooks/agent-session-relay.json` | Your Kiro work across projects |
| Project | `<repository>/.kiro/hooks/agent-session-relay.json` | This repository; commit the file to share it |

These use Kiro's standalone `v1` hook schema. Project installation must be committed before
`relay start` so the repository is clean. Ensure `relay` is on **Kiro's** PATH, then open a new Kiro
session. Installation preserves other hook files and is idempotent; replacing a customized Relay
file requires `--force`. Remove that dedicated file to uninstall the integration.

Three hooks implement the workflow:

| Event | Relay behavior |
| --- | --- |
| Prompt Submit (`UserPromptSubmit`) | Seal staged approvals, snapshot pre-agent code, record review/human provenance, inject the Relay protocol |
| Agent Stop (`Stop`) | Snapshot the completed agent turn and leave its changes pending |
| Pre Tool Use (`PreToolUse`) | Block direct Git commands and guide the agent to `relay agent` |

On every active handoff, the injected protocol explains the review model, permission to change
previously accepted or human-edited code, and all inspection commands. No separate Agent Skill is
needed. File lists and patches are queried on demand, not automatically stuffed into each prompt.

**With no active session, including while suspended, every hook succeeds silently without changing
Git or injecting context.** Normal Kiro conversations retain their ordinary Git behavior.

The adapter follows the [Kiro hook schema](https://kiro.dev/docs/hooks/),
[event mapping](https://kiro.dev/docs/cli/v3/hooks-migration/),
[command I/O contract](https://kiro.dev/docs/hooks/actions/), and
[configuration scopes](https://kiro.dev/docs/configuration/).
See [integration details and a live smoke test](https://github.com/Vopaaz/Agent-Session-Relay/blob/master/docs/kiro.md).

## Human commands

| Command | Effect |
| --- | --- |
| `relay start` | Start from a completely clean, stable Git workspace |
| `relay status` | Show lifecycle, reviewed checkpoint, staged approvals, and remaining proposals |
| `relay suspend` | Save full staging/workspace state and return to the origin branch |
| `relay resume [session]` | Restore the only suspended session, or a selected ID/prefix |
| `relay finish [-m "message"]` | Require full review; create and switch to a result branch with one public commit |
| `relay abort` | Ask twice, preserve all current project code in a recovery branch, then remove the session |
| `relay list` | List active and suspended sessions in this worktree |

`status` and `list` also accept `--json`. `relay recover` is available for an interrupted operation.
The usual single-session workflow never requires a session ID.

Starting rejects staged changes, unstaged changes, non-ignored untracked files, unresolved index
conflicts, and unfinished merge/rebase/cherry-pick/revert/bisect operations. An initial commit is
required. While active, use Git for staging and discarding; suspend before switching branches,
committing, stashing, or rewriting history.

## Agent commands

```bash
relay agent status                         # JSON summary; no complete patches
relay agent diff reviewed                  # Exact newly accepted hunks at the latest handoff
relay agent diff human                     # Previous Agent Stop → latest pre-agent snapshot
relay agent diff pending                   # Reviewed checkpoint → live workspace
```

Every diff supports `--name-only` and literal path filters after `--`:

```bash
relay agent diff reviewed --name-only
relay agent diff human --name-only
relay agent diff pending --name-only
relay agent diff reviewed -- Parser.kt
relay agent diff human -- Parser.kt Config.kt
relay agent diff pending -- src/parser/
```

Paths are relative to the command's current directory. Use `--name-only -z` for NUL-delimited names
in scripts. Patches include binary changes. Renames appear as a deletion/addition so path-filtered
views remain explicit. Inspection never changes the real index or working files.

`reviewed` and `human` are fixed for the latest handoff, including after Agent Stop; `pending` stays
live. Before the first handoff, the first two are empty. The human view includes direct edits **and
discards**, since both change the workspace after the agent stops. It describes provenance, not
ownership or an instruction to preserve those lines verbatim. Before the next handoff, staged
approvals are still part of the delta against the previous reviewed checkpoint.

## A complete multi-turn partial-review example

Begin on `main` at commit `C`:

```text
main
A --- B --- C
```

```bash
relay start
```

The immutable base is now `C`. Send this prompt in Kiro:

> Refactor the parser and move token configuration into the new config model.

The Prompt Submit hook teaches the agent Relay's protocol, including `relay agent status` and the
`reviewed`, `human`, and `pending` diffs. The agent changes `Parser.kt`, `Token.kt`, `Config.kt`, and
`README.md`. Agent Stop snapshots its output; all four files are pending in your usual Git UI.

Review the files:

| File | Human action |
| --- | --- |
| `Token.kt` | Correct: stage the entire file |
| `Parser.kt` | Stage only the good hunks; manually adjust another hunk and leave that edit unstaged |
| `Config.kt` | Wrong direction: leave it unstaged for the next agent turn |
| `README.md` | Unnecessary: discard the change through Git |

Your Git UI now shows:

```text
Staged:
  Token.kt
  selected Parser.kt hunks

Changes:
  Parser.kt
  Config.kt
```

Send the next prompt:

> Keep the direction I used in Parser.kt, but simplify it further.
> Rework Config.kt using the existing configuration abstraction.
>
> Also rename TokenDefinition to ParsedToken across the implementation.

Prompt Submit seals `Token.kt` and the selected parser hunks into the reviewed checkpoint. The
index becomes clean; the remaining parser and configuration changes stay pending. Relay snapshots
pre-agent code, records the newly reviewed patch and human changes, and injects the protocol again.

The agent can first get a file overview:

```bash
relay agent diff reviewed --name-only
# Parser.kt, Token.kt
relay agent diff human --name-only
# Parser.kt, README.md (the discard is also a human workspace change)
relay agent diff pending --name-only
# Config.kt, Parser.kt
```

Then it can read exact hunks only where useful:

```bash
relay agent diff reviewed -- Parser.kt   # What you accepted
relay agent diff human -- Parser.kt      # Your manual adjustment
relay agent diff pending -- Parser.kt    # What remains unresolved
```

The agent simplifies the parser, reworks the config, and edits **previously reviewed** `Token.kt` to
perform the rename. That is expected: the new token delta is pending again. Agent Stop captures the
new output. You review and stage all remaining changes, leaving no unstaged or untracked proposals.

```bash
relay finish -m "Refactor parser and configuration"
```

The session is complete. There is one public result commit, regardless of how many turns and
internal checkpoints were used:

```text
A --- B --- C                      main (unchanged)
             \
              R                    relay/result/<session>

R.parent = C
R.tree   = final reviewed tree
```

## Suspend and resume

After the agent stops, save an unfinished review to handle other work:

```bash
relay suspend
git switch -c urgent-fix
# Make the fix, then commit it normally.
git add -A
git commit -m "Handle urgent fix"
relay resume
```

Relay restores the reviewed checkpoint, staged hunks, unstaged edits, new project files, turn data,
and provenance. A file staged as version A and edited further to version B returns in that same
state. Intent-to-add, binary contents, symlinks, and executable bits are preserved using Git's model.
Unrelated ignored files remain in place. Resume requires a clean, stable normal workspace.

If multiple sessions are suspended, use `relay list`, then `relay resume <session>` (an unambiguous
prefix also works). Sessions belong to a Git worktree; linked worktrees can run independent sessions.
Other branches may advance while a session is suspended; its base commit never changes.

Suspend and abort return to the original branch's current tip. If that branch was deleted or is
checked out in another worktree, Relay returns to the original commit in detached mode instead of
moving or hijacking the branch.

## Finish and integrate the result

Finish refuses pending changes. You may stage the last accepted hunks and immediately finish;
another agent handoff is not required. Even a session with no final code delta produces one result
commit. Relay switches to `relay/result/<session>` and removes that session's internal refs/metadata.

Finish never merges, rebases, cherry-picks, or absorbs new mainline commits. Afterward, normal Git is
available again. For example, with `main` as your current development branch:

```bash
# On the result branch after relay finish:
git rebase main
# Then integrate with normal Git, for example:
git switch main
git merge --ff-only relay/result/<session>
```

Or switch to your destination branch and use `git merge relay/result/<session>` or
`git cherry-pick relay/result/<session>`. Replace `<session>` with the branch printed by finish.
These are your explicit Git operations; Relay does not run them.

## Abort preserves a recovery branch

```bash
relay abort
```

The command issues **two separate warnings and confirmations**. You must first type `abort`, then
`preserve and abort`. EOF, any other response, or declining either prompt cancels without cleanup.
There is no `--yes` shortcut. A suspended session must be resumed before aborting it.

Before deleting anything needed to resume, Relay creates `relay/aborted/<session>` with one commit
whose parent is the immutable base and whose tree contains the complete current project code:
previously reviewed code, staged/unstaged changes, agent/human edits, and non-ignored new files.
Unrelated ignored files are excluded and left in place. The recovery commit preserves code, not
staged/unstaged distinction.

Relay then removes internal session refs and provenance, returns to the origin workspace, and prints
the recovery branch name. The session cannot be resumed. **Relay does not delete the recovery branch.**
Inspect it with normal Git or switch to it. Only when you decide it is no longer needed:

```bash
git branch -D relay/aborted/<session>
```

## Architecture and support

```text
src/agent_session_relay/
  cli.py                    Human and agent command interfaces
  core/
    git.py                  Temporary indexes, trees, commits, refs, workspace transitions
    storage.py              Atomic metadata, process lock, recovery journal
    session.py              Review lifecycle and provenance baselines
  integrations/
    protocol.py             Self-contained instructions injected every active turn
    kiro/                   Installation, lifecycle adapter, shell/Git guard
docs/                       Requirements, architecture, integration contract
tests/                      Real Git workflow and adapter tests
```

State lives under the worktree's Git directory in `agent-session-relay/`, with snapshots pinned under
`refs/relay/sessions/`. Internal history never becomes an ancestor of a public result/recovery commit.
Mutations are locked and journaled; ordinary failures roll back. If a process is killed mid-transition,
`relay recover` first preserves the current code in `relay/recovered/<id>`, then restores the prior
review state. Do not edit files or run other Git operations during a lifecycle command.

Kiro IDE 1.x / CLI 3.x is implemented. Codex, Claude Code, and other harnesses can add adapters using
the same core; they are not shipped as supported integrations in this release. Legacy Kiro hook
formats are not installed. The command guard covers ordinary Git invocations, wrappers, and common
shell substitutions; it is not a security sandbox for arbitrary programs.

Version 0.1 deliberately rejects sparse checkouts, submodules/embedded repositories, and
assume-unchanged/skip-worktree flags instead of taking incomplete snapshots. Git ignore rules,
attributes, and clean/smudge filters apply normally. See
[security scope](https://github.com/Vopaaz/Agent-Session-Relay/blob/master/SECURITY.md).

## Develop

```bash
python3 -m unittest discover -s tests -v
python3 scripts/build_zipapp.py
```

See [CONTRIBUTING.md](https://github.com/Vopaaz/Agent-Session-Relay/blob/master/CONTRIBUTING.md)
for development and release checks.

Copyright 2026 Agent-Session-Relay contributors. Licensed under
[Apache-2.0](https://github.com/Vopaaz/Agent-Session-Relay/blob/master/LICENSE).
