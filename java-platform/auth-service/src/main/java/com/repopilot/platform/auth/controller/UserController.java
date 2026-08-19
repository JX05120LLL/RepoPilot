package com.repopilot.platform.auth.controller;

import com.repopilot.platform.auth.user.User;
import com.repopilot.platform.auth.user.UserRepository;
import com.repopilot.platform.common.tenant.TenantContext;
import org.springframework.security.access.prepost.PreAuthorize;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import java.util.List;
import java.util.Map;

/**
 * 用户管理（ADMIN）。列表按当前租户强制过滤，跨租户数据不可见。
 */
@RestController
@RequestMapping("/api/users")
public class UserController {

    private final UserRepository userRepository;

    public UserController(UserRepository userRepository) {
        this.userRepository = userRepository;
    }

    @GetMapping
    @PreAuthorize("hasRole('ADMIN')")
    public List<Map<String, Object>> list() {
        String tenantId = TenantContext.require();
        return userRepository.findAllByTenantId(tenantId).stream()
                .map(this::toDto)
                .toList();
    }

    private Map<String, Object> toDto(User user) {
        return Map.of(
                "id", user.getId(),
                "username", user.getUsername(),
                "email", user.getEmail(),
                "role", user.getRole(),
                "tenant_id", user.getTenantId()
        );
    }
}
