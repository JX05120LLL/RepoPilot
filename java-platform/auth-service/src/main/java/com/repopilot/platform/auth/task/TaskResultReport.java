package com.repopilot.platform.auth.task;

import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Size;

/**
 * Python 引擎回写的任务结果：状态、结论与 Diff/验证证据 JSON。
 */
public record TaskResultReport(
        @NotBlank @Size(max = 32) String status,
        @Size(max = 32) String verdict,
        @Size(max = 1_000_000) String resultJson
) {
}
