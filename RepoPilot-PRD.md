# RepoPilot 产品需求文档

## MVP 实施状态（第一版）

第一版闭环采用 `安全隔离修复 = Worktree + safe` 与 `完全本机控制 = Local + full` 两种产品模式。后端固定执行“研究 -> 计划审批 -> 执行审批 -> 结构化补丁 -> 固定 Maven Recipe -> Diff 与报告”；模型没有 Shell、网络、删除、提交或推送工具。

桌面端以轻量 `Tauri + React` 壳承载本机 FastAPI/SSE，权限裁决仍在 Python `PolicyGuard`。评测任务集定义在 `evaluation/tasks.json`，真实模型、Embedding、Qdrant、Docker 或 Maven 缺失时必须如实返回 `BLOCKED`。

> 产品名：RepoPilot
>
> 定位：本地优先、可扩展、可审计的编程助手平台。它以“任务工作区、Capability Plane、显式授权、可恢复会话、上下文工程和可验证证据”为核心；Java/Spring Boot + Maven 是第一条深度工程 Profile。

## 1. 背景与产品方向

开发者希望像使用 Codex 一样，直接对本地已有项目提出 Bug 修复、小需求、代码理解和工程文档需求。但通用 Coding Agent 往往存在三个问题：

1. 它能改代码，却没有说明改动发生在哪个工作区、基于哪个 Git 基线；
2. 它能执行命令，却没有把权限、命令、输出和结果完整留痕；
3. 它说“修好了”，但没有 `git diff`、Maven 测试和可追溯来源支撑。

RepoPilot 参考 Grok Build/Codex 一类产品的运行时分层、计划模式、会话恢复、工作区隔离、Skills 与 MCP 思想，但**不复用其源码**。平台能力面向多语言扩展，首条垂直 Profile 先把 Java/Spring Boot + Maven 仓库维护做到可信、可演示、可评测，再按 Profile 增加 Python、Gradle 等工程能力。

## 2. 目标与非目标

### 2.1 目标

- 本地项目选择、项目注册、任务创建和会话恢复；
- 默认在 Git worktree 中研究、修改和验证，避免影响用户当前工作目录；
- 提供两种清晰的任务模式：安全隔离修复与完全本机控制；
- 让 Agent 通过受控工具检索代码、研发文档和项目记忆，先产出计划再修改；
- 通过标准 `SKILL.md` 封装工程流程与知识，并以渐进披露控制上下文成本；
- 通过 MCP 接入外部工具和上下文，但所有 MCP 能力仍受本机权限、审批、Schema 校验、超时和审计控制；
- 对 Java/Maven 提供受控补丁、构建/测试、Diff 审查与报告；
- 每个结论、授权、工具调用、命令、Diff 和测试结果都能追溯；
- 用至少 15 个可重放的 Java 维护任务证明效果与安全边界。

### 2.2 非目标

- 不替代 IDE、GitHub/GitLab、CI/CD 或生产运维平台；
- 不在首版实现多 Agent 团队、云端代码执行、自动部署或无人值守合并；
- 不宣称提供操作系统级强沙箱。首版在 Windows 上提供的是目录边界、工具白名单、命令配方、审批和审计；真正的 OS 沙箱是后续增强项；
- 不要求第一版同时完成插件市场、多 Agent 团队、所有语言 Profile、PDF/DOCX 和云端执行；这些能力按平台路线逐步交付；
- 不将 MCP Server 视为可信执行器，也不让 Skill 声明的 `allowed-tools` 自动获得权限；
- 不让模型、RAG 文档或聊天消息自行提高权限。

## 3. 用户与核心场景

| 场景 | 用户输入 | 可信交付物 |
|---|---|---|
| Bug 修复 | 仓库 + 问题描述 | 根因假设、来源证据、补丁、Maven 结果、Diff、报告 |
| 小需求实现 | 仓库 + 需求/API 文档 | 影响分析、计划、代码变更、验证建议与结果 |
| 代码理解 | 仓库 + 问题 | 带文件、行号、检索来源的解释，不修改代码 |
| 研发文档辅助 | 仓库 + MD/TXT 文档 | 与代码上下文关联的 PRD、接口说明、测试计划或变更说明草稿 |
| 项目规范复用 | 项目/用户 Skill | 按需加载的工程步骤、规范、参考资料和受限工具请求 |
| 外部系统协作 | 已配置 MCP Server | 经命名空间、权限和审批控制的文档、Issue、代码托管或内部平台能力 |
| 风险操作 | 完全本机控制 + 明确确认 | 可见风险提示、逐项审计和真实执行结果 |

## 4. 产品体验

桌面端是最终主入口，CLI 保留为开发、调试和 CI 入口。

