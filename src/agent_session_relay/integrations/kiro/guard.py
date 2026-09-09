"""A workflow guard for ordinary shell invocations, not an OS security sandbox.

Recognizes executable positions, wrappers, shell substitutions, and common interpreter
subprocess calls. Mere mentions in echo/grep/file-writing arguments are not executions.
"""

from __future__ import annotations

import re
import shlex

GUIDANCE = (
    "Agent-Session-Relay is active. Direct Git commands (including inspection) and "
    "human-only Relay lifecycle commands are blocked. Use `relay agent status`, "
    "`relay agent diff reviewed`, `relay agent diff human`, or "
    "`relay agent diff pending` (with --name-only or -- paths as needed)."
)
SHELL_TOOLS = {
    "shell",
    "bash",
    "execute_bash",
    "execute_cmd",
    "execute_command",
    "exec_command",
    "run_command",
    "run_commands",
    "executecommand",
    "runshellcommand",
    "terminal",
}
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z_0-9]*=")
GIT = re.compile(r"^git(?:\.exe|-[a-z0-9_-]+)?$", re.I)


def basename(value: str) -> str:
    return value.replace("\\", "/").rsplit("/", 1)[-1].lower()


def substitutions(command: str, *, literal_quotes: bool = True):
    """Yield executable $() and backtick contents, respecting literal single quotes."""
    quote = None
    i = 0
    while i < len(command):
        char = command[i]
        if char == "\\" and quote != "'":
            i += 2
            continue
        if literal_quotes and char == "'" and quote != '"':
            quote = None if quote == "'" else "'"
        elif literal_quotes and char == '"' and quote != "'":
            quote = None if quote == '"' else '"'
        elif quote != "'" and command.startswith("$(", i):
            start, depth, j, inner_quote = i + 2, 1, i + 2, None
            while j < len(command) and depth:
                c = command[j]
                if c == "\\" and inner_quote != "'":
                    j += 2
                    continue
                if c in "\"'":
                    if inner_quote == c:
                        inner_quote = None
                    elif inner_quote is None:
                        inner_quote = c
                elif inner_quote is None:
                    if c == "(":
                        depth += 1
                    elif c == ")":
                        depth -= 1
                j += 1
            yield command[start : j - 1] if depth == 0 else command[start:]
            i = j
            continue
        elif quote != "'" and char == "`":
            j = i + 1
            while j < len(command):
                if command[j] == "\\":
                    j += 2
                elif command[j] == "`":
                    break
                else:
                    j += 1
            yield command[i + 1 : j]
            i = j + 1
            continue
        i += 1


def _heredocs(command: str, depth: int, variables: dict) -> tuple[str, bool]:
    """Remove stdin data from executable syntax; inspect it when it is actually evaluated."""
    lines = command.splitlines(keepends=True)
    result = []
    i = 0
    shells = {"sh", "bash", "zsh", "dash", "ksh", "fish"}
    while i < len(lines):
        header = lines[i]
        result.append(header)
        i += 1
        try:
            lexer = shlex.shlex(header, posix=True, punctuation_chars="<>|;&")
            words = list(lexer)
        except ValueError:
            continue
        documents = [(n, words[n + 1]) for n, word in enumerate(words[:-1]) if word == "<<"]
        for position, delimiter in documents:
            strip_tabs = delimiter.startswith("-")
            if strip_tabs:
                delimiter = delimiter[1:]
            body = []
            while i < len(lines):
                line = lines[i].rstrip("\r\n")
                if strip_tabs:
                    line = line.lstrip("\t")
                if line == delimiter:
                    break
                body.append(lines[i])
                i += 1
            if i == len(lines):
                return command, True
            i += 1
            text = "".join(body)
            quoted = bool(re.search(r"<<-?\s*['\"]" + re.escape(delimiter), header))
            if not quoted and any(
                shell_violation(part, depth + 1, variables)
                for part in substitutions(text, literal_quotes=False)
            ):
                return command, True
            # Shell/interpreter stdin and a pipeline into one execute the body as code.
            executables = [basename(word) for word in words[:position] + words[position + 2 :]]
            if any(word in shells for word in executables):
                if shell_violation(text, depth + 1, variables):
                    return command, True
            elif any(word.startswith(("python", "node", "ruby", "perl")) for word in executables):
                interpreter = next(
                    word
                    for word in executables
                    if word.startswith(("python", "node", "ruby", "perl"))
                )
                if shell_violation(interpreter + " -c " + shlex.quote(text), depth + 1, variables):
                    return command, True
    return "".join(result), False


