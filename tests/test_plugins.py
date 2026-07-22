from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from repopilot_guard.plugins import PluginError, PluginRegistry
from repopilot_guard.skills import SkillRegistry


def _write_plugin(root: Path, *, with_mcp: bool = True) -> None:
    (root / "skills" / "spring-review").mkdir(parents=True)
    (root / "repopilot-plugin.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "id": "spring-tools",
                "name": "Spring 工程规范",
                "version": "1.0.0",
                "description": "提供 Spring Boot 维护流程。",
                "skills_root": "skills",
                **({"mcp_config": "mcp.toml"} if with_mcp else {}),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (root / "skills" / "spring-review" / "SKILL.md").write_text(
        "---\n"
        "name: spring-review\n"
        "description: Spring Boot 代码审阅\n"
        "allowed-tools: read_file, search_code\n"
        "---\n"
        "先确认 Controller、Service 和测试的现有边界。\n",
        encoding="utf-8",
    )
    if with_mcp:
        (root / "mcp.toml").write_text(
            '[[servers]]\nname = "docs"\ntransport = "streamable_http"\nurl = "https://mcp.example.com"\n',
            encoding="utf-8",
        )


class PluginRegistryTests(unittest.TestCase):
    def test_install_exposes_only_verified_plugin_skill_roots_and_audits_actions(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            plugin_root = root / "plugin"
            _write_plugin(plugin_root)
            registry = PluginRegistry(root / "state.sqlite")
            try:
                installed = registry.install(plugin_root)

                self.assertTrue(installed.enabled)
                self.assertEqual("VERIFIED", installed.integrity_status)
                self.assertEqual([plugin_root / "skills"], list(registry.active_skill_roots()))
                self.assertEqual([plugin_root / "mcp.toml"], list(registry.active_mcp_configs()))
                skills = SkillRegistry.discover(plugin_roots=registry.active_skill_roots())
                self.assertEqual("plugin", skills.manifest("spring-review").scope.value)
                self.assertEqual("PLUGIN_INSTALLED", registry.audit()[0]["action"])

                disabled = registry.disable("spring-tools")
                self.assertFalse(disabled.enabled)
                self.assertEqual((), registry.active_skill_roots())
                self.assertEqual("PLUGIN_DISABLED", registry.audit()[0]["action"])
            finally:
                registry.close()

    def test_modified_plugin_fails_closed_until_user_explicitly_reinstalls(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            plugin_root = root / "plugin"
            _write_plugin(plugin_root, with_mcp=False)
            registry = PluginRegistry(root / "state.sqlite")
            try:
                registry.install(plugin_root)
                skill = plugin_root / "skills" / "spring-review" / "SKILL.md"
                skill.write_text(skill.read_text(encoding="utf-8") + "\n被外部进程改写。\n", encoding="utf-8")

                self.assertEqual("TAMPERED", registry.get("spring-tools").integrity_status)
                self.assertEqual((), registry.active_skill_roots())
                with self.assertRaisesRegex(PluginError, "PLUGIN_INTEGRITY_CHECK_FAILED"):
                    registry.enable("spring-tools")
                self.assertEqual("PLUGIN_ENABLE_BLOCKED", registry.audit()[0]["action"])

                reinstalled = registry.install(plugin_root)
                self.assertEqual("VERIFIED", reinstalled.integrity_status)
                self.assertEqual("PLUGIN_REINSTALLED", registry.audit()[0]["action"])
            finally:
                registry.close()

    def test_manifest_path_escape_and_remove_do_not_delete_source_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            plugin_root = root / "plugin"
            _write_plugin(plugin_root)
            manifest = json.loads((plugin_root / "repopilot-plugin.json").read_text(encoding="utf-8"))
            manifest["skills_root"] = "../outside"
            (plugin_root / "repopilot-plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
            registry = PluginRegistry(root / "state.sqlite")
            try:
                with self.assertRaisesRegex(PluginError, "PLUGIN_PATH_ESCAPE"):
                    registry.install(plugin_root)

                manifest["skills_root"] = "skills"
                (plugin_root / "repopilot-plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
                registry.install(plugin_root)
                self.assertTrue(registry.remove("spring-tools"))
                self.assertTrue(plugin_root.is_dir())
                self.assertEqual("PLUGIN_REMOVED", registry.audit("spring-tools")[0]["action"])
            finally:
                registry.close()


if __name__ == "__main__":
    unittest.main()
