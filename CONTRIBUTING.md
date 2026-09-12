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

The package version has one source of truth: `src/agent_session_relay/__init__.py`. Update it and
write `docs/releases/v<version>.md` with the changes and any upgrade steps. Then prepare and
validate both PyPI distributions without uploading anything:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade build twine ruff
python -m unittest discover -s tests -v
ruff check .
python scripts/build_pypi.py
python scripts/build_zipapp.py
```

Inspect the files under `dist/pypi/` and test the wheel if desired. To publish them to the real PyPI,
run the following explicit command; Twine will request your PyPI token if it is not configured:

```bash
python -m twine upload dist/pypi/*
```

For a rehearsal, use `python -m twine upload --repository testpypi dist/pypi/*` instead. A
`v<version>` tag triggers a workflow that checks the tag against the package version and creates a
**draft** GitHub release containing the validated wheel, source distribution, zipapp, and checksum.
The draft uses `docs/releases/v<version>.md` as its release notes. It never uploads to PyPI; a
maintainer must run the Twine command separately and publish the draft.
