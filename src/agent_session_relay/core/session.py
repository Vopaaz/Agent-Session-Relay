"""The Relay lifecycle and semantic baselines, independent of any agent harness."""

from __future__ import annotations

import copy
import uuid
from collections.abc import Callable
from datetime import datetime, timezone

from .errors import RelayError
from .git import Git
from .storage import Store


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Relay:
    def __init__(self, git: Git):
        self.git = git
        self.store = Store(git)

    @staticmethod
    def prefix(session: dict) -> str:
        return f"refs/relay/sessions/{session['id']}/"

    def active(self, state: dict, *, required: bool = True) -> dict | None:
        session = state["sessions"].get(state["active"])
        if session and session["state"] == "active":
            return session
        if required:
            raise RelayError("No active Relay session. Use `relay start` or `relay resume`.")
        return None

    def validate(self, session: dict) -> None:
        self.git.assert_stable()
        head = self.git.head()
        if head["ref"] is not None or head["commit"] != session["reviewed"]:
            raise RelayError(
                "HEAD changed outside Relay. Preserve your work, then restore the "
                f"managed detached HEAD ({session['reviewed']}). "
                "Use `relay suspend` before normal Git history operations."
            )

    def snapshot(self, session: dict, label: str, tree: str) -> str:
        commit = self.git.commit(tree, session["base_commit"], f"Relay {session['id']}: {label}")
        self.git.pin(self.prefix(session) + label, commit)
        return commit

    @staticmethod
    def validate_message(message: str) -> str:
        message = message.strip()
        if not message:
            raise RelayError("Provide a non-empty, custom message describing this session's work.")
        return message

    def session_message(self, session: dict) -> str | None:
        ref = self.prefix(session) + "message"
        commit = self.git.refs(ref).get(ref)
        return self.git.commit_message(commit) if commit else None

    def write_message(self, session: dict, message: str) -> None:
        # A single atomic ref update is the entire mutation: HEAD, index, and state stay intact.
        commit = self.git.commit(
            self.git.tree(session["base_commit"]), session["base_commit"], message
        )
        self.git.pin(self.prefix(session) + "message", commit)

    def set_message(
        self,
        message: str | None = None,
        name: str | None = None,
        *,
        editor: Callable[[str | None], str] | None = None,
    ) -> dict:
        with self.store.lock():
            self.store.assert_ready()
            state = self.store.load()
            if name is None:
                session = self.active(state, required=False) or self.select_suspended(state, None)
            else:
                candidates = [s for s in state["sessions"].values() if s["id"].startswith(name)]
                if len(candidates) != 1:
                    raise RelayError(
                        "Session ID must match exactly one session. Use `relay list`."
                    )
                session = candidates[0]
            if message is not None:
                message = self.validate_message(message)
                self.write_message(session, message)
                return {**session, "message": message}
            if editor is None:
                raise RelayError("Provide a session message or a message editor.")
            initial = self.session_message(session)

        # Interactive input must not block status, hooks, or other Relay commands.
        message = self.validate_message(editor(initial))
        with self.store.lock():
            self.store.assert_ready()
            current = self.store.load()["sessions"].get(session["id"])
            if current is None or self.session_message(current) != initial:
                raise RelayError(
                    "Message editing cancelled: the session ended or its message changed "
                    "while the editor was open. Retry with the current session."
                )
            self.write_message(current, message)
            return {**current, "message": message}

    def list_sessions(self) -> list[dict]:
        self.store.assert_ready()
        if not self.store.load()["sessions"]:
            return []
        with self.store.lock():
            self.store.assert_ready()
            return [
                {**session, "message": self.session_message(session)}
                for session in self.store.load()["sessions"].values()
            ]

    def start(self, message: str | None = None) -> dict:
        with self.store.lock():
            self.store.assert_ready()
            state = self.store.load()
            if self.active(state, required=False):
                raise RelayError("A Relay session is already active; suspend or finish it first.")
            self.git.assert_clean()
            try:
                origin = self.git.head()
            except RelayError as exc:
                raise RelayError("Create an initial Git commit before starting Relay.") from exc
            sid = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]
            if message is not None:
                message = self.validate_message(message)
            session = {
                "id": sid,
                "state": "active",
                "base_commit": origin["commit"],
                "origin": origin,
                "reviewed": origin["commit"],
                "created_at": now(),
                "phase": "human",
                "turn": 0,
                "turns": [],
                "last_post": origin["commit"],
            }
            with self.store.transaction(state, sid, "start", self.git.tree(origin["commit"])):
                self.git.pin(self.prefix(session) + "base", origin["commit"], create=True)
                self.git.pin(self.prefix(session) + "reviewed", origin["commit"], create=True)
                if message is not None:
                    self.write_message(session, message)
                self.git.set_head({"ref": None, "commit": origin["commit"]})
                state["sessions"][sid] = session
                state["active"] = sid
                self.store.save(state)
            return {**copy.deepcopy(session), "message": message}

    def handoff(self, owner: str | None = None) -> bool:
        # Inactive hooks must not even create the Relay directory or its lock file.
        if not self.active(self.store.load(), required=False):
            return False
        with self.store.lock():
            self.store.assert_ready()
            state = self.store.load()
            session = self.active(state, required=False)
            if not session:
                return False
            self.validate(session)
            if session["phase"] == "agent":
                previous_owner = session["turns"][-1].get("owner")
                if owner and previous_owner and owner != previous_owner:
                    raise RelayError(
                        "Another Kiro conversation owns the open agent turn. "
                        "Wait for its Agent Stop hook before starting a new turn."
                    )
                # Prompt retries / hooks installed at both scopes must not erase provenance.
                return True
            workspace = self.git.workspace_tree(session["reviewed"])
            approved = self.git.index_tree()
            previous_reviewed = session["reviewed"]
            with self.store.transaction(state, session["id"], "handoff", workspace):
                if approved != self.git.tree(previous_reviewed):
                    session["reviewed"] = self.git.commit(
                        approved, previous_reviewed, f"Relay {session['id']}: reviewed checkpoint"
                    )
                    self.git.pin(self.prefix(session) + "reviewed", session["reviewed"])
                self.git.set_head({"ref": None, "commit": session["reviewed"]})
                self.git.run("read-tree", session["reviewed"])
                session["turn"] += 1
                pre = self.snapshot(session, f"turn-{session['turn']:06d}-pre", workspace)
                session["turns"].append(
                    {
                        "number": session["turn"],
                        "started_at": now(),
                        "owner": owner,
                        "reviewed_from": previous_reviewed,
                        "reviewed_to": session["reviewed"],
                        "human_from": session["last_post"],
                        "pre": pre,
                        "post": None,
                    }
                )
                session["phase"] = "agent"
                self.store.save(state)
            return True

    def stop(self, owner: str | None = None) -> bool:
        if not self.active(self.store.load(), required=False):
            return False
        with self.store.lock():
            self.store.assert_ready()
            state = self.store.load()
            session = self.active(state, required=False)
            if not session:
                return False
            self.validate(session)
            if session["phase"] != "agent":
                return True
            turn = session["turns"][-1]
            if owner and turn.get("owner") and owner != turn["owner"]:
                raise RelayError("This Agent Stop belongs to a different Kiro conversation.")
            workspace = self.git.workspace_tree(session["reviewed"])
            with self.store.transaction(state, session["id"], "agent-stop", workspace):
                post = self.snapshot(session, f"turn-{session['turn']:06d}-post", workspace)
                turn.update({"post": post, "stopped_at": now()})
                session["last_post"] = post
                session["phase"] = "human"
                # Agent output is always a proposal, including anything it accidentally staged.
                self.git.run("read-tree", session["reviewed"])
                self.store.save(state)
            return True

    def suspend(self) -> tuple[dict, dict]:
        with self.store.lock():
            self.store.assert_ready()
            state = self.store.load()
            session = self.active(state)
            self.validate(session)
            workspace = self.git.workspace_tree(session["reviewed"])
            origin = self.git.available_origin(session["origin"])
            with self.store.transaction(state, session["id"], "suspend", workspace):
                session["suspended_workspace"] = self.snapshot(
                    session, "suspend-workspace", workspace
                )
                session["suspended_index_tree"] = self.git.index_tree()
                index = self.git.text(
                    "hash-object", "-w", "--stdin", data=self.git.normalized_index()
                )
                self.git.pin(self.prefix(session) + "suspend-index", index)
                self.git.pin(
                    self.prefix(session) + "suspend-intent-to-add",
                    self.git.text("hash-object", "-w", "--stdin", data=b""),
                )
                # Pin the staged tree too: raw index blobs do not make staged objects GC-reachable.
                self.snapshot(session, "suspend-staged", session["suspended_index_tree"])
                session["suspended_index"] = index
                self.git.materialize(workspace, self.git.tree(origin["commit"]))
                self.git.set_head(origin)
                session["state"] = "suspended"
                session["suspended_at"] = now()
                state["active"] = None
                self.store.save(state)
            return copy.deepcopy(session), origin

    def select_suspended(self, state: dict, name: str | None) -> dict:
        candidates = [
            s
            for s in state["sessions"].values()
            if s["state"] == "suspended" and (name is None or s["id"].startswith(name))
        ]
        if not candidates:
            raise RelayError("No matching suspended session. Use `relay list`.")
        if len(candidates) != 1:
            choices = ", ".join(s["id"] for s in candidates)
            raise RelayError(
                f"Multiple suspended sessions: {choices}. Use `relay resume <session>`."
            )
        return candidates[0]

    def resume(self, name: str | None = None) -> dict:
        with self.store.lock():
            self.store.assert_ready()
            state = self.store.load()
            if self.active(state, required=False):
                raise RelayError("A Relay session is already active; suspend or finish it first.")
            session = self.select_suspended(state, name)
            self.git.assert_clean()
            workspace = self.git.workspace_tree()
            with self.store.transaction(state, session["id"], "resume", workspace):
                self.git.materialize(workspace, self.git.tree(session["suspended_workspace"]))
                self.git.set_head({"ref": None, "commit": session["reviewed"]})
                self.git.restore_index(
                    self.git.run("cat-file", "blob", session["suspended_index"]).stdout
                )
                session["state"] = "active"
                state["active"] = session["id"]
                self.store.save(state)
            return copy.deepcopy(session)

    def finish(
        self, message: str | None = None, *, editor: Callable[[str | None], str] | None = None
    ) -> str:
        expected_session = None
        # An editor may stay open for a long time. Release the lock while it runs, then
        # repeat the full preflight with the captured session identity before committing.
        while True:
            with self.store.lock():
                self.store.assert_ready()
                state = self.store.load()
                session = self.active(state, required=False)
                if expected_session is not None and (
                    session != expected_session or self.session_message(session) is not None
                ):
                    raise RelayError(
                        "Finish cancelled: the session or its message changed while editing. "
                        "Review the current session before retrying."
                    )
                session = self.active(state)
                self.validate(session)
                workspace = self.git.workspace_tree(session["reviewed"])
                approved = self.git.index_tree()
                if workspace != approved:
                    if expected_session is not None:
                        raise RelayError(
                            "Finish cancelled: pending changes appeared while editing the message. "
                            "Review and stage them before retrying."
                        )
                    raise RelayError(
                        "Pending changes remain. Review and stage (or discard) all unstaged "
                        "changes and non-ignored untracked files before `relay finish`."
                    )
                if message is None:
                    message = self.session_message(session)
                if message is not None:
                    message = self.validate_message(message)
                    branch = f"relay/result/{session['id']}"
                    ref = "refs/heads/" + branch
                    # Public history has exactly one parent and no internal checkpoints.
                    commit = self.git.commit(
                        approved, session["base_commit"], message, internal=False
                    )
                    with self.store.transaction(
                        state, session["id"], "finish", workspace
                    ) as journal:
                        if self.git.refs(ref).get(ref):
                            raise RelayError(
                                f"Result branch already exists: {branch}. It was not overwritten."
                            )
                        self.store.record_created_ref(journal, ref, commit)
                        self.git.pin(ref, commit, create=True)
                        self.git.set_head({"ref": ref, "commit": commit})
                        self.git.run("read-tree", commit)
                        self.remove_session(state, session)
                    return branch
                if editor is None:
                    raise RelayError(
                        "A custom session message is required before finishing. "
                        'Use `relay message -m "Describe the work"` or '
                        '`relay finish -m "Describe the work"`.'
                    )
                # Fail before asking the human to write if their public identity is unavailable.
                self.git.text("var", "GIT_AUTHOR_IDENT")
                self.git.text("var", "GIT_COMMITTER_IDENT")
                expected_session = copy.deepcopy(session)
            message = self.validate_message(editor(None))

    def abort(self, expected_id: str) -> tuple[str, dict]:
        """The CLI collects one explicit confirmation before calling this mutation."""
        with self.store.lock():
            self.store.assert_ready()
            state = self.store.load()
            session = self.active(state)
            if session["id"] != expected_id:
                raise RelayError(
                    "The active session changed during confirmation; run `relay abort` again."
                )
            self.validate(session)
            workspace = self.git.workspace_tree(session["reviewed"])
            branch = f"relay/aborted/{session['id']}"
            ref = "refs/heads/" + branch
            origin = self.git.available_origin(session["origin"])
            # Recovery must work even if no public Git identity has been configured.
            commit = self.git.commit(
                workspace, session["base_commit"], f"Preserve aborted Relay session {session['id']}"
            )
            with self.store.transaction(state, session["id"], "abort", workspace) as journal:
                existing = self.git.refs(ref).get(ref)
                if existing:
                    parents = self.git.text("rev-list", "--parents", "-n", "1", existing).split()[
                        1:
                    ]
                    if parents != [session["base_commit"]] or self.git.tree(existing) != workspace:
                        raise RelayError(
                            f"Recovery branch already exists: {branch}. It was not overwritten. "
                            "Keep it by renaming that branch before retrying abort."
                        )
                    # A previous failed cleanup may already have saved this exact workspace.
                else:
                    self.store.record_created_ref(journal, ref, commit, preserve=True)
                    self.git.pin(ref, commit, create=True)
                self.git.materialize(workspace, self.git.tree(origin["commit"]))
                self.git.set_head(origin)
                self.remove_session(state, session)
            return branch, origin

    def remove_session(self, state: dict, session: dict) -> None:
        del state["sessions"][session["id"]]
        state["active"] = None
        self.store.save(state)
        self.git.delete_refs(self.git.refs(self.prefix(session)))

    def status(self) -> dict:
        self.store.assert_ready()
        state = self.store.load()
        session = self.active(state, required=False)
        if not session:
            return {
                "active": False,
                "lifecycle": "suspended" if state["sessions"] else "inactive",
                "suspended_sessions": sorted(state["sessions"]),
            }
        with self.store.lock():
            state = self.store.load()
            session = self.active(state)
            self.validate(session)
            workspace = self.git.workspace_tree(session["reviewed"])
            index = self.git.index_tree()
            reviewed = self.git.tree(session["reviewed"])
            turn = session["turns"][-1] if session["turns"] else None
            return {
                "active": True,
                "session": session["id"],
                "message": self.session_message(session),
                "lifecycle": session["state"],
                "phase": session["phase"],
                "turn": session["turn"],
                "base_commit": session["base_commit"],
                "origin": session["origin"],
                "reviewed_checkpoint": session["reviewed"],
                "pending_changes": workspace != reviewed,
                "staged_approvals": index != reviewed,
                "unstaged_changes": workspace != index,
                "provenance": {
                    "available": turn is not None,
                    "newly_reviewed": bool(
                        turn
                        and self.git.tree(turn["reviewed_from"])
                        != self.git.tree(turn["reviewed_to"])
                    ),
                    "human_edits": bool(
                        turn and self.git.tree(turn["human_from"]) != self.git.tree(turn["pre"])
                    ),
                },
                "inspection_commands": [
                    "relay agent status",
                    "relay agent diff reviewed",
                    "relay agent diff human",
                    "relay agent diff pending",
                ],
                "diff_options": ["--name-only", "-- path/to/file [path/to/other]"],
            }

    def diff(
        self, kind: str, paths: list[str], *, name_only: bool = False, null: bool = False
    ) -> bytes:
        if not self.active(self.store.load(), required=False):
            raise RelayError("No active Relay session. Use `relay start` or `relay resume`.")
        with self.store.lock():
            self.store.assert_ready()
            session = self.active(self.store.load())
            self.validate(session)
            if kind == "pending":
                left = session["reviewed"]
                right = self.git.workspace_tree(left)
            elif session["turns"]:
                turn = session["turns"][-1]
                if kind == "reviewed":
                    left, right = turn["reviewed_from"], turn["reviewed_to"]
                else:
                    left, right = turn["human_from"], turn["pre"]
            else:
                left = right = session["reviewed"]
            return self.git.diff(left, right, paths, name_only=name_only, null=null)
