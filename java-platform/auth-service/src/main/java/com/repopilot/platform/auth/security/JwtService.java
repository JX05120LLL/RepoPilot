package com.repopilot.platform.auth.security;

import io.jsonwebtoken.Claims;
import io.jsonwebtoken.Jwts;
import io.jsonwebtoken.security.Keys;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;

import javax.crypto.SecretKey;
import java.nio.charset.StandardCharsets;
import java.time.Instant;
import java.util.Date;

/**
 * JWT 签发与校验。Access Token 短时效，Refresh Token 长时效。
 * 密钥从配置注入，绝不硬编码或入库。
 */
@Service
public class JwtService {

    private final SecretKey key;
    private final long accessTtlSeconds;
    private final long refreshTtlSeconds;

    public JwtService(
            @Value("${repopilot.auth.jwt-secret}") String secret,
            @Value("${repopilot.auth.access-ttl-seconds:900}") long accessTtlSeconds,
            @Value("${repopilot.auth.refresh-ttl-seconds:604800}") long refreshTtlSeconds
    ) {
        this.key = Keys.hmacShaKeyFor(secret.getBytes(StandardCharsets.UTF_8));
        this.accessTtlSeconds = accessTtlSeconds;
        this.refreshTtlSeconds = refreshTtlSeconds;
    }

    public String generateAccessToken(String subject, String tenantId, String role) {
        return generate(subject, tenantId, role, accessTtlSeconds, "access");
    }

    public String generateRefreshToken(String subject, String tenantId) {
        return generate(subject, tenantId, null, refreshTtlSeconds, "refresh");
    }

    private String generate(String subject, String tenantId, String role, long ttlSeconds, String tokenType) {
        Instant now = Instant.now();
        return Jwts.builder()
                .subject(subject)
                .claim("tenant_id", tenantId)
                .claim("token_type", tokenType)
                .claim("role", role)
                .issuedAt(Date.from(now))
                .expiration(Date.from(now.plusSeconds(ttlSeconds)))
                .signWith(key)
                .compact();
    }

    public Claims parse(String token) {
        return Jwts.parser()
                .verifyWith(key)
                .build()
                .parseSignedClaims(token)
                .getPayload();
    }

    public long accessTtlSeconds() {
        return accessTtlSeconds;
    }
}
