"""One review session spanning explicitly selected worktrees below a common directory."""

from __future__ import annotations

import copy
import json
import os
import uuid
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .errors import RelayError
from .git import Git
from .session import Relay, now
from .storage import MetadataStore, atomic_json, remove_journal


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("expected an object")
        return value
    except (ValueError, OSError) as exc:
        raise RelayError(
            f"Cannot read Relay metadata at {path}; preserve it for recovery."
        ) from exc


class WorkspaceStore(MetadataStore):
    schema = 1

    def load(self) -> dict:
        state = super().load()
        state.setdefault("repositories", [])
        try:
            names = []
            for repo in state["repositories"]:
                name = repo["name"]
                path = Path(name)
                if not name or path.is_absolute() or ".." in path.parts or str(path) != name:
                    raise ValueError("invalid member path")
                if name == "." or not isinstance(repo["git_dir"], str):
                    raise ValueError("invalid worktree")
                names.append(name)
            if len(set(names)) != len(names):
                raise ValueError("duplicate members")
            for session in state["sessions"].values():
                if set(session["members"]) != set(names):
                    raise ValueError("invalid session members")
        except (KeyError, TypeError, ValueError) as exc:
            raise RelayError(f"Invalid workspace metadata at {self.path}.") from exc
        return state


class Workspace:
    def __init__(self, root: Path | str, cwd: Path | str = "."):
        self.root = Path(root).resolve()
        self.cwd = Path(cwd).resolve()
        self.store = WorkspaceStore(self.root / ".relay")

    def active(self, state: dict, *, required: bool = True) -> dict | None:
        return Relay.active(self, state, required=required)

    def _members(self, state: dict) -> dict[str, Relay]:
        members = {}
        for repo in state["repositories"]:
            name = repo["name"]
            try:
                git = Git(self.root / name)
                if (
                    git.root.resolve() != self.root / name
                    or str(git.git_dir.resolve()) != repo["git_dir"]
                ):
                    raise RelayError("the registered worktree has moved or been replaced")
            except RelayError as exc:
                raise RelayError(f"Workspace member {name}: {exc}") from exc
            members[name] = Relay(git)
        return members

    @contextmanager
    def _member_locks(self, members: dict[str, Relay]):
        with ExitStack() as stack:
            for relay in sorted(members.values(), key=lambda r: str(r.git.git_dir)):
                stack.enter_context(relay.store.lock())
            yield

    @contextmanager
    def _locked(self):
        with self.store.lock():
            self.store.assert_ready()
            state = self.store.load()
            members = self._members(state)
            with self._member_locks(members):
                for relay in members.values():
                    relay.store.assert_ready()
                yield state, members

    def _validate(self, group: dict, members: dict[str, Relay]) -> None:
        for name, relay in members.items():
            state = relay.store.load()
            session = state["sessions"].get(group["members"][name])
            if session is None or any(
                session[key] != group[key] for key in ("state", "phase", "turn", "next_turn")
            ):
                raise RelayError(f"Workspace member {name} has inconsistent session state.")
            if group["state"] == "active":
                if state["active"] != session["id"]:
                    raise RelayError(f"Workspace member {name} has a different active session.")
                relay.validate(session)

    @staticmethod
    def _sync(group: dict, members: dict[str, Relay]) -> None:
        name, relay = next(iter(members.items()))
        session = relay.store.load()["sessions"][group["members"][name]]
        for key in ("state", "phase", "turn", "next_turn"):
            group[key] = session[key]

    def _journals(self, members: dict[str, Relay]) -> dict[str, dict]:
        journals = {}
        for name, relay in members.items():
            if relay.store.journal_path.exists():
                journal = read_json(relay.store.journal_path)
                if journal.get("coordinator") != str(self.store.journal_path):
                    raise RelayError(f"Member {name} has an unrelated interrupted operation.")
                journals[name] = journal
        return journals

    def _cleanup(self, members: dict[str, Relay]) -> None:
        for name, journal in self._journals(members).items():
            members[name].store.complete(journal)
        remove_journal(self.store.journal_path)

    def _rollback(self, journal: dict, members: dict[str, Relay]) -> None:
        # Keep every before-image pinned until all members and the coordinator are restored.
        for name, local in self._journals(members).items():
            members[name].store.rollback(local, cleanup=False)
        self.store.save(journal["state"])
        journal["decision"] = "rollback"
        atomic_json(self.store.journal_path, journal)

    @contextmanager
    def _transaction(
        self,
        state: dict,
        group: dict,
        members: dict[str, Relay],
        action: str,
        *,
        previous: dict | None = None,
    ):
        """Prepare all members; publish one durable decision before releasing any backups."""
        snapshots = {}
        for name, relay in members.items():
            relay.git.assert_stable()
            local = relay.active(relay.store.load(), required=False)
            snapshots[name] = relay.workspace_tree(local) if local else relay.git.workspace_tree()
        journal = {
            "state": copy.deepcopy(state if previous is None else previous),
            "repositories": state["repositories"],
            "members": group["members"],
            "action": action,
            "decision": "pending",
        }
        atomic_json(self.store.journal_path, journal)
        try:
            for name, relay in members.items():
                local = relay.store.prepare(
                    relay.store.load(),
                    group["members"][name],
                    action,
                    snapshots[name],
                    coordinator=self.store.journal_path,
                )
                relay.store.coordinated_journal = local
            yield
            self.store.save(state)
            journal["decision"] = "commit"
            atomic_json(self.store.journal_path, journal)
        except BaseException as original:
            try:
                self._rollback(journal, members)
                self._cleanup(members)
            except BaseException as rollback_error:
                raise RelayError(
                    f"Workspace {action} failed; recovery could not complete: {rollback_error}. "
                    "Snapshots are retained. Run `relay recover` from the workspace or a member."
                ) from original
            raise
        else:
            # Failures after the commit decision must finish cleanup, never undo the operation.
            self._cleanup(members)
        finally:
            for relay in members.values():
                relay.store.coordinated_journal = None

    def _configuration(self, paths: list[str]) -> list[dict]:
        repositories = []
        roots = []
        for path in paths:
            git = Git(self.cwd / path)
            root = git.root.resolve()
            if root != (self.cwd / path).resolve():
                raise RelayError(f"--repo must name a worktree root: {path}")
            if root == self.root or not root.is_relative_to(self.root):
                raise RelayError("Every --repo must be below the common workspace directory.")
            if any(root.is_relative_to(other) or other.is_relative_to(root) for other in roots):
                raise RelayError("Workspace members must be distinct, non-overlapping worktrees.")
            existing = find_workspace(root)
            if existing is not None and existing.root != self.root:
                raise RelayError(f"Worktree {path} already belongs to workspace {existing.root}.")
            roots.append(root)
            repositories.append(
                {
                    "name": root.relative_to(self.root).as_posix(),
                    "git_dir": str(git.git_dir.resolve()),
                }
            )
        return sorted(repositories, key=lambda r: r["name"])

    def start(self, message: str | None = None, *, repos: list[str] | None = None) -> dict:
        with self.store.lock():
            self.store.assert_ready()
            state = self.store.load()
            previous = copy.deepcopy(state)
            if self.active(state, required=False):
                raise RelayError("A Relay session is already active; suspend or finish it first.")
            if repos is not None:
                configured = self._configuration(repos)
                if state["sessions"] and configured != state["repositories"]:
                    raise RelayError("Finish existing workspace sessions before changing members.")
                state["repositories"] = configured
            if not state["repositories"]:
                raise RelayError("Select workspace members with `relay start --repo <path> ...`.")
            members = self._members(state)
            with self._member_locks(members):
                for name, relay in members.items():
                    relay.store.assert_ready()
                    local = relay.store.load()
                    expected = {s["members"][name] for s in state["sessions"].values()}
                    if local["active"] or set(local["sessions"]) != expected:
                        raise RelayError(
                            f"Member {name} has an independent Relay session; finish or abort it "
                            "before starting this workspace session."
                        )
                    relay.git.assert_clean()
                    try:
                        relay.git.head()
                    except RelayError as exc:
                        raise RelayError(f"Create an initial Git commit in {name} first.") from exc
                if message is not None:
                    message = Relay.validate_message(message)
                sid = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]
                group = {
                    "id": sid,
                    "state": "active",
                    "created_at": now(),
                    "message": message,
                    "messages": {},
                    "phase": "human",
                    "turn": 0,
                    "next_turn": "normal",
                    # Distinct namespaces also support linked worktrees sharing one Git database.
                    "members": {name: f"{sid}-{i:02d}" for i, name in enumerate(members, 1)},
                }
                with self._transaction(state, group, members, "start", previous=previous):
                    for name, relay in members.items():
                        relay.start(message, session_id=group["members"][name])
                    state["sessions"][sid] = group
                    state["active"] = sid
                return copy.deepcopy(group)

    def _select(self, state: dict, name: str | None, *, suspended: bool = False) -> dict:
        if suspended:
            return Relay.select_suspended(self, state, name)
        if name is None:
            return self.active(state, required=False) or self._select(state, None, suspended=True)
        matches = [s for s in state["sessions"].values() if s["id"].startswith(name)]
        if len(matches) != 1:
            raise RelayError("Session ID must match exactly one session. Use `relay list`.")
        return matches[0]

    def repository(self, name: str, state: dict | None = None) -> str:
        state = state if state is not None else self.store.load()
        for repo in state["repositories"]:
            if name == repo["name"] or (self.cwd / name).resolve() == self.root / repo["name"]:
                return repo["name"]
        raise RelayError(f"Unknown workspace repository: {name}")

    @property
    def git(self) -> Git:
        # Editor configuration is stable across entry directories: use the first member.
        return next(iter(self._members(self.store.load()).values())).git

    def set_message(self, message=None, name=None, *, editor=None, repo=None) -> dict:
        with self._locked() as (state, members):
            group = self._select(state, name)
            self._validate(group, members)
            selected = self.repository(repo, state) if repo is not None else None
            initial = group["messages"].get(selected, group["message"])
            expected = copy.deepcopy(group)
        if message is None:
            if editor is None:
                raise RelayError("Provide a session message or a message editor.")
            message = editor(initial)
        message = Relay.validate_message(message)
        with self._locked() as (state, members):
            group = state["sessions"].get(expected["id"])
            if group != expected:
                raise RelayError("Message editing cancelled: the workspace session changed.")
            self._validate(group, members)
            with self._transaction(state, group, members, "message"):
                if selected is None:
                    group["message"] = message
                else:
                    group["messages"][selected] = message
                for member, relay in members.items():
                    value = group["messages"].get(member, group["message"])
                    if value is not None:
                        relay.set_message(value, group["members"][member])
            return {**copy.deepcopy(group), "message": message}

    def list_sessions(self) -> list[dict]:
        self.store.assert_ready()
        with self.store.lock():
            self.store.assert_ready()
            return list(self.store.load()["sessions"].values())

    def set_btw(self, *, cancel=False) -> str:
        with self._locked() as (state, members):
            group = self.active(state)
            self._validate(group, members)
            with self._transaction(state, group, members, "btw"):
                for relay in members.values():
                    relay.set_btw(cancel=cancel)
                self._sync(group, members)
            return group["next_turn"]

    def btw_active(self) -> bool:
        self.store.assert_ready()
        state = self.store.load()
        if not self.active(state, required=False):
            return False
        with self._locked() as (state, members):
            self._validate(self.active(state), members)
            return next(iter(members.values())).btw_active()

    def handoff(self, owner: str | None = None) -> dict | None:
        if not self.active(self.store.load(), required=False):
            return None
        with self._locked() as (state, members):
            group = self.active(state, required=False)
            if group is None:
                return None
            self._validate(group, members)
            contexts = {}
            if group["phase"] == "agent":
                # Duplicate delivery must not create snapshots or consume approvals.
                contexts = {name: relay.handoff(owner) for name, relay in members.items()}
            else:
                with self._transaction(state, group, members, "handoff"):
                    contexts = {name: relay.handoff(owner) for name, relay in members.items()}
                    self._sync(group, members)
            first = next(iter(contexts.values()))
            return {
                "kind": first["kind"],
                "continuing": first["continuing"],
                "workspace": str(self.root),
                "repositories": {name: str(self.root / name) for name in members},
                **{
                    key: [
                        f"{name}/{path}"
                        for name, context in contexts.items()
                        for path in context[key]
                    ]
                    for key in ("human_paths", "reviewed_paths")
                },
            }

    def stop(self, owner: str | None = None, *, require_open=False) -> dict | None:
        self.store.assert_ready()
        if not self.active(self.store.load(), required=require_open):
            return None
        with self._locked() as (state, members):
            group = self.active(state, required=require_open)
            if group is None:
                return None
            self._validate(group, members)
            if group["phase"] != "agent":
                if require_open:
                    raise RelayError("No agent turn is in progress; Relay is already human.")
                return None
            snapshots = {}
            with self._transaction(state, group, members, "agent-stop"):
                for name, relay in members.items():
                    for area, commit in (
                        relay.stop(owner, require_open=require_open) or {}
                    ).items():
                        snapshots[f"{name} {area}"] = commit
                self._sync(group, members)
            return snapshots or None

    def suspend(self) -> tuple[dict, dict]:
        with self._locked() as (state, members):
            group = self.active(state)
            self._validate(group, members)
            with self._transaction(state, group, members, "suspend"):
                origins = {name: relay.suspend()[1] for name, relay in members.items()}
                self._sync(group, members)
                state["active"] = None
            return copy.deepcopy(group), origins

    def resume(self, name=None) -> dict:
        with self._locked() as (state, members):
            if self.active(state, required=False):
                raise RelayError("A Relay session is already active; suspend or finish it first.")
            group = self._select(state, name, suspended=True)
            self._validate(group, members)
            for relay in members.values():
                if relay.active(relay.store.load(), required=False):
                    raise RelayError("A workspace member has another active session.")
                relay.git.assert_clean()
            with self._transaction(state, group, members, "resume"):
                for member, relay in members.items():
                    relay.resume(group["members"][member])
                self._sync(group, members)
                state["active"] = group["id"]
            return copy.deepcopy(group)

    def _finish_preflight(self, group: dict, members: dict[str, Relay]) -> dict[str, bool]:
        self._validate(group, members)
        changes = {}
        for name, relay in members.items():
            session = relay.active(relay.store.load())
            approved = relay.git.index_tree()
            if relay.workspace_tree(session) != approved:
                raise RelayError(f"Pending changes remain in {name}. Review and stage them first.")
            changes[name] = approved != relay.git.tree(session["base_commit"])
            if changes[name]:
                relay.git.text("var", "GIT_AUTHOR_IDENT")
                relay.git.text("var", "GIT_COMMITTER_IDENT")
                branch = "refs/heads/relay/result/" + session["id"]
                if relay.git.refs(branch).get(branch):
                    raise RelayError(f"Result branch already exists in {name}: {branch}")
        return changes

    def finish(self, message=None, *, editor=None) -> dict:
        expected = None
        explicit = message is not None
        while True:
            with self._locked() as (state, members):
                group = self.active(state)
                if expected is not None and group != expected:
                    raise RelayError(
                        "Finish cancelled: the workspace session changed while editing."
                    )
                changes = self._finish_preflight(group, members)
                value = message if message is not None else group["message"]
                if value is not None:
                    value = Relay.validate_message(value)
                    results = {}
                    with self._transaction(state, group, members, "finish"):
                        for name, relay in members.items():
                            if changes[name]:
                                description = (
                                    value if explicit else group["messages"].get(name, value)
                                )
                                branch = relay.finish(description)
                                results[name] = {
                                    "branch": branch,
                                    "commit": relay.git.resolve("HEAD"),
                                }
                            else:
                                session, origin = relay.suspend()
                                relay.remove_session(relay.store.load(), session)
                                results[name] = {"branch": None, "origin": origin}
                        del state["sessions"][group["id"]]
                        state["active"] = None
                    return results
                if editor is None:
                    raise RelayError("A custom session message is required before finishing.")
                expected = copy.deepcopy(group)
            message = Relay.validate_message(editor(None))

    def abort(self, expected_id: str) -> tuple[dict, dict]:
        with self._locked() as (state, members):
            group = self.active(state)
            if group["id"] != expected_id:
                raise RelayError("The active workspace session changed during confirmation.")
            self._validate(group, members)
            with self._transaction(state, group, members, "abort"):
                results = {
                    name: relay.abort(group["members"][name]) for name, relay in members.items()
                }
                del state["sessions"][group["id"]]
                state["active"] = None
            return (
                {name: result[0] for name, result in results.items()},
                {name: result[1] for name, result in results.items()},
            )

    def recover(self) -> dict:
        with self.store.lock():
            if not self.store.journal_path.exists():
                raise RelayError("No interrupted Relay operation needs recovery.")
            journal = read_json(self.store.journal_path)
            members = self._members(journal)
            with self._member_locks(members):
                branches = {}
                if journal["decision"] == "pending":
                    # Preserve *all* current work before restoring the first member.
                    for name, local in self._journals(members).items():
                        git = members[name].git
                        git.assert_stable()
                        workspace = git.workspace_tree(previous=local["workspace"])
                        branch = "relay/recovered/" + uuid.uuid4().hex[:12]
                        commit = git.commit(
                            workspace,
                            local["head"]["commit"],
                            "Relay workspace recovery",
                        )
                        git.pin("refs/heads/" + branch, commit, create=True)
                        branches[name] = branch
                    self._rollback(journal, members)
                self._cleanup(members)
                return {"decision": journal["decision"], "branches": branches}

    def status(self) -> dict:
        self.store.assert_ready()
        state = self.store.load()
        if not self.active(state, required=False):
            return {
                "active": False,
                "lifecycle": "suspended" if state["sessions"] else "inactive",
                "suspended_sessions": sorted(state["sessions"]),
                "workspace": str(self.root),
            }
        with self._locked() as (state, members):
            group = self.active(state)
            self._validate(group, members)
            statuses = {
                name: {**relay.status(), "path": str(self.root / name)}
                for name, relay in members.items()
            }
            first = next(iter(statuses.values()))
            return {
                "active": True,
                "session": group["id"],
                "message": group["message"],
                "workspace": str(self.root),
                "repositories": statuses,
                **{
                    key: first[key]
                    for key in ("lifecycle", "phase", "turn", "turn_kind", "next_turn")
                },
                **{
                    key: any(status[key] for status in statuses.values())
                    for key in (
                        "session_changes",
                        "pending_changes",
                        "staged_approvals",
                        "unstaged_changes",
                    )
                },
                "provenance": {
                    key: any(s["provenance"][key] for s in statuses.values())
                    for key in first["provenance"]
                },
            }

    def agent_status(self) -> dict:
        status = self.status()
        if not status["active"]:
            return {"active": False}
        keys = (
            "active",
            "phase",
            "turn",
            "turn_kind",
            "session_changes",
            "pending_changes",
            "provenance",
        )
        return {
            **{key: status[key] for key in keys},
            "workspace": str(self.root),
            "repositories": {
                name: {**{key: item[key] for key in keys}, "path": item["path"]}
                for name, item in status["repositories"].items()
            },
        }

    def diff(self, kind, paths, *, name_only=False, null=False, stat=False, repo=None) -> bytes:
        with self._locked() as (state, members):
            self._validate(self.active(state), members)
            if repo is not None:
                selected = self.repository(repo, state)
                members = {selected: members[selected]}
            filters: dict[str, list[str]] = {name: [] for name in members}
            included = set(members) if not paths else set()
            for path in paths:
                # Lexical normalization preserves Git's treatment of symlinks as paths.
                absolute = Path(os.path.abspath(self.cwd / path))
                matched = False
                for name in members:
                    root = self.root / name
                    if absolute.is_relative_to(root):
                        filters[name].append(str(absolute))
                    elif root.is_relative_to(absolute) and absolute.is_relative_to(self.root):
                        filters[name].append(str(root))
                    else:
                        continue
                    included.add(name)
                    matched = True
                if not matched:
                    raise RelayError(
                        f"Diff path is outside the selected workspace repositories: {path}"
                    )
            output = []
            for name, relay in members.items():
                if name not in included:
                    continue
                data = relay.diff(
                    kind,
                    filters[name],
                    name_only=name_only,
                    null=name_only,
                    stat=stat,
                    prefix=name + "/",
                )
                if name_only:
                    for path in data.split(b"\0"):
                        if not path:
                            continue
                        full = os.fsencode(name) + b"/" + path
                        if null:
                            output.append(full + b"\0")
                        else:
                            text = os.fsdecode(full)
                            if any(ord(c) < 32 or c in '\\"' for c in text):
                                text = json.dumps(text, ensure_ascii=True)
                            output.append(os.fsencode(text) + b"\n")
                elif data:
                    if stat:
                        output.append(("Repository: " + json.dumps(name) + "\n").encode())
                    output.append(data)
            return b"".join(output)


def find_workspace(cwd: Path | str) -> Workspace | None:
    location = Path(cwd).resolve()
    git = None
    for root in (location, *location.parents):
        workspace = Workspace(root, location)
        if not workspace.store.path.exists() and not workspace.store.journal_path.exists():
            continue
        state = workspace.store.load()
        repositories = state["repositories"]
        if workspace.store.journal_path.exists():
            repositories = read_json(workspace.store.journal_path)["repositories"]
        if location == root:
            return workspace
        if git is None:
            try:
                git = Git(location)
            except RelayError:
                return None
        if any(git.root.resolve() == root / repo["name"] for repo in repositories):
            return workspace
    return None


def discover(cwd: Path | str = ".", *, required: bool = True) -> Relay | Workspace | None:
    if workspace := find_workspace(cwd):
        return workspace
    try:
        return Relay(Git(cwd))
    except RelayError:
        if required:
            raise
        return None
