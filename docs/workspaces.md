# Multi-repository sessions

A workspace session coordinates one feature across explicitly selected Git worktrees. The human
reviews each repository with their normal Git UI. One prompt hands off every current member; one
Stop closes the shared turn. Members can finish together or leave as their work is completed.

## Start and discovery

```bash
cd parent
relay start --repo ./r1 --repo ./r2 -m "Implement the feature"
```

Each argument must identify a distinct worktree root below `parent`. Members must not overlap or
contain one another. Every member needs an initial commit, a clean and stable workspace, and no
active Relay session. Other suspended sessions keep their own scope and review state. Linked
worktrees are supported, including members sharing a Git object database.

The member list is explicit. `parent/.relay/state.json` stores active/suspended
sessions, each with its own member list; there is no shared repository configuration. Hooks must
already be loaded. No environment selector is required or provided.

The parent, each selected repository, and its subdirectories resolve to the same session. Discovery
walks ancestors for `.relay/` and verifies the current Git worktree against the member list. Unselected
sibling repositories and independent nested repositories are outside the session. Hook process cwd
and event cwd use these same rules; conflicting active sessions cause an error. A tool's own working
directory cannot disable the active workspace's guard.

`relay resume` restores the selected session's exact scope. New multi-repository sessions specify
their members with `relay start --repo ...`; plain `relay start` starts in the current Git worktree.
Finished sessions leave no repository configuration to reuse.

## Add repositories during a session

```bash
# While the active session is in the human phase, from parent/r1:
relay repo add ../r2 ../r3
```

Each path is relative to cwd and must identify a clean, stable Git worktree root with an initial
commit and no active session. Existing members may have staged approvals and pending changes.
`add` requires an active session in the human phase; inactive, suspended, and agent-phase calls
fail. It affects only that session, including when other sessions use the same worktrees.

For a standalone session, Relay selects the nearest common parent of the original and all new
worktrees as the workspace root. The original session ID, base, checkpoints, staging, and turn
history are preserved. Neither the command nor the harness needs to change cwd. Existing
multi-repository sessions keep their root; additions must be below it. Members cannot overlap.

New members use their own HEAD as their immutable base and reviewed checkpoint. They join the
next handoff with the session's next-turn mode and turn numbering, without invented earlier
snapshots. They use the same member records and lifecycle as initial members. Diff/status,
per-repository messages, finish, abort, suspend/resume, and recovery include them normally.
Standalone suspended sessions remain in their original stores and are discoverable with `list`,
`message`, and `resume` from their worktrees. All paths are validated before enrollment, and the
whole batch is one recoverable group transaction. If validation or enrollment fails, no new members
are kept.

## Finish selected repositories during a session

```bash
# During the human phase, from parent:
relay repo finish ./r1 ./r2 -m "Complete these packages"
# Or from a member/subdirectory, using a registered name or a cwd-relative worktree root:
relay repo finish r2
```

At least one target is required. Every target must identify a distinct current member. Only selected
members must be fully reviewed, including the last staged approvals; other members may have staged,
unstaged, or untracked changes. The command requires an active session in the human phase. It keeps
the normal workspace and member locks and consistency checks; unavailable or unstable members still
need attention before a lifecycle operation can proceed.

Selected members finish with the same result commits and branch behavior as `relay finish`.
Unchanged selected members return to their origin without an empty commit. The entire batch,
including removal from the member list and per-member message overrides, is one recoverable
transaction. A failure rolls it back. Unselected members keep their HEAD, index, review history,
messages, next-turn mode, and conversation ownership.

Message priority remains per-member override, then this command's `-m`, then the saved shared message.
Only selected members need messages. If a required message is missing, one editor opens using the
first selected member in repository-name order. `-m` and editor input apply only to this finish;
they do not change the shared message for remaining members.

The remaining session keeps its ID and workspace root, even with one member left. Finishing the
last member ends the session. Later status, diff, suspend, resume, finish, and abort cover only the
remaining members; they do not undo completed results. Other suspended sessions retain their own
membership. A completed repository may be added again, using its current HEAD as a new immutable
base and a fresh member ID; its earlier result is not folded into the new result commit.

A finished repository is no longer a discovery entry for this session. If the agent was started
there, start a new agent session from the common parent or a remaining member. Completing a later
addition leaves the original member's entry intact. Hooks in an active workspace retain their
existing Git guard, which also blocks direct agent Git commands targeting unrelated repositories.
Normal human Git usage is unaffected. In a standalone session, `relay repo finish .` finishes that
session like `relay finish`, with the same human-phase requirement as other `repo finish` calls.

## Review and inspection

Each member retains its own base, reviewed HEAD, index, snapshots, and semantic diffs. Phase, turn,
next-turn mode, and conversation ownership are coordinated. Normal/btw and interrupted-turn rules
apply to the entire group. Duplicate hooks from different member directories continue the same turn.

Every prompt hook includes the current members and their absolute paths, including single-repository
sessions and continuations. The first hook in each conversation introduces review semantics; later
hooks use a brief Relay reminder. Each new normal turn includes per-file
added/deleted line counts for approvals and human edits, with workspace-relative paths such as
`r1/src/main.py`. Binary changes are marked `binary`.

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
them. Suspended sessions may have different membership. `list` and ID prefixes
refer to logical workspace sessions.

`relay message -m "Feature description"` sets the shared description. `relay message --repo r1 -m
"Configure events"` sets a member override, preserved when the shared description changes. Finish
always prefers each member's override. Explicit `finish -m` replaces only the shared description for
that finish; members without overrides use it, or the saved shared description when `-m` is omitted.
If every member has an override, no shared description or editor is needed. Otherwise, when the
shared description is missing, finish opens one editor to supply it for members without overrides.
The first selected member in repository-name order supplies Git editor/template configuration.
Editing holds no locks; saving rechecks the group and selected member review states. Workspace
metadata stores descriptions/overrides, and member descriptions also use native Git refs.

Finish requires every member's workspace to equal its index, including approvals staged since the
last handoff. Changed members need a public Git identity. Each gets one result commit, parented by
its own fixed base, on `relay/result/<member-session-id>`. Distinct member IDs prevent
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
Relay will not silently skip it. Select reviewed members explicitly with `relay repo finish`.
