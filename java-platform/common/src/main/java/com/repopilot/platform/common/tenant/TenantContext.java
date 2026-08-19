package com.repopilot.platform.common.tenant;

/**
 * 租户上下文：从 JWT 中提取的 tenant_id，随请求线程透传。
 * 由 JWT 过滤器在进入业务前写入，请求结束时清理。
 */
public final class TenantContext {

    private static final ThreadLocal<String> CURRENT_TENANT = new ThreadLocal<>();

    private TenantContext() {
    }

    public static void set(String tenantId) {
        CURRENT_TENANT.set(tenantId);
    }

    public static String get() {
        return CURRENT_TENANT.get();
    }

    public static String require() {
        String tenantId = CURRENT_TENANT.get();
        if (tenantId == null || tenantId.isBlank()) {
            throw new IllegalStateException("TENANT_CONTEXT_MISSING");
        }
        return tenantId;
    }

    public static void clear() {
        CURRENT_TENANT.remove();
    }
}