def shell_violation(command: str, depth: int = 0, variables: dict | None = None) -> bool:
    if depth > 12:
        return True
    variables = dict(variables or {})
    command, blocked = _heredocs(command, depth, variables)
    if blocked:
        return True
    if any(shell_violation(part, depth + 1, variables) for part in substitutions(command)):
        return True
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()<>\n")
    lexer.whitespace = " \t\r"
    lexer.wordchars += "${}"
    try:
        tokens = list(lexer)
    except ValueError:
        # Malformed/unsupported shell syntax cannot be reliably inspected.
        return True
    statements, current = [], []
    for token in tokens:
        if token and all(c in ";&|()\n" for c in token):
            statements.append(current)
            current = []
        else:
            current.append(token)
    statements.append(current)
    for words in statements:
        if _statement_violation(words, variables, depth):
            return True
    return False


def _statement_violation(words: list[str], variables: dict, depth: int) -> bool:
    words = list(words)
    while words:
        first = words[0]
        if ASSIGNMENT.match(first):
            key, value = first.split("=", 1)
            variables[key] = value
            words.pop(0)
        elif first in ("if", "then", "elif", "else", "do", "!", "{", "}"):
            words.pop(0)
        elif first and all(c in "<>" for c in first):
            words = words[2:]
        else:
            break
    if not words:
        return False
    command = words[0]
    if command.startswith("$"):
        command = variables.get(command[1:].strip("{}"), command)
    executable = basename(command)
    args = words[1:]
    if GIT.fullmatch(executable):
        return True
    if executable in ("relay", "relay.pyz"):
        return not args or args[0] not in ("agent", "--help", "--version")
    if executable in ("command", "builtin", "exec", "nohup", "time"):
        while args and args[0].startswith("-"):
            args.pop(0)
        return _statement_violation(args, variables, depth)
    if executable in ("env", "sudo", "xargs"):
        takes_value = (
            {"-u", "--unset", "-C", "--chdir", "-S", "--split-string"}
            if executable == "env"
            else (
                {
                    "-u",
                    "-g",
                    "-h",
                    "-p",
                    "-C",
                    "-T",
                    "-R",
                    "-D",
                    "--user",
                    "--group",
                    "--host",
                    "--prompt",
                    "--chdir",
                    "--chroot",
                }
                if executable == "sudo"
                else {
                    "-I",
                    "-i",
                    "-n",
                    "--max-args",
                    "-P",
                    "--max-procs",
                    "-d",
                    "--delimiter",
                    "-s",
                    "-E",
                }
            )
        )
        while args and (args[0].startswith("-") or ASSIGNMENT.match(args[0])):
            arg = args.pop(0)
            if executable == "env" and arg in ("-S", "--split-string") and args:
                return shell_violation(" ".join(args), depth + 1, variables)
            if arg in takes_value and args:
                args.pop(0)
        return _statement_violation(args, variables, depth)
    if executable in ("sh", "bash", "zsh", "dash", "ksh", "fish", "pwsh", "powershell", "cmd"):
        for i, arg in enumerate(args[:-1]):
            if (
                arg.startswith("-")
                and not arg.startswith("--")
                and "c" in arg[1:]
                or arg.lower() in ("/c", "-command", "--command")
            ):
                return shell_violation(args[i + 1], depth + 1, variables)
        return False
    if executable == "eval":
        return shell_violation(" ".join(args), depth + 1, variables)
    if executable == "find":
        return any(
            _statement_violation(args[i + 1 :], variables, depth)
            for i, arg in enumerate(args)
            if arg in ("-exec", "-execdir", "-ok", "-okdir")
        )
    if executable.startswith(("python", "node", "ruby", "perl")):
        script = " ".join(args)
        if "agent_session_relay" in script and "-m" in args and "agent" not in args:
            return True
        if any(
            word in script
            for word in ("subprocess", "os.system", "os.popen", "exec", "spawn", "system(")
        ):
            if re.search(r"['\"](?:[^'\"\s]+/)?git(?:\.exe)?(?:\s|['\"])", script):
                return True
    return False


def violation(payload: dict) -> str | None:
    tool = payload.get("tool_name", payload.get("toolName", ""))
    if not isinstance(tool, str) or not tool:
        return "Missing tool context; Relay cannot validate this tool invocation. " + GUIDANCE
    lower = tool.lower()
    if (
        lower.startswith(("@git/", "mcp__git__", "git_"))
        or lower == "git"
        or re.search(r"(?:/|__)git_(?:status|diff|log|add|commit|checkout|reset)", lower)
    ):
        return GUIDANCE
    tool_input = payload.get("tool_input", payload.get("toolInput", {}))
    is_shell = lower in SHELL_TOOLS or "shell" in lower or "bash" in lower
    if not is_shell:
        return None
    commands = []
    if isinstance(tool_input, dict):
        for key in ("command", "cmd", "commands", "script", "code"):
            value = tool_input.get(key)
            if isinstance(value, str):
                commands.append(value)
            elif isinstance(value, list):
                if not all(isinstance(part, str) for part in value):
                    return "Unrecognized shell command payload. " + GUIDANCE
                commands.extend(value)
    if not commands:
        return "Missing shell command payload. " + GUIDANCE
    return GUIDANCE if any(shell_violation(command) for command in commands) else None
