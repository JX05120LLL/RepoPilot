"""插件签名测试辅助工具；私钥只存在于测试进程，绝不进入项目示例或运行时。"""

from __future__ import annotations

import json
from base64 import b64encode
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from repopilot_guard.plugins import PluginRegistry, _canonical_json, _package_sha256


TEST_KEY_ID = "test-publisher"
_PRIVATE_KEY = Ed25519PrivateKey.generate()


def public_key_base64() -> str:
    return b64encode(
        _PRIVATE_KEY.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    ).decode("ascii")


def sign_plugin(root: Path, *, key_id: str = TEST_KEY_ID, private_key: Ed25519PrivateKey | None = None) -> None:
    signing_key = private_key or _PRIVATE_KEY
    package_sha256 = _package_sha256(root)
    payload = _canonical_json(
        {"schema_version": 1, "algorithm": "ed25519", "key_id": key_id, "package_sha256": package_sha256}
    ).encode("utf-8")
    (root / "repopilot-plugin.sig").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "algorithm": "ed25519",
                "key_id": key_id,
                "package_sha256": package_sha256,
                "signature": b64encode(signing_key.sign(payload)).decode("ascii"),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def trust_test_publisher(registry: PluginRegistry) -> None:
    registry.add_trust_key(TEST_KEY_ID, public_key_base64())
