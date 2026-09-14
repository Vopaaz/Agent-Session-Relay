"""Compact, self-contained context for each kind of Relay turn."""

import json

NORMAL = """The reviewed checkpoint contains previously accepted changes.
Changes relative to that checkpoint remain pending. Reviewed code and human edits are soft
guidance; you may revise them. Leave your changes unstaged for human review.
"""

BTW = """This is a read-only BTW turn. Review handoff is deferred.
Answer without modifying project files or Git state, including through shell commands.
If edits are requested, explain that this turn is read-only and ask for a new prompt.
Do not switch modes yourself.
"""

INSPECTION = """Do not run Git directly. Leave session and turn-mode control to the human. Use:
  relay agent status          # mode, review state
  relay agent diff session    # session base to live workspace, approved or pending
  relay agent diff reviewed   # approvals sealed this turn
  relay agent diff human      # human edits, fixed at turn start
  relay agent diff pending    # reviewed checkpoint to live workspace
Diffs support --name-only, --stat, and -- <paths...>; paths are relative to cwd.

Use `relay agent git <args...>`, if necessary, for read-only inspection outside this session.
Prefer `relay agent diff` for current-session changes.
Avoid interpreting Relay-managed refs, branches, or commits as project history;
they include internal bookkeeping.
"""


def protocol(context: dict) -> str:
    result = (
        "[Agent-Session-Relay active]\n\n"
        "This Git repository is managed by Relay.\n"
        "Relay coordinates code review across human and agent turns, using Git\n"
        "for tracking human approvals and edits.\n\n"
    )
    result += BTW if context["kind"] == "btw" else NORMAL
    result += "\n" + INSPECTION
    if context["kind"] == "normal" and context["reviewed_paths"]:
        result += (
            "\nApprovals are sealed into the reviewed checkpoint this turn.\n"
            "Approved hunks are excluded from `diff pending`, but the rest of the file "
            "they are in may still contain pending changes.\n"
            "Approved changes (repository-relative paths, JSON):\n"
        )
        result += json.dumps(context["reviewed_paths"], ensure_ascii=True) + "\n"
    if context["kind"] == "normal" and context["human_paths"]:
        result += "\nHuman changes (repository-relative paths, JSON):\n"
        result += json.dumps(context["human_paths"], ensure_ascii=True) + "\n"
    return result
