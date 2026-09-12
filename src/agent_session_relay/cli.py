from __future__ import annotations

import argparse
import json
import sys

from . import __version__
from .core.errors import RelayError
from .core.git import Git
from .core.session import Relay
from .editor import edit_message
from .integrations.kiro.adapter import install, run_hook


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="relay", description="Agent-Session-Relay: Git staging is human review."
    )
    root.add_argument("--version", action="version", version=f"Agent-Session-Relay {__version__}")
    commands = root.add_subparsers(dest="command", required=True)
    start = commands.add_parser("start", help="start from a clean Git workspace")
    start.add_argument(
        "-m", "--message", action="append",
        help="describe the session; repeat for separate paragraphs (no editor at start)",
    )
    message = commands.add_parser("message", help="set or replace a session's final commit message")
    message.add_argument(
        "-m", "--message", action="append",
        help="describe the work; repeat for paragraphs; omit to open the Git editor",
    )
    message.add_argument(
        "session",
        nargs="?",
        help="session ID/prefix; defaults to the active or only suspended session",
    )
    status = commands.add_parser("status", help="show session and review state")
    status.add_argument("--json", action="store_true", help="output structured state")
    btw = commands.add_parser("btw", help="make the next prompt a one-turn, read-only aside")
    btw.add_argument("--cancel", action="store_true", help="make the next prompt normal again")
    commands.add_parser("suspend", help="save the full review state and return to normal Git")
    resume = commands.add_parser("resume", help="restore a suspended session")
    resume.add_argument("session", nargs="?", help="session ID or unambiguous prefix")
    finish = commands.add_parser(
        "finish", help="create one public result commit from reviewed code"
    )
    finish.add_argument(
        "-m", "--message", action="append",
        help="override the saved message; otherwise use it or open the Git editor",
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
    diff = agent_commands.add_parser("diff", help="inspect review/provenance patches")
    diff.add_argument("kind", choices=("reviewed", "human", "pending", "btw"))
    diff.add_argument("--turn", type=int, help="saved btw turn number (for diff btw)")
    diff.add_argument("--staged", action="store_true", help="inspect saved btw index changes")
    diff.add_argument("--name-only", action="store_true", help="only output involved file names")
    diff.add_argument("-z", "--null", action="store_true", help="NUL delimit --name-only output")
    restore = agent_commands.add_parser(
        "restore-btw",
        help="apply saved btw changes during a normal agent turn, leaving them unstaged",
    )
    restore.add_argument("turn", type=int, help="saved btw turn number")
    restore.add_argument(
        "--staged", action="store_true", help="retrieve the saved index delta instead"
    )
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


def confirm_abort(session_id: str) -> bool:
    print(
        "WARNING: Aborting ends this Relay session permanently; it cannot be resumed.\n"
        "Relay will delete its intermediate checkpoints, snapshots, and review/provenance state.\n"
        "Your current code will be preserved first in a recovery branch, including staged,\n"
        "unstaged, and non-ignored untracked project files.\n"
        f"Recovery branch: relay/aborted/{session_id}\n"
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
    relay = Relay(Git())
    messages = getattr(args, "message", None)
    message = "\n\n".join(messages) if messages is not None else None
    if args.command == "start":
        session = relay.start(message)
        print(
            f"Relay session started: {session['id']}\nBase: {session['base_commit']}\n"
            f"Message: {message_subject(session['message'])}\n"
            "Review with normal Git staging; send a prompt in Kiro to hand off to the agent."
        )
    elif args.command == "btw":
        mode = relay.set_btw(cancel=args.cancel)
        print(
            "Next prompt: btw (read-only, one turn). Review handoff will be deferred.\n"
            "Use `relay btw --cancel` to cancel before submitting the prompt."
            if mode == "btw" else "Next prompt: normal."
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
                    args.kind, paths, name_only=args.name_only, null=args.null,
                    number=args.turn, staged=args.staged,
                )
            )
        elif args.command == "agent" and args.agent_command == "restore-btw":
            relay.restore_btw(args.turn, staged=args.staged)
            print(f"Saved btw turn {args.turn} changes applied; they remain unstaged for review.")
        else:
            status = relay.status()
            if args.command == "agent" or args.json:
                print(json.dumps(status, indent=2))
            elif not status["active"]:
                print(f"Relay is {status['lifecycle']}.")
                if status["suspended_sessions"]:
                    for session in relay.list_sessions():
                        print(
                            f"Suspended: {session['id']}  "
                            f"{message_subject(session['message'])}"
                        )
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
                for recovery in status["btw_recoveries"]:
                    number = recovery["turn"]
                    print(
                        f"Saved btw turn {number}: `relay agent diff btw --turn {number}` "
                        "(--staged for index changes); "
                        f"retrieve in a normal agent turn: `relay agent restore-btw {number}`"
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


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    paths = []
    # Everything after -- is a literal path, even a name such as --name-only.
    if "--" in argv and argv[:2] == ["agent", "diff"]:
        boundary = argv.index("--")
        paths, argv = argv[boundary + 1 :], argv[:boundary]
    args = parser().parse_args(argv)
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
