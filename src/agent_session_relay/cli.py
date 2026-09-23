from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .core.errors import RelayError
from .core.readonly_git import COMMANDS
from .core.readonly_git import run as run_readonly_git
from .core.workspace import Workspace, discover, find_workspace
from .editor import edit_message
from .integrations.kiro.adapter import install, run_hook
from .integrations.protocol import stop_notice


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="relay", description="Agent-Session-Relay: Git staging is human review."
    )
    root.add_argument("--version", action="version", version=f"Agent-Session-Relay {__version__}")
    commands = root.add_subparsers(dest="command", required=True)
    start = commands.add_parser("start", help="start from a clean Git workspace")
    start.add_argument(
        "--repo",
        dest="repos",
        action="append",
        metavar="PATH",
        help="include a worktree below cwd; repeat to start one multi-repository session",
    )
    start.add_argument(
        "-m",
        "--message",
        action="append",
        help="describe the session; repeat for separate paragraphs (no editor at start)",
    )
    message = commands.add_parser("message", help="set or replace a session's final commit message")
    message.add_argument(
        "-m",
        "--message",
        action="append",
        help="describe the work; repeat for paragraphs; omit to open the Git editor",
    )
    message.add_argument(
        "session",
        nargs="?",
        help="session ID/prefix; defaults to the active or only suspended session",
    )
    message.add_argument("--repo", help="override the message for one workspace repository")
    status = commands.add_parser("status", help="show session and review state")
    status.add_argument("--json", action="store_true", help="output structured state")
    commands.add_parser(
        "end-interrupted-agent-turn",
        help="human-only: close an agent turn after manually interrupting the agent",
        description=(
            "Close an open agent turn after you have manually interrupted the agent. "
            "Run before editing, staging, or discarding changes. This command does not stop "
            "the agent process. It performs the same cleanup as Agent Stop and returns Relay "
            "to the human phase. Requires an active session with an open agent turn; "
            "inactive, suspended, and human-phase sessions are rejected."
        ),
    )
    btw = commands.add_parser("btw", help="make the next prompt a one-turn, read-only aside")
    btw.add_argument("--cancel", action="store_true", help="make the next prompt normal again")
    commands.add_parser("suspend", help="save the full review state and return to normal Git")
    resume = commands.add_parser("resume", help="restore a suspended session")
    resume.add_argument("session", nargs="?", help="session ID or unambiguous prefix")
    finish = commands.add_parser(
        "finish", help="create one public result commit from reviewed code"
    )
    finish.add_argument(
        "-m",
        "--message",
        action="append",
        help=(
            "override the saved session/shared message; workspace repository overrides take "
            "priority; open the Git editor only when a required message is missing"
        ),
    )
    commands.add_parser(
        "abort", help="terminate after one confirmation, preserving a recovery branch"
    )
    listing = commands.add_parser(
        "list", help="list active and suspended sessions in this worktree"
    )
    listing.add_argument("--json", action="store_true")
    commands.add_parser(
        "recover", help="recover an interrupted Relay operation, preserving current code"
    )
    agent = commands.add_parser("agent", help="agent-facing semantic inspection")
    agent_commands = agent.add_subparsers(dest="agent_command", required=True)
    agent_commands.add_parser("status", help="output agent-readable session state as JSON")
    git = agent_commands.add_parser(
        "git",
        help="run a supported read-only Git query outside this session's changes",
        description=(
            "Run supported read-only Git queries; prefer relay agent diff for current-session "
            "changes. Relay-managed refs/commits are accepted, but include internal bookkeeping. "
            "Commands: " + ", ".join(sorted(COMMANDS)) + ". "
            "Use branch/tag --list for patterns; config get/list or --get/--get-all/"
            "--get-regexp/--get-urlmatch/--list; reflog show/exists; worktree list; "
            "remote [-v] or remote get-url. Unknown options, write forms, global Git options, "
            "aliases, output files, external diff/textconv, and signature verification "
            "are unsupported. "
            "Arguments and streams retain Git syntax and exit codes; paths are relative to cwd. "
            "In a multi-repository workspace, prefix the query with --repo <name> to run it "
            "from that member's root."
        ),
    )
    git.add_argument("git_args", nargs=argparse.REMAINDER, help="Git subcommand and arguments")
    diff = agent_commands.add_parser(
        "diff",
        help="inspect session/review/provenance changes",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "session   Session base -> live workspace; net human and agent changes, "
            "approved or pending.\n"
            "reviewed  Approvals sealed at the latest turn entry; fixed during the turn.\n"
            "human     Human edits at the latest turn entry; fixed during the turn.\n"
            "pending   Reviewed checkpoint -> live workspace."
        ),
        epilog=(
            "Use -- <paths...> for literal paths relative to cwd. "
            "Empty diffs produce no output and exit 0; invalid options fail on stderr."
        ),
    )
    diff.add_argument("kind", choices=("session", "reviewed", "human", "pending"))
    diff.add_argument(
        "--repo", help="inspect only this workspace repository; paths remain cwd-relative"
    )
    output = diff.add_mutually_exclusive_group()
    output.add_argument("--name-only", action="store_true", help="only output involved file names")
    output.add_argument("--stat", action="store_true", help="show per-file change statistics")
    diff.add_argument("-z", "--null", action="store_true", help="NUL delimit --name-only output")
    kiro = commands.add_parser("kiro", help="Kiro integration")
    kiro_commands = kiro.add_subparsers(dest="kiro_command", required=True)
    installer = kiro_commands.add_parser("install", help="install standalone Kiro lifecycle hooks")
    scope = installer.add_mutually_exclusive_group(required=True)
    scope.add_argument("--global", dest="global_scope", action="store_true")
    scope.add_argument("--project", action="store_true")
    installer.add_argument(
        "--force", action="store_true", help="replace customized Relay hook configuration"
    )
    hook = kiro_commands.add_parser("hook", help="adapter entry point called by Kiro")
    hook.add_argument("event", choices=("prompt-submit", "agent-stop", "pre-tool-use"))
    return root


