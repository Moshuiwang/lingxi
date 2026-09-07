"""指标 ID → 中文名的规范读取口（随包配置；`core/` 可直接 import）。

用户可见的文案里**不能**出现 ``sub_recharge_money`` 这类内部 snake_case 指标 ID：
它既不是这个人看得懂的东西，也是内部标识。中文名的唯一来源是
``config/admin_metric_alias_map.toml``——产品负责人 2026-08-30（Trace #469 S-1）
逐条裁定填入的九条别名。管理员一侧早就按它显示中文
（``core/admin/display_names.AdminDisplayNames.metric_label``，那份文档明确写着
"不再显示 sub_new_count 这类内部 ID"）；本模块把**同一份数据**交给用户可见的两条
路径：欢迎卡与「当前可用范围」通知。同一份产品裁定不该只在管理员那边生效。

## 方向

别名表写成「中文别名 → 指标 ID」——管理命令的**输入**侧要的是那个方向（用户打中文，
系统换成 ID）。**展示**侧要的是反过来的一份，:func:`reverse_metric_labels` 只做这
一件事。同一个指标 ID 被写了多个别名时取排序后的第一个：展示侧必须是确定的一个值，
同一个人的同一份权限两次渲染不能出现两种说法。

## 为什么放在 `config/` 而不是 `adapters/`

`core/` 不做文件 I/O、也不 import `adapters/`（代码框架「二、三层之间的 import 规则」
第 1 条，`check_core_layering.py` 连传递闭包一起守），但**可以** import
`lingxi.config.*`——`core` 取随包内容目录走的正是 ``config.content`` 这条既有路径，
本模块与它同一个位置、同一个理由。

解析实现全仓**只有本模块一份**：``adapters/admin_metric_alias_map_file.py`` 调用它
取输入侧那个方向，不另写一份迟早漂移的解析。

## 缓存节奏与管理命令面的差别（不是漏改）

:func:`default_metric_labels` 按进程缓存一次：别名表随镜像发布，改它本来就要发一次版，
缓存不会让任何人看到过期的中文名。管理命令面那条路仍然是**现读**（编辑立即生效，见
适配器模块文档）——两处节奏不同是各自场景的取舍。

## fail-open 的边界

读取或格式失败一律返回空映射，与适配器同一条纪律。**空映射的后果由调用方各自决定**，
本模块不替它们选：欢迎卡查不到中文名时**整条跳过不发**（宁可漏发也不把内部 ID 印在
卡上，见 ``core/outreach/audience``），「可用范围」通知查不到时**原样展示 ID**（不发
通知比发一条说不全的通知更糟，姿势同该模块公司位的既有回落）。
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType

#: 指标 ID 的形状校验。与 ``core/admin/commands.py`` 的 ``_METRIC_TOKEN_PATTERN``
#: 逐字同一形状——不 import 那个模块的私有常量（本仓库既有的"结构相同、不共享导入"
#: 惯例）；一致性由两边测试各自钉住同一个值。
_METRIC_VALUE_PATTERN = re.compile(r"^[A-Za-z0-9_.@:一-鿿-]{1,128}$")


def default_metric_alias_map_path() -> Path:
    """随包发布的别名表路径。"""
    return Path(__file__).resolve().parent / "admin_metric_alias_map.toml"


def load_metric_alias_map(path: Path | None = None) -> Mapping[str, str]:
    """解析「中文别名 → 指标 ID」映射；读取或格式失败一律返回空映射。

    ``path`` 为 ``None`` 时落回包内默认路径。只保留 ``[aliases]`` 表下键非空字符串、
    值符合 :data:`_METRIC_VALUE_PATTERN` 形状的条目——单条目形状不对就跳过而非让整份
    解析失败，理由同"读取失败就当空表"：这个右值不再经过任何下游形状校验，加载器自己
    是唯一把关点，不值得因一条脏配置让其余已写对的别名也失效。
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

    同一个 ID 有多个别名时取**排序后的第一个**，不是"文件里的最后一条"：字典序是
    与文件行序无关的确定结果，编辑别名表时挪动行不会悄悄换掉用户看到的说法。
    """
    labels: dict[str, str] = {}
    for alias in sorted(aliases):
        labels.setdefault(aliases[alias], alias)
    return labels


@lru_cache(maxsize=1)
def default_metric_labels() -> Mapping[str, str]:
    """随包别名表的展示侧映射（进程内只读一次，见模块文档「缓存节奏」）。

    返回只读视图：缓存对象被调用方就地改掉会污染同进程内其余全部渲染。
    """
    return MappingProxyType(dict(reverse_metric_labels(load_metric_alias_map())))


__all__ = [
    "default_metric_alias_map_path",
    "default_metric_labels",
    "load_metric_alias_map",
    "reverse_metric_labels",
]
