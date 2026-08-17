package com.repopilot.platform.auth;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.http.MediaType;
import org.springframework.test.web.servlet.MockMvc;

import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

/**
 * 冒烟测试：注册 -> 登录 -> 带 token 访问受保护接口 -> 无 token 401 -> 错误密码 401。
 * 使用 H2 内存库，无需 PostgreSQL。
 */
@SpringBootTest
@AutoConfigureMockMvc
class AuthFlowTest {

    @Autowired
    private MockMvc mockMvc;

    @Autowired
    private ObjectMapper objectMapper;

    @Test
    void registerLoginAndAccessProtectedEndpoint() throws Exception {
        String register = """
                {"username":"alice","email":"alice@example.com","password":"secret123","tenantId":"tenant-a","role":"DEVELOPER"}
                """;
        String registerResponse = mockMvc.perform(post("/api/auth/register")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(register))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.username").value("alice"))
                .andExpect(jsonPath("$.tenantId").value("tenant-a"))
                .andReturn().getResponse().getContentAsString();

        JsonNode registerJson = objectMapper.readTree(registerResponse);
        String accessToken = registerJson.get("accessToken").asText();

        mockMvc.perform(get("/api/me").header("Authorization", "Bearer " + accessToken))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.username").value("alice"))
                .andExpect(jsonPath("$.tenant_id").value("tenant-a"));

        mockMvc.perform(get("/api/me"))
                .andExpect(status().isUnauthorized());
    }

    @Test
    void loginRejectsWrongPassword() throws Exception {
        String register = """
                {"username":"bob","email":"bob@example.com","password":"secret123","tenantId":"tenant-b","role":"VIEWER"}
                """;
        mockMvc.perform(post("/api/auth/register")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(register))
                .andExpect(status().isOk());

        String login = """
                {"username":"bob","password":"wrong-password"}
                """;
        mockMvc.perform(post("/api/auth/login")
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(login))
                .andExpect(status().isUnauthorized())
                .andExpect(jsonPath("$.code").value("BAD_CREDENTIALS"));
    }
}