def describe_origin(origin: dict) -> str:
    return (
        origin["ref"].removeprefix("refs/heads/")
        if origin["ref"]
        else "detached " + origin["commit"][:12]
    )


def message_subject(message: str | None) -> str:
    return message.splitlines()[0] if message else "(not set; required before finish)"


def confirm_abort(session_id: str, branches: dict | None = None) -> bool:
    recovery = (
        "Recovery branches:\n"
        + "\n".join(f"  {name}: {branch}" for name, branch in branches.items())
        if branches is not None
        else f"Recovery branch: relay/aborted/{session_id}"
    )
    print(
        "WARNING: Aborting ends this Relay session permanently; it cannot be resumed.\n"
        "Relay will delete its intermediate checkpoints, snapshots, and review/provenance state.\n"
        "Your current code will be preserved first in a recovery branch, including staged,\n"
        "unstaged, and non-ignored untracked project files.\n"
        f"{recovery}\n"
        "The recovery commit preserves code; the staging distinction will be lost."
    )
    try:
        return input("Type 'abort' to confirm the cleanup: ").strip() == "abort"
    except EOFError:
        return False


def execute(args, paths: list[str]) -> int:
    if args.command == "kiro":
        if args.kiro_command == "hook":
            return run_hook(args.event)
        path = install(global_scope=args.global_scope, force=args.force)
        print(
            f"Kiro integration installed at {path}\n"
            "Ensure `relay` is on Kiro's PATH and open a new Kiro session.\n"
            "Without an active Relay session, these hooks are silent and inert."
        )
        if args.project:
            print("Commit the project hook file before `relay start`, or install globally instead.")
        return 0
    if args.command == "agent" and args.agent_command == "git":
        if args.git_args[:1] == ["--repo"]:
            if len(args.git_args) < 3:
                raise RelayError("Use `relay agent git --repo <name> <git-subcommand> ...`.")
            workspace = discover()
            if not isinstance(workspace, Workspace):
                raise RelayError("--repo requires a multi-repository workspace.")
            workspace.store.assert_ready()
            name = workspace.repository(args.git_args[1])
            return run_readonly_git(args.git_args[2:], cwd=str(workspace.root / name))
        return run_readonly_git(args.git_args)
    if args.command == "start" and args.repos:
        workspace = find_workspace(Path.cwd())
        if workspace is not None and workspace.root != Path.cwd().resolve():
            raise RelayError(f"Configure workspace members from {workspace.root}.")
        relay = Workspace(Path.cwd())
    else:
        relay = discover()
    messages = getattr(args, "message", None)
    message = "\n\n".join(messages) if messages is not None else None
    if isinstance(relay, Workspace):
        return execute_workspace(args, paths, relay, message)
    if getattr(args, "repo", None) is not None:
        raise RelayError("--repo requires a multi-repository workspace.")
    if args.command == "start":
        session = relay.start(message)
        print(
            f"Relay session started: {session['id']}\nBase: {session['base_commit']}\n"
            f"Message: {message_subject(session['message'])}\n"
            "Review with normal Git staging; send a prompt in Kiro to hand off to the agent."
        )
    elif args.command == "end-interrupted-agent-turn":
        sys.stdout.write(stop_notice(relay.stop(require_open=True)))
        print(
            "Interrupted agent turn ended. Relay is now in the human phase.\n"
            "Review, edit, or stage your changes before the next prompt."
        )
    elif args.command == "btw":
        mode = relay.set_btw(cancel=args.cancel)
        print(
            "Next prompt: btw (read-only, one turn). Review handoff will be deferred.\n"
            "Use `relay btw --cancel` to cancel before submitting the prompt."
            if mode == "btw"
            else "Next prompt: normal."
        )
    elif args.command == "message":
        session = relay.set_message(
            message, args.session, editor=lambda initial: edit_message(relay.git, initial)
        )
        print(f"Session message updated: {session['id']}\n{session['message']}")
    elif args.command in ("status", "agent"):
        if args.command == "agent" and args.agent_command == "diff":
            if args.null and not args.name_only:
                raise RelayError("--null requires --name-only.")
            sys.stdout.buffer.write(
                relay.diff(
                    args.kind,
                    paths,
                    name_only=args.name_only,
                    null=args.null,
                    stat=args.stat,
                )
            )
        else:
            status = relay.agent_status() if args.command == "agent" else relay.status()
            if args.command == "agent" or args.json:
                print(json.dumps(status, indent=2))
            elif not status["active"]:
                print(f"Relay is {status['lifecycle']}.")
                if status["suspended_sessions"]:
                    for session in relay.list_sessions():
                        print(f"Suspended: {session['id']}  {message_subject(session['message'])}")
                    print("Use `relay resume` to continue.")
            else:
                turn_label = (
                    "Current agent turn" if status["phase"] == "agent" else "Last agent turn"
                )
                print(
                    f"Session: {status['session']} "
                    f"({status['lifecycle']}, {status['phase']} turn)\n"
                    f"{turn_label}: {status['turn_kind'] or 'none'}; "
                    f"next prompt: {status['next_turn']}\n"
                    f"Message: {message_subject(status['message'])}\n"
                    f"Base: {status['base_commit']}\nReviewed: {status['reviewed_checkpoint']}\n"
                    f"Staged approvals: {'yes' if status['staged_approvals'] else 'none'}\n"
                    "Unstaged / untracked proposals: "
                    f"{'yes' if status['unstaged_changes'] else 'none'}\n"
                    "Turn provenance: "
                    f"{'available' if status['provenance']['available'] else 'none yet'}"
                )
    elif args.command == "list":
        sessions = relay.list_sessions()
        if args.json:
            print(json.dumps(sessions, indent=2))
        elif not sessions:
            print("No Relay sessions in this worktree.")
        else:
            for session in sorted(sessions, key=lambda s: s["id"]):
                print(
                    f"{session['id']}  {session['state']}  base={session['base_commit'][:12]}  "
                    f"turns={session['turn']}  {message_subject(session['message'])}"
                )
    elif args.command == "suspend":
        session, origin = relay.suspend()
        print(
            f"Relay session suspended: {session['id']}\nReturned to {describe_origin(origin)}.\n"
            "Review state is saved. Hooks are now inert. Use `relay resume` to continue."
        )
    elif args.command == "resume":
        session = relay.resume(args.session)
        print(f"Relay session resumed: {session['id']}\nStaged and unstaged review state restored.")
    elif args.command == "finish":
        branch = relay.finish(message, editor=lambda initial: edit_message(relay.git, initial))
        print(
            f"Relay session finished. Switched to {branch}.\n"
            "One result commit; its only parent is the immutable session base.\n"
            "Use normal Git to rebase, merge, or cherry-pick the result when ready."
        )
    elif args.command == "abort":
        relay.store.assert_ready()
        session = relay.active(relay.store.load())
        if not confirm_abort(session["id"]):
            print("Abort cancelled. Relay session and code were not changed.")
            return 1
        branch, origin = relay.abort(session["id"])
        print(
            "Relay session aborted.\n\nYour workspace at the time of abort was preserved at:\n\n"
            f"  {branch}\n\nRelay's intermediate review/provenance state has been removed.\n"
            f"Returned to {describe_origin(origin)}.\n\n"
            "If you are certain you no longer need the recovery snapshot, delete it\n"
            f"later with normal Git, for example:\n\n  git branch -D {branch}"
        )
    elif args.command == "recover":
        with relay.store.lock():
            branch = relay.store.recover()
        print(
            f"Previous Relay state restored. Code from before recovery was preserved at {branch}."
        )
    return 0


