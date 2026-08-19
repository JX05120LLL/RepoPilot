package com.repopilot.platform.auth.user;

import org.springframework.data.jpa.repository.JpaRepository;

import java.util.List;
import java.util.Optional;

public interface UserRepository extends JpaRepository<User, String> {

    Optional<User> findByUsername(String username);

    Optional<User> findByTenantIdAndEmail(String tenantId, String email);

    boolean existsByUsername(String username);

    List<User> findAllByTenantId(String tenantId);
}
