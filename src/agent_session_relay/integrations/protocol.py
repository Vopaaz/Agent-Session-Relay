PROTOCOL = """[Agent-Session-Relay active]

This workspace uses Relay's Git-based human/agent review workflow. At each
handoff, the human's staged hunks become part of the reviewed checkpoint;
all other workspace changes remain pending.

- Reviewed checkpoint: accepted so far, but editable; later edits become pending.
- Human edits and discards are guidance, not immutable code.

Do not invoke Git directly during this active session, including for inspection.
Relay owns the session baselines. Leave changes unstaged for human review.
Only the human runs Relay lifecycle commands. For inspection, use `relay agent`:

  relay agent status
      Session and review/provenance summary.
  relay agent diff reviewed
      Hunks accepted at this handoff.
  relay agent diff human
      Net workspace changes during the latest human turn.
  relay agent diff pending
      Complete unresolved delta, including new project files; updates as you edit.

Each diff accepts `--name-only` and path filters via `-- <path>...`; paths are
relative to the current directory. `reviewed` and `human` are fixed for this
turn; `pending` is live.
"""
