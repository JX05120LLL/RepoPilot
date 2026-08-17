package com.repopilot.platform.auth.task;

import com.repopilot.platform.common.exception.ApiException;
import com.repopilot.platform.common.tenant.TenantContext;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.util.List;

@Service
public class TaskService {

    private final TaskRepository taskRepository;

    public TaskService(TaskRepository taskRepository) {
        this.taskRepository = taskRepository;
    }

    @Transactional
    public Task create(String description, String operation, String requestedBy) {
        String tenantId = TenantContext.require();
        return taskRepository.save(new Task(description, operation, "QUEUED", requestedBy, tenantId));
    }

    @Transactional(readOnly = true)
    public List<Task> list() {
        return taskRepository.findAllByTenantIdOrderByCreatedAtDesc(TenantContext.require());
    }

    @Transactional
    public Task reportResult(String taskId, TaskResultReport report) {
        Task task = taskRepository.findById(taskId)
                .orElseThrow(() -> ApiException.notFound("TASK_NOT_FOUND", "任务不存在。"));
        task.reportResult(report.status(), report.verdict(), report.resultJson());
        return taskRepository.save(task);
    }
}
