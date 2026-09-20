from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from ...core.errors import RelayError
from ...core.git import Git
from ...core.storage import atomic_json
from ...core.workspace import Workspace, discover
from ..protocol import protocol, stop_notice
from .guard import violation


def hook_config() -> dict:
    # Standalone v1 hooks are shared by Kiro IDE 1.x and CLI 3.x.
    hooks = []
    for event, command in (
        ("UserPromptSubmit", "prompt-submit"),
        ("Stop", "agent-stop"),
        ("PreToolUse", "pre-tool-use"),
    ):
        hooks.append(
            {
                "name": "agent-session-relay-" + command,
                "trigger": event,
                "action": {"type": "command", "command": "relay kiro hook " + command},
                "timeout": 60,
                "enabled": True,
            }
        )
    # No matcher: inspect all tool metadata, including Git MCP tools and shell tool aliases.
    return {"version": "v1", "hooks": hooks}


def install(*, global_scope: bool, force: bool = False) -> Path:
    root = Path.home() if global_scope else Git().root
    directory = root / ".kiro" / "hooks"
    for component in (root / ".kiro", directory):
        if component.is_symlink():
            raise RelayError(f"Refusing to install through a symlink: {component}")
    path = directory / "agent-session-relay.json"
    desired = hook_config()
    if path.is_symlink():
        raise RelayError(f"Refusing to replace a symlink: {path}")
    if path.exists():
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            current = None
        if current == desired:
            return path
        if not force:
            raise RelayError(
                f"Existing Relay hook configuration differs: {path}. "
                "Keep a copy of your customizations, then use --force to replace it."
            )
    atomic_json(path, desired)
    return path


def run_hook(event: str) -> int:
    raw = "" if sys.stdin.isatty() else sys.stdin.read(4 * 1024 * 1024 + 1)
    invalid = False
    try:
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict) or len(raw) > 4 * 1024 * 1024:
            raise ValueError("invalid hook payload")
    except ValueError:
        payload, invalid = {}, True

    # Kiro executes in the workspace root; cwd in the event also supports launchers that don't.
    # An external tool working directory must never deactivate an active workspace's guard.
    locations = [os.getcwd()]
    if isinstance(payload.get("cwd"), str):
        locations.append(payload["cwd"])
    relay = None
    for location in dict.fromkeys(locations):
        candidate = discover(location, required=False)
        if candidate is None:
            continue
        # A workspace journal can precede its active pointer during a failed start,
        # or outlive it during finish. It must never silently disable the hook.
        if isinstance(candidate, Workspace):
            candidate.store.assert_ready()
        if candidate.active(candidate.store.load(), required=False):
            candidate.store.assert_ready()
            if relay is not None and relay.store.path != candidate.store.path:
                raise RelayError("Hook directories identify different active Relay sessions.")
            relay = candidate
    if relay is None:
        return 0
    if invalid:
        raise RelayError("Invalid Kiro hook JSON; the active Relay session was not changed.")
    owner = payload.get("session_id")
    if not isinstance(owner, str):
        owner = None
    if event == "prompt-submit":
        if context := relay.handoff(owner):
            sys.stdout.write(protocol(context))
    elif event == "agent-stop":
        sys.stdout.write(stop_notice(relay.stop(owner)))
    else:
        relay.store.assert_ready()
        reason = violation(payload, btw=relay.btw_active())
        if reason:
            sys.stderr.write(reason + "\n")
            return 2
    return 0
