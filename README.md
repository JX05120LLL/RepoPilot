# RepoPilot

RepoPilot 是一款本地优先、可扩展、可审计的编程助手。产品体验对标 Codex/Grok Build：它可以理解已有项目、检索代码与研发资料、调用受控工具、提出并执行修改、运行验证，并通过 Skills、MCP 和 RAG 持续扩展能力。Java/Spring Boot + Maven 是第一条深度工程 Profile，而不是最终语言边界。

## MVP 闭环（阶段五至八）

第一版已将产品模式收敛为两种：`安全隔离修复 = Worktree + safe`，`完全本机控制 = Local + full`。Agent 固定经过研究、计划审批、执行审批、结构化补丁、固定 Maven Recipe、Diff/证据/报告；只有真实 Diff 与 Maven 成功证据同时存在才会判定 `PASSED`。

本机后端：`uv run repopilot-guard api serve`。桌面壳位于 `desktop/`，其开发窗口会优先复用已存在的本机 API；未发现 API 时，会从仓库根目录通过 `uv run repopilot-guard api serve` 启动仅监听回环地址的后端，并在窗口退出时回收自己启动的进程。前端不能绕过 Python `PolicyGuard`。

评测目录 [evaluation/tasks.json](evaluation/tasks.json) 包含 15 个可重放任务定义，覆盖 Java 修改、RAG、敏感路径、路径逃逸、提示注入、审批拒绝、Maven 失败与断点恢复。`evaluate prepare` 可为每项生成独立 Java/Maven Git 基线与 JSON/CSV 清单，`evaluate validate-baseline` 会在独立 clone 中复现修复前的 Maven 失败；两者都不会伪造模型或修复成功结果。

## 推荐模型配置

默认本机配置已拆分为两个 OpenAI-compatible 服务：DeepSeek `deepseek-v4-pro` 负责 Agent 对话、工具调用与补丁计划；阿里云百炼 `text-embedding-v4` 以 `1024` 维负责代码和文档 RAG。请仅在本机 `.env` 中填写 `REPOPILOT_CHAT_API_KEY` 与 `REPOPILOT_EMBEDDING_API_KEY`，不要提交该文件。DeepSeek 官方文档给出了 `https://api.deepseek.com` 和 `deepseek-v4-pro`；百炼官方文档给出了兼容端点和 `text-embedding-v4` 的 1024 维调用方式。

首版深度支持 Java/Spring Boot + Maven。上传资料和文档生成均服务于代码任务、需求分析、设计、接口、测试和变更说明。

### 运行遥测与成本估算

每个任务都会产生受控的 `telemetry.json`：记录 LangGraph 节点耗时，以及模型供应商明确回传的输入、输出和总 Token。桌面端的“本次运行用量”面板、任务报告和 `GET /api/tasks/{thread_id}/telemetry` 返回同一份汇总；它们不包含提示词、完整代码、模型原文或密钥。

若希望显示本地成本估算，可在未提交的 `.env` 中同时配置以下三个值。单价按每 100 万 Token 计；任一单价缺失、币种不同或供应商未回传用量时，RepoPilot 会明确显示“不可估算”，不会伪造 `0` 成本。

~~~dotenv
REPOPILOT_CHAT_INPUT_PRICE_PER_MILLION=
REPOPILOT_CHAT_OUTPUT_PRICE_PER_MILLION=
REPOPILOT_CHAT_PRICE_CURRENCY=CNY
~~~

若要启用任务级预算门禁，再配置以下任一上限。预算由启动后端的环境变量读取，在任务创建时冻结进 SQLite checkpoint；桌面端、任务描述、Skill、MCP 返回内容和模型都只能查看，不能上调或关闭。模型每次调用后都会校验：超过上限即 `BLOCKED`；已配置预算却没有供应商用量或可靠成本时，同样停止继续调用，避免无声超支。

~~~dotenv
REPOPILOT_TASK_MAX_TOTAL_TOKENS=120000
REPOPILOT_TASK_MAX_ESTIMATED_COST=3.00
~~~

## 真实 Demo 前置检查

首次演示前先运行只读诊断。只有 Chat、Embedding、Qdrant 和 SQLite 均为 `READY`，Agent 才会进入 RAG、研究与计划阶段：

