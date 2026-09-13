# Agent-Session-Relay — Implementation Requirements

## 1. Project

Canonical project name:

**Agent-Session-Relay**

CLI:

```bash
relay
```

GitHub repository、README、release 等公开名称统一使用 **Agent-Session-Relay**。需要 lowercase package name 时可使用 `agent-session-relay`。

Agent-Session-Relay 是一个基于 Git 的 human/agent collaborative coding workflow，用于解决多轮 Agent 修改中的 review 状态管理：

* Agent 修改代码；
* 用户通过任意标准 Git UI review；
* 部分修改认可；
* 部分修改由用户手动调整；
* 部分修改留给 Agent 下一轮继续处理；
* Agent 可以继续修改任何代码，包括此前已经 review 过的代码；
* 用户不需要反复向 Agent 解释 staged / unstaged / committed changes 的来源和含义。

Relay 不提供自己的 code review UI。Git staging UI 就是 human review surface。

---

# 2. Core model

Relay session 有三个主要状态。

### Reviewed checkpoint

截至当前用户已经 review 并认可的代码状态。

这是 **soft approval**，不是保护或锁定。

Agent 后续可以再次修改任何 reviewed code；新的 delta 会重新成为 pending review。

### Staged changes

表示：

> 用户在当前 human turn 中刚刚 review 并认可、但尚未吸收到 reviewed checkpoint 的修改。

用户使用正常 Git UI：

* Stage file → 当前文件修改认可；
* Stage selected hunks → 对应 hunks 认可；
* Unstage → 撤销本轮认可。

下一次 human → agent handoff 时，Relay 自动把 staged changes 吸收到新的 reviewed checkpoint。

### Unstaged changes

表示：

> 当前仍需要 review 的 proposal。

可能来自：

* 上一轮 Agent；
* 用户自己手动编辑；
* Agent 对此前 reviewed code 的再次修改。

用户手动编辑同样只是 soft proposal，不 authoritative。

---

# 3. Git model

Git 是 Relay 的状态存储和 diff engine。

Relay 可以使用 internal commits / trees / refs 保存：

* immutable session base commit；
* reviewed checkpoints；
* 每轮 Agent 的 pre/post snapshots；
* suspend 时的 index/worktree；
* provenance metadata。

这些内部 history 不进入用户最终 Git history。

```text
Internal Relay state
  many checkpoints / snapshots
              ↓
relay finish
              ↓
Result branch
  exactly one public commit
```

---

# 4. Commands

Human-facing commands:

```bash
relay start
relay status
relay suspend
relay resume
relay finish
relay abort
relay list
```

如果存在多个 suspended sessions：

```bash
relay resume <session>
```

常规单 session workflow 不应要求用户管理 session ID。

Agent-facing commands:

```bash
relay agent status

relay agent diff reviewed
relay agent diff human
relay agent diff pending
```

Diff commands 支持按 path 限制：

```bash
relay agent diff pending -- Parser.kt
relay agent diff human -- Parser.kt Config.kt
```

同时支持只返回涉及的文件名，而不输出 patch：

```bash
relay agent diff reviewed --name-only
relay agent diff human --name-only
relay agent diff pending --name-only
```

这些命令是 Agent 在 active Relay session 中理解 Git/review state 的标准接口。

---

# 5. `relay start`

只能从完全 clean、稳定的 Git workspace 开始。

如果存在：

* staged changes；
* unstaged changes；
* relevant untracked files；
* merge in progress；
* rebase in progress；
* cherry-pick / revert 等未完成 Git operation；

则直接拒绝开始。

Relay 记录：

```text
base_commit
origin branch/ref
session identity
```

`base_commit` 在整个 session 生命周期中 immutable。

例如：

```text
A --- B --- C
          ↑
      relay start
```

则：

```text
base_commit = C
```

后续其他 branches/mainline 如何变化，都不会改变它。

---

# 6. Human → Agent handoff

普通 turn 的 Kiro `Prompt Submit` hook 是 review handoff boundary。

一次性只读 btw turn 的规则见第 22 节。

如果没有 active Relay session，或者 Relay 当前 suspended：

```text
No-op.
No stdout.
No context injection.
No Git changes.
```

因此安装 Relay 不影响普通 Agent conversations。

如果存在 active Relay session：

### 1. Seal staged approvals

handoff 前：

```text
reviewed checkpoint

staged:
A

unstaged:
B
C
```

Relay 将 A 吸收到新的 internal reviewed checkpoint。

handoff 后：

