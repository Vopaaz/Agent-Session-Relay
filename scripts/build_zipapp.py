#!/usr/bin/env python3
"""Build a standalone Relay executable using only Python's standard library."""

from __future__ import annotations

import argparse
import hashlib
import shutil
import tempfile
import zipapp
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=root / "dist" / "relay.pyz")
    args = parser.parse_args()
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="relay-build-") as directory:
        staging = Path(directory)
        shutil.copytree(
            root / "src" / "agent_session_relay",
            staging / "agent_session_relay",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        shutil.copyfile(root / "LICENSE", staging / "LICENSE")
        (staging / "__main__.py").write_text(
            "from agent_session_relay.cli import main\nraise SystemExit(main())\n", encoding="utf-8"
        )
        zipapp.create_archive(staging, output, interpreter="/usr/bin/env python3", compressed=True)
    output.chmod(0o755)
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    checksum = output.with_suffix(output.suffix + ".sha256")
    checksum.write_text(f"{digest}  {output.name}\n", encoding="utf-8")
    print(f"Built {output}\nSHA256 {digest}")


if __name__ == "__main__":
    main()
