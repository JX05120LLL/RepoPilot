package com.repopilot.platform.auth.tenant;

import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Size;

public record TenantCreateRequest(
        @NotBlank @Size(min = 2, max = 64) String name,
        @NotBlank @Size(max = 32) String plan
) {
}
