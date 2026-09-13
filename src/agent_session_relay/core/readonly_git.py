"""Audited Git query forms, independent of the harness and Relay ref names.

Unknown commands/options fail closed. This is a workflow guard, not a sandbox for
hostile repository configuration, filters, or replacement Git executables.
"""

from __future__ import annotations

import os
import re
import subprocess

from .errors import RelayError


def options(flags: str = "", values: str = "", optional: str = "") -> dict[str, str]:
    """Optional values must be attached; required values may also be separate."""
    return {
        **dict.fromkeys(flags.split(), "flag"),
        **dict.fromkeys(values.split(), "value"),
        **dict.fromkeys(optional.split(), "optional"),
    }


DIFF = options(
    "-p -u -s -w -b -z -R --patch --no-patch --raw --patch-with-raw --patch-with-stat "
    "--numstat --shortstat --summary --compact-summary --name-only --name-status "
    "--check --binary --full-index --no-color --no-renames --relative --text -a "
    "--ignore-space-at-eol --ignore-cr-at-eol --ignore-blank-lines --ignore-all-space "
    "--ignore-space-change --minimal --patience --histogram --no-indent-heuristic "
    "--indent-heuristic "
    "--exit-code --quiet --no-ext-diff --no-textconv --no-prefix --ita-invisible-in-index "
    "--ita-visible-in-index --pickaxe-all --pickaxe-regex --cumulative",
    "-U -S -G -I --unified --inter-hunk-context --diff-filter --diff-algorithm "
    "--word-diff-regex --src-prefix --dst-prefix --line-prefix --find-object "
    "--stat-width --stat-name-width --stat-count --stat-graph-width",
    "-M -C -B --find-renames --find-copies --break-rewrites --color --word-diff "
    "--color-words --stat --dirstat --dirstat-by-file --abbrev --ignore-submodules "
    "--submodule --relative",
)

REVISIONS = options(
    "--all --reflog --not --reverse --first-parent --exclude-first-parent-only "
    "--merges --no-merges --no-min-parents --no-max-parents --ancestry-path "
    "--topo-order --date-order --author-date-order --full-history --simplify-merges "
    "--simplify-by-decoration --dense --sparse --remove-empty --boundary "
    "--left-right --left-only --right-only --cherry --cherry-mark --cherry-pick "
    "--walk-reflogs --do-walk --all-match --invert-grep --regexp-ignore-case "
    "--extended-regexp --basic-regexp --fixed-strings --perl-regexp -i -E -F -P",
    "-n --max-count --skip --since --after --until --before --since-as-filter "
    "--author --committer --grep --grep-reflog --min-parents --max-parents --exclude --glob",
    "--branches --tags --remotes --no-walk",
)

LOG = DIFF | REVISIONS | options(
    "--oneline --graph --no-decorate --parents --children --abbrev-commit --no-abbrev-commit "
    "--show-notes --no-notes --show-linear-break --no-show-signature --follow -m -c --cc --root",
    "--format --date --encoding --decorate-refs --decorate-refs-exclude -L --diff-merges",
    "--pretty --decorate --notes --expand-tabs",
)

REFS = options(
    "--ignore-case --no-color",
    "--format --sort --points-at",
    "--contains --no-contains --merged --no-merged --color",
)

CONFIG = options(
    "--get --get-all --get-regexp --get-urlmatch --list -l --global --system --local "
    "--worktree --show-origin --show-scope --name-only -z --null --includes --no-includes "
    "--fixed-value --bool --int --bool-or-int --path --expiry-date",
    "--file -f --blob --type --default",
)

