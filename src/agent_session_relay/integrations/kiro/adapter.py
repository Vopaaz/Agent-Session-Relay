from __future__ import annotations

import json
from pathlib import Path

from ...core.errors import RelayError
from ...core.git import Git
from ...core.storage import atomic_json
from ..hooks import run_hook  # noqa: F401


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
