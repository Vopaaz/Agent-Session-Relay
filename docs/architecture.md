# Architecture

The [requirements](requirements.md) define the product. The core has no dependency on Kiro or on
an agent SDK. `Git` handles plumbing, `Store` handles durability, and `Relay` handles review semantics.
`Workspace` coordinates those per-worktree engines for a multi-repository session. The
[workspace contract](workspaces.md) extends the single-worktree behavior below.

## Workspace coordination

`core/workspace.py` discovers `.relay/` at an explicit common parent. Its schema-1 state holds member
paths/worktree identities, the active pointer, and logical workspace sessions. Member engines retain
schema-2 storage in their own Git directories; standalone sessions need no migration. Member IDs
share the group ID prefix, with distinct suffixes for linked worktrees sharing one object database.
Discovery from any member returns the whole workspace. Nonmembers remain independent.

Status/diffs aggregate member views. Phase, mode, turn, ownership, and lifecycle stay in step; bases,
checkpoints, indexes, and snapshots remain independent. One injected protocol includes membership and
workspace-relative changed paths. Diff filters remain relative to cwd.

`MetadataStore` supplies versioned JSON and locks. `Store.prepare()` separates creation of a recovery
journal from its commit point so the coordinator can prepare every member before invoking existing
`Relay` methods. Those methods reuse their prepared journal and already-held lock. All previous
session refs are pinned, including provenance removed by finish/abort.

After all members succeed, the coordinator saves workspace state and records a durable commit
decision. Only then does it release member journals and recovery refs. Ordinary errors restore all
members before recording completed rollback. Recovery of an undecided operation preserves current
code in all prepared members before rollback; recovery after commit only completes cleanup. A retained
coordinator journal blocks hooks and lifecycle commands, including after partial start/finish.

## Baselines

An active session uses the existing worktree with a **detached HEAD at its reviewed checkpoint**.
The origin branch is never advanced by internal checkpoints. This makes any standard Git staging UI
the review surface without requiring a custom diff renderer or a second copy of the repository.

| Symbol | Stored value |
| --- | --- |
| B | Immutable session base commit |
| R | Current reviewed checkpoint commit |
| I | Real Git index, representing this turn's staged approvals on top of R |
| W | Complete current project workspace tree |
| Pn | Pre-agent snapshot for turn n |
| Qn | Post-agent snapshot for turn n |

At a normal Prompt Submit, with `I` and `W` captured independently:

1. Remember the previous reviewed checkpoint `Rold`.
2. If `tree(I)` differs from `tree(Rold)`, create a checkpoint with tree I and parent Rold.
3. Move only detached HEAD and the internal reviewed ref to the new checkpoint; load its tree into I.
4. Leave working files untouched, including partial-file proposals and untracked files.
5. Save W as Pn, and record `(Rold, Rnew, last_normal_post, Pn)` for provenance.
6. Mark the phase `agent`. Build context from the saved turn comparisons before committing the
   transaction, so a failed stats read rolls back the handoff. The adapter emits Relay instructions
   and separate per-file line counts for newly approved changes and human edits.

The initial `Q0` is B. Thus edits made between start and the first prompt are human proposals.

At a normal Agent Stop, save W as Qn and mark the phase `human`. Reset only the index to R, ensuring agent
output remains unreviewed even if a tool accidentally staged it. A duplicate Stop in the human phase
does nothing, so it cannot erase approvals made after the first Stop.

An additional Prompt Submit while the phase is already `agent` is a continuation, including
repeated hook delivery. It does not create another turn, seal approvals, or change the original
provenance. The returned context carries a transient `continuing` flag; the brief reminder says that
reviewed/human diffs retain their original handoff and omits the original approval/human stats.
The saved reviewed/human diffs remain queryable. This does not distinguish an interrupted response from a
hook retry, and no cancellation event is inferred or persisted. A different known conversation
owner is still rejected.

The human command `relay end-interrupted-agent-turn` closes an open turn after the user has stopped
the agent and before human edits or staging begin. It calls the same mode-specific Stop transaction
with `require_open=True`: inactive/suspended sessions and the human phase are errors. This check and
the close occur under the same lock. Automatic Stop hooks remain silent no-ops after completion.
The command does not stop a process or verify cancellation; it records the explicit human boundary.
Normal turns preserve W and reset I to R; btw turns follow their existing save/restore behavior.
Failures use the same rollback/recovery mechanism. No additional persistent interruption state is needed.

