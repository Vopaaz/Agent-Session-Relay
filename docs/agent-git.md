# Read-only Git queries for agents

Use `relay agent diff` for current-session changes. Use `relay agent git <command> <args...>`
to inspect history outside that session, for example:

```bash
relay agent git log --graph --oneline main
relay agent git show abc123:path/to/file
relay agent git diff main~2 main -- src/
relay agent git merge-base --is-ancestor abc123 main
relay agent git branch --list 'feature/*'
```

Relay-managed refs, branches, and commits include internal bookkeeping; avoid interpreting them
as ordinary project history. This is guidance, not a target restriction: queries may name any ref,
branch, or object, including Relay objects, HEAD, revision ranges, and `--all`.

## Supported forms

The command accepts an explicit allowlist of command/option combinations. Supported families are:

| Purpose | Commands/forms |
| --- | --- |
| History and patches | `log`, `show`, `diff`, `blame`, `shortlog` |
| Ancestry and revision resolution | `merge-base`, `rev-list`, `rev-parse`, `describe` |
| Files, objects, and content | `ls-tree`, `ls-files`, `cat-file`, `grep`, `count-objects` |
| Working tree query | `status` (prefer Relay status/diffs for session semantics) |
| Ref queries | `show-ref`, `for-each-ref`, `symbolic-ref <ref>` |
| Branches/tags | `branch`, `tag`; require `--list` or `-l` before supplying name patterns |
| Configuration | `config get/list`, or one of `--get`, `--get-all`, `--get-regexp`, `--get-urlmatch`, `--list` |
| Reflogs | `reflog`, `reflog show [ref]`, `reflog exists <ref>` |
| Worktrees | `worktree list` |
| Remote configuration | `remote [-v]`, `remote get-url [--all] [--push] <name>`; no network request |

Common formatting, revision filtering, and path options are supported, including `log -5`, `-n 5`,
`--graph`, `--oneline`, `--format='%h %s'`, diff `--stat`/`--name-only`, and literal arguments after
`--` in commands with path filters. Options must use their complete spelling; unrecognized options
and long-option abbreviations are rejected. Optional values use attached syntax, such as
`--contains=main`, `--porcelain=v2`, and `--untracked-files=all`.

Mutating forms, unknown commands, aliases/extensions, and Git global options (`-c`, `-C`,
`--git-dir`, etc.) are rejected. Some query-like forms also have side effects: `diff --output=file`
writes a file, `describe --dirty`/`--broken` may refresh the index, and remerge diffs can run merge
drivers. These forms, external diff/textconv options, signature-verification formats/sorts,
named pretty formats, and revision options read from stdin are unsupported. `cat-file --batch`
and `--batch-check` may read object queries from stdin.

## Execution behavior

Validation lives in the command itself and applies both with and without an active session,
including during btw. It does not create Relay state or require an active turn. Active-session
hooks continue to block direct Git calls and guide agents to this entry point.

Accepted arguments retain their order and Git syntax. Paths resolve from the caller's current
directory. Stdin, stdout, stderr, and exit status are passed through, including binary/NUL output
and statuses such as `diff --exit-code` returning 1. `relay agent git --help` describes Relay's
supported interface; Git's interactive help viewers are not launched.

Execution disables the pager, hooks, fsmonitor, optional index refresh, automatic maintenance,
external diff/textconv, implicit signature verification, and lazy network fetches. Environment
overrides for repository/index routing, command configuration, and tracing are removed; config
file locations are preserved. Built-in default pretty/merge-diff formats and ref sorting prevent
configuration from implicitly enabling unsupported query behavior.

This has the same trust boundary as the existing Relay workflow guard. Repository configuration,
ordinary clean filters, and the Git executable must be trusted; this is not an OS sandbox for
arbitrary programs. Shell output redirection remains the shell's responsibility (and is blocked
by the btw guard). See [the security policy](../SECURITY.md).
