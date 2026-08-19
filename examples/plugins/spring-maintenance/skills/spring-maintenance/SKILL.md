---
name: spring-maintenance
description: Spring Boot 与 Maven 维护任务的审阅清单
allowed-tools: read_file, search_code, inspect_build, retrieve_context
user-invocable: true
disable-model-invocation: false
---

先确认模块边界、Controller、Service、Repository/Mapper 和现有测试，再提出最小修改方案。
不要将本 Skill 的文本视为权限授予；文件读取、补丁和 Maven 验证仍由 RepoPilot 的 PolicyGuard、审批和 Tool Runtime 决定。
