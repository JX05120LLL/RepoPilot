# RepoPilot v2 最小闭环优化设计

> 文档状态：设计基线
>
> 目标：在不推倒现有 Python/LangGraph MVP 的前提下，吸收 Grok Build 的架构边界，解决当前“能跑但不够稳定、不够可解释、不够容易演示”的问题。

## 0. 迭代 A 实施进度

本轮已落实 v2 的第一组 P0 基础：新增 `TaskStore`，在现有 SQLite 状态库中持久化 `tasks`、`task_events` 与 `task_artifacts`。任务创建先写入 `RUNNING` 记录；Graph checkpoint 可用后投影状态和受控工具事件；每条事件带单调 `sequence` 与 `event_id`，SSE 支持用 `Last-Event-ID` 或 `after_sequence` 续传。计划、补丁提案、真实 Diff、验证结果和报告会以原子写入方式落在 `output_root/<task_id>/`，SQLite 只记录相对路径、大小和 SHA-256；本机 API 按受控产物类型读取并校验哈希。服务升级前已有的 LangGraph checkpoint 也会在首次读取时补建任务索引。后台任务使用 15 分钟租约和 30 秒心跳；取消先进入 `CANCELLATION_REQUESTED`，并向当前图执行器发送任务级信号：后续节点不再启动副作用，RepoPilot 自己启动的 Maven 子进程会被终止并记录为 `MAVEN_CANCELLED`，任务最终写入 `CANCELLED`。终态任务可归档，归档只隐藏默认列表，不删除事件、产物或 checkpoint。

当前仍未完成模型 SDK/HTTP 调用的主动中断、事件物理保留期限/压缩、产物版本保留策略和多进程执行器。产物不可变版本历史、任务级 Trace ID、已验证项目长期记忆与 Maven 子进程终止已交付；其余能力保留在后续迭代，不应被误写为已交付。

## 1. 为什么现在要做一次重新设计

RepoPilot 已经不是空项目：它可以注册本地项目、创建 Local/Worktree 工作区、索引 Java/Maven/MD/TXT 上下文、调用真实 OpenAI-compatible 模型进行只读研究、输出结构化计划、在审批点暂停、并通过 FastAPI + SSE 将过程推送到桌面前端。

最近的真实联调也证明了主链路可运行：非 Git 的 Spring Cloud Shop 项目以 `full-local` 进入只读研究，完成 Qdrant 检索、受控文件读取、模型工具调用和计划生成，最终到达 `WAITING_APPROVAL`。这是一项重要进展，但它不等于“简历级闭环已经稳定完成”。

当前需要优化的重点不是再增加一个工具，而是把下面三件事边界化：

1. **任务是什么**：可恢复任务、会话、计划、补丁、报告分别是什么产物；
2. **模型能做什么**：模型只能提出意图，工具网关和策略层才决定实际效果；
3. **什么才算成功**：研究成功、计划成功、补丁已应用、Maven 已通过、最终 `PASSED` 必须是不同状态。

## 2. 研究参考：Grok Build 值得借鉴什么