## One-turn btw

`relay btw` sets `next_turn=btw`; `--cancel` resets it to `normal`. This changes only metadata, in
the human phase. The next successful Prompt Submit consumes the selection and stores a turn with
`kind=btw`, Pn, and the expanded full index. It leaves R, HEAD, and I alone. Duplicate prompts use
the open turn's kind and original snapshots. Turn numbers count both normal and btw activities.

The btw human view is `last_normal_post → Pn`; its reviewed view is empty. Btw does not advance
`last_post`, so human changes accumulate across any number of btw turns until a normal handoff.
An initial btw uses B as the human baseline. Inspecting a diff never consumes it.

At btw Stop, compare the workspace and normalized index with entry. If unchanged, only record
completion. If changed, pin emergency workspace and index snapshots, including staged-only blobs
and intent-to-add, before restoring Pn and the pre index. Reuse the existing transaction journal for
rollback and interruption recovery. After success, the adapter reports the snapshot commit IDs for
workspace and/or staging changes and confirms restoration to the last human-to-agent handoff.
These IDs are returned for that notice only, without persisting them in turn state.
The main review baseline is unchanged in either case; the next prompt defaults to normal.

Emergency snapshots are Git refs only: they are never recorded as turn output or recovery metadata,
and are excluded from semantic diffs, status, and subsequent handoffs. A completed btw turn keeps
`post=null`; its effective workspace remains Pn. The only btw controls are `relay btw` and
`relay btw --cancel`. Generic `relay agent git` queries can inspect the snapshots when needed.
The snapshots belong to the session namespace, survive suspend/resume, and are removed by
finish/abort with the rest of the session. There is no extra recovery branch or permanent btw state.

## Semantic inspection

| View | Git tree comparison |
| --- | --- |
| Session | B → live W (net changes by both human and agent, regardless of approval) |
| Reviewed | Rold → Rnew at the latest handoff |
| Human | Latest normal Q → Pn at the latest turn entry |
| Pending | R → live W |

Partial-hunk approval is represented by Git's actual index tree, not a per-file label. Human
provenance includes discards, reversions, additions, and deletions during the human interval. It
cannot distinguish who typed the same bytes; turn boundaries provide the provenance assumption.

Agent status projects the shared status snapshot into `active`, `phase`, `turn`, `turn_kind`,
`session_changes` (W != B), `pending_changes` (W != R), and `provenance`.
The provenance flags describe the latest turn entry, including human edits at btw entry; before
the first handoff, `available` and both change flags are false. Without an active session the
agent sees only `active: false`. Internal identities, human controls, index details, and static
help are omitted.
Human `relay status --json` retains full diagnostics, including unsealed approvals
(`staged_approvals`, I != R) and `unstaged_changes` (the current index plus non-ignored untracked
files, snapshotted from disk, differs from I). The latter blocks finish when true;
finish also requires a custom session message.
The reviewed/human views stay fixed while the agent works; session and pending are live. All views
support patch output, `--name-only` (optionally `-z`), or `--stat`, with literal path filtering.
Empty output remains a successful empty diff; invalid options fail on stderr with a nonzero exit.

Approval stats are derived from the turn's Rold → Rnew comparison, not from the live index:
sealing resets I to R, so `staged_approvals` is normally false after an actual approval. Continuation
hooks retain those comparisons without reannouncing stats. Btw seals nothing and emits neither
stats section. `git diff --numstat -z` supplies exact additions/deletions, binary markers, and complete
paths without patch content. An approved path may still contain pending hunks.

Snapshot and diff operations use a temporary index. Snapshotting copies the real index, expands any
split index, accounts for tracked files removed from the index but still present on disk, stages the
workspace only in the private index, and writes a tree. R and the latest effective workspace snapshot
keep previously captured project files in scope even after ignore rules change. Finish checks and
`unstaged_changes` instead use I as the snapshot baseline, so previously captured files that are
now ignored and absent from I do not become phantom unstaged changes. Full recovery snapshots
still preserve those files. Paths and patches stay within Git's binary,
symlink, executable-bit, ignore, and attribute semantics. Git hooks and external diff/textconv drivers
are not run by Relay; ordinary clean/smudge filters still apply.

