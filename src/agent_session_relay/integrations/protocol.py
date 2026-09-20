"""Compact agent context and turn completion notices."""

import json

CONTINUATION = (
    "An existing Relay agent turn is in progress, possibly because it was forcibly interrupted.\n"
    "No new human-to-agent handoff has occurred. Review and human-edit context still refers\n"
    "to this turn's original handoff.\n"
)

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
    multi = "repositories" in context
    result = (
        "[Agent-Session-Relay active]\n\n"
        + (
            "These Git repositories share one Relay session.\n"
            if multi
            else "This Git repository is managed by Relay.\n"
        )
        + "Relay coordinates code review across human and agent turns, using Git\n"
        "for tracking human approvals and edits.\n\n"
    )
    if multi:
        result += "Workspace: " + json.dumps(context["workspace"]) + "\n"
        result += "Repositories (name to absolute path, JSON):\n"
        result += json.dumps(context["repositories"], ensure_ascii=True) + "\n\n"
    if context["continuing"]:
        result += CONTINUATION + "\n"
    result += BTW if context["kind"] == "btw" else NORMAL
    result += "\n" + INSPECTION
    if multi:
        result += (
            "\nStatus and semantic diffs cover every member, regardless of cwd.\n"
            "Diff output paths are workspace-relative. Use --repo <name> to select a member;\n"
            "Path filters after -- remain relative to cwd. For history queries, use\n"
            "`relay agent git --repo <name> <args...>`. Lifecycle controls act on the group.\n"
        )
    path_scope = "workspace-relative" if multi else "repository-relative"
    if context["kind"] == "normal" and context["reviewed_paths"]:
        result += (
            "\nApprovals are sealed into the reviewed checkpoint this turn.\n"
            "Approved hunks are excluded from `diff pending`, but the rest of the file "
            "they are in may still contain pending changes.\n"
            f"Approved changes ({path_scope} paths, JSON):\n"
        )
        result += json.dumps(context["reviewed_paths"], ensure_ascii=True) + "\n"
    if context["kind"] == "normal" and context["human_paths"]:
        result += f"\nHuman changes ({path_scope} paths, JSON):\n"
        result += json.dumps(context["human_paths"], ensure_ascii=True) + "\n"
    return result


def stop_notice(snapshots: dict[str, str] | None) -> str:
    if not snapshots:
        return ""
    result = "Project file writes or staging changes detected during a read-only BTW turn.\n"
    for area, commit in snapshots.items():
        label = area.capitalize() if area in ("workspace", "staging") else area
        result += f"{label} changes were saved in snapshot {commit}.\n"
    return result + (
        "The original workspace and staging state from the last human-to-agent handoff "
        "were restored.\n"
    )
