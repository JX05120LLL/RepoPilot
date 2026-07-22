# 阶段四学习实验：LangGraph 只读研究工作流

## 1. Agent 不是一次模型调用

普通聊天通常是“问题 -> 回答”。Coding Agent 则需要多步完成任务：准备工作区、检查环境、找相关代码、阅读文件、形成计划、等待确认。LangGraph 把这些步骤组织成可以暂停和恢复的状态图。

~~~text
INTAKE -> WORKSPACE -> PREFLIGHT -> INGEST -> RETRIEVE
                                      |
                                      v
                           ANALYZE <-> RESEARCH_TOOLS
                                      |
                                      v
                              PLAN -> APPROVAL -> REPORT
~~~

## 2. 为什么模型不能直接调用 Shell

模型只看到五个只读工具：列目录、搜索代码、读取文件、检查 Maven 配置和检索 RAG。工具执行前仍经过 `PolicyGuard`，因此模型即使输出 `run_shell`、路径逃逸或敏感文件，也只会收到 `BLOCKED` 结果。

这就是 Agent 的关键架构思维：**模型负责提出下一步，程序负责决定这一步能不能执行。**

## 3. 什么是受限循环

模型可能先搜索 `OrderService`，再读取命中文件，最后继续搜索权限校验调用链。这个过程需要循环，但循环不能无限进行。本项目限制为最多 6 轮模型决策、12 次工具调用；超过上限后仍可输出计划，但要标明证据不足。

## 4. checkpoint 与审批

LangGraph 把状态保存到 SQLite，并用 `thread_id` 找回同一个任务。计划生成后进入 `WAITING_APPROVAL`：关闭程序、重启后仍能恢复到同一个确认点。

确认计划不等于确认修改代码。阶段四只保存计划，最终报告永远是 `UNVERIFIED`；阶段五才会在同一线程加入补丁和 Maven 验证。

## 5. 运行条件

真实 `agent plan` 需要 OpenAI-compatible Chat/Embedding 配置和正在运行的 Qdrant。缺少任一条件时会返回 `BLOCKED`，这是为了避免“没有证据却假装分析成功”。单元测试使用 fake model、fake Embedding 和 fake Qdrant 验证图逻辑，不需要真实密钥。
