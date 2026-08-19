package com.repopilot.platform.auth.controller;

import com.repopilot.platform.common.tenant.TenantContext;
import org.springframework.security.core.Authentication;
import org.springframework.security.core.GrantedAuthority;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 当前登录用户信息（受保护接口，用于验证 JWT 认证链路）。
 */
@RestController
@RequestMapping("/api/me")
public class MeController {

    @GetMapping
    public Map<String, Object> me(Authentication authentication) {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("username", authentication.getName());
        List<String> authorities = authentication.getAuthorities().stream()
                .map(GrantedAuthority::getAuthority)
                .toList();
        body.put("authorities", authorities);
        body.put("tenant_id", TenantContext.get());
        return body;
    }
}
