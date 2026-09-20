"""Per-worktree metadata, process locking, and recoverable workspace transitions."""

from __future__ import annotations

import copy
import json
import os
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path

from .errors import RelayError
from .git import Git


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + "-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def remove_journal(path: Path) -> None:
    """Persist the commit point before releasing the objects needed for rollback."""
    path.unlink(missing_ok=True)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


class MetadataStore:
    schema = 2

    def __init__(self, directory: Path):
        self.directory = directory
        self.path = self.directory / "state.json"
        self.journal_path = self.directory / "transaction.json"
        self._locked = False
        self.coordinated_journal: dict | None = None

    def load(self) -> dict:
        if not self.path.exists():
            return {"schema": self.schema, "active": None, "sessions": {}}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if (
                value["schema"] != self.schema
                or not isinstance(value["sessions"], dict)
                or (value["active"] is not None and value["active"] not in value["sessions"])
            ):
                raise ValueError("invalid or unsupported schema")
            for sid, session in value["sessions"].items():
                if session["id"] != sid or session["state"] not in ("active", "suspended"):
                    raise ValueError("invalid session")
            return value
        except (ValueError, KeyError, TypeError) as exc:
            raise RelayError(
                f"Relay metadata is invalid at {self.path}; preserve it for recovery."
            ) from exc

    def save(self, value: dict) -> None:
        atomic_json(self.path, value)

    def assert_ready(self) -> None:
        if self.journal_path.exists() and self.coordinated_journal is None:
            raise RelayError(
                "An interrupted Relay operation needs recovery. Run `relay recover`; "
                "it preserves the current code before restoring the previous session state."
            )

    @contextmanager
    def lock(self):
        # A workspace coordinator holds every member lock for the whole operation.
        # Calls into that same Store instance may safely enter the lock again.
        if self._locked:
            yield
            return
        try:
            import fcntl
        except ImportError as exc:
            raise RelayError(
                "This release requires a POSIX platform (Linux, macOS, or WSL)."
            ) from exc
        self.directory.mkdir(parents=True, exist_ok=True)
        with (self.directory / "lock").open("a+b") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RelayError(
                    "Another Relay command is running in this worktree; retry shortly."
                ) from exc
            try:
                self._locked = True
                yield
            finally:
                self._locked = False
                fcntl.flock(stream, fcntl.LOCK_UN)