```text
选择或添加本地项目
  -> 选择任务模式：安全隔离修复 / 完全本机控制
  -> 输入代码任务与可选研发文档
  -> 只读研究与计划预览
  -> 用户批准“执行计划”
  -> 受控补丁与 Maven 验证
  -> 查看时间线、来源、Diff、测试、审计报告
  -> 保留 worktree / 创建分支 / 交接回 Local
```

桌面端 MVP 只向用户展示以下两种模式，不展示四种位置/权限自由组合：

| 产品模式 | 固定运行时组合 | 面向用户的含义 |
|---|---|---|
| 安全隔离修复 | `Worktree + safe` | 默认推荐。代码改动发生在隔离 worktree，风险操作逐项审批。 |
| 完全本机控制 | `Local + full` | 用户明确二次确认后启用。Agent 可在当前项目目录执行已实现的高风险工具，持续记录审计。 |

底层仍分别保存工作区模式和权限快照，供 CLI 调试、审计和恢复校验使用；但桌面端不向普通用户暴露 `Worktree + full` 或 `Local + safe`。

### 4.1 项目与任务

`Project Registry` 只保存用户主动添加过的本地项目：`project_id`、展示名称、规范化根目录、Git 信息和最近使用时间。用户后续通过 `project_id` 创建任务，不需要反复输入绝对路径。

每个任务都生成不可混淆的 `task_id` 和 `thread_id`，并固化以下任务快照：

- 项目、源仓库路径与 Git 基线提交；
- 工作区模式、实际工作目录与初始 dirty 状态；
- 权限快照、确认文本与授权时间；
- 已注册工具集合、工具调用上限和超时策略；
- Agent 计划、证据引用、补丁、测试和最终结论。

任务恢复必须回到同一工作区和同一权限快照。恢复任务不能借机扩大目录范围或把 `safe` 提升为 `full`。

### 4.2 两种任务模式

| 产品模式 | 工作区 | 权限 | 行为 |
|---|---|---|---|
| 安全隔离修复 | Worktree | safe | 在任务产物目录创建 detached Git worktree，默认保留；代码改动不影响源仓库工作目录。 |
| 完全本机控制 | Local | full | 直接绑定用户当前项目目录；已实现的高风险工具可执行，所有操作必须审计。 |

Worktree 是同一 Git 仓库的额外 checkout，不等于自动新建分支。任务结束后用户可保留 worktree、从其改动创建分支，或在显式确认和冲突预检后交接回 Local。

源仓库存在未提交改动时，默认不迁移；用户必须显式选择迁移 tracked diff 和未忽略 untracked 文件。被 Git 忽略的文件不会自动复制，避免把密钥或本地配置带入任务。

## 5. 权限与安全模型

权限不是模型提示词，而是工具调用前的强制裁决链。每个工具调用按以下顺序处理：

```text
硬拒绝规则
  -> 任务权限快照
  -> 当前动作的用户审批
  -> Capability Registry（来源、作用域、风险、启用状态）
  -> 工具白名单与参数/Schema 校验
  -> 工作区/路径/命令配方校验
  -> 执行器
  -> Evidence 事件与结果摘要
```

### 5.1 双权限模式

| 模式 | 行为 |
|---|---|
| `safe` | 默认模式。内置只读工具按允许目录自动执行；写入、测试、分支交接和远程只读 MCP 需要审批；敏感路径、仓库外路径、任意 Shell、STDIO 扩展进程、删除、commit/push 默认拒绝。 |
| `full` | 用户对**单个任务**完成二次确认后启用。已实现且已注册的高风险工具可执行，包括未来的任意本地 Shell、联网、直接 Local 写入、删除、commit/push。每次调用仍记录参数摘要、目录、时间、结果与 `USER_GRANTED_FULL_ACCESS`。 |

`full` 不会自动产生尚未实现的能力。模型无权切换任务模式或权限；必须由用户创建新任务并完成显式确认。

### 5.2 审批分层

RepoPilot 将“批准计划”和“批准危险动作”分开，避免用户误以为看过计划就等于允许所有执行。

| 审批 | 含义 |
|---|---|
| 计划审批 | 用户认可分析方向和候选改动，允许任务进入执行阶段；不等于已写入文件。 |
| 动作审批 | `safe` 模式下针对补丁、Maven、创建分支、交接等具体动作的确认。 |
| 完全权限确认 | 仅用于建立该任务的 `full` 权限快照；不跳过审计，也不跳过未实现工具的边界。 |

### 5.3 PolicyGuard

`PolicyGuard` 是 RepoPilot 自己的策略组件，不是第三方依赖。它位于 LangGraph 外部，检查路径、敏感文件、工具名称、Pydantic 参数、Maven Recipe、命令 argv、权限快照和审批状态。