Index copies preserve the source file's timestamp so Git still detects same-size edits made within
the filesystem's timestamp resolution. Restored raw index blobs have no original file timestamp;
Relay assigns an old, nonzero mtime so Git rechecks potentially racy entries against their contents.
The index bytes, staged blobs, and intent-to-add entries remain intact.

Agent history queries use `core/readonly_git.py`, separately from Relay's internal Git plumbing.
`relay agent git` validates explicit command/option forms before starting Git, preserves the
caller's cwd and streams, and does not acquire a Relay lock or create session state. Its allowlist
does not inspect ref names or object ownership. Queries mentioning Relay objects are accepted;
the protocol directs agents to semantic diffs for session changes and discourages interpreting
internal bookkeeping as project history. See [query behavior](agent-git.md).

## Storage

Each worktree has its own state and lock:

```text
<git-dir>/agent-session-relay/
  state.json                Versioned active pointer and active/suspended session records
  lock                      POSIX advisory process lock
  transaction.json          Present only during a recoverable transition

refs/relay/sessions/<id>/
  base                      Immutable B
  reviewed                  Current R; parent chain retains earlier reviewed checkpoints
  message                   Optional commit whose native message describes the session
  turn-000001-pre            P1 (and subsequent turn snapshots)
  turn-000001-post           Q1
  turn-000002-pre-index      Btw entry: expanded raw index blob
  turn-000002-pre-staged     Btw entry: staged-only object protection
  turn-000002-pre-intent-to-add
  turn-000002-post           Emergency workspace backup, only when btw changed workspace/index
  turn-000002-post-index     Emergency index backup
  turn-000002-post-staged
  turn-000002-post-intent-to-add
  suspend-workspace         Full suspended W
  suspend-index             Blob containing the expanded raw Git index
  suspend-staged            Commit protecting all staged blobs from GC
  suspend-intent-to-add     Empty blob required by intent-to-add entries

refs/relay/transactions/<id>/
  workspace                 Before-transition workspace snapshot
  index                     Before-transition raw index blob
  staged                    GC protection for staged-only object contents
  intent-to-add             GC protection for the empty blob
  saved/<session-ref-name>  Previous session refs, including message and every provenance snapshot
```

Snapshot commits have B as their parent; checkpoint commits have the previous checkpoint as parent.
Per-turn metadata retains entry/provenance commit IDs and normal-turn output commit IDs. Session IDs
include UTC time and a random suffix, so multiple worktrees can share the object database/ref namespace
without sharing an active session. Sessions in another worktree are managed from that worktree.
State uses schema 2. There is no migration or compatibility reader for sessions from other versions.

## Suspend, resume, finish, abort

Session descriptions are native commit messages stored at the optional `message` ref. Its commit
has B's tree and B as its only parent; it never moves HEAD or changes review baselines. `start -m`
creates it inside the start transaction. `relay message [session] [-m ...]` creates a replacement commit
and atomically updates just that ref under the worktree lock, without writing message text into
state JSON. No workspace transaction is needed for this single-ref mutation. Missing refs mean no
custom message has been set. `status` and `list` read the current message from Git; ordinary session
cleanup and rollback include its ref. Workspace transactions also pin the previous message until
their commit point, so a crash during finish/abort
cleanup followed by Git GC cannot remove the message needed for recovery.

Interactive editing lives in `editor.py`; the CLI supplies an editor callback to the core. Start
never edits interactively. Message edits prefill the existing message, or use `commit.template`
when no message exists. Finish calls the editor only if neither an explicit nor saved message is
available. `git var GIT_EDITOR` resolves the user's editor command, executed with inherited terminal
streams and a separately passed temporary `COMMIT_EDITMSG` path. The buffer is removed afterward;
the normal Git commit buffer is untouched. Git's stripspace plumbing handles edited-message cleanup
and comment prefixes, with `commit.cleanup` modes and unchanged-template rejection.

The editor runs without holding the Relay lock, a workspace transaction, or a Git index lock;
status and lifecycle hooks remain available. Saving rechecks the selected session and its previous
message under the lock so concurrent message changes or session removal are not overwritten.
Finish checks pending changes and public Git identity before invoking the editor, then revalidates
the session record, message, HEAD, and complete index/workspace after it returns, before starting its
workspace transaction. A changed session or a cancelled editor does not create a result commit.

