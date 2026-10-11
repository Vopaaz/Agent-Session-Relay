"""Shared lifecycle dispatch; adapters select their harness's wire format."""

from __future__ import annotations

import json
import os
import sys

from ..core.errors import RelayError
from ..core.workspace import Workspace, discover
from .guard import violation
from .protocol import protocol, stop_notice

EVENTS = {
    "UserPromptSubmit": "prompt-submit",
    "Stop": "agent-stop",
    "PreToolUse": "pre-tool-use",
}
PAYLOAD_LIMIT = 4 * 1024 * 1024


def active_relay(payload: dict):
    # An external tool cwd must never deactivate the current workspace's guard.
    locations = [os.getcwd()]
    if isinstance(payload.get("cwd"), str):
        locations.append(payload["cwd"])
    relay = None
    for location in dict.fromkeys(locations):
        candidate = discover(location, required=False)
        if candidate is None:
            continue
        # A coordinator journal can precede its active pointer or outlive it during finish.
        if isinstance(candidate, Workspace):
            candidate.store.assert_ready()
        if candidate.active(candidate.store.load(), required=False):
            candidate.store.assert_ready()
            if relay is not None and relay.store.path != candidate.store.path:
                raise RelayError("Hook directories identify different active Relay sessions.")
            relay = candidate
    return relay


def run_hook(event: str, *, harness: str = "kiro") -> int:
    raw = "" if sys.stdin.isatty() else sys.stdin.read(PAYLOAD_LIMIT + 1)
    invalid = False
    try:
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict) or len(raw) > PAYLOAD_LIMIT:
            raise ValueError("invalid hook payload")
    except ValueError:
        payload, invalid = {}, True

    relay = active_relay(payload)
    if relay is None:
        return 0
    if invalid:
        raise RelayError(f"Invalid {harness} hook JSON; the active Relay session was not changed.")
    if harness != "kiro" and event != "pre-tool-use" and payload.get("agent_id"):
        # Child tools are guarded, but child lifecycle events must not close the parent turn.
        return 0
    owner = payload.get("session_id")
    if not isinstance(owner, str) or not owner:
        owner = None
    if harness != "kiro":
        # Keep legacy Kiro owners unchanged; new harnesses cannot share each other's IDs.
        owner = f"{harness}:{owner or 'unknown'}"
    if event == "prompt-submit":
        if context := relay.handoff(owner):
            message = protocol(context)
            if harness == "kiro":
                sys.stdout.write(message)
            else:
                print(json.dumps({"hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit", "additionalContext": message,
                }}))
    elif event == "agent-stop":
        message = stop_notice(relay.stop(owner))
        if message:
            if harness == "kiro":
                sys.stdout.write(message)
            else:
                # A warning informs the human without asking the agent to continue working.
                print(json.dumps({"systemMessage": message}))
    else:
        relay.store.assert_ready()
        reason = violation(payload, btw=relay.btw_active())
        if reason:
            sys.stderr.write(reason + "\n")
            return 2
    return 0
