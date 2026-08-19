package com.repopilot.platform.auth;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.http.MediaType;
import org.springframework.test.web.servlet.MockMvc;

import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

/**
 * 多租户隔离：A 租户的管理员查询用户时，只能看到本租户用户，看不到 B 租户用户。
 */
@SpringBootTest
@AutoConfigureMockMvc
class TenantIsolationTest {

    @Autowired
    private MockMvc mockMvc;

    @Autowired
    private ObjectMapper objectMapper;

    @Test
    void crossTenantUsersAreIsolated() throws Exception {
        register("iso-alice", "iso-tenant-a");
        register("iso-mallory", "iso-tenant-b");

        String aliceToken = login("iso-alice");

        String usersJson = mockMvc.perform(get("/api/users").header("Authorization", "Bearer " + aliceToken))
                .andExpect(status().isOk())
                .andReturn().getResponse().getContentAsString();

        assertTrue(usersJson.contains("iso-alice"), "本租户用户应可见");
        assertFalse(usersJson.contains("iso-mallory"), "跨租户用户不应可见");
    }

    private void register(String username, String tenantId) throws Exception {
        String body = """
                {"username":"%s","email":"%s@example.com","password":"secret123","tenantId":"%s","role":"ADMIN"}
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
        return json.get("accessToken").asText();
    }
}