Suspend pins W, I's raw bytes, I's tree, and provenance before leaving. The expanded index preserves
staged/unstaged distinctions and intent-to-add without depending on a sharedindex file's lifetime.
To leave the workspace, Relay loads W into the real index, refreshes its stat cache, then uses Git's
two-tree update to restore the origin. This removes only captured project additions; it does not run
`git clean`. Ignored path collisions are rejected before checkout. Resume reverses the operation and
restores the saved index separately from W.

Finish requires W = I and a non-empty custom message. An explicit `finish -m` overrides the saved
session message. In a multi-repository workspace, it replaces only the shared message for that
finish; per-member overrides always take priority. A shared message is required only if a member
has no override, so finish skips editing when every member already has its own message.
If a required message is still missing, the human CLI opens the configured Git editor and requires
a valid custom message; an empty message, unchanged template, or editor failure cancels finish.
The full subject/body becomes the public commit message. Finish creates `commit-tree(I, parent=B)`,
creates a result branch using an atomic create-only ref update, switches to it, and removes the
session. It never uses a squash merge that might accidentally pull internal parents or another
branch into the result.

Abort's single confirmation lives in the human CLI. The core rechecks the confirmed session identity
after acquiring the lock. It creates `commit-tree(W, parent=B)` and a normal recovery branch before
returning to the origin and removing the session. Recovery includes non-ignored untracked code; it
does not preserve the staging distinction. A suspended session must first be resumed.

Both result and recovery branches refuse to overwrite a pre-existing branch. Origin checkout falls
back to detached B when the origin branch is unavailable, rather than altering another worktree.
Deleting internal refs/metadata does not forcibly prune Git objects or scrub reflogs.

## Durability and failure handling

Mutations hold a per-worktree `flock`. Before changing HEAD/index/worktree, Relay pins recovery
objects and atomically writes a journal containing the previous state, HEAD, index, and owned refs.
State JSON is replaced atomically and synced. Ordinary exceptions roll back. Journal removal is the
transaction commit point; internal transaction refs can then be removed.

If the process is killed, commands stop with a recovery message. `relay recover` preserves code
currently on disk in a public `relay/recovered/<id>` branch **before** restoring the before-operation
review state. This also saves edits made after the interruption. Recovery does not guess whether to
resume a partially completed finish/abort. Hardware failure, disk exhaustion, and external concurrent
Git mutations may still require inspecting the retained journal and snapshots.

Relay locking coordinates Relay processes; it does not lock out editors or Git UIs. Wait for the
agent to stop before human review and do not run concurrent file/Git mutations during a lifecycle
command. A second agent conversation cannot acquire an agent turn owned by another conversation.
Duplicate prompt events during the same open turn preserve its original provenance.

## Adapter boundary

An adapter discovers the active worktree, parses lifecycle metadata, calls `handoff` / `stop`, emits
Relay context and new normal-turn approval/human stats, and translates tool events into guard
decisions. The first handoff for a conversation owner within a Relay session gets the full protocol;
later hooks get brief mode-specific reminders. This is derived from saved turn owners, including
across suspend/resume; missing owners share one fallback introduction. No additional persistent
event state is needed. The core does not read prompt text or call an agent API. New adapters belong
under `integrations/<harness>/`.

Kiro, Codex, and Claude Code use the shared dispatcher in `integrations/hooks.py` and the shared
shell/write guards. Kiro keeps its standalone v1 configuration and plain-text output. Codex and
Claude Code merge command hooks into their native JSON configuration, inject structured prompt
context, and emit BTW restoration notices as `systemMessage` JSON at Stop. Their conversation IDs
are prefixed by harness so matching IDs in different harnesses cannot acquire each other's turn;
legacy Kiro owner IDs are preserved. Child tool calls remain guarded; child lifecycle events do
not close a parent turn. No `SubagentStop` or automatic interruption cleanup is installed.

The shell parser is a workflow guard, not a general interpreter or security sandbox. See
[Kiro](kiro.md), [Codex](codex.md), [Claude Code](claude-code.md), and [SECURITY.md](../SECURITY.md).
