"""内置工具、Skill 与 MCP 共享的能力目录和信任策略。"""

from __future__ import annotations

import re
from copy import deepcopy
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Iterable, Mapping

from repopilot_guard.permissions import PermissionGrant


_CAPABILITY_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,254}$")


class CapabilityKind(str, Enum):
    """能力的来源类型。"""

    BUILTIN_TOOL = "builtin_tool"
    SKILL = "skill"
    MCP_SERVER = "mcp_server"
    MCP_TOOL = "mcp_tool"


class CapabilityScope(str, Enum):
    """能力配置的生效范围，项目级优先于用户级和插件级。"""

    BUNDLED = "bundled"
    PLUGIN = "plugin"
    USER = "user"
    PROJECT = "project"


class CapabilityRisk(str, Enum):
    """可组合的能力风险标签。"""

    READ = "read"
    WRITE = "write"
    PROCESS = "process"
    NETWORK = "network"
    SECRET_ACCESS = "secret_access"


@dataclass(frozen=True, slots=True)
class CapabilityDescriptor:
    """只描述能力，不携带密钥、文件全文或可执行对象。"""

    capability_id: str
    name: str
    description: str
    kind: CapabilityKind
    scope: CapabilityScope
    source: str
    risks: frozenset[CapabilityRisk] = field(default_factory=lambda: frozenset({CapabilityRisk.READ}))
    enabled: bool = True
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not _CAPABILITY_ID_PATTERN.fullmatch(self.capability_id):
            raise ValueError("INVALID_CAPABILITY_ID")
        if not self.name.strip():
            raise ValueError("CAPABILITY_NAME_REQUIRED")
        if not self.description.strip():
            raise ValueError("CAPABILITY_DESCRIPTION_REQUIRED")
        if not self.source.strip():
            raise ValueError("CAPABILITY_SOURCE_REQUIRED")
        if not self.risks:
            raise ValueError("CAPABILITY_RISK_REQUIRED")
        object.__setattr__(self, "metadata", MappingProxyType(deepcopy(dict(self.metadata))))

    def to_dict(self) -> dict[str, object]:
        return {
            "capability_id": self.capability_id,
            "name": self.name,
            "description": self.description,
            "kind": self.kind.value,
            "scope": self.scope.value,
            "source": self.source,
            "risks": sorted(risk.value for risk in self.risks),
            "enabled": self.enabled,
            "metadata": deepcopy(dict(self.metadata)),
        }


@dataclass(frozen=True, slots=True)
class CapabilityDecision:
    """能力进入执行器前的统一裁决。"""

    allowed: bool
    requires_approval: bool
    code: str
    reason: str

    def to_dict(self) -> dict[str, object]:
        return {
            "allowed": self.allowed,
            "requires_approval": self.requires_approval,
            "code": self.code,
            "reason": self.reason,
        }


class CapabilityPolicy:
    """将任务权限映射为能力级决策；模型不能修改此对象。"""

    def decide(
        self,
        descriptor: CapabilityDescriptor,
        permission: PermissionGrant,
        *,
        approved: bool = False,
    ) -> CapabilityDecision:
        if not descriptor.enabled:
            return CapabilityDecision(False, False, "CAPABILITY_DISABLED", "能力已被配置禁用。")

        if permission.is_full_access:
            return CapabilityDecision(
                True,
                False,
                "USER_GRANTED_FULL_ACCESS",
                "该任务已确认完全权限；能力仍需经过参数校验和审计。",
            )

        if CapabilityRisk.SECRET_ACCESS in descriptor.risks:
            return CapabilityDecision(False, False, "SECRET_ACCESS_BLOCKED_SAFE", "安全模式禁止能力读取密钥。")
        if CapabilityRisk.PROCESS in descriptor.risks and descriptor.kind is not CapabilityKind.BUILTIN_TOOL:
            return CapabilityDecision(False, False, "PROCESS_CAPABILITY_BLOCKED_SAFE", "安全模式禁止启动外部进程型扩展。")

        approval_risks = {CapabilityRisk.WRITE, CapabilityRisk.PROCESS, CapabilityRisk.NETWORK}
        if descriptor.risks.intersection(approval_risks) and not approved:
            return CapabilityDecision(
                False,
                True,
                "CAPABILITY_APPROVAL_REQUIRED",
                "该能力包含写入或网络风险，需要用户针对当前任务批准。",
            )
        return CapabilityDecision(True, False, "CAPABILITY_ALLOWED", "能力符合当前任务权限。")


class CapabilityRegistry:
    """线程安全需求出现前保持简单、确定性的进程内能力目录。"""

    def __init__(self, descriptors: Iterable[CapabilityDescriptor] = ()) -> None:
        self._descriptors: dict[str, CapabilityDescriptor] = {}
        self.register_many(descriptors)

    def register(self, descriptor: CapabilityDescriptor) -> None:
        if descriptor.capability_id in self._descriptors:
            raise ValueError("DUPLICATE_CAPABILITY_REGISTRATION")
        self._descriptors[descriptor.capability_id] = descriptor

    def register_many(self, descriptors: Iterable[CapabilityDescriptor]) -> None:
        candidates = tuple(descriptors)
        ids = [item.capability_id for item in candidates]
        if len(ids) != len(set(ids)) or any(item in self._descriptors for item in ids):
            raise ValueError("DUPLICATE_CAPABILITY_REGISTRATION")
        for descriptor in candidates:
            self._descriptors[descriptor.capability_id] = descriptor

    def get(self, capability_id: str) -> CapabilityDescriptor | None:
        return self._descriptors.get(capability_id)

    def list(
        self,
        *,
        kind: CapabilityKind | None = None,
        enabled_only: bool = False,
    ) -> tuple[CapabilityDescriptor, ...]:
        selected = (
            descriptor
            for descriptor in self._descriptors.values()
            if (kind is None or descriptor.kind is kind) and (not enabled_only or descriptor.enabled)
        )
        return tuple(sorted(selected, key=lambda item: item.capability_id))

    def to_dict(self) -> dict[str, object]:
        return {"capabilities": [item.to_dict() for item in self.list()]}
