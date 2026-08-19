package com.repopilot.platform.auth.task;

import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Size;

public record TaskCreateRequest(
        @NotBlank @Size(max = 12000) String description,
        @NotBlank @Size(max = 16) String operation
) {
}