def execute_workspace(args, paths: list[str], relay: Workspace, message: str | None) -> int:
    command = args.command

    def editor(initial):
        return edit_message(relay.git, initial)

    if command == "start":
        session = relay.start(message, repos=args.repos)
        print(f"Relay workspace session started: {session['id']}\nWorkspace: {relay.root}")
        print("Repositories: " + ", ".join(session["members"]))
        print(f"Message: {message_subject(session['message'])}")
        print("Review in each repository's Git UI; all repositories finish together.")
    elif command == "message":
        session = relay.set_message(message, args.session, editor=editor, repo=args.repo)
        print(f"Session message updated: {session['id']}\n{session['message']}")
    elif command == "btw":
        mode = relay.set_btw(cancel=args.cancel)
        print(f"Next prompt for all repositories: {mode}.")
    elif command == "end-interrupted-agent-turn":
        sys.stdout.write(stop_notice(relay.stop(require_open=True)))
        print("Interrupted agent turn ended for all repositories. Relay is now in the human phase.")
    elif command == "agent" and args.agent_command == "diff":
        if args.null and not args.name_only:
            raise RelayError("--null requires --name-only.")
        sys.stdout.buffer.write(
            relay.diff(
                args.kind,
                paths,
                name_only=args.name_only,
                null=args.null,
                stat=args.stat,
                repo=args.repo,
            )
        )
    elif command in ("status", "agent"):
        status = relay.agent_status() if command == "agent" else relay.status()
        if command == "agent" or args.json:
            print(json.dumps(status, indent=2))
        elif not status["active"]:
            print(f"Relay workspace is {status['lifecycle']}.")
            for session in relay.list_sessions():
                print(f"Suspended: {session['id']}  {message_subject(session['message'])}")
        else:
            print(
                f"Session: {status['session']} ({status['phase']} phase)\nWorkspace: {relay.root}"
            )
            print(
                f"Turn: {status['turn']} ({status['turn_kind'] or 'none'}); "
                f"next prompt: {status['next_turn']}"
            )
            print(f"Message: {message_subject(status['message'])}")
            for name, item in status["repositories"].items():
                print(
                    f"  {name}: staged approvals={'yes' if item['staged_approvals'] else 'none'}; "
                    f"unstaged/untracked proposals={'yes' if item['unstaged_changes'] else 'none'}"
                )
    elif command == "list":
        sessions = relay.list_sessions()
        if args.json:
            print(json.dumps(sessions, indent=2))
        elif not sessions:
            print("No Relay sessions in this workspace.")
        else:
            for session in sessions:
                print(
                    f"{session['id']}  {session['state']}  turns={session['turn']}  "
                    f"{message_subject(session['message'])}"
                )
    elif command == "suspend":
        session, origins = relay.suspend()
        print(f"Relay workspace session suspended: {session['id']}")
        for name, origin in origins.items():
            print(f"  {name}: returned to {describe_origin(origin)}")
        print("Review state is saved. Hooks are inert. Use `relay resume` to continue.")
    elif command == "resume":
        session = relay.resume(args.session)
        print(f"Relay workspace session resumed: {session['id']}\nAll review states restored.")
    elif command == "finish":
        results = relay.finish(message, editor=editor)
        print("Relay workspace session finished.")
        for name, result in results.items():
            if result["branch"]:
                print(f"  {name}: {result['branch']} ({result['commit']})")
            else:
                print(f"  {name}: no net changes; returned to {describe_origin(result['origin'])}")
        print("Each changed repository has one result commit, parented by its own session base.")
    elif command == "abort":
        relay.store.assert_ready()
        session = relay.active(relay.store.load())
        branches = {name: "relay/aborted/" + sid for name, sid in session["members"].items()}
        if not confirm_abort(session["id"], branches):
            print("Abort cancelled. Relay session and code were not changed.")
            return 1
        branches, origins = relay.abort(session["id"])
        print("Relay workspace session aborted. Current code was preserved at:")
        for name, branch in branches.items():
            print(f"  {name}: {branch}; returned to {describe_origin(origins[name])}")
    elif command == "recover":
        result = relay.recover()
        print(
            "Workspace operation committed; cleanup completed."
            if result["decision"] == "commit"
            else "Previous workspace review state restored."
        )
        for name, branch in result["branches"].items():
            print(f"  {name}: code from before recovery preserved at {branch}")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    paths = []
    git_args = None
    if argv[:2] == ["agent", "git"] and argv[2:] not in (["--help"], ["-h"]):
        # Relay must not consume Git options, reorder them, or reinterpret its --.
        git_args, argv = argv[2:], argv[:2]
    # Everything after -- is a literal path, even a name such as --name-only.
    if "--" in argv and argv[:2] == ["agent", "diff"]:
        boundary = argv.index("--")
        paths, argv = argv[boundary + 1 :], argv[:boundary]
    args = parser().parse_args(argv)
    if git_args is not None:
        args.git_args = git_args
    try:
        return execute(args, paths)
    except BrokenPipeError:
        return 0
    except (RelayError, OSError) as exc:
        print(f"relay: {exc}", file=sys.stderr)
        return 2 if args.command == "kiro" and args.kiro_command == "hook" else 1
    except KeyboardInterrupt:
        print(
            "\nrelay: interrupted; any incomplete transition can be restored with `relay recover`.",
            file=sys.stderr,
        )
        return 130
