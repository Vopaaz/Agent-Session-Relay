# Agent-Session-Relay

[English](README.md) · [贡献指南](CONTRIBUTING.md) · [架构说明](docs/architecture.md)

**使用熟悉的 Git staging UI，在多轮 Agent 协作中持续 review 代码。**

Agent 修改了四个文件，你认可其中一个，只 stage 另一个文件里正确的 hunks，亲手调整部分代码，
把剩余问题交给 Agent 下一轮继续。Agent-Session-Relay 记录哪些版本已经认可、哪些代码由你调整、
哪些修改仍需 review，让你不必每轮重新解释 staged / unstaged changes 的来源和意义。

命令行名称为 `relay`。Relay 提供 session 状态与语义 diff；review 界面仍然是你原来的 Git UI。

## 核心模型

| 状态 | 含义 | 操作方式 |
| --- | --- | --- |
| Reviewed checkpoint | 截至目前认可的版本，后续仍可修改 | 需要时继续改进 |
| Staged | 当前 human turn 刚认可的 hunks | Stage 文件或选中 hunks；unstage 撤销本轮认可 |
| Unstaged / 新文件 | 仍需 review 的 proposal | 直接编辑、交给 Agent 继续处理，或通过 Git discard |
| 用户手动编辑 | 表达方向的 soft proposals，保留轮次来源信息 | Review 通过前保持 unstaged |

Reviewed 是 **soft approval**，不是锁定。Agent 可以再次 rename、refactor 或修改已经认可的代码，
新 delta 会重新成为 pending review。用户亲手编辑的代码也可以继续调整，并不具有不可修改的权威性。

每次提交 prompt，Relay 自动将 staged approvals 吸收到内部 reviewed checkpoint，让 index 相对
新 checkpoint 保持 clean，并保留其余 proposals。Agent Stop 时保存输出快照，修改仍以正常
unstaged changes 或未跟踪新文件的形式显示。

**Human 使用 Git。Agent 使用 Relay。Relay 在内部使用 Git。**

## 安装

需要 **Python 3.10+**、**Git 2.37+**，以及 Linux、macOS 或 WSL。Python 运行时没有第三方依赖。
首个支持的 harness 是 Kiro IDE 1.x / CLI 3.x。

