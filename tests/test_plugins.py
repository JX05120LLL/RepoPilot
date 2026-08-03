from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from base64 import b64encode
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from repopilot_guard.cli import main
from repopilot_guard.plugins import PluginError, PluginRegistry, _canonical_json, _normalize_repository_url, _package_sha256
from repopilot_guard.skills import SkillRegistry


_TEST_PRIVATE_KEY = Ed25519PrivateKey.generate()
_TEST_KEY_ID = "test-publisher"


def _public_key_base64() -> str:
    return b64encode(
        _TEST_PRIVATE_KEY.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    ).decode("ascii")


def _sign_plugin(root: Path) -> None:
    package_sha256 = _package_sha256(root)
    payload = _canonical_json(
        {"schema_version": 1, "algorithm": "ed25519", "key_id": _TEST_KEY_ID, "package_sha256": package_sha256}
    ).encode("utf-8")
    (root / "repopilot-plugin.sig").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "algorithm": "ed25519",
                "key_id": _TEST_KEY_ID,
                "package_sha256": package_sha256,
                "signature": b64encode(_TEST_PRIVATE_KEY.sign(payload)).decode("ascii"),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _registry(database: Path) -> PluginRegistry:
    registry = PluginRegistry(database)
    registry.add_trust_key(_TEST_KEY_ID, _public_key_base64())
    return registry


def _write_plugin(root: Path, *, with_mcp: bool = True, requires_repopilot: str | None = None) -> None:
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
                **({"requires_repopilot": requires_repopilot} if requires_repopilot else {}),
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
    _sign_plugin(root)


