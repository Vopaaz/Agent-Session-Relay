from __future__ import annotations

import os
from pathlib import Path

from ...core.git import Git
from ..hooks import run_hook as dispatch
from ..json_hooks import hook_config as command_hooks
from ..json_hooks import install_json


def hook_config() -> dict:
    return command_hooks("claude-code")


def install(*, global_scope: bool, force: bool = False) -> Path:
    directory = (
        Path(os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")).expanduser()
        if global_scope else Git().root / ".claude"
    )
    return install_json(directory, "settings.json", "claude-code", force=force)


def run_hook(event: str) -> int:
    return dispatch(event, harness="claude-code")
