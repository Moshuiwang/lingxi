"""读取「公司 + 职能 → 指标名」翻译映射配置文件（标准库 tomllib，无新增依赖）。

文件 I/O 放在 adapters：``core.permission.metric_translation`` 只接收已解析的文档，
保证映射规则本身可以在无文件系统的单测里被完整证伪。形状照
:mod:`lingxi.adapters.role_function_map_file`（同一份三层分工：配置文件随包发布 →
本模块解析 → ``core`` 校验并使用）。
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
import tomllib

from lingxi.core.permission.metric_translation import build_company_function_metric_map

_CONFIG_FILE_NAME = "company_function_metric_map.toml"


def default_company_function_metric_map_path() -> Path:
    """随包发布的配置文件路径。"""

    return Path(__file__).resolve().parents[1] / "config" / _CONFIG_FILE_NAME


def load_company_function_metric_map(
    path: Path | None = None,
) -> Mapping[str, Mapping[str, tuple[str, ...]]]:
    """解析并校验配置文件；文件缺失或格式错误一律抛错，不静默退化为空映射。

    **空映射本身是合法内容**（``[companies]`` 表存在但没有任何条目——产品负责人尚未
    填入映射时的正常状态，见配置文件与 ``core.permission.metric_translation`` 的模块
    文档），这里不拒绝它。这里拒绝的是"文件读不出来"或"文件里的东西形状不对"：那两种
    是配置错误，不该被悄悄当成"暂时没有内容"处理——否则一次文件损坏会和"产品负责人还
    没填"表现成同一个空映射，运维无法分辨。
    """

    config_path = path or default_company_function_metric_map_path()
    with config_path.open("rb") as config_file:
        document = tomllib.load(config_file)
    return build_company_function_metric_map(document)