def _git(directory: Path, *arguments: str) -> None:
    subprocess.run(
        ("git", "-C", str(directory), *arguments),
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


class PluginRegistryTests(unittest.TestCase):
    def test_source_lock_url_rejects_malformed_port_without_raw_value_error(self) -> None:
        with self.assertRaisesRegex(PluginError, "PLUGIN_SOURCE_LOCK_INVALID"):
            _normalize_repository_url("https://github.com:invalid/acme/spring-tools.git")

    def test_signed_git_source_lock_blocks_origin_or_revision_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = root / "publisher-repository"
            plugin_root = repository / "plugin"
            _write_plugin(plugin_root, with_mcp=False)
            (plugin_root / "repopilot-plugin.sig").unlink()
            _git(root, "init", "publisher-repository")
            _git(repository, "config", "user.email", "publisher@example.test")
            _git(repository, "config", "user.name", "RepoPilot Test Publisher")
            _git(repository, "add", "plugin/repopilot-plugin.json", "plugin/skills")
            _git(repository, "commit", "-m", "initial plugin")
            _git(repository, "remote", "add", "origin", "git@github.com:acme/spring-tools.git")
            private_key_file = root / "publisher.pem"
            private_key_file.write_bytes(
                _TEST_PRIVATE_KEY.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                )
            )
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(
                    0,
                    main(
                        [
                            "plugin",
                            "sign",
                            "--source",
                            str(plugin_root),
                            "--key-id",
                            _TEST_KEY_ID,
                            "--private-key-file",
                            str(private_key_file),
                            "--lock-current-git-source",
                        ]
                    ),
                )
            self.assertEqual("LOCKED", json.loads(output.getvalue())["signature"]["source_lock_status"])

            registry = _registry(root / "state.sqlite")
            try:
                installed = registry.install(plugin_root)
                self.assertEqual("VERIFIED", installed.signature_status)
                self.assertEqual("LOCKED", installed.source_lock_status)

                _git(repository, "remote", "set-url", "origin", "https://github.com/acme/other-tools.git")
                with self.assertRaisesRegex(PluginError, "PLUGIN_SOURCE_LOCK_MISMATCH"):
                    registry.install(plugin_root)

                _git(repository, "remote", "set-url", "origin", "git@github.com:acme/spring-tools.git")
                _git(repository, "commit", "--allow-empty", "-m", "unrelated revision")
                with self.assertRaisesRegex(PluginError, "PLUGIN_SOURCE_LOCK_MISMATCH"):
                    registry.install(plugin_root)
            finally:
                registry.close()
    def test_cli_signs_package_from_pem_without_exposing_private_key(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            plugin_root = root / "plugin"
            _write_plugin(plugin_root)
            (plugin_root / "repopilot-plugin.sig").unlink()
            private_key_file = root / "publisher.pem"
            private_key_file.write_bytes(
                _TEST_PRIVATE_KEY.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                )
            )
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(
                    0,
                    main(
                        [
                            "plugin",
                            "sign",
                            "--source",
                            str(plugin_root),
                            "--key-id",
                            _TEST_KEY_ID,
                            "--private-key-file",
                            str(private_key_file),
                            "--state-db",
                            str(root / "state.sqlite"),
                        ]
                    ),
                )
            payload = json.loads(output.getvalue())
            self.assertEqual("repopilot-plugin.sig", payload["signature"]["signature_file"])
            self.assertNotIn("private", json.dumps(payload).lower())
            registry = _registry(root / "state.sqlite")
            try:
                self.assertEqual("VERIFIED", registry.install(plugin_root).signature_status)
            finally:
                registry.close()

    def test_cli_manages_local_trust_keys_without_persisting_private_material(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "state.sqlite"
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(
                    0,
                    main(
                        [
                            "plugin",
                            "trust-add",
                            "--key-id",
                            _TEST_KEY_ID,
                            "--public-key-base64",
                            _public_key_base64(),
                            "--state-db",
                            str(database),
                        ]
                    ),
                )
            payload = json.loads(output.getvalue())
            self.assertEqual(_TEST_KEY_ID, payload["trust_key"]["key_id"])
            self.assertNotIn("private", json.dumps(payload).lower())
            self.assertNotIn(_public_key_base64(), json.dumps(payload))

            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(0, main(["plugin", "trust-list", "--state-db", str(database)]))
            self.assertEqual([_TEST_KEY_ID], [item["key_id"] for item in json.loads(output.getvalue())["trust_keys"]])

            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(
                    0,
                    main(["plugin", "trust-remove", "--key-id", _TEST_KEY_ID, "--state-db", str(database)]),
                )
            self.assertEqual("PLUGIN_TRUST_KEY_REMOVED", json.loads(output.getvalue())["code"])

    def test_signature_requires_trusted_publisher_and_trust_revocation_disables_runtime_roots(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            plugin_root = root / "plugin"
            _write_plugin(plugin_root)
            registry = PluginRegistry(root / "state.sqlite")
            try:
                installed = registry.install(plugin_root)
                self.assertEqual("UNTRUSTED", installed.signature_status)
                self.assertFalse(installed.to_dict()["active"])
                self.assertEqual((), registry.active_skill_roots())
                with self.assertRaisesRegex(PluginError, "PLUGIN_SIGNATURE_CHECK_FAILED"):
                    registry.enable("spring-tools")

                registry.add_trust_key(_TEST_KEY_ID, _public_key_base64())
                self.assertEqual("VERIFIED", registry.get("spring-tools").signature_status)
                registry.enable("spring-tools")
                self.assertTrue(registry.get("spring-tools").to_dict()["active"])
                self.assertNotEqual((), registry.active_mcp_configs())

                registry.remove_trust_key(_TEST_KEY_ID)
                revoked = registry.get("spring-tools")
                self.assertEqual("UNTRUSTED", revoked.signature_status)
                self.assertFalse(revoked.to_dict()["active"])
                self.assertEqual((), registry.active_mcp_configs())
                self.assertEqual("PLUGIN_TRUST_KEY_REMOVED", registry.trust_audit()[0]["action"])
            finally:
                registry.close()

    def test_version_incompatible_plugin_is_rejected_and_existing_plugin_fails_closed_after_upgrade(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            incompatible_root = root / "incompatible"
            _write_plugin(incompatible_root, requires_repopilot=">=0.2.0,<0.3.0")
            registry = _registry(root / "state.sqlite")
            try:
                with self.assertRaisesRegex(PluginError, "PLUGIN_REPOPILOT_VERSION_INCOMPATIBLE"):
                    registry.install(incompatible_root)

                compatible_root = root / "compatible"
                _write_plugin(compatible_root, requires_repopilot=">=0.1.0,<0.2.0")
                registry.install(compatible_root)
                with patch("repopilot_guard.plugins.REPOPILOT_VERSION", "0.2.0"):
                    record = registry.get("spring-tools")
                    self.assertEqual("INCOMPATIBLE", record.compatibility_status)
                    self.assertFalse(record.to_dict()["active"])
                    self.assertEqual((), registry.active_skill_roots())
                    self.assertEqual((), registry.active_mcp_configs())
                    registry.disable("spring-tools")
                    with self.assertRaisesRegex(PluginError, "PLUGIN_COMPATIBILITY_CHECK_FAILED"):
                        registry.enable("spring-tools")
                    self.assertEqual("PLUGIN_ENABLE_BLOCKED", registry.audit()[0]["action"])
            finally:
                registry.close()

    def test_cli_lists_versions_and_rolls_back_only_registered_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            plugin_root = root / "plugin"
            _write_plugin(plugin_root, with_mcp=False)
            database = root / "state.sqlite"
            registry = _registry(database)
            try:
                first = registry.install(plugin_root)
                skill = plugin_root / "skills" / "spring-review" / "SKILL.md"
                skill.write_text(skill.read_text(encoding="utf-8") + "\n升级内容。\n", encoding="utf-8")
                _sign_plugin(plugin_root)
                registry.install(plugin_root)
            finally:
                registry.close()

            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(
                    0,
                    main(["plugin", "versions", "--plugin-id", "spring-tools", "--state-db", str(database)]),
                )
            self.assertEqual(2, len(json.loads(output.getvalue())["versions"]))
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(
                    0,
                    main(
                        [
                            "plugin",
                            "rollback",
                            "--plugin-id",
                            "spring-tools",
                            "--package-sha256",
                            first.package_sha256,
                            "--state-db",
                            str(database),
                        ]
                    ),
                )
            self.assertEqual(first.package_sha256, json.loads(output.getvalue())["plugin"]["package_sha256"])

    def test_install_exposes_only_verified_plugin_skill_roots_and_audits_actions(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            plugin_root = root / "plugin"
            _write_plugin(plugin_root)
            registry = _registry(root / "state.sqlite")
            try:
                installed = registry.install(plugin_root)

                self.assertTrue(installed.enabled)
                self.assertEqual("VERIFIED", installed.integrity_status)
                self.assertNotEqual(plugin_root, installed.root_path)
                self.assertEqual([installed.root_path / "skills"], list(registry.active_skill_roots()))
                self.assertEqual([installed.root_path / "mcp.toml"], list(registry.active_mcp_configs()))
                skills = SkillRegistry.discover(plugin_roots=registry.active_skill_roots())
                self.assertEqual("plugin", skills.manifest("spring-review").scope.value)
                self.assertEqual("PLUGIN_INSTALLED", registry.audit()[0]["action"])

                disabled = registry.disable("spring-tools")
                self.assertFalse(disabled.enabled)
                self.assertEqual((), registry.active_skill_roots())
                self.assertEqual("PLUGIN_DISABLED", registry.audit()[0]["action"])
            finally:
                registry.close()

    def test_source_changes_require_explicit_reinstall_and_can_roll_back_to_verified_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            plugin_root = root / "plugin"
            _write_plugin(plugin_root, with_mcp=False)
            registry = _registry(root / "state.sqlite")
            try:
                first = registry.install(plugin_root)
                skill = plugin_root / "skills" / "spring-review" / "SKILL.md"
                skill.write_text(skill.read_text(encoding="utf-8") + "\n被外部进程改写。\n", encoding="utf-8")

                # 已安装版本运行于受控快照，源目录后续变化不能影响当前任务能力。
                self.assertEqual("VERIFIED", registry.get("spring-tools").integrity_status)
                self.assertIn(
                    "先确认 Controller",
                    (first.root_path / "skills" / "spring-review" / "SKILL.md").read_text(encoding="utf-8"),
                )

                _sign_plugin(plugin_root)
                upgraded = registry.install(plugin_root)
                versions = registry.versions("spring-tools")
                self.assertNotEqual(first.package_sha256, upgraded.package_sha256)
                self.assertEqual(2, len(versions))
                self.assertEqual(upgraded.package_sha256, versions[0].package_sha256)

                rolled_back = registry.rollback("spring-tools", first.package_sha256)
                self.assertEqual(first.package_sha256, rolled_back.package_sha256)
                self.assertIn(
                    "先确认 Controller",
                    (rolled_back.root_path / "skills" / "spring-review" / "SKILL.md").read_text(encoding="utf-8"),
                )
                self.assertEqual("PLUGIN_ROLLED_BACK", registry.audit()[0]["action"])

                registry.disable("spring-tools")
                snapshot_skill = rolled_back.root_path / "skills" / "spring-review" / "SKILL.md"
                snapshot_skill.write_text(snapshot_skill.read_text(encoding="utf-8") + "\n篡改快照。\n", encoding="utf-8")
                self.assertEqual("TAMPERED", registry.get("spring-tools").integrity_status)
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
            registry = _registry(root / "state.sqlite")
            try:
                with self.assertRaisesRegex(PluginError, "PLUGIN_PATH_ESCAPE"):
                    registry.install(plugin_root)

                manifest["skills_root"] = "skills"
                (plugin_root / "repopilot-plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
                _sign_plugin(plugin_root)
                registry.install(plugin_root)
                self.assertTrue(registry.remove("spring-tools"))
                self.assertTrue(plugin_root.is_dir())
                self.assertEqual("PLUGIN_REMOVED", registry.audit("spring-tools")[0]["action"])
            finally:
                registry.close()


if __name__ == "__main__":
    unittest.main()
