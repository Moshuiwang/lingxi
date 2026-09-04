"""读取「指标中文别名 → 真实指标 ID」映射配置文件（标准库 tomllib，无新增依赖）。

文件 I/O 放在 adapters：``core/admin/router.py`` 只接收已解析好的字符串到
字符串映射，三层分工同 :mod:`lingxi.adapters.company_function_metric_map_file`：
配置文件随包发布 → 本模块解析 → 调用方使用。

现读不缓存：唯一调用方在每次写命令时现读，换来编辑别名表立即生效、无需
重启进程。fail-open：读取或格式失败一律返回空映射，与
``company_function_metric_map_file`` 响亮失败的纪律刻意相反——纯展示层
便利机制，一次文件损坏不该让写命令整体不可用；数据完整性由真实指标目录
与数据库约束另行把守。

别名命中后的右值不再经过任何下游形状校验、直接落库并渲染进通知正文，
因此加载器自己是唯一把关点，见 :data:`_METRIC_VALUE_PATTERN`。
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping
from pathlib import Path

#: 与 ``core/admin/commands.py`` 的 ``_METRIC_TOKEN_PATTERN`` 逐字同一形状——
#: 不 import 那个模块的私有常量（本仓库既有的"结构相同、不共享导入"惯例，见
#: ``core/permission/merge_sources.py`` 模块文档对同类重复字面量的说明）。
#: 一致性由两边测试各自钉住同一个值，任何一边改动都需要同步核对另一边。
_METRIC_VALUE_PATTERN = re.compile(r"^[A-Za-z0-9_.@:一-鿿-]{1,128}$")


def default_admin_metric_alias_map_path() -> Path:
    """随包发布的配置文件路径。"""
    return Path(__file__).resolve().parents[1] / "config" / "admin_metric_alias_map.toml"


def load_admin_metric_alias_map(path: Path | None = None) -> Mapping[str, str]:
    """解析「别名 → 真实指标 ID」映射；读取或格式失败一律返回空映射（模块文档
    「fail-open」一节）。

    ``path`` 为 ``None`` 时落回包内默认路径。返回值只保留 ``[aliases]`` 表下
    键非空字符串、值符合 :data:`_METRIC_VALUE_PATTERN` 形状的条目——单条目
    形状不对就跳过而非让整份解析失败，理由同「读取失败就当空表」：这个右值
    不会再经过任何下游校验（模块文档末段），加载器自己是唯一把关点，不值得
    因一条脏配置让其余已写对的别名也失效。
    """
    config_path = path or default_admin_metric_alias_map_path()
    try:
        with config_path.open("rb") as config_file:
            document = tomllib.load(config_file)
    except (OSError, tomllib.TOMLDecodeError):
        return {}

    aliases = document.get("aliases")
    if not isinstance(aliases, Mapping):
        return {}

    return {
        key: value
        for key, value in aliases.items()
        if isinstance(key, str)
        and key
        and isinstance(value, str)
        and _METRIC_VALUE_PATTERN.fullmatch(value)
    }
