"""指标 ID → 中文名的规范读取口（随包配置；``core/`` 可直接 import）。

用户可见的文案里不能出现 ``sub_recharge_money`` 这类内部标识——它不是这个人看得
懂的东西。中文名的唯一来源是 ``config/admin_metric_alias_map.toml``，管理员一侧
的展示早就在用同一份数据。

别名表本身写成「中文别名 → 指标 ID」，因为管理命令的**输入**侧要那个方向；展示侧
要反过来的一份，:func:`reverse_metric_labels` 只做这一件事。

放在 ``config/`` 而不是 ``adapters/``：``core/`` 不做文件 I/O、也不 import
``adapters/``，但可以 import ``lingxi.config.*``——取随包内容目录走的正是这条既有
路径。解析实现全仓只此一份，``adapters/admin_metric_alias_map_file`` 调用它取输入
侧那个方向，不另写一份迟早漂移的解析。
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType

#: 指标 ID 的形状校验。与 ``core/admin/commands.py`` 的 ``_METRIC_TOKEN_PATTERN``
#: 逐字同一形状——不 import 那个模块的私有常量（本仓库既有的"结构相同、不共享
#: 导入"惯例）；一致性由两边测试各自钉住同一个值。
_METRIC_VALUE_PATTERN = re.compile(r"^[A-Za-z0-9_.@:一-鿿-]{1,128}$")


def default_metric_alias_map_path() -> Path:
    """随包发布的别名表路径。"""
    return Path(__file__).resolve().parent / "admin_metric_alias_map.toml"


def load_metric_alias_map(path: Path | None = None) -> Mapping[str, str]:
    """解析「中文别名 → 指标 ID」映射；读取或格式失败一律返回空映射。

    ``path`` 为 ``None`` 时落回包内默认路径。只保留键非空、值符合
    :data:`_METRIC_VALUE_PATTERN` 形状的条目——单条目形状不对就跳过而非整份失败：
    这个右值不再经过任何下游校验，加载器自己是唯一把关点，不值得因一条脏配置让其余
    已写对的别名也失效。空映射的后果由调用方各自决定：欢迎卡失败关闭整条跳过，
    「当前可用范围」通知原样展示 ID。
    """
    config_path = path or default_metric_alias_map_path()
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


def reverse_metric_labels(aliases: Mapping[str, str]) -> Mapping[str, str]:
    """把「别名 → 指标 ID」翻成展示侧要的「指标 ID → 中文名」。

    **保持文件里的先后**：返回映射的键序就是用户可见的展示顺序（欢迎卡「指标」字段
    与示例句都按它排）。挑哪个指标先出现是产品选择，把它放在别名表里意味着改顺序
    不必动代码；字典序会把这个选择夺走，换成一个与业务无关的排法。

    同一个 ID 写了多个别名时取**文件里第一次出现的那个**，与顺序同一条依据。
    """
    labels: dict[str, str] = {}
    for alias, metric_id in aliases.items():
        labels.setdefault(metric_id, alias)
    return labels


@lru_cache(maxsize=1)
def default_metric_labels() -> Mapping[str, str]:
    """随包别名表的展示侧映射（进程内只读一次）。

    缓存是安全的：别名表随镜像发布，改它本来就要发一次版。管理命令面那条路仍然现读，
    两处节奏不同是各自场景的取舍。返回只读视图——缓存对象被就地改掉会污染同进程内
    其余全部渲染。
    """
    return MappingProxyType(dict(reverse_metric_labels(load_metric_alias_map())))


@lru_cache(maxsize=1)
def default_example_metrics() -> tuple[str, ...]:
    """欢迎卡示例句优先使用的指标 ID（随包别名表 ``[examples] prefer``）。

    读不到、格式不对或为空时返回空元组——调用方据此退回展示顺序，不会因为这份
    可选配置缺失就发不出卡。
    """
    try:
        with default_metric_alias_map_path().open("rb") as config_file:
            document = tomllib.load(config_file)
    except (OSError, tomllib.TOMLDecodeError):
        return ()
    examples = document.get("examples")
    if not isinstance(examples, Mapping):
        return ()
    prefer = examples.get("prefer")
    if not isinstance(prefer, list):
        return ()
    return tuple(item for item in prefer if isinstance(item, str) and item)


__all__ = [
    "default_example_metrics",
    "default_metric_alias_map_path",
    "default_metric_labels",
    "load_metric_alias_map",
    "reverse_metric_labels",
]
