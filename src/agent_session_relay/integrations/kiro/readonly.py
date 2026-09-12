"""Small best-effort btw guard. Snapshots handle writes these rules do not recognize."""

import shlex

GUIDANCE = (
    "This is a read-only btw turn. Do not modify project files or staging, or retry "
    "the write through another tool. If edits are needed, ask for a normal turn."
)

WRITE_TOOLS = {
    "write", "fswrite", "writefile", "createfile", "edit", "editfile", "multiedit",
    "applypatch", "deletefile", "fsdelete", "movefile", "renamefile", "strreplace",
}
WRITE_COMMANDS = {
    "rm", "mv", "cp", "mkdir", "rmdir", "touch", "tee", "truncate", "install",
    "chmod", "chown", "ln", "patch",
}


def write_tool(name: str) -> bool:
    name = name.lower().rsplit("/", 1)[-1].rsplit("__", 1)[-1]
    return name.replace("_", "").replace("-", "") in WRITE_TOOLS


def obvious_write(command: str) -> bool:
    # Keep quotes so literal strings such as `echo '>'` are not mistaken for redirection.
    lexer = shlex.shlex(command, posix=False, punctuation_chars=";&|()<>\n")
    lexer.whitespace = " \t\r"
    try:
        tokens = list(lexer)
    except ValueError:
        return False  # The ordinary Git guard handles malformed shell syntax.
    statements, words = [], []
    for token in tokens:
        if token and all(c in "<>&|" for c in token) and ">" in token:
            return True
        if token and all(c in ";&|()\n" for c in token):
            statements.append(words)
            words = []
        else:
            words.append(token.strip("\"'"))
    statements.append(words)
    for words in statements:
        if not words:
            continue
        executable = words[0].replace("\\", "/").rsplit("/", 1)[-1]
        args = words[1:]
        if executable in WRITE_COMMANDS:
            return True
        if executable in ("sed", "perl") and any(
            arg.startswith("-i") or arg.startswith("--in-place") for arg in args
        ):
            return True
        if executable == "relay" and args[:2] == ["agent", "restore-btw"]:
            return True
    return False