COMMANDS = {
    "log": LOG,
    "show": LOG,
    "diff": DIFF | options("--cached --staged --no-index"),
    "status": options(
        "-s --short -b --branch --show-stash -z --null --long -v --verbose "
        "--no-renames --renames --no-ahead-behind --ahead-behind",
        optional="--porcelain -u --untracked-files --ignore-submodules --column --find-renames",
    ),
    "blame": REVISIONS | options(
        "--root --show-stats --progress --no-progress --porcelain --line-porcelain "
        "--incremental --show-name --show-number --show-email --long -p -l -t -f -n -s -e -w",
        "-L --contents --date --encoding --ignore-rev --ignore-revs-file",
        "-M -C --abbrev",
    ),
    "grep": options(
        "--cached --no-index --untracked --no-exclude-standard --exclude-standard "
        "--recurse-submodules -a --text -I -i --ignore-case -w --word-regexp "
        "-v --invert-match -h -H --full-name -E --extended-regexp -G --basic-regexp "
        "-P --perl-regexp -F --fixed-strings -n --line-number --column "
        "-l --files-with-matches --name-only -L --files-without-match -z --null "
        "-o --only-matching -c --count --no-color --break --heading -q --quiet "
        "--all-match --and --or --not ( ) --no-textconv",
        "-e -f -A -B -C --after-context --before-context --context --max-depth --threads",
        "--color",
    ),
    "rev-list": REVISIONS | options(
        "--count --objects --objects-edge --objects-edge-aggressive --unpacked --header "
        "--timestamp --parents --children --quiet --use-bitmap-index --no-object-names",
        "--format --date --filter --missing",
        "--pretty --abbrev --disk-usage",
    ),
    "rev-parse": options(
        "--verify --quiet -q --short --symbolic --symbolic-full-name --abbrev-ref "
        "--all --not --revs-only --no-revs --flags --no-flags "
        "--show-toplevel --show-prefix --show-cdup --git-dir --absolute-git-dir "
        "--git-common-dir --is-inside-git-dir --is-inside-work-tree --is-bare-repository "
        "--is-shallow-repository --show-superproject-working-tree --local-env-vars",
        "--git-path --path-format --resolve-git-dir --since --after --until --before "
        "--glob --exclude --default",
        "--short --abbrev-ref --branches --tags --remotes --show-object-format",
    ),
    "merge-base": options("-a --all --octopus --independent --is-ancestor --fork-point"),
    "ls-tree": options(
        "-d -r -t -l -z --name-only --name-status --object-only --full-name --full-tree",
        "--format", "--abbrev",
    ),
    "ls-files": options(
        "-c --cached -d --deleted -m --modified -o --others -i --ignored -s --stage "
        "-u --unmerged -k --killed -z -t -v -f --eol --full-name --error-unmatch "
        "--exclude-standard --deduplicate --sparse",
        "-x --exclude -X --exclude-from --exclude-per-directory --with-tree --format",
        "--abbrev",
    ),
    "cat-file": options(
        "-t -s -e -p --batch-all-objects --buffer --unordered -z -Z",
        optional="--batch --batch-check",
    ),
    "show-ref": options("--head --heads --branches --tags --dereference -d --verify -q --quiet "
                        "--exists", optional="--hash -s --abbrev"),
    "for-each-ref": REFS | options(
        "--shell --perl --python --tcl --include-root-refs", "--count --exclude",
    ),
    "describe": options(
        "--all --tags --contains --always --long --exact-match --first-parent --debug",
        # --dirty refreshes/writes the real index even with GIT_OPTIONAL_LOCKS=0.
        "--abbrev --candidates --match --exclude",
    ),
    "shortlog": REVISIONS | options(
        "-n --numbered -s --summary -e --email --committer", "--group --format", "-w",
    ),
    "branch": REFS | options(
        "--list -l -a --all -r --remotes -v --verbose --show-current --no-abbrev "
        "--no-column --omit-empty", optional="--abbrev --column",
    ),
    "tag": REFS | options("--list -l --no-column --omit-empty", optional="-n --column"),
    "config": CONFIG,
    "symbolic-ref": options("--quiet -q --short --recurse --no-recurse"),
    "reflog": LOG,
    "worktree": options("--porcelain -v --verbose -z"),
    "remote": options("-v --verbose"),
    "count-objects": options("-v --verbose -H --human-readable"),
}

PATH_COMMANDS = {
    "log", "show", "diff", "status", "blame", "grep", "rev-list", "ls-tree", "ls-files",
}
NUMBER_COMMANDS = {"log", "show", "rev-list", "reflog"}


def reject(detail: str) -> None:
    raise RelayError(
        f"relay agent git: {detail}. Only supported read-only Git queries are allowed; "
        "use `relay agent git --help` for supported forms. "
        "Prefer `relay agent diff` for current-session changes."
    )


def check_value(command: str, name: str, value: str | None) -> None:
    # Signature formatting/sorting can execute a configured verifier. Check every
    # occurrence, including repeated sort keys. Named formats can hide placeholders.
    value = value or ""
    if name == "--diff-merges" and value not in {
        "off", "none", "on", "first-parent", "separate", "combined", "dense-combined",
    }:
        # Remerge diffs perform merges and can invoke configured merge drivers.
        reject("unsupported merge diff mode")
    if name == "--submodule" and value not in {"", "short", "log"}:
        reject("only short/log submodule summaries are supported")
    if name in {"--format", "--pretty", "--sort"}:
        if "%G" in value or re.search(r"%\(\*?signature(?:[:)])", value):
            reject("signature verification formats are unsupported")
        if name == "--sort" and value.lstrip("-*").startswith("signature"):
            reject("signature verification sorting is unsupported")
    if name in {"--pretty", "--format"} and command in {
        "log", "show", "reflog", "rev-list", "shortlog",
    }:
        if value and "%" not in value and not value.startswith(("format:", "tformat:")):
            if value not in {"oneline", "short", "medium", "full", "fuller", "reference",
                             "email", "mboxrd", "raw"}:
                reject("named pretty formats are unsupported; pass a literal format")


