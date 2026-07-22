# RepoPilot 评测任务集

`tasks.json` 定义 15 个可重放的 Java/Maven 维护场景。每个任务记录预期修改范围、固定 Maven Recipe 和预期状态；评测以真实 Diff、Maven 结果、修改范围、权限拦截与 JSONL Evidence 为依据，不以模型文本自评为成功依据。

J01、J02、J03、J04、J06 已升级为行为型 fixture，分别覆盖 Controller 空白参数、Service 跨租户与空租户访问、Mapper 分页条件、DTO `@NotBlank` 与错误 Java release。它们在修复前必须真实 Maven 失败；只有 Agent 修改允许文件、修复后 Maven 通过且源仓库保持不变，才计为 `PASSED`。五项均已取得真实 Agent `PASSED`：J01/J02/J03 使用 `test`，J04 使用 `targeted_test`，J06 使用 `compile`；每项报告均包含实际 Diff、退出码、Surefire 清单或编译结果和源 fixture 不变断言。本地真实评测产物默认写入 `.repopilot-evaluation/`，该目录已被 Git 忽略。

使用以下命令会为 15 项任务分别生成独立的最小 Java/Maven Git 仓库，并写入固定 `HEAD`、场景信息及路径断言到 `fixtures.json`、`fixtures.csv`：

```powershell
uv run repopilot-guard evaluate prepare --output D:\repopilot-evaluation\run-001
```

输出目录必须为空，工具不会覆盖已有评测证据。`fixture_status=READY` 只表示基线仓库与静态断言准备完成；`agent_status=NOT_RUN` 明确表示尚未运行真实模型、补丁或 Maven，不能视为修复成功。下一步端到端执行时，应为每项 fixture 记录模型/提示版本、任务 thread ID、真实 Diff、Maven 结果与安全断言，再生成最终评测结论。

不调用模型即可验证所有已声明的修复前基线：

```powershell
uv run repopilot-guard evaluate validate-baseline `
  --fixtures D:\repopilot-evaluation\run-001 `
  --output D:\repopilot-evaluation\baseline-001 `
  --all
```

验证器会把每项 fixture clone 到独立目录后执行固定 Maven Recipe，保存退出码、截断日志、Surefire 文件清单和源 fixture 不变断言。任务声明的基线为 `FAILED` 时，Maven 失败且证据完整才是验证成功；Maven 意外通过、不可用或源 fixture 被改变都会使报告失败。

在已配置模型、Embedding、Qdrant 且确认愿意消耗模型额度后，可以对单项 fixture 运行真实 Graph：

```powershell
uv run repopilot-guard evaluate run `
  --fixtures D:\repopilot-evaluation\run-001 `
  --output D:\repopilot-evaluation\result-j01 `
  --task-id J01 `
  --approval auto
```

`--approval auto` 只会在独立 fixture 内自动通过计划和执行审批；它不会放宽 `PolicyGuard`，也不会操作你的真实项目。输出包含 `evaluation-report.json`、`evaluation-report.csv`、`evaluation-report.md`。报告保存实际 `actual_status`、`changed_paths`、`scope_valid`、Maven 状态与验证代码；模型阻断、仍待审批、Maven 失败或修改范围越界时，即使任务定义期望 `PASSED` 也会记为不匹配。需要批量运行时必须显式传入 `--all`。
