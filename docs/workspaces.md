# Multi-repository sessions

A workspace session coordinates one feature across explicitly selected Git worktrees. The human
reviews each repository with their normal Git UI. One prompt hands off every member; one Stop closes
the shared turn. All members finish together.

## Start and discovery

```bash
cd parent
relay start --repo ./r1 --repo ./r2 -m "Implement the feature"
```

Each argument must identify a distinct worktree root below `parent`. Members must not overlap or
contain one another. Every member needs an initial commit, a clean and stable workspace, and no
independent Relay session. Finish or abort existing standalone sessions before enrollment. Linked
worktrees are supported, including members sharing a Git object database.

The member list is explicit. `parent/.relay/state.json` records configuration and active/suspended
workspace sessions. Hooks must already be loaded; discovery does not install them or change the
harness's filesystem permissions. No environment selector is required or provided.

The parent, each selected repository, and its subdirectories resolve to the same session. Discovery
walks ancestors for `.relay/` and verifies the current Git worktree against the member list. Unselected
sibling repositories and independent nested repositories are outside the session. Hook process cwd
and event cwd use these same rules; conflicting active sessions cause an error. A tool's own working
directory cannot disable the active workspace's guard.

After finish or abort, configuration remains. `relay start` from any recognized entry starts another
session with the same members. To change membership, finish or abort all active and suspended
workspace sessions, then run `relay start --repo ...` again from `parent`. Membership cannot change
within a session. Standalone usage outside configured workspaces keeps its existing behavior.

## Review and inspection

Each member retains its own base, reviewed HEAD, index, snapshots, and semantic diffs. Phase, turn,
next-turn mode, and conversation ownership are coordinated. Normal/btw and interrupted-turn rules
apply to the entire group. Duplicate hooks from different member directories continue the same turn.

Status and unfiltered semantic diffs cover all members regardless of cwd. Status JSON adds a
`workspace` path and `repositories` mapping keyed by workspace-relative repository name. Top-level
change/provenance flags are true when any member has that property. Agent status continues to omit
internal session and Git identities; human status retains per-member diagnostics.

```bash
relay agent diff session --stat
relay agent diff reviewed --name-only
relay agent diff human --name-only -z
relay agent diff pending --repo r2
```

Names are workspace-relative (`r1/src/main.py`); patches use `a/r1/...` and `b/r1/...`; stat output
labels each repository. Empty diffs produce no output. Line-oriented names containing control
characters, quotes, or backslashes are JSON-quoted; `-z` emits literal bytes separated by NUL.
Partial-hunk staging approves only those hunks.

`--repo` accepts a registered name or a cwd-relative path to a member. Path filters after `--` stay
relative to cwd, even with a repository selector:

```bash
# In parent:
relay agent diff pending --repo r2 -- r2/src/main.py
# In parent/r2/src:
relay agent diff pending --repo r2 -- main.py
```

History queries are single-repository queries. `relay agent git --repo r2 log --oneline main` uses
the existing read-only validator in r2's root; Git path operands then refer to that root. The selector
must precede the Git subcommand. Without it, `relay agent git` keeps the caller's cwd and does not
inspect Relay state.

## Lifecycle and messages

`btw`, `end-interrupted-agent-turn`, `suspend`, `resume`, `finish`, and `abort` operate on the whole
session. Suspend saves every workspace and full index; resume checks all members before restoring
them. Multiple suspended sessions with the same membership are supported. `list` and ID prefixes
refer to logical workspace sessions.

`relay message -m "Feature description"` sets the shared description. `relay message --repo r1 -m
"Configure events"` sets a member override, preserved when the shared description changes. Explicit
`finish -m` overrides all saved descriptions for that finish. If the shared description is missing,
finish opens one editor using the first member in repository-name order for Git editor/template
configuration. Editing holds no locks; saving rechecks the group and all member review states.
Workspace metadata stores descriptions/overrides, and member descriptions also use native Git refs.

Finish requires every member's workspace to equal its index, including approvals staged since the
last handoff. Changed members need a public Git identity. Each gets one result commit, parented by
its own fixed base, on `relay/result/<workspace-session-id>-<member-number>`. The suffix prevents
collisions between linked worktrees. Internal checkpoints are excluded from public ancestry.
Unchanged members return to their available origin without creating an empty commit.

Abort asks once, listing every member's recovery branch. Current code is preserved in each repository
before review state is removed. Recovery branches remain available even if a later abort failure
causes the group to roll back.

## Failure and recovery

Operations hold the workspace lock and all member locks in stable order. A coordinator journal at
`parent/.relay/transaction.json` records the previous workspace state and a durable commit/rollback
decision. Every member receives a normal per-worktree recovery journal and pinned before-images
before lifecycle mutations begin. All prior session refs stay protected from GC until the group
commits or fully rolls back.

Ordinary failures roll back every prepared member. A process interruption blocks normal commands
and hooks until recovery, even if a partial start/finish has no active pointer. Run `relay recover`
from the parent or any registered member. For an undecided operation, recovery first preserves
current code in every prepared member, including edits after the interruption, then restores all
previous review states. If the durable decision already committed, recovery only completes cleanup
and leaves current code in place.

This provides a recoverable group lifecycle; Git does not supply an atomic transaction spanning
repositories. Relay locks do not exclude editors or Git UIs. Avoid concurrent mutations during
lifecycle commands. If a member is unavailable, restore its registered worktree before retrying;
Relay will not skip it or finish a subset.
