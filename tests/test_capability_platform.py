from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from langchain_core.tools import StructuredTool

from repopilot_guard.capabilities import (
    CapabilityDescriptor,
    CapabilityKind,
    CapabilityPolicy,
    CapabilityRegistry,
    CapabilityRisk,
    CapabilityScope,
)
from repopilot_guard.mcp import (
    McpCapabilityRegistry,
    McpConfigError,
    McpConfigLoader,
    McpToolDescriptor,
)
from repopilot_guard.permissions import FULL_ACCESS_CONFIRMATION, PermissionGrant, PermissionMode
from repopilot_guard.skills import SkillError, SkillRegistry
from repopilot_guard.tool_runtime import ToolRuntime


def _write_skill(root: Path, name: str, description: str, body: str, *, allowed_tools: str = "read_file") -> Path:
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    path = skill_dir / "SKILL.md"
    path.write_text(
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        f"allowed-tools: {allowed_tools}\n"
        "user-invocable: true\n"
        "disable-model-invocation: false\n"
        "---\n"
        f"{body}\n",
        encoding="utf-8",
    )
    return path


class SkillRegistryTests(unittest.TestCase):
    def test_project_skill_overrides_user_and_body_is_progressively_loaded(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            user_root = root / "user-skills"
            project = root / "project"
            _write_skill(user_root, "java-fix", "用户规则", "不要出现在目录中")
            project_path = _write_skill(project / ".agents" / "skills", "java-fix", "项目规则", "只修改候选 Java 文件")

            registry = SkillRegistry.discover(project_root=project, user_roots=(user_root,))
            catalog = registry.catalog().to_dict()

            self.assertEqual("project", catalog["skills"][0]["scope"])
            self.assertNotIn("只修改候选", str(catalog))
            loaded = registry.load("java-fix")
            self.assertEqual(project_path.resolve(), loaded.manifest.path)
            self.assertIn("只修改候选 Java 文件", loaded.instructions)
            self.assertEqual("UNTRUSTED_SKILL_INSTRUCTIONS", loaded.to_dict()["security_label"])

    def test_skill_change_after_discovery_requires_rediscovery(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            path = _write_skill(project / ".agents" / "skills", "java-fix", "项目规则", "初始正文")
            registry = SkillRegistry.discover(project_root=project)
            path.write_text(path.read_text(encoding="utf-8") + "已变化\n", encoding="utf-8")

            with self.assertRaisesRegex(SkillError, "SKILL_CHANGED_AFTER_DISCOVERY"):
                registry.load("java-fix")

    def test_invalid_skill_is_reported_without_blocking_valid_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            skills = project / ".agents" / "skills"
            _write_skill(skills, "valid-skill", "有效规则", "正文")
            invalid = skills / "invalid"
            invalid.mkdir(parents=True)
            (invalid / "SKILL.md").write_text("没有 frontmatter", encoding="utf-8")

            catalog = SkillRegistry.discover(project_root=project).catalog().to_dict()

            self.assertEqual(["valid-skill"], [item["name"] for item in catalog["skills"]])
            self.assertEqual("SKILL_FRONTMATTER_REQUIRED", catalog["issues"][0]["code"])

    def test_yaml_alias_is_blocked_before_loading_skill_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            skill_dir = project / ".agents" / "skills" / "alias-skill"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\nname: alias-skill\ndescription: &desc 规则\ncompatibility: *desc\n---\n正文\n",
                encoding="utf-8",
            )

            issues = SkillRegistry.discover(project_root=project).issues

            self.assertEqual("SKILL_YAML_ALIAS_BLOCKED", issues[0].code)

    def test_catalog_budget_omits_metadata_instead_of_loading_bodies(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            skills = project / ".agents" / "skills"
            _write_skill(skills, "first-skill", "第一条规则", "很长的正文" * 100)
            _write_skill(skills, "second-skill", "第二条规则", "另一段正文" * 100)

            catalog = SkillRegistry.discover(project_root=project).catalog(max_chars=160)

            self.assertTrue(catalog.truncated)
            self.assertGreater(catalog.omitted_count, 0)


class McpConfigurationTests(unittest.TestCase):
    def test_remote_configuration_uses_env_reference_and_requires_safe_approval(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "mcp.toml"
            path.write_text(
                "version = 1\n"
                "[[servers]]\n"
                'name = "docs"\n'
                'transport = "streamable_http"\n'
                'url = "https://mcp.example.com/v1"\n'
                'scope = "project"\n'
                'access = "read_only"\n'
                'bearer_token_env = "DOCS_MCP_TOKEN"\n',
                encoding="utf-8",
            )

            configuration = McpConfigLoader.load(path)
            server = configuration.servers[0]
            descriptor = server.capability()
            policy = CapabilityPolicy()

            waiting = policy.decide(descriptor, PermissionGrant.safe())
            approved = policy.decide(descriptor, PermissionGrant.safe(), approved=True)
            self.assertEqual("CAPABILITY_APPROVAL_REQUIRED", waiting.code)
            self.assertTrue(waiting.requires_approval)
            self.assertTrue(approved.allowed)
            self.assertEqual("CONFIGURED_NOT_CONNECTED", server.to_dict()["connection_status"])

    def test_stdio_is_blocked_in_safe_and_allowed_in_confirmed_full(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "mcp.toml"
            path.write_text(
                "[[servers]]\n"
                'name = "local-tools"\n'
                'transport = "stdio"\n'
                'command = "mcp-local-tools"\n'
                'args = ["--stdio"]\n',
                encoding="utf-8",
            )
            descriptor = McpConfigLoader.load(path).servers[0].capability()
            policy = CapabilityPolicy()

            safe = policy.decide(descriptor, PermissionGrant.safe(), approved=True)
            full = policy.decide(
                descriptor,
                PermissionGrant(PermissionMode.FULL, FULL_ACCESS_CONFIRMATION),
            )

            self.assertEqual("PROCESS_CAPABILITY_BLOCKED_SAFE", safe.code)
            self.assertTrue(full.allowed)
            self.assertEqual("USER_GRANTED_FULL_ACCESS", full.code)

    def test_scope_escalation_and_secret_command_argument_are_rejected(self) -> None:
        cases = (
            '[[servers]]\nname="bad"\ntransport="streamable_http"\nurl="https://example.com"\nscope="user"\n',
            '[[servers]]\nname="bad"\ntransport="stdio"\ncommand="server"\nargs=["--token", "secret-value"]\n',
        )
        expected_codes = ("MCP_SCOPE_MISMATCH", "MCP_SECRET_ARGUMENT_BLOCKED")
        for content, expected_code in zip(cases, expected_codes, strict=True):
            with self.subTest(expected_code=expected_code), tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / "mcp.toml"
                path.write_text(content, encoding="utf-8")
                with self.assertRaises(McpConfigError) as raised:
                    McpConfigLoader.load(path)
                self.assertEqual(expected_code, raised.exception.code)

    def test_stdio_arguments_are_not_exposed_in_audit_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "mcp.toml"
            path.write_text(
                '[[servers]]\nname="local"\ntransport="stdio"\ncommand="server"\nargs=["--stdio"]\n',
                encoding="utf-8",
            )

            payload = McpConfigLoader.load(path).servers[0].to_dict()

            self.assertNotIn("args", payload)
            self.assertEqual(1, payload["args_count"])

    def test_inline_secret_and_url_credentials_are_rejected(self) -> None:
        cases = (
            '[[servers]]\nname="bad"\ntransport="streamable_http"\nurl="https://example.com"\ntoken="secret"\n',
            '[[servers]]\nname="bad"\ntransport="streamable_http"\nurl="https://user:secret@example.com"\n',
        )
        for content in cases:
            with self.subTest(content=content[:30]), tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / "mcp.toml"
                path.write_text(content, encoding="utf-8")
                with self.assertRaises(McpConfigError):
                    McpConfigLoader.load(path)

    def test_mcp_tools_are_namespaced_and_filtered_by_allowlist(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "mcp.toml"
            path.write_text(
                "[[servers]]\n"
                'name="docs"\n'
                'transport="streamable_http"\n'
                'url="https://mcp.example.com"\n'
                'allowed_tools=["search"]\n',
                encoding="utf-8",
            )
            registry = McpCapabilityRegistry(McpConfigLoader.load(path))
            registered = registry.register_tools(
                "docs",
                (
                    McpToolDescriptor("docs", "search", "搜索文档", {"type": "object", "properties": {}}),
                    McpToolDescriptor("docs", "delete", "删除文档", {"type": "object", "properties": {}}),
                ),
            )

            self.assertEqual(["mcp__docs__search"], [item.capability_id for item in registered])


class CapabilityRuntimeTests(unittest.TestCase):
    def test_safe_mode_distinguishes_builtin_recipe_from_external_process(self) -> None:
        policy = CapabilityPolicy()
        builtin = CapabilityDescriptor(
            capability_id="maven_test",
            name="maven_test",
            description="运行固定 Maven test Recipe。",
            kind=CapabilityKind.BUILTIN_TOOL,
            scope=CapabilityScope.BUNDLED,
            source="repopilot:maven",
            risks=frozenset({CapabilityRisk.PROCESS}),
        )
        external = CapabilityDescriptor(
            capability_id="mcp_server__local",
            name="local",
            description="外部 STDIO MCP。",
            kind=CapabilityKind.MCP_SERVER,
            scope=CapabilityScope.PROJECT,
            source="mcp:local",
            risks=frozenset({CapabilityRisk.PROCESS}),
        )

        self.assertEqual("CAPABILITY_APPROVAL_REQUIRED", policy.decide(builtin, PermissionGrant.safe()).code)
        self.assertTrue(policy.decide(builtin, PermissionGrant.safe(), approved=True).allowed)
        self.assertEqual(
            "PROCESS_CAPABILITY_BLOCKED_SAFE",
            policy.decide(external, PermissionGrant.safe(), approved=True).code,
        )

    def test_runtime_enforces_capability_policy_before_tool_execution(self) -> None:
        calls: list[str] = []

        def external_lookup(query: str) -> dict[str, object]:
            calls.append(query)
            return {"status": "READY", "code": "LOOKUP_COMPLETE"}

        tool = StructuredTool.from_function(external_lookup, name="external_lookup", description="远程查询。")
        descriptor = CapabilityDescriptor(
            capability_id="external_lookup",
            name="external_lookup",
            description="远程查询。",
            kind=CapabilityKind.BUILTIN_TOOL,
            scope=CapabilityScope.BUNDLED,
            source="test",
            risks=frozenset({CapabilityRisk.READ, CapabilityRisk.NETWORK}),
        )
        capabilities = CapabilityRegistry((descriptor,))

        blocked = ToolRuntime((tool,), capabilities=capabilities).invoke("external_lookup", {"query": "java"})
        self.assertEqual("CAPABILITY_APPROVAL_REQUIRED", blocked.code)
        self.assertEqual([], calls)

        allowed = ToolRuntime(
            (tool,),
            capabilities=capabilities,
            approved_capabilities=("external_lookup",),
        ).invoke("external_lookup", {"query": "java"})

        self.assertEqual("LOOKUP_COMPLETE", allowed.code)
        self.assertEqual(["java"], calls)

    def test_registry_rejects_duplicate_capability_atomically(self) -> None:
        descriptor = CapabilityDescriptor(
            capability_id="read_file",
            name="read_file",
            description="读取文件。",
            kind=CapabilityKind.BUILTIN_TOOL,
            scope=CapabilityScope.BUNDLED,
            source="test",
        )
        registry = CapabilityRegistry((descriptor,))

        with self.assertRaisesRegex(ValueError, "DUPLICATE_CAPABILITY_REGISTRATION"):
            registry.register_many((descriptor,))
        self.assertEqual(1, len(registry.list()))


if __name__ == "__main__":
    unittest.main()