~~~powershell
docker compose up -d qdrant
uv run repopilot-guard doctor
~~~

安全隔离修复要求目标目录是具有至少一个提交的 Git 仓库，因为它需要创建 detached worktree、冻结基线并在完成后生成可信 Diff。建议使用项目副本或专用演示分支准备基线，不要让 RepoPilot 自动初始化、提交或清理你的工作目录：

~~~powershell
git -C D:\code\demo-spring-app status
git -C D:\code\demo-spring-app rev-parse HEAD
~~~

非 Git 项目仅能在经过二次确认的完全本机控制模式下执行只读研究；它无法创建 Worktree，也无法提供 Git Diff 证据，因此不能作为完整修复通过 `PASSED` 验收。桌面端会提前提示并禁用该项目的安全隔离修复入口。

## 产品交互

首次使用时，用户通过系统文件夹选择器添加本地项目；项目根目录、显示名称和最近使用时间会保存在本地 SQLite。之后直接从侧边栏项目列表选择，不需要反复输入目录。创建任务时，桌面端只提供两种清晰模式：

| 产品模式 | 运行时组合 | 含义 |
|---|---|---|
| 安全隔离修复 | `Worktree + safe` | 默认推荐。Agent 在隔离 Git worktree 中工作，风险动作需要审批。 |
| 完全本机控制 | `Local + full` | 用户二次确认后启用。Agent 在当前本地项目中执行已实现的高风险工具，并完整审计。 |

默认模式是安全隔离修复：

~~~
用户当前项目
        |
        v
创建 detached worktree
        |
        v
Agent 在隔离目录读代码、改代码、跑测试
        |
        v
用户查看 diff，选择继续在 worktree、创建分支或交接回 Local
~~~

Worktree 不是自动创建的新分支。它是同一 Git 仓库的第二个工作目录，默认处于 detached HEAD；用户需要时再显式创建分支。Local 更接近 Codex 的直接修改体验，适合用户愿意让 Agent 在当前项目目录操作的场景。

## 核心能力

- 本地 Coding Agent：代码理解、Bug 修复、小型需求实现、工程文档草稿；
- 双模式工作区：安全隔离修复默认隔离和审批；完全本机控制由用户按任务强确认，并执行已实现的高风险工具；
- 代码与文档 RAG：按项目、提交、路径和来源过滤检索；在已过滤候选集内融合向量、关键词和路径信号稳定重排，结果携带引用与评分依据；
- Context Broker：按总字符预算组合项目规则、RAG、Skill 目录/选中正文与能力快照，并把来源和内容哈希冻结在任务 checkpoint；
- Skills：按项目/用户/内置作用域发现标准 `SKILL.md`，目录阶段只披露名称、描述和路径，显式或确定性匹配选中后才加载正文；
- MCP：基于官方 Python SDK 连接 STDIO/Streamable HTTP 服务；任务只能绑定项目配置中已发现、`read_only` 且经用户批准的工具，并在每次调用前复验配置和输入 Schema；
- Capability Plane：内置工具、Skill 与 MCP 工具进入同一能力目录，再由任务权限、审批和 `PolicyGuard` 决定是否执行；
- LangGraph 工作流：任务可暂停、审批、恢复；服务端 Trace ID 贯穿任务索引、SSE 事件和桌面审阅，所有节点和工具事件可追踪；模型 Token/成本上限会随任务冻结并 fail-closed；
- 真实工程证据：diff、Maven 测试、来源、权限授予和工具调用共同决定最终结论。

## 架构

~~~
Tauri Desktop + React / CLI
        |
任务模式：安全隔离修复 / 完全本机控制
        |
FastAPI + SSE
        |
Task Service + LangGraph Coding Workflow
        |
Context Broker：RAG / 会话摘要 / 项目规则
        |
Capability Plane：Built-in Tools / Skills / MCP
        |
Trust Gateway：权限快照 / 审批 / PolicyGuard / 审计
        |
Workspace Manager + Git + Maven + Qdrant + SQLite + Evidence
~~~

## 当前进度

已实现并通过自动测试：

