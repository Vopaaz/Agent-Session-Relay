#!/usr/bin/env python3
"""Build and validate clean PyPI artifacts without uploading them."""

from __future__ import annotations

import argparse
import importlib.util
import runpy
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "dist" / "pypi"


def package_version() -> str:
    namespace = runpy.run_path(str(ROOT / "src" / "agent_session_relay" / "__init__.py"))
    return str(namespace["__version__"])


def require_release_tools() -> None:
    missing = [name for name in ("build", "twine") if importlib.util.find_spec(name) is None]
    if not missing:
        return
    raise SystemExit(
        f"Missing release tools for {sys.executable}: {', '.join(missing)}\n"
        "Create and activate a virtual environment, then install them:\n\n"
        "  python3 -m venv .venv\n"
        "  . .venv/bin/activate\n"
        "  python -m pip install --upgrade build twine\n"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", help="require an exact v<package-version> release tag")
    args = parser.parse_args()
    version = package_version()
    if args.tag and args.tag != f"v{version}":
        parser.error(f"tag {args.tag!r} does not match package version v{version}")

    require_release_tools()
    if OUTPUT.is_symlink():
        raise SystemExit(f"Refusing to replace symlink: {OUTPUT}")
    if OUTPUT.exists():
        shutil.rmtree(OUTPUT)
    OUTPUT.mkdir(parents=True)

    subprocess.run(
        [sys.executable, "-m", "build", "--outdir", str(OUTPUT), str(ROOT)], check=True
    )
    expected = {
        OUTPUT / f"agent_session_relay-{version}-py3-none-any.whl",
        OUTPUT / f"agent_session_relay-{version}.tar.gz",
    }
    actual = {path for path in OUTPUT.iterdir() if path.is_file()}
    if actual != expected:
        names = ", ".join(sorted(path.name for path in actual)) or "none"
        raise SystemExit(f"Unexpected PyPI artifacts: {names}")
    subprocess.run(
        [
            sys.executable,
            "-m",
            "twine",
            "check",
            "--strict",
            *(str(path) for path in sorted(expected)),
        ],
        check=True,
    )
    print("Artifacts are ready under dist/pypi; nothing was uploaded.")


if __name__ == "__main__":
    main()
