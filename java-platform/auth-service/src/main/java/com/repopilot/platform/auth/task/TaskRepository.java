package com.repopilot.platform.auth.task;

import org.springframework.data.jpa.repository.JpaRepository;

import java.util.List;

public interface TaskRepository extends JpaRepository<Task, String> {

    List<Task> findAllByTenantIdOrderByCreatedAtDesc(String tenantId);
}