- LangChain/LangGraph/Qdrant、SQLite checkpoint、项目注册、Git 基线/Worktree、safe/full 权限与受控只读工具；
- Java/XML/MD/TXT RAG、仅写已验证事实的项目长期记忆、受限研究循环、结构化计划、计划重写反馈与计划/执行两级审批；
- 结构化补丁、固定 Maven Recipe、Git Diff、FastAPI/SSE、持久任务/产物清单、不可变产物版本历史和 React 本机审阅界面；
- 标准 `SKILL.md` 渐进发现、作用域覆盖、内容篡改检测，以及 MCP TOML 配置、真实 STDIO/Streamable HTTP Transport、工具发现、受控调用、限长输出和熔断；
- 15 条评测任务定义、独立 Git fixture/端到端执行器、修改范围校验与持续增长的 `unittest` 回归集。

已真实联调：

- 使用 OpenAI-compatible Chat/Embedding、Qdrant 和真实 Spring Cloud 项目，完成上下文索引、受控研究、SSE 事件推送和计划审批暂停；
- 代码理解任务不会修改项目文件，也不会将计划误报为修复成功；
- J01 已完成真实 Java 闭环：修复前 2 个 JUnit 测试中 1 个失败，Agent 经两级审批后只修改 `OrderController.java`，隔离 Worktree 中 Maven 测试通过，源仓库保持不变；
- J02 已完成增强版真实闭环：修复前跨租户与空租户测试失败，Agent 只修改 `OrderService.java`，修复后 4 个测试全部通过且源 fixture 保持不变；
- J03、J04、J06 已完成真实 Java 闭环：分别修复 Mapper 分页 SQL、DTO `@NotBlank` 校验和错误 Java release，三项均只修改契约允许路径并按声明的 Maven Recipe 成功验证；
- J01、J02、J03、J04、J06 已完成真实 Maven 修复前基线验证，五项均按任务契约失败且源 fixture 的 HEAD/Git 状态保持不变；
- 计划和补丁 JSON 支持一次受限契约纠错，Embedding 支持瞬时错误重试，审计只记录脱敏字段与异常类型。
- MCP 已用真实本地 STDIO 与 Streamable HTTP 测试 Server 验证 initialize、tools/list、tools/call、ping、超时和关闭；安全模式默认不连接，桌面端探测后可显式勾选具体工具，Graph 只绑定该任务冻结的 `read_only` 工具，并在调用前复验配置与 Schema。
- Context Broker 已接入 `RETRIEVE -> ANALYZE`：项目 `AGENTS.md`、项目 Skill、RAG 片段和只读工具清单按预算组合；Skill 篡改、超预算和禁用模型调用均不会被静默放过。

仍需补强：

- 继续完成 J05、J07 与安全/失败场景的真实 Agent 评测，并为 J05、J07 和 S/V 场景补齐对应行为或策略基线；
- 已实现协作式取消：API 会向当前图执行器发送任务级取消信号，节点边界会停止后续副作用，RepoPilot 自己启动的 Maven 子进程会被终止并以 `MAVEN_CANCELLED` 留痕；模型服务本身仍取决于其 SDK/HTTP 调用是否支持主动中断；
- 为事件物理归档和产物版本历史补齐可配置的保留/压缩策略；
- 为 MCP 补 OAuth、任务生命周期持久连接池、服务级并发限制与大输出 Artifact 化；当前任务使用短连接和冻结 Schema，不会在后台长期保留高权限会话；
- 为 Context Broker 补会话摘要、真正的 BM25/符号索引和模型重排，并继续建设插件包、Hooks、多语言 Profile 与受控子 Agent；
- 任意 Shell、网络、commit/push 只有在对应工具、权限和审计边界完成后才会开放。

## 当前本地运行

~~~powershell
uv sync --python 3.12
uv run python -m unittest discover -s tests -t . -v
~~~

## CLI 快速开始

当原生桌面端尚未打包时，推荐通过 `task` 完成日常 Coding Agent 流程。`task` 只提供两种产品模式，并输出脱敏的任务摘要、审批状态和下一步命令；底层 `agent`、`workspace`、`index` 等命令继续保留给调试、评测和高级排障。

### 安装 CLI

开发者可以在仓库内直接使用 `uv run`。需要将 CLI 交给其他本机用户试用时，使用 Python 3.12 和 `uv` 安装隔离工具环境；安装不会写入模型 Key，仍需在运行前配置 `.env` 或等价环境变量：