默认使用 [pipx](https://pipx.pypa.io/) 安装已发布的 CLI：

```bash
pipx install agent-session-relay
relay --version
```

以后可通过 `pipx upgrade agent-session-relay` 升级。如果 PATH 中还找不到 `relay`，请执行一次
`pipx ensurepath`，再打开新的终端。

也可以仅使用 Python 标准库，从仓库构建独立可执行文件：

```bash
git clone https://github.com/Vopaaz/Agent-Session-Relay.git
cd Agent-Session-Relay
python3 scripts/build_zipapp.py
mkdir -p ~/.local/bin
install -m 755 dist/relay.pyz ~/.local/bin/relay
export PATH="$HOME/.local/bin:$PATH"
relay --version
```

按需将 PATH 设置加入 shell 配置。该归档可在支持的平台之间使用，但仍依赖 PATH 中的 Python 和 Git。
构建同时生成 `dist/relay.pyz.sha256` 校验文件。

从源码 checkout 安装时，可执行 `pipx install .`，或在虚拟环境里执行 `python -m pip install .`。
开发时可直接使用 `./relay`。

Finish 前请配置 Git 的 `user.name` 和 `user.email`，最终公开 commit 使用你的正常 Git 身份。
内部快照和 abort recovery 在未配置该身份时也可以创建。

## Kiro 接入

选择一个安装范围：

```bash
relay kiro install --global
# 或者，在目标仓库中执行：
relay kiro install --project
```

| 范围 | 实际写入的文件 | 生效范围 |
| --- | --- | --- |
| Global | `~/.kiro/hooks/agent-session-relay.json` | 当前用户的各个 Kiro 项目 |
| Project | `<仓库>/.kiro/hooks/agent-session-relay.json` | 当前仓库；提交此文件即可共享 |

配置使用 Kiro 独立 `v1` hook 格式。项目级安装后，先提交 hook 文件，再执行 `relay start`，确保
工作区 clean。确认 **Kiro 进程的 PATH** 能找到 `relay`，然后打开新的 Kiro session。
安装不会改动其他 hook 文件，重复安装相同配置不会重写；覆盖手动定制过的 Relay 配置需要 `--force`。
删除这个专用文件即可卸载接入。

接入包含三个 hooks：

| 事件 | Relay 的操作 |
| --- | --- |
| Prompt Submit (`UserPromptSubmit`) | 吸收 staged approvals、保存 pre-agent 快照、记录 review/human provenance、注入 Relay 协议 |
| Agent Stop (`Stop`) | 保存本轮 post-agent 快照，让 Agent 的修改保持 pending |
| Pre Tool Use (`PreToolUse`) | 拦截 Agent 直接执行 Git，引导使用 `relay agent` |

每个 active handoff 都注入完整、自包含的协议，解释 review 语义、允许再次修改 reviewed/user-edited
code，以及所有查询命令。不需要单独安装 Agent Skill。具体文件列表与 patches 由 Agent 按需查询。

**没有 active session 时，包括 suspended 期间，所有 hooks 都成功静默退出，不修改 Git，也不注入
context。** 普通 Kiro 对话中的 Git 使用不受影响。

接入依据 Kiro 官方的 [hook schema](https://kiro.dev/docs/hooks/)、
[事件映射](https://kiro.dev/docs/cli/v3/hooks-migration/)、
[命令输入输出规则](https://kiro.dev/docs/hooks/actions/) 和
[配置范围](https://kiro.dev/docs/configuration/)。
详见 [Kiro 接入与实际验收步骤](docs/kiro.md)。

## Human-facing 命令

| 命令 | 效果 |
| --- | --- |
| `relay start [-m "message"]` | 从完全 clean、稳定的 Git 工作区开始，可选填写本次工作的说明 |
| `relay message [session] [-m "message"]` | 设置 session 说明；省略 `-m` 则打开 Git 编辑器 |
| `relay status` | 查看 session 说明、生命周期、reviewed checkpoint、staged approvals 和剩余 proposals |
| `relay suspend` | 保存完整 staging/workspace 状态，返回原分支 |
| `relay resume [session]` | 恢复唯一的 suspended session，或指定 ID/前缀 |
| `relay finish [-m "message"]` | 要求全部 review 完成和有效说明；尚无说明时打开编辑器，再创建唯一结果 commit |
| `relay abort` | 两次确认后保存 recovery branch，再终止并清理 session |
| `relay list` | 列出当前 worktree 的 active / suspended sessions 及各自说明的首行 |

`status` 和 `list` 还支持 `--json`。操作意外中断时可使用额外的 `relay recover`。
普通单 session 流程不需要管理 session ID。

Start 会拒绝 staged changes、unstaged changes、未忽略的 untracked files、未解决的 index conflicts，
以及尚未完成的 merge/rebase/cherry-pick/revert/bisect 等操作。仓库需要已有初始 commit。
Active 期间，用户使用 Git staging/discard；切分支、commit、stash 或修改 history 前请先 suspend。

## Session 说明与 commit message

Session 说明会成为最终唯一 commit 的 message，也会显示在 `status` 和 `list` 中。

```bash
relay start       # 直接开始，不打开编辑器
relay message     # 用 Git 编辑器填写或修改说明
relay finish      # 审阅完成后使用已有说明；尚未填写则打开编辑器
```

三个命令都可用 `-m "说明"` 直接设置；多个 `-m` 按段落拼接。
编辑器预填已有说明，并沿用 Git 的编辑器和 `commit.template` 配置。
空白说明、未修改的模板或取消编辑，都不会结束 session。

## Agent-facing 命令

```bash
relay agent status                         # JSON 概览，不默认输出完整 patch
relay agent diff reviewed                  # 最近 handoff 中刚认可的精确 hunks
relay agent diff human                     # 上一轮 Agent Stop → 当前 pre-agent 快照
relay agent diff pending                   # Reviewed checkpoint → 实时 workspace
```

所有 diff 都支持 `--name-only` 和 `--` 后的路径过滤：

```bash
relay agent diff reviewed --name-only
relay agent diff human --name-only
relay agent diff pending --name-only
relay agent diff reviewed -- Parser.kt
relay agent diff human -- Parser.kt Config.kt
relay agent diff pending -- src/parser/
```

路径相对于命令执行时所在目录，按字面路径处理。脚本可以用 `--name-only -z` 获取 NUL 分隔的文件名。
Patch 包含 binary changes；rename 以删除/新增显示，便于明确过滤路径。查询不会改变真实 index 或工作文件。

`reviewed` 和 `human` 固定在最近一次 handoff，Agent Stop 后也仍可查询；`pending` 始终实时更新。
第一次 handoff 前，前两个 diff 为空。Human diff 包含直接编辑以及 **discard**，因为两者都是 Agent
Stop 后的 workspace 变化。这只表达来源与方向，不表示那些行必须原样保留。下一次 handoff 前，
本轮 staged approvals 仍包含在相对旧 reviewed checkpoint 的 delta 中。

## 完整的多轮 partial-review 示例

开始时，`main` 指向 `C`：

```text
main
A --- B --- C
```

```bash
relay start
```

Session 的 immutable base 为 `C`。在 Kiro 中发送：

> Refactor the parser and move token configuration into the new config model.

Prompt Submit hook 向 Agent 介绍 Relay review 协议，以及 `relay agent status` 和 `reviewed`、
`human`、`pending` 三类 diff。Agent 修改 `Parser.kt`、`Token.kt`、`Config.kt`、`README.md`。
Agent Stop 保存输出快照，四个文件在普通 Git UI 中显示为 pending。

用户逐项 review：

| 文件 | 操作 |
| --- | --- |
| `Token.kt` | 完全正确，stage 整个文件 |
| `Parser.kt` | 只 stage 正确 hunks；手动调整另一个 hunk，手动部分保持 unstaged |
| `Config.kt` | 方向不对，保持 unstaged，让 Agent 下一轮重做 |
| `README.md` | 不需要，使用 Git UI discard |

此时 Git UI 显示：

```text
Staged:
  Token.kt
  selected Parser.kt hunks

Changes:
  Parser.kt
  Config.kt
```

发送下一条 prompt：

> Keep the direction I used in Parser.kt, but simplify it further.
> Rework Config.kt using the existing configuration abstraction.
>
> Also rename TokenDefinition to ParsedToken across the implementation.

Prompt Submit 将 `Token.kt` 和 staged parser hunks 吸收到 reviewed checkpoint，清空 index 中相对
新 checkpoint 的 delta，保留其余 Parser/Config pending changes。随后保存 pre-agent 快照和本轮
review/human provenance，再次注入完整协议。

Agent 可以先查看文件范围：

```bash
relay agent diff reviewed --name-only
# Parser.kt、Token.kt
relay agent diff human --name-only
# Parser.kt、README.md（discard 也是 human workspace change）
relay agent diff pending --name-only
# Config.kt、Parser.kt
```

再按需要查看具体 hunks：

```bash
relay agent diff reviewed -- Parser.kt   # 用户认可了什么
relay agent diff human -- Parser.kt      # 用户手动调整了什么
relay agent diff pending -- Parser.kt    # 目前还有哪些 unresolved 内容
```

Agent 继续简化 parser、重做 config，并为了 rename 再次修改**此前已 reviewed** 的 `Token.kt`。
这是正常行为，新的 Token delta 再次成为 pending。Agent Stop 保存新快照后，用户完成最终 review，
stage 所有剩余修改，确保没有 unstaged 或 untracked proposals。

```bash
relay finish -m "Refactor parser and configuration"
```

无论内部有多少轮次或 checkpoints，公开结果都只有一个 commit：

```text
A --- B --- C                      main（未移动）
             \
              R                    relay/result/<session>

R.parent = C
R.tree   = final reviewed tree
```

## Suspend / Resume

Agent 停止后，可以暂存未完成的 review，处理其他任务：

```bash
relay suspend
git switch -c urgent-fix
# 正常修改，然后提交 urgent fix。
git add -A
git commit -m "Handle urgent fix"
relay resume
```

Relay 会恢复 reviewed checkpoint、staged hunks、unstaged edits、新项目文件、turn metadata 和
provenance。一个文件如果 stage 了版本 A，又在 workspace 改成版本 B，恢复后仍保留这个区别。
Intent-to-add、binary 内容、符号链接、可执行位按 Git 模型保存；无关 ignored files 留在原位。
Resume 同样要求当前普通工作区 clean 且稳定。

多个 session 被 suspended 时，先 `relay list`，再 `relay resume <session>`，也支持唯一匹配的前缀。
Session 属于所在 Git worktree；不同 linked worktrees 可以独立运行。暂停期间其他 branch/history
可以继续前进，session 的 base_commit 始终不变。

Suspend / abort 返回原分支的当前 tip。如果原分支被删除或已在另一个 worktree 中 checkout，Relay
会回到起始 commit 的 detached 状态，避免移动或占用其他工作区的分支。

## Finish 与结果集成

存在 pending changes 时不能 finish。最后一批 hunks review 通过后，可直接 stage 并 finish，
不必额外发送一次 prompt。说明可以预先保存、通过 `-m` 当场提供，或在两者都没有时由自动打开的
编辑器填写，不再生成默认说明。
即使最终 tree 与 base 相同，也创建一个使用该 message 的结果 commit。
Relay 切换到 `relay/result/<session>`，清理该 session 的内部 refs 与 metadata。

Finish 不进行 merge、rebase、cherry-pick，也不吸收 session 期间新增的 mainline commits。
Finish 后恢复正常 Git workflow。例如目标开发分支是 `main`：

```bash
# relay finish 后当前就在结果分支：
git rebase main
# 然后通过正常 Git 集成，例如：
git switch main
git merge --ff-only relay/result/<session>
```

也可切换到目标分支，选择 `git merge relay/result/<session>` 或
`git cherry-pick relay/result/<session>`。把 `<session>` 替换为 finish 输出的真实分支名称部分。
这些操作由用户明确执行，Relay 不会自动代办。

## Abort 与 recovery branch

```bash
relay abort
```

命令会给出 **两次独立警告与确认**：第一次输入 `abort`，第二次输入 `preserve and abort`。
任意一次拒绝、输入其他内容或 EOF，都会取消操作而不清理 session。没有 `--yes` 跳过入口。
要终止 suspended session，请先恢复该 session。

实际清理之前，Relay 创建 `relay/aborted/<session>`，其中一个 commit 的 parent 是 immutable base，
tree 包含 abort 时完整的项目代码：此前 reviewed、staged、unstaged、Agent/用户修改，以及未忽略的
新文件。无关 ignored files 不纳入 commit，并留在原位。Recovery 保留代码，不保留 staging 区别。

之后清理内部 session refs、checkpoints、snapshots 与 provenance，返回原普通工作区，并明确打印
recovery branch 名称。此 session 无法再 resume。**Relay 不会自动删除 recovery branch。**
可以使用普通 Git 查看或切换到它。确认不再需要时，再自行执行：

```bash
git branch -D relay/aborted/<session>
```

## 架构与支持范围

```text
src/agent_session_relay/
  cli.py                    Human / Agent 命令入口
  core/
    git.py                  临时 index、trees、commits、refs 与工作区切换
    storage.py              原子 metadata、进程锁、恢复日志
    session.py              Review 生命周期与 provenance baselines
  integrations/
    protocol.py             每个 active turn 注入的自包含说明
    kiro/                   安装、生命周期适配与 shell/Git guard
docs/                       需求、架构、接入规则
tests/                      真实 Git 工作流与 adapter 测试
```

状态位于当前 worktree 的 Git 目录下 `agent-session-relay/`，快照由
`refs/relay/sessions/` 下的 refs 保持可达。内部 history 不会成为最终公开 result/recovery commit 的
祖先。修改操作使用锁与恢复日志，普通失败会回滚。进程在切换过程中意外退出时，`relay recover`
会先将当前代码保存到 `relay/recovered/<id>`，再恢复操作前的 review state。
执行生命周期命令期间，请勿同时编辑文件或运行其他 Git 操作。

本版本实现 Kiro IDE 1.x / CLI 3.x。Codex、Claude Code 等可通过相同 Core 扩展 adapter，目前未随
本版本提供正式接入。Git guard 覆盖普通 Git 调用、常见 wrappers
与 shell substitutions，不是针对任意程序的安全沙箱。

Relay 明确拒绝 sparse checkout、submodules/embedded repositories，以及
assume-unchanged/skip-worktree 标志，避免产生不完整快照。Git ignore、attributes 和 clean/smudge
filters 按正常 Git 规则生效。详见 [安全说明](SECURITY.md)。

## 开发与验证

```bash
python3 -m unittest discover -s tests -v
python3 scripts/build_zipapp.py
```

开发与发布检查见 [CONTRIBUTING.md](CONTRIBUTING.md)。

Copyright 2026 Agent-Session-Relay contributors。采用 [Apache-2.0](LICENSE) 许可证。
