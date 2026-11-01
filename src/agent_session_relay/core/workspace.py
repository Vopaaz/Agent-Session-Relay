"""One review session spanning explicitly selected worktrees below a common directory."""

from __future__ import annotations

import copy
import json
import os
import stat
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
    schema = 2

    @staticmethod
    def member_records(members: dict, repositories: list[dict]) -> dict:
        identities = {repo["name"]: repo["git_dir"] for repo in repositories}
        return {name: {"id": sid, "git_dir": identities[name]} for name, sid in members.items()}

    def upgrade(self, state: dict) -> dict:
        """Read schema 1 without retaining its workspace-wide configuration or rewriting peers."""
        if state.get("schema") != 1:
            return state
        state = copy.deepcopy(state)
        repositories = state.pop("repositories", [])
        for group in state["sessions"].values():
            group["members"] = self.member_records(group["members"], repositories)
            group["owners"] = []
            group["turn_kind"] = None
            for member in group["members"].values():
                directory = Path(member["git_dir"]) / "agent-session-relay"
                local = read_json(directory / "state.json")["sessions"].get(member["id"])
                if local is None and (directory / "transaction.json").exists():
                    local = read_json(directory / "transaction.json")["state"]["sessions"].get(
                        member["id"]
                    )
                for turn in (local or {}).get("turns", []):
                    if turn.get("owner") not in group["owners"]:
                        group["owners"].append(turn.get("owner"))
                    if turn["number"] == group["turn"]:
                        group["turn_kind"] = turn["kind"]
        state["schema"] = self.schema
        return state

    def load(self) -> dict:
        try:
            raw = read_json(self.path) if self.path.exists() else None
            state = self.upgrade(raw) if raw and raw.get("schema") == 1 else super().load()
            if state["active"] is not None and state["active"] not in state["sessions"]:
                raise ValueError("invalid active session")
            for sid, session in state["sessions"].items():
                if session["id"] != sid or session["state"] not in ("active", "suspended"):
                    raise ValueError("invalid session")
                if not session["members"]:
                    raise ValueError("empty session")
                roots = []
                ids = set()
                for name, member in session["members"].items():
                    path = Path(name)
                    if (
                        not name or path.is_absolute() or ".." in path.parts
                        or str(path) != name or name == "."
                        or not isinstance(member["git_dir"], str)
                        or not isinstance(member["id"], str)
                        or not member["id"] or member["id"] in ids
                    ):
                        raise ValueError("invalid member")
                    if any(path.is_relative_to(p) or p.is_relative_to(path) for p in roots):
                        raise ValueError("overlapping members")
                    roots.append(path)
                    ids.add(member["id"])
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise RelayError(f"Invalid workspace metadata at {self.path}.") from exc
        return state


