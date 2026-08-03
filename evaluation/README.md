# RepoPilot 评测任务集

默认 `tasks.json` 定义 15 个可重放的 Java/Maven 维护场景。每个任务记录预期修改范围、固定 Build Recipe 和预期状态；评测以真实 Diff、Build 结果、修改范围、权限拦截与 JSONL Evidence 为依据，不以模型文本自评为成功依据。

`profiles/tasks.json` 额外定义 Gradle、pytest、Node/npm 与 Node/pnpm 的最小任务集：每种 Profile 均包含修复、真实失败、安全阻断和 checkpoint 恢复场景。该目录已验证 fixture 生成、Recipe 分派与报告投影；pytest、npm 与 pnpm 的真实修复前失败基线可在本机复验，pnpm 通过固定 `corepack pnpm` 或直接 `pnpm` 入口运行。Gradle fixture 不打包 Wrapper，因此运行机器必须提供 Gradle Wrapper 或本机 Gradle；缺失时验证会如实输出 `GRADLE_UNAVAILABLE`/`BLOCKED`，不能当作失败基线或能力通过。尚未完成跨模型重复运行统计，因此不能替代默认 Java/Maven 评测的稳定性结论。

J01、J02、J03、J04、J06 与 V02 使用行为型 fixture，覆盖 Controller 空白参数、Service 跨租户与空租户访问、Mapper 分页条件、DTO `@NotBlank`、错误 Java release 和 checkpoint 恢复。V01 额外保留一个与目标修改无关的真实失败测试，用于证明 Agent 不会把“已经修改代码”误报为 `PASSED`。本地完整评测产物默认写入 `.repopilot-evaluation/`，该目录已被 Git 忽略；可提交的脱敏结果摘要见 [RESULTS.md](RESULTS.md)。

使用以下命令会为默认 15 项任务分别生成独立的最小 Java/Maven Git 仓库，并写入固定 `HEAD`、场景信息及路径断言到 `fixtures.json`、`fixtures.csv`：

```powershell
uv run repopilot-guard evaluate prepare --output D:\repopilot-evaluation\run-001
```

要准备 Gradle、pytest、Node/npm 或 Node/pnpm Profile fixture，显式切换目录，避免与默认评测统计混合：

```powershell
uv run repopilot-guard evaluate prepare `
  --catalog evaluation/profiles/tasks.json `
  --output D:\repopilot-evaluation\profiles-001
```

输出目录必须为空，工具不会覆盖已有评测证据。`fixture_status=READY` 只表示基线仓库与静态断言准备完成；`agent_status=NOT_RUN` 明确表示尚未运行真实模型、补丁或受控验证 Recipe，不能视为修复成功。下一步端到端执行时，应为每项 fixture 记录模型/提示版本、任务 thread ID、真实 Diff、验证结果与安全断言，再生成最终评测结论。

不调用模型即可验证所有已声明的修复前基线：

```powershell
uv run repopilot-guard evaluate validate-baseline `
  --fixtures D:\repopilot-evaluation\run-001 `
  --output D:\repopilot-evaluation\baseline-001 `
  --all
```

验证器会把每项 fixture clone 到独立目录后执行固定 Build Recipe，保存退出码、截断日志、对应测试报告清单和源 fixture 不变断言。任务声明的基线为 `FAILED` 时，构建失败且证据完整才是验证成功；构建意外通过、不可用或源 fixture 被改变都会使报告失败。

在已配置模型、Embedding、Qdrant 且确认愿意消耗模型额度后，可以对单项 fixture 运行真实 Graph：

```powershell
uv run repopilot-guard evaluate run `
  --fixtures D:\repopilot-evaluation\run-001 `
  --output D:\repopilot-evaluation\result-j01 `
  --task-id J01 `
  --approval auto
```

`--approval auto` 只会在独立 fixture 内自动通过计划和执行审批；它不会放宽 `PolicyGuard`，也不会操作你的真实项目。输出包含 `evaluation-report.json`、`evaluation-report.csv`、`evaluation-report.md`。报告保存实际 `actual_status`、`changed_paths`、`scope_valid`、Maven 状态与验证代码；模型阻断、仍待审批、Maven 失败或修改范围越界时，即使任务定义期望 `PASSED` 也会记为不匹配。需要批量运行时必须显式传入 `--all`。

如需观察同一模型配置在多轮任务中的稳定性，使用矩阵模式。它会将每轮原始证据分别写入 `run-001`、`run-002` 等子目录，并额外生成 `evaluation-matrix.json`、`evaluation-matrix.csv`、`evaluation-matrix.md`；汇总报告只统计匹配率和实际终态分布，不能替代单轮的 Diff、审批和验证审阅：

```powershell
uv run repopilot-guard evaluate matrix `
  --fixtures D:\repopilot-evaluation\run-001 `
  --output D:\repopilot-evaluation\matrix-j01 `
  --task-id J01 `
  --approval auto `
  --repetitions 3
```

矩阵至少运行 2 次、最多 30 次。任意一轮任务与期望不匹配时，命令返回 `FAILED` 和退出码 `2`，不会以平均值掩盖失败；运行前应固定代码版本、fixture 集合、模型名、Embedding 模型和系统环境。

JSON 和 Markdown 报告会自动记录 RepoPilot 版本与 Git 状态、任务目录 SHA-256、fixture 集合 SHA-256、操作系统、Python 版本及脱敏 Provider 标识。报告不会写入 Base URL、API Key 或本机 Maven 绝对安装路径；模型名称格式异常时以 `INVALID_IDENTIFIER_REDACTED` 代替。

每次发布候选版本必须重新生成 fixture，不能复用旧目录。fixture 内容由当前 `evaluation.py` 生成；复用旧目录会让任务定义、失败测试和 Git 基线与当前代码不一致，导致评测结果失去可比性。
