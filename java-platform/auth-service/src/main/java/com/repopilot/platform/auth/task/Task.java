package com.repopilot.platform.auth.task;

import com.repopilot.platform.common.entity.BaseEntity;
import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.Index;
import jakarta.persistence.Table;

import java.time.Instant;

/**
 * 编码任务：Java 平台的任务编排实体，Python 引擎作为执行器。
 * 结果由 Python 引擎经 service-token 接口回写（Diff / 验证证据 / 用量摘要）。
 */
@Entity
@Table(
        name = "platform_task",
        indexes = {
                @Index(name = "idx_task_tenant_status", columnList = "tenant_id,status")
        }
)
public class Task extends BaseEntity {

    @Column(name = "description", nullable = false, length = 12000)
    private String description;

    @Column(name = "operation", nullable = false, length = 16)
    private String operation;

    @Column(name = "status", nullable = false, length = 32)
    private String status;

    @Column(name = "requested_by", nullable = false, length = 64)
    private String requestedBy;

    @Column(name = "verdict", length = 32)
    private String verdict;

    @Column(name = "result_json", columnDefinition = "TEXT")
    private String resultJson;

    @Column(name = "finished_at")
    private Instant finishedAt;

    protected Task() {
    }

    public Task(String description, String operation, String status, String requestedBy, String tenantId) {
        this.description = description;
        this.operation = operation;
        this.status = status;
        this.requestedBy = requestedBy;
        setTenantId(tenantId);
    }

    public String getDescription() {
        return description;
    }

    public String getOperation() {
        return operation;
    }

    public String getStatus() {
        return status;
    }

    public String getRequestedBy() {
        return requestedBy;
    }

    public String getVerdict() {
        return verdict;
    }

    public String getResultJson() {
        return resultJson;
    }

    public Instant getFinishedAt() {
        return finishedAt;
    }

    public void reportResult(String status, String verdict, String resultJson) {
        this.status = status;
        this.verdict = verdict;
        this.resultJson = resultJson;
        this.finishedAt = Instant.now();
    }
}