class Workspace:
    def __init__(self, root: Path | str, cwd: Path | str = "."):
        self.root = Path(root).resolve()
        self.cwd = Path(cwd).resolve()
        self.store = WorkspaceStore(self.root / ".relay")

    def active(self, state: dict, *, required: bool = True) -> dict | None:
        group = Relay.active(self, state, required=False)
        if group is not None and self._visible(group):
            return group
        if required:
            raise RelayError("No active Relay session. Use `relay start` or `relay resume`.")
        return None

    def _visible(self, group: dict) -> bool:
        if self.cwd == self.root:
            return True
        try:
            root = Git(self.cwd).root.resolve()
        except RelayError:
            return False
        return any(root == self.root / name for name in group["members"])

    def _members(self, group: dict) -> dict[str, Relay]:
        members = {}
        for name, member in sorted(group["members"].items()):
            try:
                git = Git(self.root / name)
                if (
                    git.root.resolve() != self.root / name
                    or str(git.git_dir.resolve()) != member["git_dir"]
                ):
                    raise RelayError("the registered worktree has moved or been replaced")
            except RelayError as exc:
                raise RelayError(f"Workspace member {name}: {exc}") from exc
            members[name] = Relay(git, coordinator=str(self.store.path))
        return members

    @contextmanager
    def _member_locks(self, members: dict[str, Relay]):
        with ExitStack() as stack:
            for relay in sorted(members.values(), key=lambda r: str(r.git.git_dir)):
                stack.enter_context(relay.store.lock())
            yield

    @contextmanager
    def _locked(self, name: str | None = None, *, suspended: bool = False):
        with self.store.lock():
            self.store.assert_ready()
            state = self.store.load()
            group = self._select(state, name, suspended=suspended)
            members = self._members(group)
            with self._member_locks(members):
                for relay in members.values():
                    relay.store.assert_ready()
                yield state, members

    def _validate(self, group: dict, members: dict[str, Relay]) -> None:
        for name, relay in members.items():
            state = relay.store.load()
            session = state["sessions"].get(group["members"][name]["id"])
            if session is None or any(
                session[key] != group[key] for key in ("state", "phase", "turn", "next_turn")
            ):
                raise RelayError(f"Workspace member {name} has inconsistent session state.")
            relay.check_owner(session)
            if group["state"] == "active":
                if state["active"] != session["id"]:
                    raise RelayError(f"Workspace member {name} has a different active session.")
                relay.validate(session)

    @staticmethod
    def _sync(group: dict, members: dict[str, Relay]) -> None:
        name, relay = next(iter(members.items()))
        session = relay.store.load()["sessions"][group["members"][name]["id"]]
        for key in ("state", "phase", "turn", "next_turn"):
            group[key] = session[key]
        for name, relay in members.items():
            turns = relay.store.load()["sessions"][group["members"][name]["id"]]["turns"]
            if turns and turns[-1]["number"] == group["turn"]:
                group["turn_kind"] = turns[-1]["kind"]
                break

    def _journals(self, members: dict[str, Relay]) -> dict[str, dict]:
        journals = {}
        for name, relay in members.items():
            if relay.store.journal_path.exists():
                journal = read_json(relay.store.journal_path)
                if journal.get("coordinator") != str(self.store.journal_path):
                    raise RelayError(f"Member {name} has an unrelated interrupted operation.")
                journals[name] = journal
        return journals

    def journal(self) -> dict:
        journal = read_json(self.store.journal_path)
        if "repositories" in journal:
            journal["members"] = self.store.member_records(
                journal["members"], journal.pop("repositories")
            )
            journal["state"] = self.store.upgrade(journal["state"])
        return journal

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
            "members": copy.deepcopy(group["members"]),
            "action": action,
            "decision": "pending",
        }
        atomic_json(self.store.journal_path, journal)
        try:
            for name, relay in members.items():
                local = relay.store.prepare(
                    relay.store.load(),
                    group["members"][name]["id"],
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

    def _parse_members(self, paths: list[str], *, argument: str = "--repo") -> dict:
        members = {}
        roots = []
        for path in paths:
            try:
                if not stat.S_ISDIR((self.cwd / path).stat().st_mode):
                    raise RelayError("path is not a directory")
                git = Git(self.cwd / path)
            except FileNotFoundError as exc:
                raise RelayError(f"Invalid {argument} '{path}': directory does not exist") from exc
            except (OSError, RelayError) as exc:
                raise RelayError(f"Invalid {argument} '{path}': {exc}") from exc
            root = git.root.resolve()
            if root != (self.cwd / path).resolve():
                raise RelayError(f"{argument} must name a worktree root: {path}")
            if root == self.root or not root.is_relative_to(self.root):
                raise RelayError("Every repository must be below the common workspace directory.")
            if any(root.is_relative_to(other) or other.is_relative_to(root) for other in roots):
                raise RelayError("Workspace members must be distinct, non-overlapping worktrees.")
            existing = find_workspace(root)
            if existing is not None and existing.active(existing.store.load(), required=False):
                raise RelayError(f"Worktree {path} already belongs to workspace {existing.root}.")
            roots.append(root)
            members[root.relative_to(self.root).as_posix()] = {
                "git_dir": str(git.git_dir.resolve()),
            }
        return dict(sorted(members.items()))

    @staticmethod
    def _check_new_member(name: str, relay: Relay) -> None:
        relay.store.assert_ready()
        if relay.store.load()["active"]:
            raise RelayError(f"Member {name} already has an active Relay session.")
        relay.git.assert_clean()
        try:
            relay.git.head()
        except RelayError as exc:
            raise RelayError(f"Create an initial Git commit in {name} first.") from exc

    def start(self, message: str | None = None, *, repos: list[str] | None = None) -> dict:
        with self.store.lock():
            self.store.assert_ready()
            state = self.store.load()
            previous = copy.deepcopy(state)
            if state["active"]:
                raise RelayError("A Relay session is already active; suspend or finish it first.")
            if not repos:
                raise RelayError("Select workspace members with `relay start --repo <path> ...`.")
            sid = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]
            configured = self._parse_members(repos)
            group = {
                "id": sid,
                "state": "active",
                "created_at": now(),
                "message": message,
                "messages": {},
                "phase": "human",
                "turn": 0,
                "turn_kind": None,
                "owners": [],
                "next_turn": "normal",
                "members": {
                    name: {**member, "id": f"{sid}-{i:02d}"}
                    for i, (name, member) in enumerate(configured.items(), 1)
                },
            }
            members = self._members(group)
            with self._member_locks(members):
                for name, relay in members.items():
                    self._check_new_member(name, relay)
                if message is not None:
                    message = Relay.validate_message(message)
                    group["message"] = message
                with self._transaction(state, group, members, "start", previous=previous):
                    for name, relay in members.items():
                        relay.start(message, session_id=group["members"][name]["id"])
                    state["sessions"][sid] = group
                    state["active"] = sid
                return copy.deepcopy(group)

    def add(self, path: str, *, original: Relay | None = None) -> dict:
        """Enroll a clean worktree, adopting only the active standalone session if needed."""
        with self.store.lock():
            self.store.assert_ready()
            state = self.store.load()
            previous = copy.deepcopy(state)
            if original is None:
                group = self.active(state)
            else:
                if state["active"]:
                    raise RelayError("The common directory already has an active Relay session.")
                local = original.active(original.store.load())
                name = original.git.root.relative_to(self.root).as_posix()
                group = {
                    **{key: local[key] for key in (
                        "id", "state", "created_at", "phase", "turn", "next_turn",
                    )},
                    "turn_kind": local["turns"][-1]["kind"] if local["turns"] else None,
                    "owners": list(dict.fromkeys(t.get("owner") for t in local["turns"])),
                    "message": None,
                    "messages": {},
                    "members": {name: {
                        "id": local["id"], "git_dir": str(original.git.git_dir.resolve()),
                    }},
                }
            if group["phase"] != "human":
                raise RelayError(
                    "Repository additions require the human phase. Wait for Agent Stop."
                )
            configured = self._parse_members([path], argument="repo add")
            name, member = next(iter(configured.items()))
            root = self.root / name
            if any(root.is_relative_to(self.root / other)
                   or (self.root / other).is_relative_to(root) for other in group["members"]):
                raise RelayError("Session members must be distinct, non-overlapping worktrees.")
            # IDs never depend on ordering, including linked worktrees sharing Git refs.
            member["id"] = group["id"] + "-" + uuid.uuid4().hex[:8]
            expanded = copy.deepcopy(group)
            expanded["members"][name] = member
            members = self._members(expanded)
            with self._member_locks(members):
                for relay in members.values():
                    relay.store.assert_ready()
                if original is None:
                    self._validate(group, {n: r for n, r in members.items() if n != name})
                else:
                    current = original.active(original.store.load())
                    if current != local:
                        raise RelayError("Session changed while adding a repository; retry.")
                    original.validate(current)
                    expanded["message"] = original.session_message(current)
                self._check_new_member(name, members[name])
                with self._transaction(state, expanded, members, "repo-add", previous=previous):
                    if original is not None:
                        local_state = original.store.load()
                        local_state["sessions"][group["id"]]["coordinator"] = str(self.store.path)
                        original.store.save(local_state)
                    added = members[name]
                    added.start(expanded["message"], session_id=member["id"])
                    local_state = added.store.load()
                    session = local_state["sessions"][member["id"]]
                    session["turn"] = group["turn"]
                    session["next_turn"] = group["next_turn"]
                    added.store.save(local_state)
                    state["sessions"][group["id"]] = expanded
                    state["active"] = group["id"]
                return copy.deepcopy(expanded)

    def _select(self, state: dict, name: str | None, *, suspended: bool = False) -> dict:
        state = {**state, "sessions": {
            sid: group for sid, group in state["sessions"].items() if self._visible(group)
        }}
        if suspended:
            return Relay.select_suspended(self, state, name)
        if name is None:
            return self.active(state, required=False) or self._select(state, None, suspended=True)
        matches = [s for s in state["sessions"].values() if s["id"].startswith(name)]
        if len(matches) != 1:
            raise RelayError("Session ID must match exactly one session. Use `relay list`.")
        return matches[0]

    def repository(self, name: str, group: dict | None = None) -> str:
        group = group if group is not None else self.active(self.store.load())
        for member in group["members"]:
            if name == member or (self.cwd / name).resolve() == self.root / member:
                return member
        raise RelayError(f"Unknown workspace repository: {name}")

    @property
    def git(self) -> Git:
        return self.editor_git()

    def editor_git(self, name: str | None = None) -> Git:
        # Editor configuration is stable across entry directories: use the first member.
        return next(iter(self._members(self._select(self.store.load(), name)).values())).git

    def set_message(self, message=None, name=None, *, editor=None, repo=None) -> dict:
        with self._locked(name) as (state, members):
            group = self._select(state, name)
            self._validate(group, members)
            selected = self.repository(repo, group) if repo is not None else None
            initial = group["messages"].get(selected, group["message"])
            expected = copy.deepcopy(group)
        if message is None:
            if editor is None:
                raise RelayError("Provide a session message or a message editor.")
            message = editor(initial)
        message = Relay.validate_message(message)
        with self._locked(expected["id"]) as (state, members):
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
                        relay.set_message(value, group["members"][member]["id"])
            return {**copy.deepcopy(group), "message": message}

    def list_sessions(self) -> list[dict]:
        self.store.assert_ready()
        with self.store.lock():
            self.store.assert_ready()
            return [g for g in self.store.load()["sessions"].values() if self._visible(g)]

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
            continuing = group["phase"] == "agent"
            full_context = not continuing and owner not in group["owners"]
            if group["phase"] == "agent":
                # Duplicate delivery must not create snapshots or consume approvals.
                contexts = {name: relay.handoff(owner) for name, relay in members.items()}
            else:
                with self._transaction(state, group, members, "handoff"):
                    contexts = {name: relay.handoff(owner) for name, relay in members.items()}
                    self._sync(group, members)
                    if owner not in group["owners"]:
                        group["owners"].append(owner)
            first = next(iter(contexts.values()))
            return {
                "kind": first["kind"],
                "continuing": first["continuing"],
                "full_context": full_context,
                "has_prior_turns": group["turn"] > 1,
                "workspace": str(self.root),
                "repositories": {name: str(self.root / name) for name in members},
                **{
                    key: [
                        {**stats, "path": f"{name}/{stats['path']}"}
                        for name, context in contexts.items()
                        for stats in context[key]
                    ]
                    for key in ("human_stats", "reviewed_stats")
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
        with self._locked(name, suspended=True) as (state, members):
            if state["active"]:
                raise RelayError("A Relay session is already active; suspend or finish it first.")
            group = self._select(state, name, suspended=True)
            self._validate(group, members)
            for relay in members.values():
                if relay.active(relay.store.load(), required=False):
                    raise RelayError("A workspace member has another active session.")
                relay.git.assert_clean()
            with self._transaction(state, group, members, "resume"):
                for member, relay in members.items():
                    relay.resume(group["members"][member]["id"])
                self._sync(group, members)
                state["active"] = group["id"]
            return copy.deepcopy(group)

    def _finish_preflight(self, group: dict, members: dict[str, Relay]) -> dict[str, bool]:
        self._validate(group, members)
        changes = {}
        for name, relay in members.items():
            session = relay.active(relay.store.load())
            approved = relay.git.index_tree()
            if relay.has_unstaged_changes(relay.workspace_tree(session), approved):
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
                descriptions = {name: group["messages"].get(name, value) for name in members}
                if all(description is not None for description in descriptions.values()):
                    results = {}
                    with self._transaction(state, group, members, "finish"):
                        for name, relay in members.items():
                            if changes[name]:
                                branch = relay.finish(descriptions[name])
                                results[name] = {
                                    "branch": branch,
                                    "commit": relay.git.resolve("HEAD"),
                                }
                            else:
                                session = relay.active(relay.store.load())
                                origin = relay.git.available_origin(session["origin"])
                                # Ignored historical captures are no longer project files;
                                # leave them in place when returning an unchanged member.
                                relay.git.materialize(
                                    relay.git.index_tree(), relay.git.tree(origin["commit"])
                                )
                                relay.git.set_head(origin)
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
                    name: relay.abort(group["members"][name]["id"])
                    for name, relay in members.items()
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
            journal = self.journal()
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
            suspended = [g["id"] for g in state["sessions"].values() if self._visible(g)]
            return {
                "active": False,
                "lifecycle": "suspended" if suspended else "inactive",
                "suspended_sessions": sorted(suspended),
                "workspace": str(self.root),
            }
        with self._locked() as (state, members):
            group = self.active(state)
            self._validate(group, members)
            statuses = {
                name: {**relay.status(), "path": str(self.root / name)}
                for name, relay in members.items()
            }
            return {
                "active": True,
                "session": group["id"],
                "message": group["message"],
                "workspace": str(self.root),
                "repositories": statuses,
                "lifecycle": group["state"],
                **{key: group[key] for key in ("phase", "turn", "turn_kind", "next_turn")},
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
                    for key in ("available", "newly_reviewed", "human_edits")
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
                selected = self.repository(repo, self.active(state))
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


def _workspaces(cwd: Path | str) -> list[Workspace]:
    location = Path(cwd).resolve()
    result = []
    for root in (location, *location.parents):
        workspace = Workspace(root, location)
        if workspace.store.journal_path.exists():
            if workspace._visible(workspace.journal()):
                result.append(workspace)
        elif workspace.store.path.exists():
            state = workspace.store.load()
            if location == root or any(workspace._visible(g) for g in state["sessions"].values()):
                result.append(workspace)
    return result


def find_workspace(cwd: Path | str) -> Workspace | None:
    candidates = _workspaces(cwd)
    for workspace in candidates:
        if workspace.store.journal_path.exists() or workspace.active(
            workspace.store.load(), required=False
        ):
            return workspace
    return next(iter(candidates), None)


def discover(cwd: Path | str = ".", *, required: bool = True) -> Relay | Workspace | None:
    workspace = find_workspace(cwd)
    if workspace is not None and (
        workspace.store.journal_path.exists()
        or workspace.active(workspace.store.load(), required=False)
    ):
        return workspace
    try:
        local = Relay(Git(cwd))
    except RelayError:
        if workspace is not None:
            return workspace
        if required:
            raise
        return None
    if local.active(local.store.load(), required=False):
        return local
    return workspace or local


def session_sources(cwd: Path | str = ".") -> list[tuple[Relay | Workspace, dict]]:
    """Find logical sessions without exposing their per-worktree implementation records."""
    result = []
    managed = set()
    for workspace in _workspaces(cwd):
        for group in workspace.list_sessions():
            result.append((workspace, group))
            managed.update(member["id"] for member in group["members"].values())
    try:
        local = Relay(Git(cwd))
    except RelayError:
        return result
    result.extend((local, session) for session in local.list_sessions()
                  if session["id"] not in managed)
    return result


def select_session(
    cwd: Path | str, name: str | None, *, suspended: bool = False,
) -> tuple[Relay | Workspace, str]:
    sources = session_sources(cwd)
    if name is None and not suspended:
        active = [(relay, s) for relay, s in sources if s["state"] == "active"]
        if active:
            sources = active
    if suspended or name is None:
        sources = [(relay, s) for relay, s in sources if s["state"] == "suspended"
                   or (not suspended and s["state"] == "active")]
    matches = [(relay, s) for relay, s in sources if name is None or s["id"].startswith(name)]
    if len(matches) != 1:
        if not matches:
            raise RelayError("No matching Relay session. Use `relay list`.")
        label = "Multiple suspended sessions: " if suspended else "Multiple matching sessions: "
        raise RelayError(label + ", ".join(s["id"] for _, s in matches)
                         + ". Use a session ID from `relay list`.")
    relay, session = matches[0]
    return relay, session["id"]


def add_repository(path: str, cwd: Path | str = ".") -> tuple[Workspace, dict]:
    relay = discover(cwd)
    relay.store.assert_ready()
    session = relay.active(relay.store.load())
    if session["phase"] != "human":
        raise RelayError("Repository additions require the human phase. Wait for Agent Stop.")
    if isinstance(relay, Workspace):
        return relay, relay.add(path)
    # Resolve the common directory without changing either process's working directory.
    target = Path(cwd).resolve() / path
    workspace = Workspace(Path(os.path.commonpath([relay.git.root, target.resolve()])), cwd)
    workspace._parse_members([str(relay.git.root), path], argument="repo add")
    return workspace, workspace.add(path, original=relay)
