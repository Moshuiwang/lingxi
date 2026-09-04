"""读取角色职能映射配置文件（标准库 tomllib，无新增依赖）。

文件 I/O 放在 adapters：`core.permission.role_function` 只接收已解析的文档，
保证映射规则本身可以在无文件系统的单测里被完整证伪。
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from pathlib import Path

from lingxi.core.permission.role_function import build_role_function_map

_CONFIG_FILE_NAME = "galaxy_role_function_map.toml"


def default_role_function_map_path() -> Path:
    """随包发布的配置文件路径。"""
    return Path(__file__).resolve().parents[1] / "config" / _CONFIG_FILE_NAME


def load_role_function_map(path: Path | None = None) -> Mapping[str, str]:
    """解析并校验配置文件；文件缺失或格式错误一律抛错，不静默退化为空映射。

    空映射会让全部角色显示为「未映射」，那是一种看起来正常的失败。
    """
    config_path = path or default_role_function_map_path()
    with config_path.open("rb") as config_file:
        document = tomllib.load(config_file)
    return build_role_function_map(document)