本方案参考的是公开架构和行为边界，不复制 Grok Build 的 Rust 源码、名称、UI 或插件实现。上游仓库将入口 TUI、Agent Shell、工具库和工作区能力拆为独立 crate；README 明确列出了 `xai-grok-shell`、`xai-grok-tools`、`xai-grok-workspace` 与 `xai-grok-pager` 的职责。[Grok Build README](https://github.com/xai-org/grok-build)

### 2.1 参考点一：运行时分层，而不是“一个 Agent 类做一切”

Grok Build 的结构可抽象为：界面负责交互，Agent 负责推理和会话，工具层负责工具契约，工作区层负责文件/Git/执行，持久化层负责恢复。其工作区 crate 的职责直接包含本机文件系统、VCS、执行和发现；工具 crate 独立承载文件、终端和搜索能力。[workspace crate](https://raw.githubusercontent.com/xai-org/grok-build/main/crates/codegen/xai-grok-workspace/Cargo.toml) [tools crate](https://raw.githubusercontent.com/xai-org/grok-build/main/crates/codegen/xai-grok-tools/Cargo.toml)

RepoPilot 应借鉴这个**边界**，而不是转为 Rust：

```text
桌面端 / CLI
    -> Task API 与事件协议
    -> Task Service（任务、审批、恢复）
    -> LangGraph Agent Coordinator（决策编排）
    -> Context Broker（检索、上下文预算、项目规则）
    -> Tool Gateway（工具契约、PolicyGuard、审批、审计）
    -> Workspace Service（Git、Worktree、补丁、Maven、Diff）
    -> SQLite / JSONL / Qdrant / Artifact Directory
```

### 2.2 参考点二：Plan Mode 是执行约束，不是“模型多说几句计划”

Grok Build 的 Plan Mode 将研究与实现分离：计划阶段读取和搜索代码，计划文件是唯一可编辑产物，其他文件编辑会被拒绝；计划经过用户预览、评论、修改和批准后才进入实现。[Plan Mode](https://raw.githubusercontent.com/xai-org/grok-build/main/crates/codegen/xai-grok-pager/docs/user-guide/19-plan-mode.md)

RepoPilot 目前已有“研究 -> `ChangePlan` -> 计划审批”，这是正确起点。v2 要补足两点：

- 为每个任务生成可读的 `output_root/<task_id>/plan.md` 和结构化 `plan.json`；
- 计划审批已支持“批准 / 拒绝 / 带反馈要求重写”，而不是只有布尔值；重写最多两次，每次都会重新进入计划审批，不会越过执行审批。

这能让用户看见一个可以审阅的工程产物，而不是从 JSON 时间线里猜模型的意图。

### 2.3 参考点三：权限模式与规则是不同维度

Grok Build 的授权链把 Hook、显式 deny/ask/allow 规则、已记住的授权、内置只读放行和当前模式分层；其中 deny 优先级最高。[Permissions and Safety](https://raw.githubusercontent.com/xai-org/grok-build/main/crates/codegen/xai-grok-pager/docs/user-guide/22-permissions-and-safety.md)

RepoPilot 不需要在 MVP 实现完整 Hook/MCP 生态，但应保留同样的结论：

```text
硬拒绝与任务不可变约束
    -> 项目规则与目录范围
    -> 任务权限快照
    -> 当前审批动作
    -> 已注册工具与 Pydantic 参数
    -> 实际执行器
    -> 证据事件
```

`safe/full` 只是“默认授权策略”，不能取代敏感路径、工作区边界、Maven Recipe 和工具参数校验。

### 2.4 参考点四：会话、工作区、事件是三个不同对象

Grok Build 的会话持久化采用增量 JSONL，并保留摘要、计划和信号等小状态文件，同时以 SQLite FTS 做会话检索；会话可以恢复、分叉或绑定隔离 worktree。[Session Management](https://raw.githubusercontent.com/xai-org/grok-build/main/crates/codegen/xai-grok-pager/docs/user-guide/17-sessions.md)

RepoPilot 的 SQLite checkpoint 已经能恢复 LangGraph 状态，但 v2 不能只把 checkpoint 当作全部任务数据库：它还需要可查询的任务记录、事件序号和稳定产物目录。

## 3. 当前代码事实盘点

下表只记录当前仓库中已经存在或已真实联调过的能力，不把 PRD 中的未来能力当成完成事实。

| 区域 | 当前事实 | 证据 |
|---|---|---|
| 项目与工作区 | 有 SQLite `ProjectRegistry`、Git 基线、detached worktree、Local 模式、dirty 迁移和非 Git Local 只读研究例外 | `project_registry.py`、`workspace.py`、工作区测试 |
| 权限 | 有 `PermissionGrant`/`PermissionSnapshot`、`PolicyGuard`、敏感路径/路径逃逸/Maven Recipe 校验 | `permissions.py`、`policy.py`、`test_workspace_tools.py` |
| RAG | 有 Java/XML/MD/TXT Loader、确定性切块、Qdrant 集合、`project_id + repo_commit` 过滤和 SQLite 去重记录 | `context.py`、`qdrant_bootstrap.py` |
| Agent | 已实现 `INTAKE -> WORKSPACE -> PREFLIGHT -> INGEST -> RETRIEVE -> ANALYZE <-> RESEARCH_TOOLS -> PLAN -> PLAN_APPROVAL -> EXECUTION_APPROVAL -> PATCH -> VERIFY -> REVIEW -> REPORT` 图 | `graph.py` |
| 工具 | 研究期只注册文件列表、搜索、读取、构建检查和上下文检索；未知工具不会变成 Shell | `ResearchToolExecutor`、`ToolRuntime` |
| 写入与验证 | 已有结构化文本替换、原子校验、Git diff、Maven Recipe 执行器和两级审批节点 | `execution.py`、`graph.py` |
| 本地接口 | FastAPI、SSE、后台运行任务、项目/任务/审批/Diff/报告接口存在，且只监听本机 | `api.py` |
| 桌面端 | 有 React/Vite 页面，可选择项目和两种模式、提交任务、显示事件并审批；尚不是完成打包验证的 Tauri 产品 | `desktop/src/App.tsx` |
| 真实联调 | 已使用真实 Embedding/Qdrant/聊天模型完成一次代码理解任务并到达计划审批 | 本地 SQLite checkpoint 与 SSE 运行记录 |
| 自动测试 | 当前全量 `unittest` 为 88 项；前端 `npm run build` 可通过 | 本地测试运行结果 |
| 评测 | 有 15 条任务定义 JSON、独立 Java/Maven Git 基线生成器和实际结果 JSON/CSV/Markdown 报告执行器；尚未运行真实模型全量报告 | `evaluation/tasks.json`、`evaluation.py` |

### 3.1 当前状态的正确解释

```text
研究成功：工具和上下文被实际调用，得到证据
计划成功：模型输出能通过 ChangePlan 校验，任务到达 WAITING_APPROVAL
执行成功：补丁被原子应用，并产生真实 diff
验证成功：Maven Recipe 退出成功，得到测试证据
最终 PASSED：真实 diff + Maven 成功 + 证据完整
```

因此，`RESEARCH_LIMIT_REACHED` 不等于失败：它表示到达受控研究预算后停止继续调用工具，再用已获得证据生成计划。`WAITING_APPROVAL` 也不等于修复成功，它只说明计划已经可供用户审阅。

## 4. 当前不足与风险

### P0：产品事实和文档不一致

README、PRD 和开发计划中仍同时存在“阶段五至八已形成骨架”和“尚未实现真实模型、补丁、API、桌面端”的相互矛盾描述。对面试官或用户而言，这会让项目完成度不可判断。

**改进**：所有公开文档改用三列状态：`已实现并测试`、`已真实联调`、`设计预留`。后续只从单一“能力矩阵”生成 README 摘要。

### P0：任务服务已持久化，但治理能力仍不完整

本轮已移除 API 进程内 `task_runs`：`TaskStore` 提供 SQLite `tasks`、`task_events`、`task_artifacts`、运行心跳和任务列表；服务重启后可以从任务记录或已有 Graph checkpoint 重新发现任务。任务产物以原子落盘和 SHA-256 清单保护读取完整性。仍缺少取消、超时回收、事件归档、产物版本历史和多进程 worker，因此还不能称为完整的生产级任务系统。

**下一步改进**：任务创建时先持久化 `CREATED`，后台执行更新 heartbeat；引入明确的 `CANCELLED`、租约超时、产物版本与恢复策略；API 和桌面端继续以任务服务为入口，而不是直接读取 LangGraph 内部状态。

### P0：SSE 已可续传，但尚未完成事件归档与背压治理

本轮 SSE 已从持久化 `task_events` 读取，并支持单调 `sequence`、`event_id`、`Last-Event-ID` 和 `after_sequence`。连接断开后不会再从 GraphState 的第一个事件重复播放。当前仍以短轮询同步 Graph checkpoint，且没有事件保留期限、分页查询或客户端背压控制。

**下一步改进**：`EvidenceStore.append()` 同时写 JSONL 与 `task_events`；前端以事件 ID 去重；为长任务增加事件分页、归档和最大流量限制。

### P0：非 Git Local 研究与可验证修复混在同一产品模式

当前 `full-local` 为了支持代码理解允许非 Git 目录进入只读研究，但后续补丁的 Git Diff、交接和基线保证依赖 Git。若直接允许非 Git 写入，会失去“基于哪一版修改”的关键证据。

**改进**：将能力拆为：

| 场景 | 允许能力 |
|---|---|
| 非 Git 项目 | 仅 `理解 / 检索 / 文档计划`，最终为 `UNVERIFIED` |
| Git 但无提交 | 只读诊断，提示用户先创建基线提交 |
| Git 且有提交 | 才允许 Worktree 修复或 Local 受确认修复 |

不要用“完全本机控制”暗示非 Git 项目也能交付可信修复。

### P1：GraphState 过大，业务对象混杂

`GraphState` 同时保存任务输入、权限快照、工作区、模型消息、工具事件、计划、补丁、验证与 diff。它虽然方便 MVP，但会导致 checkpoint 膨胀、状态演进难迁移、单节点测试困难，也可能把完整代码工具输出放入状态库。

**改进**：GraphState 只保留 ID 和当前阶段；任务、消息、证据、计划和产物分别存储并按需读取。

```text
轻量 GraphState
  task_id / thread_id / phase / approval_id / retry_count

外部持久化对象
  TaskRecord / SessionJournal / EvidenceEvent / ChangePlan / PatchProposal / VerificationResult
```

### P1：研究预算静态，证据质量没有硬校验

`MAX_RESEARCH_ROUNDS = 6`、`MAX_TOOL_CALLS = 12` 固定。真实“介绍项目”任务中，模型可能用多次 `list_files` 消耗预算；`ChangePlan` 的 evidence 由模型填写，没有强制关联一个真实 `evidence_id`，候选文件也可能包含未阅读路径。

**改进**：

- 为每种任务类型设置预算：代码理解 6 次、Bug 修复 12 次、小需求 16 次；
- 引入 `EvidenceReference(event_id, path, line_start, line_end)`；计划提交前服务端校验 `event_id` 存在且路径确实出现过；
- `candidate_files` 默认只能来自 RAG 来源或成功读取/搜索结果；模型额外提出的路径进入 `unverified_candidates`；
- 以“每轮新增高价值证据”作为继续研究条件，重复目录枚举直接停止。

### P1：执行审批仍不够可读

计划审批已经支持 `approve | revise | reject` 与可选反馈，`revise` 会携带反馈回到 `PLAN`，最多两次后必须批准、拒绝或创建新任务。执行审批仍无法在界面中完整展示结构化补丁摘要、目标文件、旧文本匹配次数、Maven Recipe 和预计风险。

**改进**：审批改为：

```json
{
  "decision": "approve | revise | reject",
  "comment": "可选的修改意见",
  "expected_revision": 3
}
```

执行审批应只在补丁提案已生成后出现，并展示文件、变更摘要和验证命令。

### P1：`full-local` 的产品承诺需要更诚实

用户期待类似 Codex 的完全权限体验，但当前实现仍刻意不注册任意 Shell、网络、删除、commit/push。这一安全边界是对的；问题在于 UI 名称“完全本机控制”容易被理解为所有操作已可用。

**改进**：短期 UI 改为“完全本机控制（当前工具集）”，详情卡明确列出本次已注册工具。长期若实现 Shell，也应让用户看到“本次授予：文件写入、Maven、Git 分支；未授予：网络、删除、push”这样的 capability 清单，而不是一个模糊的万能开关。

### P2：RAG 和长期记忆的边界尚不完整

目前 Qdrant 的 `coding_context` 已可用；`project_memory` 的“只写已验证事实”规则主要停留在文档，尚未形成受验证结果驱动的写入管道。检索也以向量为主，缺少项目规则、关键字/BM25 兜底和上下文压缩。

**改进**：

- 增加 `ProjectRuleLoader`：读取根目录到工作目录的 `AGENTS.md`/`.repopilot/rules.md`，按层级合并，仅作为不可信上下文；
- 新增 `VerifiedFactWriter`：只接受 `PASSED` 任务的事实，且每条事实必须链接 diff、Maven 结果和来源；
- v2 不强制引入第二个向量库。先在现有 Qdrant 之上实现“路径过滤 + 字面量搜索 + 向量检索”的轻量混合召回；
- 将发送给模型的工具全文限制为每文件、每轮和总 token 预算，超过预算只给摘要和可继续读取的引用。

### P2：桌面端还只是调试视图

当前页面直接渲染 JSON 时间线，缺少计划阅读、来源卡片、补丁预览、Maven 摘要、任务恢复列表、项目添加和文档上传等产品化信息架构。Tauri 打包流程也未形成可复现验收。

**改进**：先不追求漂亮聊天 UI，优先做四个稳定面板：任务列表、计划/审批、证据与来源、Diff/验证报告。每个面板只显示服务端结构化数据。

### P2：评测基线已就绪，但端到端基准尚未完成

15 条任务 JSON 定义现在可通过 `evaluate prepare` 生成独立 Java/Maven Git fixture，并输出固定提交、场景与静态路径断言的 JSON/CSV 清单。`evaluate run` 可对明确指定的 fixture 调用真实 Graph，并输出实际结果 JSON/CSV/Markdown；它不会把期望状态改写为实际状态。尚未执行当前模型服务的 15 项全量报告，模型/提示版本快照和更细的端到端安全断言仍需补充。

**改进**：每个任务拥有独立 Git fixture 或统一 fixture 的固定 commit，并写出 `setup -> run -> assertions -> cleanup`。将模型输出质量和系统安全性分开统计。

## 5. v2 目标架构

### 5.1 组件职责

| 组件 | 职责 | 不负责什么 |
|---|---|---|
| Desktop/CLI | 创建任务、展示状态、提交审批 | 不做权限裁决、不拼接命令 |
| Task Service | 任务记录、状态机、恢复、取消、产物索引 | 不直接调用模型工具 |
| Agent Coordinator | LangGraph 节点、模型决策、重试边界 | 不直接写文件或执行 Maven |
| Context Broker | 项目规则、RAG、搜索、上下文预算、来源引用 | 不改变权限 |
| Tool Gateway | 工具定义、参数校验、策略裁决、审批检查、审计 | 不负责 UI 状态 |
| Workspace Service | Git 基线、Worktree、补丁、Maven、Diff | 不接受模型任意 Shell 字符串 |
| Artifact Store | 计划、补丁、报告、日志、Diff 文件 | 不推断业务结论 |

### 5.2 任务状态机

状态机由 Task Service 管理，LangGraph 节点只是推动状态迁移：

```text
CREATED
  -> PREPARING_WORKSPACE
  -> PREFLIGHT
  -> RESEARCHING
  -> PLAN_REVIEW
  -> PATCH_DRAFTING
  -> EXECUTION_REVIEW
  -> APPLYING
  -> VERIFYING
  -> REPORTING
  -> PASSED | FAILED | BLOCKED | UNVERIFIED | CANCELLED
```

规则：

- `PLAN_REVIEW` 只允许创建/修改 `plan.md` 任务产物，禁止项目代码写入；
- `PATCH_DRAFTING` 只允许生成补丁提案，禁止落盘；
- `EXECUTION_REVIEW` 必须展示补丁摘要和 Maven Recipe；
- `PASSED` 只能由报告节点根据 `Diff + Maven PASSED + Evidence` 派生，模型不能直接输出；
- `CANCELLED` 与 `BLOCKED` 不同：前者是用户终止，后者是安全/依赖/环境不满足。

### 5.3 持久化模型

```text
SQLite
  projects
  tasks
  approvals
  task_events(sequence, event_id, type, summary, created_at)
  task_artifacts(kind, path, sha256, created_at)
  context_chunks
  checkpoints（LangGraph）

Task artifact directory
  <output_root>/<task_id>/
    task.json
    plan.md
    plan.json
    patch.json
    diff.patch
    maven-result.json
    report.md
    evidence.jsonl

Qdrant
  coding_context：可检索代码/文档切块
  project_memory：仅验证后的项目事实
```

`evidence.jsonl` 是事件审计的可读副本，SQLite `task_events` 是 API 续传和查询索引。两者都只存摘要；模型需要的原始片段通过 Context Broker 短时加载，不写入事件流。

### 5.4 权限与工具契约

```text
ToolIntent（模型请求）
  -> ToolDefinition（名字、参数 schema、风险类别、输出限制）
  -> PolicyDecision（allow / ask / deny，原因与规则 ID）
  -> ApprovalCheck（任务级/动作级）
  -> ToolExecution
  -> EvidenceEvent
```

建议将现有 `PolicyGuard` 扩展为不可变的四层策略：

1. **永远拒绝**：密钥、证书、`.git` 内部写入、仓库外访问、未注册工具；
2. **模式边界**：safe 只能在 Worktree 写入；Local 写入必须有 full 快照；
3. **动作审批**：补丁、Maven、分支、交接的审批记录必须匹配当前 artifact hash；
4. **项目规则**：项目可增加只读目录、禁改模块、允许测试 Recipe，不能覆盖前 3 层。

### 5.5 上下文编排

一次模型调用不直接塞入整个 RAG 结果或所有文件全文，而使用 `ContextPacket`：

```text
系统约束与任务目标
  + 项目规则摘要
  + 最多 8 个来源卡片（path, line range, score, 摘要）
  + 已读取文件的截断内容
  + 工具调用预算和已使用数
  + 不可信数据声明
```

模型需要更多内容时再调用 `read_file`。工具返回写入 SessionJournal，但传入下一轮模型时按总 token 预算压缩。这样能避免“项目介绍”消耗掉所有研究额度，也让长项目可控。

## 6. 推荐实施顺序

### 迭代 A：先让 MVP 可解释、可恢复（3-5 天）

- 建立 `TaskRecord`、`TaskEvent`、`TaskArtifact`；
- 改造 API：任务列表、按事件序号 SSE 重连、任务状态轮询、取消接口；
- 生成 `plan.md`、`report.md`，前端渲染结构化计划而非原始 JSON；
- 修正文档能力矩阵，明确“已实现/已联调/预留”；
- 禁止非 Git 任务进入 PATCH/VERIFY。

**验收**：服务重启后，任一 `WAITING_APPROVAL` 任务能从任务列表恢复，事件不重复，计划可读且不写项目代码。

### 迭代 B：让计划和证据真的可信（4-6 天）

- evidence 赋 ID，`ChangePlan` 改为引用真实 evidence ID；
- 引入 `revise` 审批和计划版本号；
- 实现研究预算策略、重复工具调用抑制、上下文压缩；
- 加载项目规则并在报告中列出本次生效规则。

**验收**：计划中的每条事实可跳转到工具/RAG 来源；不存在的文件不能作为“已证实候选文件”。

### 迭代 C：打磨可验证写入（4-6 天）

- 给结构化补丁增加多处变更、统一预校验、文件 SHA-256 前置条件和补丁版本；
- 执行审批绑定 `patch.json` hash 与 Maven Recipe；
- 固化 Maven 输出摘要、Surefire 报告和失败分类；
- 完成 Worktree 保留、创建分支、显式交接的产物和审计。

**验收**：同一批准不会重复应用补丁；源仓库不被 safe 任务修改；`PASSED` 必须有可复核 diff 与 Maven 结果。

### 迭代 D：把 Demo 变为评测（5-7 天）

- 建立 15 个 Java Git fixture 与运行脚本；
- 固定模型、提示词、工具版本、基线 commit 和预期断言；
- 输出 JSON/CSV/Markdown 评测报告；
- 桌面端展示 4 个稳定 Demo：安全修复、计划修改、策略拒绝、恢复任务。

**验收**：任何人可在干净环境运行至少一套 fixture，得到状态、Diff、Maven 和安全断言，而非只看模型文本。

## 7. v2 不做什么

为了保持简历项目聚焦，v2 不在核心闭环前引入：

- 子 Agent、插件市场、MCP 服务器、联网搜索；
- PDF/DOCX 全格式解析；
- 任意 Shell、网络、删除、commit/push；
- 多语言构建矩阵、远程容器或生产级 OS 沙箱；
- 模仿 Grok Build 的 TUI、配置格式、命令或源码实现。

这些能力在 Grok Build 中有成熟实现和对应复杂度，但不适合抢占 RepoPilot 的 Java/Maven 可验证闭环。

## 8. 面试时如何讲这一版设计

可以用下面的三句话说明项目价值：

1. “我没有把模型当成有系统权限的执行器，而是将模型输出降级为 ToolIntent，由独立的策略、审批和工作区层决定是否执行。”
2. “我把研究成功、计划成功、补丁成功、测试成功分开建模，`PASSED` 只能由 Diff 和 Maven 证据派生。”
3. “参考成熟 Coding Agent 的分层和会话/Plan Mode 思路，但针对 Java/Maven 做了更小、更可演示的本地实现，并用 Worktree 和评测任务证明边界。”

## 9. 后续文档维护规则

每次完成一个能力，更新本文件中的能力矩阵，并同步 README 的一句摘要。PRD 描述目标态，开发计划描述未来迭代，README 只描述当前可运行事实；三者不得再混写。
