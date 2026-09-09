PROTOCOL = """[Agent-Session-Relay active]

This workspace is managed by Agent-Session-Relay, a Git-based human/agent
review workflow. The human reviews proposals through normal Git staging.

- Reviewed checkpoint: the version the human has accepted so far. This is
  soft approval, not a lock. You may edit reviewed code again when needed;
  the new delta becomes pending review.
- Staged changes: hunks accepted during the current human turn. Relay seals
  these into the reviewed checkpoint at this handoff and leaves the index clean.
- Pending changes: the complete workspace delta against the reviewed checkpoint,
  including new project files. These changes still need human review.
- User-authored edits: soft proposals and directional signals, not authoritative
  or immutable code. You may refine them and modify any relevant project code.

The human uses Git; the agent uses Relay; Relay uses Git internally.
Do not invoke Git directly during this active session, even for status, diff,
log, or other inspection. Do not stage, commit, switch, reset, stash, or merge.
Relay owns the session baselines, which differ from ordinary Git terminology.
Leave your output as file edits for the human to review. Only the human runs
Relay lifecycle commands; your interface is `relay agent`.

Inspect state as needed; full patches are not injected automatically:

  relay agent status
      Session, lifecycle, reviewed checkpoint, pending and provenance availability.
  relay agent diff reviewed
      Exact hunks newly accepted by the human since the previous handoff.
  relay agent diff human
      Direct workspace changes between the previous Agent Stop and this handoff,
      including manual edits and discards. These are directional, editable proposals.
  relay agent diff pending
      The complete currently unresolved delta, updated as you edit files.

Start with a file overview when useful:

  relay agent diff reviewed --name-only
  relay agent diff human --name-only
  relay agent diff pending --name-only

Then inspect specific paths (relative to your current directory):

  relay agent diff reviewed -- path/to/Parser.kt
  relay agent diff human -- path/to/Parser.kt path/to/Config.kt
  relay agent diff pending -- path/to/Parser.kt

Reviewed and human diffs are fixed for this turn; pending always reflects the
current workspace. Use these commands to understand the human's review decisions
and directions before making further changes. The Agent Stop hook will snapshot
your output and present it as pending changes in the human's standard Git UI.
"""