```text
reviewed checkpoint includes A

index clean

unstaged:
B
C
```

尚未 review 的内容继续保持 pending。

### 2. Snapshot pre-agent state

保存当前完整 workspace。

### 3. Derive provenance

Relay 保存足够的信息，使 Agent 后续可以通过 Relay 查询：

* 用户从上一轮以来新 review 通过的 patch；
* 用户从上一轮以来直接编辑的 patch；
* 当前仍 pending 的完整 patch；
* 各类 patch 涉及的文件列表。

普通 turn 存在 human 改动时，必须自动注入完整的 human 文件列表，并明确说明有人类修改。
完整 patches 仍由 Agent 按需通过 `relay agent ...` 查询，不默认注入。

### 4. Inject Relay protocol

Prompt Submit hook 每次 active Relay handoff 都应注入一段相对稳定、自包含的 Relay 使用说明。

除普通 turn 自动注入 human 文件列表外，协议应以简洁、自包含的说明让此前不了解 Relay 的 Agent 理解：

* 当前 workspace 正处于 Relay session；
* Relay 是什么；
* reviewed / staged / pending 的含义；
* user edits 是 soft proposals；
* reviewed code 仍然允许再次修改；
* Agent 不应直接使用 Git；
* 如何通过 `relay agent` 自己查询当前 review/provenance state 和具体 diff。

例如：

```text
[Agent-Session-Relay active]

This workspace is managed by Agent-Session-Relay.

Relay is a Git-based human/agent review workflow. The user reviews your
changes through normal Git staging:

- Reviewed checkpoint: code the user previously reviewed and accepted.
  It is not locked; you may modify it again when needed.
- Staged changes are changes the user reviewed during the current human turn.
  Relay incorporates them into the reviewed checkpoint at handoff.
- Pending changes are changes that still require human review.
- User-authored edits are soft proposals and directional signals, not
  authoritative or immutable changes.

Do not invoke Git directly while this Relay session is active.
Relay owns the Git/session state used by this workflow.

Inspect the current review state through Relay:

  relay agent status

  relay agent diff reviewed
      Changes newly reviewed by the user since your previous turn.

  relay agent diff human
      Changes made directly by the user since your previous turn.

  relay agent diff pending
      The complete currently-unreviewed delta.

Use --name-only when you only need a file overview:

  relay agent diff reviewed --name-only
  relay agent diff human --name-only
  relay agent diff pending --name-only

Limit a diff to paths when useful:

  relay agent diff pending -- path/to/file

Use these commands as needed to understand the user's review decisions
before making further changes.
```

具体 wording 可以改进，但每轮都应该提供足够信息，使一个完全不了解 Relay 的 Agent 可以仅凭 hook injection 正确使用它。

不再依赖单独的 Agent Skill。

---

# 7. Agent-facing Relay inspection

## `relay agent status`

提供 Agent-readable 的 session summary。

它应概览：

* 当前 Relay session；
* 当前 lifecycle state；
* reviewed checkpoint；
* 是否存在 pending changes；
* 是否存在本轮相关 human/review provenance；
* Agent 可用的 Relay inspection commands。

它不需要默认输出完整 patch。

---

## `relay agent diff reviewed`

显示：

> 从上一轮 Agent handoff 到当前 handoff，用户新 review 通过的具体 patch。

必须支持 partial-file / partial-hunk review。

例如 `Parser.kt` 只有两个 hunks 被用户 stage，Agent 应能够查看被认可的具体 hunks，而不只是知道：

```text
Parser.kt was partially reviewed
```

只需要文件概览时：

```bash
relay agent diff reviewed --name-only
```

---

## `relay agent diff human`

显示：

> 上一轮普通 Agent Stop（初始为 session base）之后，截至本轮 pre 快照的人类修改。

Btw 不推进此起点；查询不会消费改动。

它表达 provenance 和用户方向性提示，但不意味着这些内容不可修改。

只需要文件概览时：

```bash
relay agent diff human --name-only
```

---

## `relay agent diff pending`

显示：

> 当前 reviewed checkpoint 与当前 workspace 之间仍未 review 的完整 delta。

这是 Agent 当前最重要的 unresolved view。

只需要文件概览时：

```bash
relay agent diff pending --name-only
```

---

所有 diff commands 都应支持 path filtering，例如：

```bash
relay agent diff pending -- Parser.kt
relay agent diff reviewed -- src/parser/
```

这样 Agent 可以先：

```bash
relay agent diff pending --name-only
```