~~~powershell
uv tool install .
repopilot-guard --help
~~~

首次使用可以先运行 `welcome`。它只读取 RepoPilot 的本地项目登记和 Git/Maven 预检，不调用模型、不创建 worktree、不索引代码，也不会修改任何项目文件。输出中的 `next_action.command` 会依据最近使用的项目给出下一条推荐命令；完全本机控制仍要求你手动审阅风险并显式确认：

~~~powershell
uv run repopilot-guard welcome
~~~

发布前可构建 wheel 与源码包，再从 wheel 安装验证：

~~~powershell
uv build
uv tool install .\dist\repopilot_guard-0.1.0-py3-none-any.whl
~~~

先注册本地 Git 项目。项目路径只在本机 SQLite 中保存，注册本身不会扫描、索引或修改代码：

~~~powershell
uv run repopilot-guard project add --path D:\code\sample-spring-app --name "订单服务"
uv run repopilot-guard project list
~~~

注册后先执行项目诊断。它只读取项目结构和 Git 状态，不创建 worktree、不索引代码、不调用模型；输出会明确区分“可安全隔离修复”“只能完全本机控制研究”以及 Java/Maven Profile 是否就绪：

~~~powershell
uv run repopilot-guard project doctor --project-id project-xxxx
~~~

以默认的“安全隔离修复”启动任务。RepoPilot 会在 detached worktree 中研究代码，生成计划后暂停；终端输出中的 `next_action.command` 可直接复制执行：

~~~powershell
uv run repopilot-guard task start --project-id project-xxxx --task "订单查询缺少租户权限过滤，请定位根因并提出修复计划" --thread-id order-permission-001
uv run repopilot-guard task list
uv run repopilot-guard task status --thread-id order-permission-001
uv run repopilot-guard task events --thread-id order-permission-001 --after-sequence 0
uv run repopilot-guard task decide --thread-id order-permission-001 --decision approve
~~~

`task list` 只显示任务 ID、项目 ID、模式、状态、判定和时间，不输出仓库或产物绝对路径。`task events` 读取 SQLite 中已经脱敏的证据，并返回可用于增量轮询的 `next_sequence`；重复执行时将该值传给 `--after-sequence` 即可只读取新事件。终态任务可归档但不会删除任何证据：

`task status` 会优先读取 LangGraph checkpoint，返回计划、工作区、验证和下一步操作；若 Chat、Embedding 等无关配置格式错误，或 checkpoint 暂时不可读取，则退回本机 SQLite 任务索引并返回 `TASK_STATUS_INDEX_ONLY`。回退结果只包含已持久化状态，不会猜测计划、验证证据或审批类型，也不会输出仓库绝对路径。

~~~powershell
uv run repopilot-guard task archive --thread-id order-permission-001
uv run repopilot-guard task list --include-archived
~~~

任务暂停或结束后，可以从 SQLite 登记的受控产物中审阅计划、真实 Diff、Maven 验证和报告。CLI 不接受任意文件路径；读取前会校验产物的 SHA-256，篡改或超出大小限制会明确 `BLOCKED`：

~~~powershell
uv run repopilot-guard task artifacts --thread-id order-permission-001
uv run repopilot-guard task artifact --thread-id order-permission-001 --kind plan_markdown
uv run repopilot-guard task artifact --thread-id order-permission-001 --kind git_diff
uv run repopilot-guard task artifact --thread-id order-permission-001 --kind verification
~~~

需要让 Agent 参考需求、接口或研发说明时，显式导入 MD/TXT 文档。RepoPilot 会把副本保存到本机状态目录后再索引，不修改项目目录，也不会在命令输出、RAG Payload 或 Agent 上下文中泄露最初选择的绝对路径：

~~~powershell
uv run repopilot-guard document add --project-id project-xxxx --file D:\docs\order-requirements.md
uv run repopilot-guard document list --project-id project-xxxx
~~~

若计划方向不对，不要批准后再修改。可以要求 Agent 基于反馈重新生成计划：

~~~powershell
uv run repopilot-guard task decide --thread-id order-permission-001 --decision revise --comment "先检查 Service 层的租户边界，不要修改 Controller。"
~~~

完全本机控制必须按任务明确确认。它固定映射为 `Local + full`，并且不会继承给下一次任务：

