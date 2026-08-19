package com.repopilot.platform.common.entity;

import jakarta.persistence.Column;
import jakarta.persistence.GeneratedValue;
import jakarta.persistence.GenerationType;
import jakarta.persistence.Id;
import jakarta.persistence.MappedSuperclass;
import jakarta.persistence.PrePersist;
import jakarta.persistence.PreUpdate;

import java.time.Instant;
import java.util.UUID;

/**
 * 实体基类：UUID 主键、租户列、审计时间。
 * 采用 row-level 多租户隔离：业务表统一带 tenant_id，查询由 JPA 拦截器/过滤强制约束。
 */
@MappedSuperclass
public abstract class BaseEntity {

    @Id
    @GeneratedValue(strategy = GenerationType.UUID)
    @Column(name = "id", updatable = false, nullable = false, length = 36)
    private String id;

    @Column(name = "tenant_id", nullable = false, length = 64)
    private String tenantId;

    @Column(name = "created_at", nullable = false, updatable = false)
    private Instant createdAt;

    @Column(name = "updated_at", nullable = false)
    private Instant updatedAt;

    @PrePersist
    void onCreate() {
        Instant now = Instant.now();
        this.createdAt = now;
        this.updatedAt = now;
        if (this.tenantId == null) {
            this.tenantId = TenantContextHolder.tenantIdOrSystem();
        }
    }

    @PreUpdate
    void onUpdate() {
        this.updatedAt = Instant.now();
    }

    public String getId() {
        return id;
    }

    public String getTenantId() {
        return tenantId;
    }

    public void setTenantId(String tenantId) {
        this.tenantId = tenantId;
    }

    public Instant getCreatedAt() {
        return createdAt;
    }

    public Instant getUpdatedAt() {
        return updatedAt;
    }

    /**
     * 解耦：实体基类不直接依赖租户上下文的具体实现，便于测试与未来替换。
     */
    static final class TenantContextHolder {
        static String tenantIdOrSystem() {
            try {
                String tenantId = com.repopilot.platform.common.tenant.TenantContext.get();
                return tenantId == null || tenantId.isBlank() ? "system" : tenantId;
            } catch (RuntimeException ex) {
                return "system";
            }
        }
    }
}
