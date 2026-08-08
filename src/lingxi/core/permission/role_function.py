"""银河角色名到 Lingxi 职能标签的映射（纯函数）。

产品负责人 2026-08-05 决策 2：「所有 A 打头的角色字段，比如 A运营=我们系统里的
运营，A销售=我们系统销售，将这份规则抽象出来作为配置文件，后期持续维护。」

因此本模块**只认配置**：

- 映射唯一来源是仓库内的配置文件（`lingxi/config/galaxy_role_function_map.toml`，
  由 `adapters.role_function_map_file` 读取），产品负责人在仓库里持续维护；
- 配置未覆盖的角色名**保留原文并标记「未映射」**，不做「去掉开头的 A」这类猜测。
  银河的 74 个角色命名规则不统一，含临时角色与个人专用角色，猜测会造出错误的
  职能标签，而错误的范围比没有范围更糟；
- 未映射角色不产生 Lingxi 支持职能；授权判断只能使用映射成功的职能，不能把原始角色名
  当作支持范围。没有任何映射成功的角色时，用户按未授权处理；
- 映射结果是展示与授权资格判断使用的职能标签，**不改动落库的原始角色名**；它不自行扩大
  实际权限，实际可查询范围仍由 MCP 按已同步权限决定。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

# 未被配置覆盖时对外呈现的标记，不猜测职能。
UNMAPPED_LABEL = "未映射"


@dataclass(frozen=True)
class RoleFunction:
    """一个角色名的映射结果。`role_name` 永远是原文。"""

    role_name: str
    function: str | None

    @property
    def mapped(self) -> bool:
        return self.function is not None

    @property
    def label(self) -> str:
        """可用于内部诊断的标签：命中给职能，未命中明确写「未映射」。"""

        return self.function if self.function is not None else UNMAPPED_LABEL


def build_role_function_map(document: Mapping[str, Any]) -> Mapping[str, str]:
    """校验配置文档并返回只读映射。

    文档形态：`{"roles": {"<银河角色名>": "<Lingxi 职能>"}}`。
    """

    roles = document.get("roles")
    if not isinstance(roles, Mapping):
        raise ValueError("角色映射配置缺少 [roles] 表")

    mapping: dict[str, str] = {}
    for raw_role, raw_function in roles.items():
        role_name = raw_role.strip() if isinstance(raw_role, str) else ""
        if not role_name:
            raise ValueError("角色映射配置存在空的角色名")
        if not isinstance(raw_function, str) or not raw_function.strip():
            raise ValueError(f"角色映射配置的职能必须是非空文本：{role_name}")
        if role_name in mapping:
            raise ValueError(f"角色映射配置存在重复的角色名：{role_name}")
        mapping[role_name] = raw_function.strip()

    return MappingProxyType(mapping)


def resolve_role_function(role_name: str, mapping: Mapping[str, str]) -> RoleFunction:
    """按配置精确匹配（仅去首尾空白），未命中即「未映射」。"""

    if not isinstance(role_name, str) or not role_name.strip():
        raise ValueError("角色名不能为空")
    original = role_name.strip()
    return RoleFunction(original, mapping.get(original))


def resolve_role_functions(
    role_names: Iterable[str], mapping: Mapping[str, str]
) -> tuple[RoleFunction, ...]:
    """按输入顺序逐个解析，保留重复与未映射项，不做任何过滤。"""

    return tuple(resolve_role_function(role_name, mapping) for role_name in role_names)
