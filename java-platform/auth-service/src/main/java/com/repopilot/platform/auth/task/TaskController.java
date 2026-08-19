package com.repopilot.platform.auth.task;

import com.repopilot.platform.common.exception.ApiException;
import jakarta.validation.Valid;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.security.core.Authentication;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import java.util.List;
import java.util.Map;

/**
 * 任务编排接口。创建/列表走用户 JWT；结果回写走服务间 service-token（Python 引擎专用）。
 */
@RestController
@RequestMapping("/api/tasks")
public class TaskController {

    private final TaskService taskService;
    private final String serviceToken;

    public TaskController(
            TaskService taskService,
            @Value("${repopilot.platform.service-token:}") String serviceToken
    ) {
        this.taskService = taskService;
        this.serviceToken = serviceToken;
    }

    @PostMapping
    public Map<String, Object> create(@Valid @RequestBody TaskCreateRequest request, Authentication authentication) {
        Task task = taskService.create(request.description(), request.operation(), authentication.getName());
        return toDto(task);
    }

    @GetMapping
    public List<Map<String, Object>> list() {
        return taskService.list().stream().map(this::toDto).toList();
    }

    @PostMapping("/{taskId}/result")
    public Map<String, Object> reportResult(
            @PathVariable String taskId,
            @Valid @RequestBody TaskResultReport report,
            @RequestHeader(value = "X-Service-Token", required = false) String providedToken
    ) {
        requireServiceToken(providedToken);
        Task task = taskService.reportResult(taskId, report);
        return toDto(task);
    }

    private void requireServiceToken(String providedToken) {
        if (serviceToken == null || serviceToken.isBlank() || !serviceToken.equals(providedToken)) {
            throw ApiException.unauthorized("SERVICE_TOKEN_INVALID", "服务间凭证无效。");
        }
    }

    private Map<String, Object> toDto(Task task) {
        return Map.of(
                "id", task.getId(),
                "description", task.getDescription(),
                "operation", task.getOperation(),
                "status", task.getStatus(),
                "requested_by", task.getRequestedBy(),
                "verdict", task.getVerdict() == null ? "" : task.getVerdict(),
                "tenant_id", task.getTenantId()
        );
    }
}
