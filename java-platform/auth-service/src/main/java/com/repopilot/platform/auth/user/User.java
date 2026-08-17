package com.repopilot.platform.auth.user;

import com.repopilot.platform.common.entity.BaseEntity;
import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.Index;
import jakarta.persistence.Table;

/**
 * 平台用户。密码只保存 BCrypt 摘要，绝不保存明文。
 * 租户内 email 唯一；用户名全平台唯一。
 */
@Entity
@Table(
        name = "platform_user",
        indexes = {
                @Index(name = "idx_user_tenant_email", columnList = "tenant_id,email", unique = true),
                @Index(name = "idx_user_username", columnList = "username", unique = true)
        }
)
public class User extends BaseEntity {

    @Column(name = "username", nullable = false, length = 64)
    private String username;

    @Column(name = "email", nullable = false, length = 128)
    private String email;

    @Column(name = "password_hash", nullable = false, length = 128)
    private String passwordHash;

    @Column(name = "role", nullable = false, length = 32)
    private String role;

    @Column(name = "enabled", nullable = false)
    private boolean enabled = true;

    protected User() {
    }

    public User(String username, String email, String passwordHash, String role, String tenantId) {
        this.username = username;
        this.email = email;
        this.passwordHash = passwordHash;
        this.role = role;
        setTenantId(tenantId);
    }

    public String getUsername() {
        return username;
    }

    public String getEmail() {
        return email;
    }

    public String getPasswordHash() {
        return passwordHash;
    }

    public String getRole() {
        return role;
    }

    public boolean isEnabled() {
        return enabled;
    }

    public void setEnabled(boolean enabled) {
        this.enabled = enabled;
    }
}