了解范围，然后只读取与当前任务相关的具体 diff。

---

# 8. Agent and Git

在 active Relay session 中：

> Human interacts with Git.
> Agent interacts with Relay.
> Relay interacts with Git internally.

Agent 不直接执行 Git commands。

包括 mutation：

```text
git add
git reset
git commit
git checkout
git switch
git restore
git stash
git rebase
git merge
...
```

也包括用于理解 Relay state 的直接 Git inspection：

```text
git status
git diff
git diff --cached
git log
...
```

原因是 Relay 的 semantic baselines 不等同于普通 Git HEAD/index/worktree terminology。

Agent 如果需要 review/session 状态或 diff，使用：

```bash
relay agent status
relay agent diff ...
```

Kiro integration 应在 active Relay session 中通过 tool hook 阻止 Agent 直接执行 Git commands，并引导 Agent 使用相应 Relay command。

如果 Relay inactive 或 suspended，这项 guard 完全不生效。

普通 Agent workflow 中，Agent 仍然可以正常使用 Git。

Relay 自己内部执行 Git 不受此限制。

### 只读 Git 透传窗口

Agent 可用 `relay agent git <subcommand> <args...>` 查询当前 session 之外的历史。
当前 session 的改动优先通过 `relay agent diff` 理解；注入词简短提示不要将 Relay-managed
refs、branches 或 commits 当作普通项目历史解读，因为其中包含内部记账信息。
直接 Git 调用仍然拦截；normal 与 btw turn 都可使用这个只读入口。

只允许明确识别的只读命令与参数组合，未知形式默认拒绝。混合读写子命令只开放查询形式，
拒绝写入选项、alias、自定义扩展与未经允许的全局选项。关闭 pager、external diff/textconv、
自动刷新 index 等附带行为。只读校验集中在命令内部，不依赖某一个 harness 的 hook。
不检查参数是否引用 Relay-managed commit/ref/branch，也不因此拒绝查询。
保留 Git 参数、当前目录、标准输入输出和退出码；支持范围见 `docs/agent-git.md`。

---

# 9. Agent behavior

普通 turn 的 Agent 可以自由修改任意 project code；btw turn 只读（第 22 节）：

* pending code；
* user-edited code；
* reviewed code；
* 与 prompt 相关的其他已有代码。

Reviewed 只意味着：

> 用户此前认可了那个版本。

如果 Agent 因 rename、refactor、cross-file consistency 等再次修改 reviewed code，新 delta 重新进入 pending review。

Relay 不阻止这种修改。

---

# 10. Agent → Human

普通 turn 的 Kiro `Agent Stop` hook：

1. 保存完整 post-agent snapshot；
2. 更新本轮 provenance；
3. 保持 Agent 新产生的修改作为正常 unstaged/pending Git changes。

用户随后继续通过标准 Git UI review。

---

# 11. Human review operations

### 完全正确

Stage file。

### 部分正确

只 stage 正确的 hunks。

### 用户希望自己调整

直接编辑代码，保持 unstaged。

### 希望 Agent 下一轮继续修改

保持 unstaged，并在下一条 prompt 中描述要求。

### 完全不要某项修改

使用正常 Git discard/revert UI。

Relay 不需要额外 reject command。

---

# 12. README complete workflow example

英文和中文 README 都必须给出完整 multi-turn example。

例如从：

```text
main
A --- B --- C
```

开始：

```bash
relay start
```

用户发送：

```text
Refactor the parser and move token configuration into the new config model.
```

Prompt Submit hook 向 Agent 介绍当前 Relay session、Relay review semantics，以及如何使用：

```bash
relay agent status
relay agent diff reviewed
relay agent diff human
relay agent diff pending
```

Agent 修改：

```text
Parser.kt
Token.kt
Config.kt
README.md
```

Agent Stop hook 保存 post-agent snapshot。

四个文件成为 pending changes。

---

用户 review。

### Token.kt

完全正确：

```text
Stage Token.kt
```

### Parser.kt

部分正确。

用户：

* stage 正确 hunks；
* 手动编辑另一个 hunk；
* 手动编辑部分保持 unstaged。

### Config.kt

方向不对，希望 Agent 下一轮重做。

保持 unstaged。

### README.md

完全不需要。

使用 Git UI discard。

此时：

```text
Staged:
  Token.kt
  selected Parser.kt hunks

Changes:
  Parser.kt
  Config.kt
```

---

用户发送：

```text
Keep the direction I used in Parser.kt, but simplify it further.
Rework Config.kt using the existing configuration abstraction.

Also rename TokenDefinition to ParsedToken across the implementation.
```

