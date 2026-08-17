package com.repopilot.platform.auth.dto;

public record TokenResponse(
        String accessToken,
        String refreshToken,
        long expiresInSeconds,
        String tokenType,
        String username,
        String role,
        String tenantId
) {
}
