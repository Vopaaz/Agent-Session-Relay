from __future__ import annotations

import os
from pathlib import Path

from ...core.git import Git
from ..hooks import run_hook as dispatch
from ..json_hooks import hook_config as command_hooks
from ..json_hooks import install_json


def hook_config() -> dict:
    return command_hooks("codex")


def install(*, global_scope: bool, force: bool = False) -> Path:
    directory = (
        Path(os.environ.get("CODEX_HOME") or str(Path.home() / ".codex")).expanduser()
        if global_scope else Git().root / ".codex"
    )
    return install_json(directory, "hooks.json", "codex", force=force)


def run_hook(event: str) -> int:
    return dispatch(event, harness="codex")
