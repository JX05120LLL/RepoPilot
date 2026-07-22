# 阶段三学习实验：项目注册、工作区与 RAG

## 1. 为什么要有 Project Registry

Project Registry 可以理解成桌面应用的“最近项目列表”后端。它把项目 ID 和本地目录保存到 SQLite。以后聊天任务只需要传 `project_id`，不必反复输入 `D:\code\...` 路径。

它不是扫描电脑硬盘：只有用户主动添加的目录才会写入注册表。

## 2. Local 和 Worktree 的区别

- `Local`：任务直接绑定当前项目目录。本阶段只读取和建立快照，不会修改源仓库。
- `Worktree`：Git 额外创建一个目录，默认 detached HEAD，适合让 Agent 在隔离副本工作。

未提交改动默认不会被悄悄带入 Worktree。用户显式指定 `include_uncommitted_changes` 后，系统才应用 tracked diff 并复制允许的未跟踪文件；安全模式仍会排除 `.env` 等敏感文件。

## 3. RAG 到底在做什么

RAG 不是把整个仓库一次性发给模型，而是分为四步：

~~~text
允许的代码/文档文件
  -> 按字符和行号切成 chunk
  -> Embedding 变成向量并写入 Qdrant
  -> 按项目 ID 与提交哈希检索相关 chunk
~~~

每个 chunk 都带路径、起止行、来源类型、内容哈希和项目/提交信息。因此模型回答“订单鉴权在哪里”时，后续可以展示对应文件和行号，而不是给出无法核对的猜测。

## 4. 为什么要按项目和提交过滤

两个项目可能都有 `OrderService`；同一项目不同提交的代码也可能不同。检索时必须同时过滤 `project_id` 和 `repo_commit`，避免把另一个项目或旧版本的内容混进当前任务上下文。

## 5. 当前命令实验

~~~powershell
uv run repopilot-guard project add --path D:\code\sample-spring-app
uv run repopilot-guard project list
uv run repopilot-guard workspace prepare --project-id project-xxxx --task "分析订单权限" --mode worktree
uv run repopilot-guard index project --project-id project-xxxx
uv run repopilot-guard search context --project-id project-xxxx --repo-commit <提交哈希> --query "订单权限"
~~~

本阶段的检索命令会真实调用 Embedding 和 Qdrant，因此需要先配置 `.env` 并启动 Qdrant。缺少配置或服务不可达时，程序返回 `BLOCKED`，不会假装已经建立索引。