~~~powershell
uv run repopilot-guard task start --project-id project-xxxx --task "修复本地订单权限问题" --task-mode full-local --confirm-full-access "我已了解完全权限风险"
~~~

为干净源仓库创建默认保留的隔离 worktree：

~~~powershell
uv run repopilot-guard workspace prepare --repo D:\code\sample-spring-app --task "分析订单权限问题"
~~~

注册项目后，可以用项目 ID 创建 Local 或 Worktree 任务，无需重复输入目录：

~~~powershell
uv run repopilot-guard project add --path D:\code\sample-spring-app
uv run repopilot-guard workspace prepare --project-id project-xxxx --task "分析订单权限问题" --mode worktree --include-uncommitted-changes
uv run repopilot-guard index project --project-id project-xxxx
uv run repopilot-guard search context --project-id project-xxxx --repo-commit <提交哈希> --query "订单权限"
~~~

配置模型与 Qdrant 后，可运行只读 Agent。它会生成计划并在确认处暂停，不会修改代码：

~~~powershell
uv run repopilot-guard agent plan --repo D:\code\sample-spring-app --task "订单查询缺少店铺权限" --thread-id order-permission-001
uv run repopilot-guard agent resume --thread-id order-permission-001 --approved true
~~~

若项目已通过桌面端或 `mcp probe` 确认工具，可在安全模式显式授权具体 capability；未提供该参数时不会连接 MCP：

~~~powershell
uv run repopilot-guard agent plan --repo D:\code\sample-spring-app --task "根据研发文档分析订单权限" --approve-mcp-tool mcp__docs__search
~~~

完全本机控制必须明确确认，且仅对当前任务有效：

~~~powershell
uv run repopilot-guard workspace prepare --repo D:\code\sample-spring-app --task "分析订单权限问题" --mode local --permission full --confirm-full-access "我已了解完全权限风险"
~~~

发现项目和用户 Skills。`list` 不读取正文进入目录，`inspect` 才加载选中的 Skill；Skill 中的脚本不会被这两个命令执行：

~~~powershell
uv run repopilot-guard skill list --repo D:\code\sample-spring-app
uv run repopilot-guard skill inspect --repo D:\code\sample-spring-app --name java-maven-maintenance
~~~

只读校验 MCP 配置与任务权限。该命令不会启动 STDIO 进程或连接远程服务：

~~~powershell
uv run repopilot-guard mcp validate --config D:\code\sample-spring-app\.repopilot\mcp.toml
~~~

STDIO MCP 配置中使用裸命令 `python`、`python3`、`python.exe` 或 `python3.exe` 时，RepoPilot 会固定使用自身当前的 Python 运行时，避免 Windows 的 `PATH` 拾取不兼容解释器。需要指定其它环境时，请在 MCP 配置中填写明确的相对路径或绝对路径；RepoPilot 不会改写带路径的命令。

真实探测 MCP 会完成握手、工具发现与 Ping，然后主动关闭；真实调用还会执行 JSON Schema、权限、超时和输出上限检查：

~~~powershell
uv run repopilot-guard mcp probe --config .\examples\mcp.local-echo.toml --server local-echo --workspace-root D:\code\RepoPilot --permission full --confirm-full-access "我已了解完全权限风险"
uv run repopilot-guard mcp call --config .\examples\mcp.local-echo.toml --server local-echo --workspace-root D:\code\RepoPilot --permission full --confirm-full-access "我已了解完全权限风险" --tool mcp__local-echo__echo --arguments-file .\examples\mcp.echo.arguments.json
~~~

项目桌面端也可读取项目内 `.repopilot/mcp.toml` 并进行单次探测。探测结果中的工具可勾选为“授权给下一次任务”；后端仍会重新发现工具并验证配置/Schema 哈希，不接受前端提供的工具定义。该入口和任务调用当前均采用短连接，不会在后台长期保留高权限 MCP 会话。

当任务进入研究阶段后，桌面端会显示“本次研究上下文”面板。它只展示冻结的来源、Skill 名称、已绑定工具、字符预算和快照指纹，便于审阅 RAG/Skill 的实际边界；完整代码、Skill 正文和模型提示不会通过该接口返回。