class Store(MetadataStore):
    def __init__(self, git: Git):
        super().__init__(git.git_dir / "agent-session-relay")
        self.git = git

    @contextmanager
    def transaction(self, state: dict, session_id: str, action: str, workspace: str):
        """Write ahead before a HEAD/index/worktree mutation; roll back ordinary failures.

        After process termination, explicit recovery preserves any subsequently edited code.
        Git objects are pinned before the journal is written, including the original index.
        """
        if self.coordinated_journal is not None:
            if self.coordinated_journal["namespace"] != f"refs/relay/sessions/{session_id}/":
                raise RelayError("Workspace transaction refers to a different member session.")
            yield self.coordinated_journal
            return
        journal = self.prepare(state, session_id, action, workspace)
        try:
            yield journal
        except BaseException as original:
            try:
                self.rollback(journal)
            except BaseException as rollback_error:
                raise RelayError(
                    f"{action} failed and rollback could not complete: {rollback_error}. "
                    "Snapshots are retained. Run `relay recover`."
                ) from original
            preserved = [ref for ref in journal.get("preserved_refs", []) if self.git.refs(ref)]
            if preserved and isinstance(original, Exception):
                branches = ", ".join(ref.removeprefix("refs/heads/") for ref in preserved)
                raise RelayError(
                    f"{action} failed: {original}. Previous session state restored. "
                    f"Recovery code remains at {branches}."
                ) from original
            raise
        else:
            self.complete(journal)

    def prepare(
        self, state: dict, session_id: str, action: str, workspace: str,
        *, coordinator: Path | None = None,
    ) -> dict:
        """Pin a before-image, then write its journal before any visible mutation."""
        self.assert_ready()
        git = self.git
        txid = uuid.uuid4().hex
        prefix = f"refs/relay/transactions/{txid}/"
        index_blob = git.text("hash-object", "-w", "--stdin", data=git.normalized_index())
        snapshot = git.commit(workspace, git.resolve("HEAD"), "Relay transaction recovery")
        git.pin(prefix + "workspace", snapshot)
        git.pin(prefix + "index", index_blob)
        # Intent-to-add entries reference an empty blob that write-tree deliberately omits.
        git.pin(prefix + "intent-to-add", git.text("hash-object", "-w", "--stdin", data=b""))
        staged = git.commit(
            git.index_tree(), git.resolve("HEAD"), "Relay transaction staged recovery"
        )
        git.pin(prefix + "staged", staged)
        namespace = f"refs/relay/sessions/{session_id}/"
        refs = git.refs(namespace)
        # Keep *all* prior snapshots alive if finish/abort removes the session refs.
        # A journal's JSON object IDs alone do not protect those objects from Git GC.
        for ref, oid in refs.items():
            git.pin(prefix + "saved/" + ref.removeprefix(namespace), oid)
        journal = {
            "state": copy.deepcopy(state),
            "head": git.head(),
            "workspace": snapshot,
            "index": index_blob,
            "prefix": prefix,
            "namespace": namespace,
            "refs": refs,
            "created_refs": {},
            "action": action,
        }
        if coordinator is not None:
            journal["coordinator"] = str(coordinator)
        atomic_json(self.journal_path, journal)
        return journal

    def complete(self, journal: dict) -> None:
        remove_journal(self.journal_path)
        self.git.delete_refs(self.git.refs(journal["prefix"]))

    def record_created_ref(
        self, journal: dict, ref: str, oid: str, *, preserve: bool = False
    ) -> None:
        journal["created_refs"][ref] = oid
        if preserve:
            journal.setdefault("preserved_refs", []).append(ref)
        atomic_json(self.journal_path, journal)

    def rollback(self, journal: dict, *, cleanup: bool = True) -> None:
        git = self.git
        current = git.workspace_tree(previous=journal["workspace"])
        target = git.tree(journal["workspace"])
        if current != target:
            git.materialize(current, target)
        head = journal["head"]
        if (
            head["ref"]
            and git.text("rev-parse", "--verify", head["ref"], check=False) != head["commit"]
        ):
            head = {"ref": None, "commit": head["commit"]}
        git.set_head(head)
        git.restore_index(git.run("cat-file", "blob", journal["index"]).stdout)
        current_refs = git.refs(journal["namespace"])
        for ref, oid in journal["refs"].items():
            git.pin(ref, oid)
        git.delete_refs(
            {ref: oid for ref, oid in current_refs.items() if ref not in journal["refs"]}
        )
        for ref, oid in journal["created_refs"].items():
            if ref in journal.get("preserved_refs", []):
                continue
            if git.text("rev-parse", "--verify", ref, check=False) == oid:
                git.delete_refs({ref: oid})
        self.save(journal["state"])
        if cleanup:
            self.complete(journal)

    def recover(self) -> str:
        if not self.journal_path.exists():
            raise RelayError("No interrupted Relay operation needs recovery.")
        try:
            journal = json.loads(self.journal_path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise RelayError(f"Recovery journal is invalid: {self.journal_path}") from exc
        if journal.get("coordinator"):
            raise RelayError("This operation belongs to a workspace. Run `relay recover` there.")
        self.git.assert_stable()
        # Explicit recovery may run hours later. Preserve new human edits before rolling back.
        workspace = self.git.workspace_tree(previous=journal["workspace"])
        branch = "relay/recovered/" + uuid.uuid4().hex[:12]
        parent = journal["head"]["commit"]
        commit = self.git.commit(workspace, parent, "Relay interrupted-operation recovery")
        self.git.pin("refs/heads/" + branch, commit, create=True)
        self.rollback(journal)
        return branch