Prompt Submit hook：

1. 将 Token.kt 和 staged Parser hunks吸收到 reviewed checkpoint；
2. 清空 index；
3. 保留其余 Parser.kt / Config.kt pending changes；
4. 保存 pre-agent snapshot；
5. 保存本轮 review/human-edit provenance；
6. 再次向 Agent 注入 Relay protocol 和 `relay agent` 使用方式。

普通 turn 存在 human 改动时，hook 自动注入 human 文件列表；完整 diff 仍按需查询。

Agent 可以先自行查询：

```bash
relay agent diff reviewed --name-only
relay agent diff human --name-only
relay agent diff pending --name-only
```

假设输出让 Agent 知道：

```text
reviewed:
  Token.kt
  Parser.kt

human:
  Parser.kt

pending:
  Parser.kt
  Config.kt
```

如果 Agent 想理解 Parser.kt 中用户具体认可了什么：

```bash
relay agent diff reviewed -- Parser.kt
```

想查看用户亲自修改的部分：

```bash
relay agent diff human -- Parser.kt
```

想看当前 unresolved Parser 状态：

```bash
relay agent diff pending -- Parser.kt
```

Agent 根据 rename 要求再次修改此前已经 reviewed 的 `Token.kt`。

这是正常行为。

新的 Token.kt delta 再次成为 pending review。

Agent Stop hook 保存新的 post snapshot。

---

用户最终完成 review：

```text
Stage all remaining changes
```

此时：

```text
Unstaged: none
Staged: final reviewed delta
```

然后：

```bash
relay finish
```

---

# 13. `relay finish`

`finish` 表示整个 session 已完成 human review。

仍存在 unstaged/pending changes 时不能 finish。

当前 staged changes可以在 finish 时直接纳入最终 reviewed state。

每个 session 可以由用户填写本次工作的说明：

* `relay start` 默认直接开始，不进入编辑器；可用 `-m "说明"` 设置；
* `relay message [session] [-m "说明"]` 在过程中设置或修改；没有 `-m` 时打开 Git 编辑器，也支持 suspended session；
* `relay finish -m "最终说明"` 可以覆盖此前保存的说明。

说明正文使用 session 私有 Git ref 指向的原生 commit message 存储，
不另建说明文件，也不在状态 JSON 中重复保存。`status` 和 `list` 应显示说明以便辨认 session。

`finish` 必须使用非空、自定义的 message；空白说明或未修改的模板不能结束 session。
若此前已经填写，直接 `relay finish` 使用最近保存的说明；否则没有 `-m` 时自动打开 Git 编辑器。
编辑器优先预填已有说明；没有时使用 `commit.template`。遵循 Git configured editor 和环境变量的选择规则。
空白、未修改的模板或编辑器失败都会取消本次操作并保留 session。三个命令的多个 `-m` 参数按独立段落拼接。
完整的标题和正文成为最终唯一公开 commit 的 commit message，结束后可以通过普通 Git history 查看。

假设：

```text
A --- B --- C
          ↑
        base
```

内部可能存在很多 checkpoints/snapshots，但最终只生成：

```text
A --- B --- C
           \
            R
```

其中：

```text
R.parent = C
R.tree   = final reviewed tree
```

创建明确命名的 result branch，例如：

```text
relay/result/<session>
```

对外只有一个 Relay result commit。

Relay finish 不：

* rebase；
* merge mainline；
* cherry-pick；
* 自动吸收 session 期间其他 branches 的 commits。

用户之后自行：

```bash
git rebase <current-mainline>
```

或使用普通 Git merge/cherry-pick。

---

# 14. Suspend / Resume

```bash
relay suspend
```

保存完整 session state，包括：

* reviewed checkpoint；
* staged changes；
* unstaged changes；
* index/worktree distinction；
* turn metadata；
* provenance snapshots。

然后离开 managed Relay workspace，回到正常 Git workspace。

Relay suspended 时 hooks 和 Git guard 都处于 inactive 状态。

用户可以正常：

```bash
git switch -c urgent-fix
# fix
git commit
```

之后：

```bash
relay resume
```

恢复 suspend 时完整 review UI state。

例如 suspend 前：

```text
Staged:
  Foo.kt

Changes:
  Bar.kt
  Baz.kt
```

resume 后必须恢复相同的 staged/unstaged distinction。

Suspend 期间其他 Git history 变化不会改变 immutable `base_commit`。

---

