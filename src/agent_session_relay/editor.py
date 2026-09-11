"""Human-facing commit-message editing using Git's editor and template configuration."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from .core.errors import RelayError
from .core.git import Git


def edit_message(git: Git, initial: str | None) -> str:
    editor = git.text("var", "GIT_EDITOR")
    contents = initial or ""
    if initial is None:
        template = git.text("config", "--path", "--get", "commit.template", check=False)
        if template:
            try:
                contents = (git.root / template).read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                raise RelayError(f"Cannot read UTF-8 Git commit template: {template}") from exc

    mode = git.text("config", "--get", "commit.cleanup", check=False) or "default"
    if mode == "default":
        mode = "strip"
    if mode not in ("strip", "whitespace", "verbatim", "scissors"):
        raise RelayError(f"Invalid commit.cleanup mode: {mode}")

    # Ask Git to choose the configured comment prefix, including newer core.commentString.
    options = []
    if git.text("config", "--get", "core.commentChar", check=False) == "auto" and not git.text(
        "config", "--get", "core.commentString", check=False
    ):
        # stripspace alone does not auto-select a character based on the initial message.
        for char in "#;@!$%^&|:":
            if not any(line.startswith(char) for line in contents.splitlines()):
                options = ["-c", f"core.commentChar={char}"]
                break
        else:
            raise RelayError("Cannot choose a comment character; configure core.commentChar.")
    comment = (
        git.run(*options, "stripspace", "--comment-lines", data=b"\n")
        .stdout.decode("utf-8")
        .rstrip("\n")
    )
    scissors = comment + " ------------------------ >8 ------------------------"

    def clean(text: str) -> str:
        if mode == "scissors":
            lines = text.splitlines(keepends=True)
            boundary = next(
                (i for i, line in enumerate(lines) if line.rstrip("\r\n") == scissors), len(lines)
            )
            text = "".join(lines[:boundary])
        if mode == "verbatim":
            return text
        flags = ["--strip-comments"] if mode == "strip" else []
        return git.run(*options, "stripspace", *flags, data=text.encode("utf-8")).stdout.decode(
            "utf-8"
        )

    draft = contents.rstrip("\n") + "\n"
    if mode in ("strip", "scissors"):
        hints = git.run(
            *options,
            "stripspace",
            "--comment-lines",
            data=(
                b"Describe this Relay session's work: a short subject, then an optional body.\n"
                b"Save and close to accept. An empty message cancels.\n"
            ),
        ).stdout.decode("utf-8")
        draft += "\n" + (scissors + "\n" if mode == "scissors" else "") + hints

    # COMMIT_EDITMSG enables normal editor filetype detection. It is only an editing buffer;
    # accepted messages live in Git commits, and the repository's own COMMIT_EDITMSG is untouched.
    with tempfile.TemporaryDirectory(prefix="relay-message-", dir=git.git_dir) as directory:
        path = Path(directory) / "COMMIT_EDITMSG"
        path.write_text(draft, encoding="utf-8")
        # GIT_EDITOR is a shell command by Git's contract (e.g. 'code --wait'). Only that
        # user-configured command is evaluated; the file path is passed as a separate argument.
        result = subprocess.run(
            ["/bin/sh", "-c", editor + ' "$@"', "relay-editor", str(path)], cwd=git.root
        )
        if result.returncode:
            raise RelayError("Message editing cancelled: the Git editor exited unsuccessfully.")
        try:
            message = clean(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError) as exc:
            raise RelayError("Message editing cancelled: cannot read the UTF-8 message.") from exc

    if not message.strip():
        raise RelayError("Message editing cancelled: the message is empty.")
    if initial is None and message.strip() == clean(contents).strip():
        raise RelayError("Message editing cancelled: the commit template was not changed.")
    return message
