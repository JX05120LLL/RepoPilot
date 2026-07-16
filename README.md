# RepoPilot Guard

RepoPilot Guard 是一个专注 Java/Spring Boot 仓库的安全 Coding Agent。它通过 LangChain、LangGraph 和 Qdrant 组合代码 RAG、研发文档 RAG、任务记忆和受控工具调用，帮助开发者理解代码、修复 Bug、实现小型需求，并生成与代码上下文一致的工程文档草稿。

它不是通用聊天助手：上传文档只能作为代码任务上下文，文档生成只服务需求、设计、接口、测试和变更说明。代码修改、文档写入和 Maven 测试都在隔离 Git worktree 中完成，并且有审批、diff、测试与审计证据。

## 核心能力

- 代码任务：定位 Java/Spring Boot Bug，生成计划、补丁、测试和报告；
- 代码与文档 RAG：检索仓库代码、项目文档和用户上传资料，并显示来源；
- LangGraph 工作流：支持状态图、checkpoint、人工确认、测试失败后的有限恢复；
- Qdrant 项目记忆：保存带项目/提交/来源 metadata 的代码与文档向量，以及已验证的项目事实；
- 工程文档草稿：基于代码和需求上下文生成 PRD、技术方案、接口说明、测试计划和变更说明；
- 安全执行：工具白名单、敏感文件保护、worktree 隔离、Maven Recipe 和证据链。

## 技术架构

```text
React Web Chat / CLI
        |
FastAPI + SSE
        |
LangGraph Coding Workflow
        |
LangChain Model / Tools / Retriever
        |
Qdrant: 代码、文档、长期项目记忆
        |
Git worktree + Maven Recipe + Evidence Store
```

## 当前进度

当前已完成安全控制骨架：CLI 干跑、Java/Maven 预检、状态机、敏感路径与 Maven Recipe 白名单、JSONL 审计报告和单元测试。

接下来会加入 LangChain、LangGraph、Qdrant、真实 worktree、代码/文档索引、模型工具调用和 Web Chat。当前骨架尚未执行真实补丁、Maven 或模型调用，因此只会返回 `UNVERIFIED` 或 `BLOCKED`，不会误报修复成功。

## 当前本地运行

针对一个 Java Git 仓库执行干跑任务：

```powershell
$env:PYTHONPATH = "src"
python -m repopilot_guard run --repo D:\code\sample-spring-app --task "订单查询缺少店铺权限过滤"
```

只执行预检：

```powershell
$env:PYTHONPATH = "src"
python -m repopilot_guard inspect --repo D:\code\sample-spring-app
```

运行单元测试：

```powershell
$env:PYTHONPATH = "src"
python -m unittest discover -s tests -t . -v
```

## 文档

- [产品需求文档](RepoPilot-PRD.md)
- [分阶段开发计划](开发计划.md)

所有 README、面向用户的报告、Web 文案和代码注释使用中文。