# 15. `relay abort`

`abort` 表示：

> 终止当前 Relay workflow，并清除 Relay 的 managed intermediate session state。

这是一个高影响操作，因为它会删除 Relay 用于继续 session 的 checkpoints、snapshots 和 provenance。

因此 `relay abort` 必须在实际执行前向用户进行一次明确警告/确认。

警告应清楚表达：

1. Relay session 将不能继续 resume；
2. Relay intermediate review/provenance state 将被清除；
3. 当前代码不会直接丢失；
4. Relay 会先创建一个 recovery branch 保存 abort 瞬间的完整 workspace 内容。

不要让第一次普通的 `relay abort` invocation 在没有明确确认的情况下立即完成 destructive cleanup。

---

## Abort recovery branch

在真正清理 Relay session 前，Relay 创建类似：

```text
relay/aborted/<session>
```

或：

```text
relay-aborted-<session>
```

的 recovery branch。

该 branch 包含一个 commit，其 tree 是用户执行 `abort` 瞬间看到的**完整 workspace code state**。

也就是说，无论某项内容当时属于：

* previously reviewed checkpoint；
* staged；
* unstaged；
* Agent-created；
* user-created；
* newly created/untracked project files；

都合并成 recovery commit 中的最终文件 tree。

Abort recovery branch 不需要保留 staged/unstaged distinction。

它的目的只是：

> 即使用户误操作 abort，当前所有代码仍然至少存在于一个普通 Git commit 中。

例如：

```text
A --- B --- C
           \
            X    relay/aborted/session-123
```

其中：

```text
X.parent = session.base_commit
X.tree   = complete workspace tree at abort time
```

然后 Relay：

1. 创建 recovery commit/branch；
2. 清除该 Relay session 的 internal checkpoints/snapshots/provenance；
3. 回到 session 开始前的普通 Git workspace；
4. 明确告诉用户 recovery branch 的名字。

输出应清楚告诉用户：

```text
Relay session aborted.

Your workspace at the time of abort was preserved at:

  relay/aborted/session-123

Relay's intermediate review/provenance state has been removed.

If you are certain you no longer need the recovery snapshot, delete it
later with normal Git, for example:

  git branch -D relay/aborted/session-123
```

Relay 自己不自动删除 recovery branch。

---

# 16. Kiro integration

Kiro 是第一版完整支持的 agent harness。

Architecture：

```text
Relay Core
    session state
    Git snapshots/checkpoints
    provenance
    agent-facing semantic diff/status

Kiro Adapter
    lifecycle hooks
    Git command guard
    installation
    Relay protocol injection
```

不需要单独的 Agent Skill。

至少使用：

```text
Prompt Submit
Agent Stop
Pre Tool Use for shell/Git guard
```

所有 hooks 首先判断当前 workspace 是否存在 **ACTIVE** Relay session。

如果没有：

```text
exit successfully
stdout empty
no side effects
```

---

# 17. Relay protocol injection

Agent-Session-Relay 是一个新工具，不能假设 Agent 已通过训练了解它。

因此 active Relay session 的 Prompt Submit hook 应当在每轮稳定注入 Relay protocol。

这段 context 应优先解释：

* Relay 是什么；
* human review 如何映射到 Git staging；
* reviewed/pending/user-edit 的语义；
* Agent 可以重新修改 reviewed/user-edited code；
* 为什么 Agent 不应直接使用 Git；
* `relay agent status`；
* `relay agent diff reviewed`；
* `relay agent diff human`；
* `relay agent diff pending`；
* `--name-only`；
* path filtering。

普通 turn 有 human 改动时，自动注入完整 human 文件列表，包含编辑、discard、增删文件。
Btw 不主动注入此列表，保留查询入口。任何模式均不默认注入完整 patch。
协议只解释 Relay 特有语义与必要命令，不添加通用 Agent 行为指导或重复的模式说明。

Agent 应根据当前任务自己决定：

1. 是否需要查看状态；
2. 先看哪些文件发生了什么类型的变化；
3. 是否需要进一步读取 hunk-level diff。

这使 Agent-Session-Relay 的交互方式更接近一个 Agent 可以主动查询的 source-control protocol，而不是每轮被动接收一个预生成 diff summary。

---

# 18. Other harnesses

Core 不依赖 Kiro。

预留类似：

```text
integrations/
  kiro/
  codex/
  claude-code/
  ...
```

不同 harness 自己适配：

* prompt/turn lifecycle；
* tool guards；
* Relay protocol injection。

