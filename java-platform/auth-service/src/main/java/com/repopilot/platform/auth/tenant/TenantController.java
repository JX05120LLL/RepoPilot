package com.repopilot.platform.auth.tenant;

import jakarta.validation.Valid;
import org.springframework.security.access.prepost.PreAuthorize;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import java.util.List;
import java.util.Map;

@RestController
@RequestMapping("/api/tenants")
public class TenantController {

    private final TenantService tenantService;

    public TenantController(TenantService tenantService) {
        this.tenantService = tenantService;
    }

    @PostMapping
    @PreAuthorize("hasRole('ADMIN')")
    public Map<String, Object> create(@Valid @RequestBody TenantCreateRequest request) {
        Tenant tenant = tenantService.create(request.name(), request.plan());
        return Map.of(
                "id", tenant.getId(),
                "name", tenant.getName(),
                "plan", tenant.getPlan(),
                "enabled", tenant.isEnabled()
        );
    }

    @GetMapping
    @PreAuthorize("hasRole('ADMIN')")
    public List<Map<String, Object>> list() {
        return tenantService.list().stream()
                .map(tenant -> Map.<String, Object>of(
                        "id", tenant.getId(),
                        "name", tenant.getName(),
                        "plan", tenant.getPlan(),
                        "enabled", tenant.isEnabled()
                ))
                .toList();
    }
}