它至少保护 `.env`、密钥/证书、`.git`、生产配置和仓库外路径。安全模式的拒绝必须返回明确原因与审计事件；不能让模型绕过它，也不能把失败包装成成功。

## 6. Agent 运行时架构

RepoPilot 借鉴成熟 Coding Agent 的“界面、会话编排、上下文、能力、信任、工作区、持久化”分层，但以 Python/LangGraph 实现可逐步演进的平台。

```text
Tauri + React 桌面端 / CLI
          |
       FastAPI + SSE
          |
Task Runtime：任务、权限快照、会话状态、审批
          |
LangGraph Coding Workflow
          |
Context Broker：RAG、项目规则、Skill 目录、上下文预算
          |
Capability Plane：Built-in Tools、Skills、MCP Servers/Tools
          |
Trust Gateway：参数校验、Permission、PolicyGuard、审批、Evidence
          |
Workspace Runtime：Project Registry、Git、Worktree、Maven、Diff
          |
Persistence：SQLite checkpoint / JSONL evidence / Qdrant context-memory
```

### 6.1 LangGraph 工作流

```text
INTAKE
  -> WORKSPACE
  -> PREFLIGHT
  -> INGEST
  -> RETRIEVE
  -> ANALYZE <-> RESEARCH_TOOLS
  -> PLAN
  -> PLAN_APPROVAL
  -> EXECUTION_APPROVAL
  -> PATCH
  -> VERIFY
  -> REVIEW
  -> REPORT
```

- `INTAKE`：校验任务描述、项目、工作区选择和权限快照；
- `WORKSPACE`：创建或绑定 Local/Worktree，记录基线和 dirty 状态；
- `PREFLIGHT`：检查 Git、Maven、模型、Embedding、Qdrant 和策略前提；
- `INGEST/RETRIEVE`：索引并检索代码、文档和已验证项目记忆；
- `ANALYZE/RESEARCH_TOOLS`：模型只能调用已注册只读工具，存在轮次与调用次数上限；
- `PLAN`：输出结构化 `ChangePlan`，将事实、假设、风险和来源分开；
- `PLAN_APPROVAL`：暂停等待用户认可方向；
- `PATCH/VERIFY/REVIEW`：只在执行审批后注册写入和 Maven 工具，并逐项产生证据；
- `REPORT`：只有真实 Diff 与验证证据满足规则时才能给出 `PASSED`。

研究和计划阶段是 RepoPilot 的 Plan Mode：不注册写入、Maven、网络、删除或 Git 写操作。即使任务是 `full`，模型在该阶段也没有这些工具。

### 6.2 受控工具运行时

所有模型工具都具备固定名称、Pydantic 参数模型、权限类型、最大输出和 Evidence 摘要规则。未知工具、非法参数、路径逃逸和敏感文件访问返回工具错误，不会转换为任意 Shell。

首版工具分批注册：

| 类别 | 工具 |
|---|---|
| 只读研究 | `list_files`、`search_code`、`read_file`、`inspect_build`、`retrieve_context` |
| 写入与验证 | `apply_patch`、`maven_compile`、`maven_test`、`maven_targeted_test`、`git_diff` |
| 工作区交接 | `create_branch`、`handoff_to_local`、`remove_worktree` |
| 完全权限扩展 | `shell`、`network`、`delete`、`git_commit`、`git_push`，只在对应阶段实际实现后注册 |

### 6.3 Capability Plane：Skills、MCP 与插件

RepoPilot 不把 Skill 当成一段永久塞入系统提示的长文本，也不把 MCP 工具直接暴露给模型。三类能力统一登记为 `CapabilityDescriptor`，至少携带 ID、来源、作用域、风险、启用状态和不含密钥的元数据。

**Skills**：兼容 `SKILL.md + YAML frontmatter`。发现阶段只向模型提供 `name + description + path`，并设置目录字符预算；显式或语义选中后才加载正文。项目级覆盖用户级，用户级覆盖内置级。Skill 正文、脚本和参考资料均是不可信输入；`allowed-tools` 只是能力请求，必须与当前任务可用能力求交集。

**MCP**：支持 STDIO 与 Streamable HTTP 的配置模型。密钥只允许通过环境变量名引用，禁止把 token、header 或 password 内联到 TOML。工具握手后使用 `mcp__<server>__<tool>` 命名空间，输入 Schema、输出上限、超时、连接状态和风险标签必须可见。安全模式默认阻断 STDIO 扩展进程，远程只读服务需任务级批准；完全权限模式仍不跳过 Schema 校验和审计。