def parse_options(args: list[str], spec: dict[str, str], *, command: str, paths: bool):
    """Parse only exact audited options; never guess Git's long-option abbreviations."""
    seen, operands = {}, []
    i = 0
    while i < len(args):
        token = args[i]
        i += 1
        if token == "--":
            if not paths:
                reject("this query form does not support --")
            operands.extend(args[i:])
            break
        if not token.startswith("-") or token == "-":
            operands.append(token)
            continue
        if command in NUMBER_COMMANDS and re.fullmatch(r"-[0-9]+", token):
            continue
        if token.startswith("--"):
            name, equal, value = token.partition("=")
            kind = spec.get(name)
            if kind is None or equal and kind == "flag":
                reject(f"unsupported option {name!r}")
            pending = [(name, kind, value if equal else None)]
        else:
            pending = []
            for offset, char in enumerate(token[1:], 2):
                name = "-" + char
                kind = spec.get(name)
                if kind is None:
                    reject(f"unsupported option {token!r}")
                value = (token[offset:] or None) if kind != "flag" else None
                pending.append((name, kind, value))
                if kind != "flag":
                    break
        for name, kind, value in pending:
            if kind == "value" and value is None:
                if i == len(args) or args[i].startswith("-"):
                    reject(f"{name} requires a value (use {name}=VALUE for a leading dash)")
                value, i = args[i], i + 1
            if kind == "optional" and name.startswith("-") and not name.startswith("--"):
                if value is not None and not re.fullmatch(r"[0-9]+(?:,[0-9]+)*%?", value):
                    reject(f"unsupported attached value for {name}")
            check_value(command, name, value)
            seen[name] = value
    return seen, operands


def validate(args: list[str]) -> str:
    if not args or args[0] not in COMMANDS:
        reject("unsupported command; start with a supported Git subcommand")
    command, rest = args[0], args[1:]
    spec, mode = COMMANDS[command], None
    if command == "reflog":
        if rest and rest[0] in {"show", "exists"}:
            mode, rest = rest[0], rest[1:]
            if mode == "exists":
                spec = {}
        elif rest and not rest[0].startswith("-"):
            # Require explicit 'show' before a ref, avoiding other reflog subcommands.
            reject("use reflog show [ref] or reflog exists <ref>")
    elif command == "worktree":
        if not rest or rest[0] != "list":
            reject("only worktree list is supported")
        rest = rest[1:]
    elif command == "remote" and rest and rest[0] == "get-url":
        mode, rest, spec = "get-url", rest[1:], options("--all --push")
    elif command == "config" and rest and rest[0] in {"get", "list"}:
        mode, rest = rest[0], rest[1:]
        spec = CONFIG | options("--all --regexp", "--value --url")

    seen, operands = parse_options(
        rest, spec, command=command, paths=command in PATH_COMMANDS,
    )
    if command in {"branch", "tag"} and operands and not {"--list", "-l"} & seen.keys():
        reject(f"use {command} --list to query names or patterns")
    if command == "config":
        queries = {"--get", "--get-all", "--get-regexp", "--get-urlmatch", "--list", "-l"}
        selected = queries & seen.keys()
        if len(selected) + (mode is not None) != 1:
            reject("config requires one explicit get/list query mode")
        mode = mode or next(iter(selected))
        limits = (0, 0) if mode in {"list", "--list", "-l"} else (1, 2)
        if not limits[0] <= len(operands) <= limits[1]:
            reject("unsupported config query arguments")
    if command == "symbolic-ref" and len(operands) != 1:
        reject("symbolic-ref requires exactly one ref to read")
    if command == "worktree" and operands:
        reject("worktree list does not accept operands")
    if command == "remote" and len(operands) != (1 if mode == "get-url" else 0):
        reject("use remote [-v] or remote get-url [--all] [--push] <name>")
    if command == "reflog" and mode == "exists" and len(operands) != 1:
        reject("reflog exists requires exactly one ref")

    return command


def run(args: list[str]) -> int:
    command = validate(args)
    # Preserve config locations, but discard routing, command-config, tracing, and
    # execution overrides inherited from the caller. No shell is involved.
    keep = {"GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_NOSYSTEM"}
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("GIT_") or key in keep}
    env.update({
        "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0", "GIT_PAGER": "cat",
        "GIT_NO_LAZY_FETCH": "1", "GIT_ALLOW_PROTOCOL": "",
        "GIT_TRACE2": "0", "GIT_TRACE2_EVENT": "0", "GIT_TRACE2_PERF": "0",
    })
    # Config queries only read configuration; do not contaminate their results with
    # the execution overrides needed by other commands.
    settings = [] if command == "config" else [
        "core.hooksPath=" + os.devnull, "core.fsmonitor=false", "gc.auto=0",
        "maintenance.auto=false", "diff.autoRefreshIndex=false", "log.showSignature=false",
        "log.diffMerges=separate",
        "format.pretty=medium", "branch.sort=refname", "tag.sort=refname",
    ]
    invocation = ["git", "--no-pager"]
    for setting in settings:
        invocation.extend(["-c", setting])
    invocation.append(command)
    if command in {"log", "show", "diff"} or command == "reflog" and args[1:2] != ["exists"]:
        # 'reflog show' needs its subcommand before log options.
        if command == "reflog" and args[1:2] == ["show"]:
            invocation.append("show")
            args = [command, *args[2:]]
        invocation.extend(["--no-ext-diff", "--no-textconv"])
    invocation.extend(args[1:])
    try:
        result = subprocess.run(invocation, env=env, check=False)
    except FileNotFoundError as exc:
        raise RelayError("Git is required and must be on PATH.") from exc
    return result.returncode if result.returncode >= 0 else 128 - result.returncode
