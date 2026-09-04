"""读取「指标中文别名 → 真实指标 ID」映射配置文件（标准库 tomllib，无新增依赖）。

文件 I/O 放在 adapters：``core/admin/router.py`` 只接收已解析好的字符串到字符串
映射，形状照 :mod:`lingxi.adapters.company_function_metric_map_file`/
:mod:`lingxi.adapters.role_function_map_file`（同一份三层分工：配置文件随包发布 →
本模块解析 → 调用方使用）。

## 与 ``company_function_metric_map_file.py`` 的关键差异：现读，不缓存

那个模块只在 scheduler 进程启动时被调用一次（``apps/scheduler/assembly.py``），
本模块的唯一调用方 ``adapters/admin_registry.PostgresAdminQueries.resolve_metric_name``
在**每次**收到 grant/suppress/revoke 写命令时现读——管理命令面是低频的单管理员交互
（MVP 全仓库只有一个真实管理员），一次 TOML 文件解析的成本可以忽略，换来的是产品
负责人编辑这份别名表后**立即**生效，不需要重启 gateway 容器（对比
``company_function_metric_map.toml`` 编辑后需要重启 scheduler 的既有限制，见该文件
模块文档「外置路径」一节）。因此本模块**不提供**任何缓存或 digest 日志——那些是
"只加载一次、需要知道读到了哪一版"这个场景才需要的机制，本模块的场景是"每次都读
最新内容"，天然不存在版本漂移问题。

## fail-open：读取失败视为空表，不阻塞管理命令面

配置文件缺失、格式非法，或 ``[aliases]`` 表本身不存在，本模块**不抛异常**，一律
返回空映射——与 ``company_function_metric_map_file.load_company_function_metric_map``
"文件读不出来就响亮失败"的既有纪律相反，是刻意的：一次文件损坏最多让"记不住
英文 ID 的管理员这次多打一次字"，不会让任何写命令因为一个纯展示层的便利机制而
整体不可用。真正的产品数据完整性仍然由 ``company_function_metric_map.toml``（真实
指标目录）与迁移 ``0072`` 的数据库约束把守，本文件从不参与那两道防线。

**订正（opus 审查坐实：与实现不符的表述）**：本节此前声称"命中与否两条路径下游
都要走同一套既有校验（``core/admin/commands.py`` 的 ``_METRIC_TOKEN_PATTERN``）"
——这不成立。校验只发生在 :func:`~lingxi.core.admin.commands.parse_admin_command`
解析**原始 token**那一刻；命中别名表之后，``router.py`` 用替换出来的右值调用
``prepare()``（见 ``core/admin/router.py`` 的调用点），这个右值**从未**再经过
``_METRIC_TOKEN_PATTERN`` 或任何其他形状校验——它直接流向 ``prepare()`` 的
``metric_name`` 参数、写进迁移 ``0073`` 的 ``payload`` JSON 列、渲染进确认卡片与
管理群通知正文。因此本模块的加载器自己按与 ``_METRIC_TOKEN_PATTERN`` 等价的形状
过滤右值（见 :data:`_METRIC_VALUE_PATTERN`）——脏配置在**加载时**就被跳过，不
依赖一个不成立的"下游会校验"假设。未命中别名表的原始 token 仍然照常走
``commands.py`` 的既有校验，两条路径的校验各自发生在不同位置，不是共用同一次
判断。
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
    """解析「别名 → 真实指标 ID」映射；读取或格式失败一律返回空映射（见模块文档
    「fail-open」一节，与 ``company_function_metric_map_file`` 的响亮失败纪律刻意
    相反）。

    ``path`` 为 ``None`` 时落回包内默认路径。返回值只保留 ``[aliases]`` 表下
    键非空字符串、值符合 :data:`_METRIC_VALUE_PATTERN` 形状（与
    ``core/admin/commands.py`` 的 ``_METRIC_TOKEN_PATTERN`` 同一形状，模块
    文档「fail-open」一节「订正」段）的条目——形状不对的单条目跳过而不是让
    整份解析失败，理由与"读取失败就当空表"相同：这是一个便利机制，不值得
    因为一条脏配置让其余已经写对的别名也失效。**值的形状过滤不是可选加固**：
    这个右值不会再经过任何下游校验（见模块文档同一段），加载器自己是它唯一
    的把关点。
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
