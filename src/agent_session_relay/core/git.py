"""Git plumbing. Never invokes a shell or edits a user's branch in place."""

from __future__ import annotations

import os
import stat
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path

from .errors import RelayError


class Git:
    def __init__(self, cwd: Path | str = "."):
        self.cwd = Path(cwd).absolute()
        self.root = self.cwd
        try:
            self.root = Path(self.text("rev-parse", "--show-toplevel"))
            self.git_dir = Path(self.text("rev-parse", "--absolute-git-dir"))
        except RelayError as exc:
            raise RelayError("Run Relay inside a non-bare Git working tree.") from exc

    def run(
        self, *args: str, data: bytes | None = None, env: dict | None = None, check: bool = True
    ) -> subprocess.CompletedProcess:
        process_env = os.environ.copy()
        # A hook's inherited routing must not redirect operations into another index/repository.
        for key in (
            "GIT_DIR",
            "GIT_WORK_TREE",
            "GIT_INDEX_FILE",
            "GIT_COMMON_DIR",
            "GIT_NAMESPACE",
            "GIT_PREFIX",
            "GIT_EXTERNAL_DIFF",
            "GIT_DIFF_OPTS",
            "GIT_LITERAL_PATHSPECS",
            "GIT_GLOB_PATHSPECS",
            "GIT_NOGLOB_PATHSPECS",
            "GIT_ICASE_PATHSPECS",
        ):
            process_env.pop(key, None)
        process_env.update(
            {
                "GIT_OPTIONAL_LOCKS": "0",
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_PAGER": "cat",
                "LC_ALL": "C",
            }
        )
        if env:
            process_env.update(env)
        try:
            result = subprocess.run(
                [
                    "git",
                    "--no-pager",
                    "-c",
                    "core.hooksPath=" + os.devnull,
                    "-c",
                    "gc.auto=0",
                    "-c",
                    "maintenance.auto=false",
                    *args,
                ],
                cwd=self.root,
                input=data,
                capture_output=True,
                env=process_env,
                check=False,
            )
        except FileNotFoundError as exc:
            raise RelayError("Git is required and must be on PATH.") from exc
        if check and result.returncode:
            detail = result.stderr.decode("utf-8", "replace").strip()
            raise RelayError(f"Git {args[0]} failed: {detail}")
        return result

    def text(self, *args: str, **kwargs) -> str:
        return os.fsdecode(self.run(*args, **kwargs).stdout).removesuffix("\n")

    def resolve(self, ref: str) -> str:
        return self.text("rev-parse", "--verify", ref)

    def head(self) -> dict:
        return {
            "commit": self.resolve("HEAD^{commit}"),
            "ref": self.text("symbolic-ref", "-q", "HEAD", check=False) or None,
        }

    def set_head(self, target: dict) -> None:
        if target["ref"]:
            self.run("symbolic-ref", "HEAD", target["ref"])
        else:
            self.run("update-ref", "--no-deref", "HEAD", target["commit"])

    def index_path(self) -> Path:
        path = Path(self.text("rev-parse", "--git-path", "index"))
        return path if path.is_absolute() else self.root / path

    def index_bytes(self) -> bytes:
        path = self.index_path()
        return path.read_bytes() if path.exists() else b""

    def restore_index(self, contents: bytes) -> None:
        path = self.index_path()
        lock = path.with_name(path.name + ".lock")
        try:
            with lock.open("xb") as stream:
                stream.write(contents)
                stream.flush()
                os.fsync(stream.fileno())
            if contents:
                os.replace(lock, path)
            else:
                path.unlink(missing_ok=True)
                lock.unlink()
        except FileExistsError as exc:
            raise RelayError(
                "Git index is locked by another process; retry after it exits."
            ) from exc

    def assert_stable(self) -> None:
        markers = (
            "MERGE_HEAD",
            "CHERRY_PICK_HEAD",
            "REVERT_HEAD",
            "rebase-merge",
            "rebase-apply",
            "sequencer",
            "BISECT_START",
            "index.lock",
            "HEAD.lock",
        )
        for marker in markers:
            path = Path(self.text("rev-parse", "--git-path", marker))
            if not path.is_absolute():
                path = self.root / path
            if path.exists():
                raise RelayError(f"Git operation or lock in progress ({marker}); finish it first.")
        if self.run("ls-files", "--unmerged", "-z").stdout:
            raise RelayError("Resolve the unmerged Git index before using Relay.")
        if self.text("config", "--bool", "core.sparseCheckout", check=False) == "true":
            raise RelayError("Sparse checkouts are not supported; use a full working tree.")
        for entry in self.run("ls-files", "-v", "-z").stdout.split(b"\0"):
            if entry and (entry[:1].islower() or entry[:1] == b"S"):
                raise RelayError(
                    "Clear assume-unchanged/skip-worktree index flags before using Relay."
                )
        self.assert_no_gitlinks(self.index_tree())

    def assert_clean(self) -> None:
        self.assert_stable()
        if self.run(
            "status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignore-submodules=none"
        ).stdout:
            raise RelayError(
                "A clean workspace is required: commit, stash, or discard staged, "
                "unstaged, and non-ignored untracked changes first."
            )

    def assert_no_gitlinks(self, tree: str) -> None:
        if any(
            entry.startswith(b"160000 ")
            for entry in self.run("ls-tree", "-r", "-z", tree).stdout.split(b"\0")
        ):
            raise RelayError(
                "Submodules and embedded repositories cannot be fully snapshotted; "
                "use a working tree without them."
            )

    @contextmanager
    def temporary_index(self):
        with tempfile.TemporaryDirectory(prefix="relay-index-") as directory:
            path = Path(directory) / "index"
            contents = self.index_bytes()
            if contents:
                path.write_bytes(contents)
            env = {"GIT_INDEX_FILE": str(path)}
            # Expand split indexes so saved index blobs don't depend on sharedindex.* lifetimes.
            self.run("update-index", "--no-split-index", env=env)
            yield path, env

    def index_tree(self) -> str:
        # write-tree may update the cache-tree extension. Keep even inspection off the real index.
        with self.temporary_index() as (_, env):
            return self.text("write-tree", env=env)

    def normalized_index(self) -> bytes:
        with self.temporary_index() as (path, _):
            return path.read_bytes()

    def workspace_tree(self, baseline: str | None = None) -> str:
        """Snapshot tracked + non-ignored untracked files without changing the staging UI."""
        baseline = baseline or self.resolve("HEAD")
        with self.temporary_index() as (_, env):
            indexed = {
                entry.split(b"\t", 1)[1]
                for entry in self.run("ls-files", "--stage", "-z", env=env).stdout.split(b"\0")
                if entry
            }
            # A staged deletion can leave a now-ignored file on disk. It is still session code.
            for entry in self.run("ls-tree", "-r", "-z", baseline).stdout.split(b"\0"):
                if not entry:
                    continue
                meta, name = entry.split(b"\t", 1)
                mode, kind, oid = meta.split()
                if name in indexed or kind != b"blob":
                    continue
                path = self.root / os.fsdecode(name)
                try:
                    file_mode = path.lstat().st_mode
                except (FileNotFoundError, NotADirectoryError):
                    continue
                if stat.S_ISREG(file_mode) or stat.S_ISLNK(file_mode):
                    self.run(
                        "update-index",
                        "--add",
                        "--replace",
                        "--cacheinfo",
                        os.fsdecode(mode),
                        os.fsdecode(oid),
                        os.fsdecode(name),
                        env=env,
                    )
            self.run("add", "--all", "--", ".", env=env)
            tree = self.text("write-tree", env=env)
            self.assert_no_gitlinks(tree)
            return tree

    def tree(self, commit: str) -> str:
        return self.resolve(commit + "^{tree}")

    def commit(self, tree: str, parent: str, message: str, *, internal: bool = True) -> str:
        env = None
        if internal:
            env = {
                "GIT_AUTHOR_NAME": "Agent-Session-Relay",
                "GIT_AUTHOR_EMAIL": "relay@localhost",
                "GIT_COMMITTER_NAME": "Agent-Session-Relay",
                "GIT_COMMITTER_EMAIL": "relay@localhost",
            }
        return self.text(
            "commit-tree", tree, "-p", parent, data=(message.rstrip() + "\n").encode(), env=env
        )

    def pin(self, ref: str, oid: str, *, create: bool = False) -> None:
        args = (ref, oid, "") if create else (ref, oid)
        self.run("update-ref", *args)

    def refs(self, prefix: str) -> dict:
        lines = self.text("for-each-ref", "--format=%(refname) %(objectname)", prefix)
        return dict(line.split(" ", 1) for line in lines.splitlines())

    def delete_refs(self, refs: dict) -> None:
        if refs:
            data = "".join(f"delete {ref} {oid}\n" for ref, oid in refs.items()).encode()
            self.run("update-ref", "--stdin", data=data)

    def paths(self, tree: str) -> set[bytes]:
        return set(self.run("ls-tree", "-r", "--name-only", "-z", tree).stdout.split(b"\0")) - {b""}

    def materialize(self, source: str, target: str) -> None:
        """Replace captured project files, preserving unrelated ignored files."""
        target_paths = self.paths(target)
        source_paths = self.paths(source)
        target_parents = {
            b"/".join(parts[:i])
            for path in target_paths
            for parts in [path.split(b"/")]
            for i in range(1, len(parts))
        }
        ignored = self.run("ls-files", "--others", "--ignored", "--exclude-standard", "-z").stdout
        for name in ignored.split(b"\0"):
            name = name.rstrip(b"/")
            if not name or name in source_paths:
                continue
            parts = name.split(b"/")
            if (
                name in target_paths
                or name in target_parents
                or any(b"/".join(parts[:i]) in target_paths for i in range(1, len(parts)))
            ):
                raise RelayError(
                    f"An ignored file would be overwritten: {os.fsdecode(name)}. "
                    "Move it out of the way and retry."
                )
        saved_index = self.index_bytes()
        try:
            # Make captured new files tracked so read-tree removes them when leaving the session.
            self.run("read-tree", source)
            self.run("update-index", "--refresh")
            self.run("read-tree", "-m", "-u", source, target)
        except BaseException:
            self.restore_index(saved_index)
            raise

    def available_origin(self, origin: dict) -> dict:
        ref = origin["ref"]
        if ref:
            result = self.run("rev-parse", "--verify", ref + "^{commit}", check=False)
            occupied = False
            records = self.run("worktree", "list", "--porcelain", "-z").stdout.split(b"\0\0")
            for record in records:
                fields = record.split(b"\0")
                if b"branch " + os.fsencode(ref) in fields:
                    location = next((f[9:] for f in fields if f.startswith(b"worktree ")), b"")
                    if Path(os.fsdecode(location)).resolve() != self.root.resolve():
                        occupied = True
            if result.returncode == 0 and not occupied:
                return {"ref": ref, "commit": os.fsdecode(result.stdout).strip()}
        return {"ref": None, "commit": origin["commit"]}

    def diff(
        self,
        left: str,
        right: str,
        paths: list[str],
        *,
        name_only: bool = False,
        null: bool = False,
    ) -> bytes:
        filters = []
        for name in paths:
            path = Path(os.path.abspath(self.cwd / name))
            try:
                filters.append(str(path.relative_to(self.root)))
            except ValueError as exc:
                raise RelayError(f"Diff path is outside the repository: {name}") from exc
        args = [
            "--literal-pathspecs",
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--no-color",
            "--no-renames",
            "--src-prefix=a/",
            "--dst-prefix=b/",
        ]
        args += ["--name-only"] if name_only else ["--binary", "--full-index"]
        if null:
            args.append("-z")
        return self.run(*args, left, right, "--", *filters).stdout
