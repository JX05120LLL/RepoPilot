package com.repopilot.platform.auth.tenant;

import com.repopilot.platform.common.exception.ApiException;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.util.List;

@Service
public class TenantService {

    private final TenantRepository tenantRepository;

    public TenantService(TenantRepository tenantRepository) {
        this.tenantRepository = tenantRepository;
    }

    @Transactional
    public Tenant create(String name, String plan) {
        if (tenantRepository.existsByName(name)) {
            throw ApiException.conflict("TENANT_NAME_TAKEN", "租户名称已被占用。");
        }
        return tenantRepository.save(new Tenant(name, plan));
    }

    @Transactional(readOnly = true)
    public List<Tenant> list() {
        return tenantRepository.findAll();
    }
}