本地插件包使用 `repopilot-plugin.json`，可以声明 Skill 目录、MCP 配置引用和 UI 元数据。安装会计算整个插件目录的 SHA-256 清单；内容变化后不会继续作为 Agent 上下文来源，必须由用户审查后显式重新安装。插件不会执行脚本、不会自动连接 MCP，也不会新增权限通道：

~~~powershell
uv run repopilot-guard plugin install --source .\examples\plugins\spring-maintenance
uv run repopilot-guard plugin list
uv run repopilot-guard plugin disable --plugin-id spring-maintenance
uv run repopilot-guard plugin audit --plugin-id spring-maintenance
~~~

插件启用且完整性通过时，其 Skill 才会进入 Agent 的候选目录，并继续受上下文预算、项目优先级、`PolicyGuard` 和任务权限约束。插件包含的 MCP 配置目前仅作为可审阅元数据；后续会增加任务级显式绑定，不能自动联网或启动进程。

## 桌面端测试

React 页面已可以生产构建。当前可先使用以下一键脚本测试与桌面端完全相同的本机前端和 FastAPI：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\start-desktop-preview.ps1
```

脚本会启动仅监听 `127.0.0.1:8765` 的 Python API，并在浏览器打开 `http://127.0.0.1:1420`。关闭 Vite 终端后，脚本会同时终止 API。调试原生窗口时，Tauri 会自动复用或启动 API：

```powershell
cd .\desktop
npm.cmd run tauri:dev
```

当前稳定入口是 CLI wheel 与浏览器预览，原生 `.exe` 进入打包验收阶段。正式安装包必须同时具备 Windows C++ Build Tools、Windows SDK 和 Python Agent sidecar，不能只打出 Tauri WebView 外壳。`desktop doctor` 会从 PATH 以及 Visual Studio/Windows SDK 标准安装目录发现 `link.exe`、`rc.exe`、`mt.exe`；诊断通过只代表具备构建条件，最终仍要以真实 Tauri 构建、安装、启动和进程回收测试为准。`REPOPILOT_BACKEND_EXECUTABLE` 仅用于开发或受控部署时替换默认后端启动命令；目标程序必须支持 `api serve --host 127.0.0.1 --port 8765` 参数。开发环境不需要设置该变量，Tauri 默认使用 `uv`。

生成 sidecar 不会安装系统软件，也不会把 `.env`、模型密钥、Qdrant 数据或用户项目写入安装包：

```powershell
.\scripts\build-desktop-backend-sidecar.ps1
uv run repopilot-guard desktop doctor
cd .\desktop
npm.cmd run tauri:build
```

构建脚本会通过 `uv` 临时使用 PyInstaller，在 `desktop/src-tauri/binaries/repopilot-guard.exe` 生成后端可执行文件。该文件被 `.gitignore` 排除，便于本机或 CI 按目标平台重新构建；`desktop doctor` 只有在该文件和 Tauri 资源配置同时存在时才会报告 sidecar 就绪。

安装后的桌面端不会依赖仓库工作目录。它在系统应用数据目录中读取 `.env` 并保存 `state.sqlite`；可通过以下只读命令查看当前系统对应的准确路径。命令不会创建目录、读取配置或显示密钥：

```powershell
uv run repopilot-guard desktop paths
```

开发模式仍从 RepoPilot 仓库根目录读取现有 `.env`，因此浏览器预览和 `tauri:dev` 的配置方式保持兼容。

先执行只读诊断，区分浏览器预览和原生安装包所缺的环境项。它不会启动服务、执行 Cargo 或安装组件：

```powershell
uv run repopilot-guard desktop doctor
```

## 文档

- [产品需求文档](RepoPilot-PRD.md)
- [分阶段开发计划](开发计划.md)
- [v2 最小闭环优化设计](docs/RepoPilot-v2-最小闭环优化设计.md)
- [企业级编程助手平台架构](docs/企业级编程助手平台架构.md)
- [插件包规范与学习实验](docs/插件包规范与学习实验.md)
- [阶段二学习实验](docs/阶段二-隔离工作区与权限模式.md)
- [阶段三学习实验](docs/阶段三-项目注册与RAG.md)
- [阶段四学习实验](docs/阶段四-LangGraph只读研究工作流.md)

所有面向用户的文档、报告、UI 文案和代码注释使用中文。
