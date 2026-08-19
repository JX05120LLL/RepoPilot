package com.repopilot.platform.auth.user;

import com.repopilot.platform.common.exception.ApiException;

/**
 * 平台内置角色。角色名映射为 Spring Security 的 authority（ROLE_ 前缀）。
 */
public enum Role {

    ADMIN,
    DEVELOPER,
    VIEWER;

    public String authority() {
        return "ROLE_" + name();
    }

    /**
     * 从外部输入解析角色；非法值直接拒绝，避免把任意字符串当权限写入。
     */
    public static Role fromCode(String code) {
        if (code == null || code.isBlank()) {
            throw ApiException.badRequest("INVALID_ROLE", "角色不能为空。");
        }
        try {
            return Role.valueOf(code.trim().toUpperCase());
        } catch (IllegalArgumentException ex) {
            throw ApiException.badRequest("INVALID_ROLE", "未知角色：" + code);
        }
    }
}
