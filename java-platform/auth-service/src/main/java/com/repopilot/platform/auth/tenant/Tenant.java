package com.repopilot.platform.auth.tenant;

import com.repopilot.platform.common.entity.BaseEntity;
import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.Index;
import jakarta.persistence.Table;

/**
 * 租户：SaaS 隔离边界。每个租户对应一个独立的数据范围。
 * 套餐字段（plan）为 3.4 预留，先以字符串承载。
 */
@Entity
@Table(
        name = "platform_tenant",
        indexes = {
                @Index(name = "idx_tenant_name", columnList = "name", unique = true)
        }
)
public class Tenant extends BaseEntity {

    @Column(name = "name", nullable = false, length = 64)
    private String name;

    @Column(name = "plan", nullable = false, length = 32)
    private String plan;

    @Column(name = "enabled", nullable = false)
    private boolean enabled = true;

    protected Tenant() {
    }

    public Tenant(String name, String plan) {
        this.name = name;
        this.plan = plan;
        setTenantId("system");
    }

    public String getName() {
        return name;
    }

    public String getPlan() {
        return plan;
    }

    public boolean isEnabled() {
        return enabled;
    }

    public void setEnabled(boolean enabled) {
        this.enabled = enabled;
    }
}
