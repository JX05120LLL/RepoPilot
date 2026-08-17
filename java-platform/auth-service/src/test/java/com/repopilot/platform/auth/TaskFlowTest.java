package com.repopilot.platform.auth;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.http.MediaType;
import org.springframework.test.web.servlet.MockMvc;

import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

/**
 * 任务编排：用户创建任务 -> Python 引擎经 service-token 回写结果 -> 用户列表可见。
 */
@SpringBootTest
@AutoConfigureMockMvc
class TaskFlowTest {

    @Autowired
    private MockMvc mockMvc;

    @Autowired
    private ObjectMapper objectMapper;

    @Test
    void createReportAndListTask() throws Exception {
        register("task-user", "task-tenant-a");
        String token = login("task-user");

        String createResponse = mockMvc.perform(post("/api/tasks")
                        .header("Authorization", "Bearer " + token)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"description\":\"修复订单查询 bug\",\"operation\":\"change\"}"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.status").value("QUEUED"))
                .andReturn().getResponse().getContentAsString();

        String taskId = objectMapper.readTree(createResponse).get("id").asText();

        mockMvc.perform(post("/api/tasks/" + taskId + "/result")
                        .header("X-Service-Token", "test-service-token")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"status\":\"REPORT\",\"verdict\":\"PASSED\",\"resultJson\":\"{\\\"git_diff\\\":\\\"...\\\"}\"}"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.status").value("REPORT"))
                .andExpect(jsonPath("$.verdict").value("PASSED"));

        mockMvc.perform(get("/api/tasks").header("Authorization", "Bearer " + token))
                .andExpect(status().isOk());
    }

    @Test
    void reportResultRejectsWrongServiceToken() throws Exception {
        register("task-user-2", "task-tenant-b");
        String token = login("task-user-2");

        String createResponse = mockMvc.perform(post("/api/tasks")
                        .header("Authorization", "Bearer " + token)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"description\":\"分析订单流程\",\"operation\":\"research\"}"))
                .andExpect(status().isOk())
                .andReturn().getResponse().getContentAsString();

        String taskId = objectMapper.readTree(createResponse).get("id").asText();

        mockMvc.perform(post("/api/tasks/" + taskId + "/result")
                        .header("X-Service-Token", "wrong-token")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"status\":\"REPORT\",\"verdict\":\"PASSED\",\"resultJson\":\"{}\"}"))
                .andExpect(status().isUnauthorized())
                .andExpect(jsonPath("$.code").value("SERVICE_TOKEN_INVALID"));
    }

    private void register(String username, String tenantId) throws Exception {
        String body = """
                {"username":"%s","email":"%s@example.com","password":"secret123","tenantId":"%s","role":"DEVELOPER"}
                """.formatted(username, username, tenantId);
        mockMvc.perform(post("/api/auth/register")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(body))
                .andExpect(status().isOk());
    }

    private String login(String username) throws Exception {
        String body = """
                {"username":"%s","password":"secret123"}
                """.formatted(username);
        String response = mockMvc.perform(post("/api/auth/login")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(body))
                .andExpect(status().isOk())
                .andReturn().getResponse().getContentAsString();
        JsonNode json = objectMapper.readTree(response);
        assertTrue(json.has("accessToken"));
        return json.get("accessToken").asText();
    }
}