Git/session/provenance/semantic diff implementation 共享。

第一版只需要保证 Kiro 完整工作。

---

# 19. Installation

至少支持：

```bash
relay kiro install --global
relay kiro install --project
```

### `--global`

安装 global hooks。

没有 active Relay session 时所有 Relay hooks/guards 都 inert，因此普通 Kiro interaction 不受影响。

### `--project`

将 Relay integration 安装到当前 repository 的 project configuration，方便随项目共享。

README 解释两种安装实际修改的位置和 scope。

---

# 20. Open-source repository

Canonical repository name：

**Agent-Session-Relay**

至少包含：

```text
README.md
README.zh-CN.md
LICENSE
CONTRIBUTING.md
CODE_OF_CONDUCT.md
SECURITY.md
.github/
```

推荐 Apache License 2.0。

`README.md` 为英文主 README。

`README.zh-CN.md` 为完整中文 README。

顶部互相链接。

README 至少包括：

1. 项目解决的问题；
2. mental model；
3. installation；
4. human-facing Relay commands；
5. agent-facing Relay commands；
6. Kiro integration；
7. 完整 multi-turn partial-review example；
8. Agent 如何使用 `--name-only` → targeted diff 的例子；
9. suspend/resume；
10. finish/result branch；
11. abort recovery branch 和 single-confirm behavior；
12. 如何将 result rebase/merge 到当前开发 branch；
13. architecture overview；
14. supported agent harnesses。

代码组织明确隔离：

```text
Relay core
agent-harness integrations
documentation
```

---

# 21. Product principles

```text
Git staging = human review UI.

Staged = reviewed during this human turn.

Reviewed checkpoint = accepted so far, but editable later.

Unstaged = pending review.

User edits = soft proposals.

Agent may modify any project code.

Human uses Git.
Agent uses Relay.
Relay uses Git internally.

The hook teaches the Agent how Relay works on every active turn.

The Agent queries review/provenance state through Relay as needed.

No active Relay session = Relay integration is invisible.

relay finish = one clean result branch containing one commit
whose parent is the immutable session base commit.

relay abort = terminate Relay state, but preserve the complete current
workspace in a one-commit recovery branch before cleanup.
```

Agent-Session-Relay 是 Git 上的一层 human/agent session abstraction，而不是新的 source control system 或 code review UI。


---

# 22. One-turn btw

`relay btw` 将下一次 prompt 设为只读插问，`relay btw --cancel` 取消尚未开始的选择。
Prompt Submit 成功开启本轮时消费选择；重复 hook 沿用本轮模式，下一条新 prompt 默认普通模式。
`relay status` 显示当前和下一轮模式。Relay 不解释自然语言 prompt 来自动决定模式。

Btw 在 Prompt Submit 保存完整 workspace 和 index，保留 HEAD、reviewed checkpoint、staged approvals。
Human diff 固定为上次普通 Agent Stop 到本轮入口的变化；reviewed diff 为空，pending 保持实时。
Btw 的查询和 Stop 均不消费 review 进度，下一次普通 turn 整体接收跨越 btw 的人类修改。
人类与 Agent 的非只读操作在时间上严格隔离，不额外实现并发操作检测。

只读采用三层机制：简洁的模式协议、PreToolUse 对已知写工具和明显写命令的 best-effort 拦截、
Stop 时的快照校验。拦截不扩展为通用 shell 副作用分析器，不追求穷尽覆盖。
如果请求需要实施，Agent 应说明本轮只读并请用户发送新的普通 prompt，不能自行切换模式。

Stop 无变化时不重写工作区或 index；有变化时先保存 workspace 和完整 index，再恢复入口状态。
只恢复已捕获的项目范围，不承诺恢复无关 ignored 文件或外部副作用。失败处理复用既有事务机制，
以正常工作和代码可维护性优先，不为极小概率场景增加大量防御逻辑。

`relay agent diff btw --turn N [--staged]` 查看保存的 workspace 或 index delta。
`relay agent restore-btw N [--staged]` 仅能在普通 agent turn、handoff 之后取回，修改保持 unstaged，
不把取回内容误算为人类修改。无法应用时不覆盖当前代码，交由 Agent 查看并适配 patch。
恢复记录全部属于 session，suspend/resume 保留，finish/abort 统一清理，不增加长期恢复分支。

不提供跨版本活动 session 的 backward compatibility，不编写旧格式迁移或兼容读取。
注入词使用不继承开发对话上下文的子代理评估和精简，迭代不超过 5 轮。
