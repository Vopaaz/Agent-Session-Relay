# Contributing to Agent-Session-Relay

Please open an issue for a substantial behavior change or a new harness adapter. Small fixes can go
directly to a pull request. Use English or Chinese; include a disposable-repository reproduction for
Git state bugs. Follow the [Code of Conduct](CODE_OF_CONDUCT.md).

## Development

Python 3.10+, Git 2.37+, and a POSIX platform are required. There are no runtime dependencies.

```bash
./relay --help
python3 -m unittest discover -s tests -v
python3 scripts/build_zipapp.py
dist/relay.pyz --version
```

For an editable install and linting, use a virtual environment:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e . ruff
ruff check .
```

Tests create disposable repositories in the system temporary directory. They do not install hooks
in your actual global Kiro configuration or modify your development repository's review state.
The suite covers partial-hunk staging, provenance, binary files, symlinks, intent-to-add, linked
worktrees, Git GC, lifecycle failures, interrupted-operation recovery, and hook contracts.

## Design constraints

- Keep Git/session/provenance logic in `src/agent_session_relay/core/`.
- Put harness-specific configuration, event decoding, and guards in `integrations/<harness>/`.
- Preserve the original session base. A result or abort recovery commit has exactly that one parent.
- Never promote unstaged edits to approval. A human edit is a proposal too.
- Never mutate the real index during inspection. Keep new/untracked code in snapshots.
- Protect snapshot and staged objects from Git GC, including intent-to-add's empty blob.
- An inactive or suspended hook must produce no output and no filesystem or Git changes.
- Keep mutations recoverable; do not delete a recovery branch during successful abort cleanup.
- Add behavior tests for consequential Git changes, especially failure paths. Keep both READMEs in sync.

See [architecture](docs/architecture.md), [Kiro integration](docs/kiro.md), and the original
[implementation requirements](docs/requirements.md). Do not test destructive workflows in a real
project. The tool guard is a workflow aid, not a security boundary against arbitrary executable code.

## Releases

Update the version in `pyproject.toml` and `src/agent_session_relay/__init__.py`, run tests and lint,
and build the archive. A `v*` tag triggers the workflow that creates a **draft** GitHub release
containing `relay.pyz` and its SHA-256 checksum. A maintainer reviews and publishes the draft.
Python packaging is supported from the checkout; this repository does not assume a PyPI publication.