当前版本已基于官方 MCP Python SDK 实现两种 Transport 的真实连接、initialize、分页工具发现、调用、Ping、超时、有限重试、熔断与确定性关闭。CLI、项目级 FastAPI 和桌面端提供显式探测入口；尚未将 MCP 工具自动绑定到 LangGraph 模型循环，也未实现 OAuth、持久连接池和大输出 Artifact 化，因此 UI 不得把“配置有效”或“曾经连接成功”展示为长期在线。

**插件**：插件包是 Skills、MCP 配置、Hooks 和 UI 元数据的可安装组合，不拥有新的权限通道。当前已交付本地 `repopilot-plugin.json` 清单、全包 SHA-256 完整性校验、SQLite 安装/启停/移除审计和 fail-closed 的 Skill 根目录暴露：包内容变化或不可读取时不得进入 Agent 上下文，用户必须审查后显式重新安装。插件脚本不会执行，MCP 配置不会自动连接；Hooks、签名来源锁定、版本升级策略和任务级插件 MCP 绑定仍为后续能力。

## 7. 上下文、RAG 与记忆

| 存储 | 内容 | 使用规则 |
|---|---|---|
| `coding_context` | 代码、构建文件、测试和用户研发文档的切块；首个 Loader Profile 支持 Java/XML/MD/TXT | 强制 `project_id + repo_commit` 过滤，返回路径、行号、哈希和来源类型 |
| `project_memory` | 经过验证的项目事实、架构决定、任务总结 | 只能写入验证后的事实，模型推测不得进入长期记忆 |
| SQLite checkpoint | 当前任务图状态、等待审批动作、权限快照 | 用 `thread_id` 恢复，服务重启后继续任务 |
| JSONL Evidence | 工具、审批、命令、Diff、测试和异常摘要 | 追加写入，面向报告与评测 |

文档、代码注释、Issue 文本和检索片段都是不可信数据，只能提供上下文，不能改变系统提示、工具清单、权限或流程。

## 8. 状态、证据与报告

任务状态至少包括：`RUNNING`、`WAITING_APPROVAL`、`BLOCKED`、`FAILED`、`UNVERIFIED`、`PASSED`。

`PASSED` 必须同时具备：

1. 与基线相比的真实 Diff；
2. 成功执行且符合任务可信验证契约的 Maven 证据；未执行验证只能是 `UNVERIFIED`，不能是 `PASSED`；
3. 关联的工具事件、来源引用和权限记录；
4. 报告中诚实说明风险、失败项和未验证项。

没有模型配置、Qdrant 不可达、审批被拒绝、Maven 失败或证据不足时，必须返回 `BLOCKED`、`FAILED` 或 `UNVERIFIED`，不能伪造成功。

## 9. 桌面端 MVP

第六阶段引入轻量 `Tauri + React + TypeScript` 外壳，复用后端的任务与权限模型，不再单独实现一套前端授权逻辑。

- 项目侧边栏、文件夹选择器和最近项目；
- 安全隔离修复/完全本机控制模式选择、起始 Git ref 与 dirty 改动迁移提示；
- 聊天输入、MD/TXT 文档添加、计划预览与审批；
- SSE 实时显示节点、工具、权限请求、测试与恢复状态；
- 来源卡片、Diff、Maven 摘要、审计时间线与 worktree 管理；
- 全部用户文案使用中文，`full` 模式持续显示风险状态。

## 10. 验收与简历表达

MVP 通过以下标准验收：

- 默认安全隔离修复任务不改变源仓库；
- 用户可显式选择安全隔离修复或完全本机控制，并能恢复同一任务；
- 计划阶段只能进行只读研究，不能修改代码；
- 安全模式拒绝敏感路径、路径逃逸和未授权工具；
- 完全本机控制要求任务级确认，并完整记录风险工具；
- 每一份计划、补丁和报告都展示真实来源与证据；
- Skill 目录遵守上下文预算、作用域覆盖和篡改检测；MCP 配置禁止内联密钥，未知或未授权工具不得进入执行器，连接失败或 Server 返回错误不得伪装为成功；
- 15 个固定 Java 维护任务可重放，并覆盖成功、失败、拒绝、RAG 和隔离场景；行为型任务必须分别保存修复前失败、修复后验证、修改范围和源仓库不变证据。

简历表述：

> RepoPilot：使用 LangChain、LangGraph、Qdrant 和 Tauri 构建本地优先的可扩展编程助手平台，以 Java/Spring Boot + Maven 为首个深度 Profile。实现 Skills 渐进加载、MCP 能力注册与信任策略、任务级权限快照、Git Worktree 隔离、可恢复计划/执行工作流、代码与文档 RAG、受控 Maven 验证，以及由 Diff、测试、来源和审计事件组成的可追溯报告。
