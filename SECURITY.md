# Security policy

Security fixes target the latest released version of Agent-Session-Relay.

Report vulnerabilities using
[GitHub private vulnerability reporting](https://github.com/Vopaaz/Agent-Session-Relay/security/advisories/new).
If private reporting is unavailable, contact a maintainer privately through their GitHub profile.
Do not put exploit details or private repository contents in a public issue. Include the Relay, Git,
Python, operating system, and Kiro versions, impact, and a reproduction in a disposable repository.

Relay is a local workflow tool. It does not send repository contents, prompts, or metadata to a remote
service. Its Kiro adapter does not store prompt text. Hooks send the mode-specific protocol and,
on normal turns, human-changed file names to the active agent. Inspection output may become part
of that agent's conversation as with ordinary code tools.

Snapshots and recovery branches contain project code, including non-ignored untracked files. Git
ignore rules determine which unrelated untracked files are excluded; already tracked files remain
included. Git attributes and clean/smudge filters retain their ordinary Git meaning. Use Relay in
repositories whose Git configuration and filters you trust.

The Git command guard prevents ordinary accidental direct Git use by an agent. It is **not** an OS
sandbox: executable scripts, dynamically generated programs, custom aliases, renamed binaries, and
unrecognized MCP tools can execute Git indirectly. Use the harness's actual permission/sandbox
controls for hostile code. Never grant a guard a broader security role than its tested command parser.

Btw's write guard is also best effort: it recognizes common writing tools and commands, not arbitrary
program behavior. The Stop fallback saves unexpected captured project changes before restoring the
entry workspace/index. It does not restore unrelated ignored files, files outside the repository,
or external side effects. Saved btw output is session-internal and is deleted at finish/abort.

Deleting internal refs removes Relay's ability to resume; Git may retain unreachable objects and
reflog entries until its normal garbage collection. Abort intentionally retains a normal recovery
branch. Relay never runs `git gc`, force-pushes, or automatically deletes that recovery branch.
