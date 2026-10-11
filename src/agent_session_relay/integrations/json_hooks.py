"""Install command hooks into shared JSON configuration without replacing other settings."""

from __future__ import annotations

import copy
import json
import shlex
from pathlib import Path

from ..core.errors import RelayError
from ..core.storage import atomic_json
from .hooks import EVENTS


def hook_config(harness: str) -> dict:
    return {"hooks": {
        event: [{"hooks": [{
            "type": "command", "command": f"relay {harness} hook {command}", "timeout": 60,
        }]}]
        for event, command in EVENTS.items()
    }}


def owned(handler: dict, harness: str) -> bool:
    command = handler.get("command")
    if not isinstance(command, str):
        return False
    try:
        words = shlex.split(command)
    except ValueError:
        words = command.split()
    return words[:3] == ["relay", harness, "hook"]


def merge_config(current: dict, harness: str, *, force: bool) -> dict:
    result = copy.deepcopy(current)
    hooks = result.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise RelayError("Existing hooks configuration must be a JSON object; nothing was changed.")
    desired = hook_config(harness)["hooks"]
    found = {}
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            raise RelayError(f"Invalid hook groups for {event}; nothing was changed.")
        remaining = []
        for group in groups:
            if (
                not isinstance(group, dict) or not isinstance(group.get("hooks"), list)
                or not all(isinstance(handler, dict) for handler in group["hooks"])
            ):
                raise RelayError(f"Invalid hook handlers for {event}; nothing was changed.")
            ours = [handler for handler in group["hooks"] if owned(handler, harness)]
            others = [handler for handler in group["hooks"] if not owned(handler, harness)]
            if ours:
                definition = {**group, "hooks": ours}
                found.setdefault(event, []).append(definition)
            if others or not ours:
                remaining.append({**group, "hooks": others})
        hooks[event] = remaining
    if not force and any(groups != desired.get(event) for event, groups in found.items()):
        raise RelayError(
            "Existing Relay hooks differ. Keep a copy of your customizations, then use --force "
            "to replace only Relay's entries. Other hooks and settings will be preserved."
        )
    if found == desired:
        return current
    for event, groups in desired.items():
        hooks.setdefault(event, []).extend(groups)
    return result


def install_json(directory: Path, filename: str, harness: str, *, force: bool) -> Path:
    path = directory / filename
    for component in (directory, path):
        if component.is_symlink():
            raise RelayError(f"Refusing to install through a symlink: {component}")
    current = {}
    if path.exists():
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(current, dict):
                raise ValueError("expected an object")
        except ValueError as exc:
            raise RelayError(f"Invalid JSON configuration at {path}; nothing was changed.") from exc
    merged = merge_config(current, harness, force=force)
    if merged != current:
        atomic_json(path, merged)
    return path
