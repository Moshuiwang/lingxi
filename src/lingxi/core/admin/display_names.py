"""管理员可见文案的人性化展示解析口（Issue #439 PM 补充裁定，Trace #469 S-1）。

管理卡、确认卡、终态卡、群通知与 ``core/admin/router.py`` 的文本回复共用同一份
"把内部标识翻译成人类可读文本"的需求：飞书 ``open_id``（``ou_*``）翻译成「姓名
（邮箱）」、银河公司编号翻译成「中文名（编号）」、指标 ID 翻译成中文别名。五处
调用面各自独立声明自己需要的端口（代码框架既有惯例，见
``core/admin/card_callback.py`` 模块文档"为什么是一个独立类"一节同一取舍），但
这份"翻译什么、返回什么形状"的契约完全相同，因此在这里只定义一次，供
``management_card.py``/``card_dispatch.py``/``card_callback.py``/``router.py``
四处 import 同一个 Protocol，而不是各自重新声明四份结构相同的类型。

真实实现见 ``adapters/admin_registry.PostgresAdminQueries``（结构性实现，不
继承本 Protocol）；本模块不 import adapters/、不做任何 I/O（代码框架第二节）。

## 安全边界：``user_label`` 绝不返回 open_id 本身

合同「管理员可见文案零 ou_/lpo_/pac_」是结构性要求，不是"尽量避免"——真实实现
查不到对应用户时必须退化为通用占位（如"该用户"），不能把入参 ``open_id``
原样拼进返回值。``company_label``/``metric_label`` 没有这条限制：公司编号与
指标 ID 是业务代码（银河/问数 MCP 的外部编号），不是 ``ou_``/``lpo_``/``pac_``
这一类系统内部标识，查不到中文名时原样展示编号本身是可接受的兜底，不违反
"零内部号"这条底线。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol


class AdminDisplayNames(Protocol):
    """管理员可见展示名解析口。方法都只读，不做任何权限判断——是否应当
    展示这条信息由调用方决定，本接口只负责"把已经决定要展示的标识翻译成什么
    文本"。
    """

    def user_label(self, *, open_id: str) -> str:
        """把飞书 ``open_id`` 翻译成「姓名（邮箱）」；只有姓名或只有邮箱时展示
        那一个；两者都缺失（含查无此用户）时返回通用占位，绝不返回 ``open_id``
        本身。"""
        ...

    def company_label(self, *, company_id: str) -> str:
        """把银河公司编号翻译成「中文名（编号）」（数据源 ``galaxy_country.
        name_cn``，按当前有效银河批次现读）；查无中文名时原样返回 ``company_id``
        ——公司编号是业务代码，不是需要隐藏的内部系统标识。"""
        ...

    def metric_label(self, *, metric_id: str) -> str:
        """把指标 ID 反查成中文别名（``config/admin_metric_alias_map.toml``）；
        查无别名时原样返回 ``metric_id``，理由同 :meth:`company_label`。"""
        ...

    def company_labels(self, *, company_ids: Sequence[str]) -> Mapping[str, str]:
        """:meth:`company_label` 的批量变体（Trace #469 修复包 B，B-7：连接
        风暴收敛）——一次调用翻译一组公司编号，语义与逐个调用
        :meth:`company_label` 完全等价（查无中文名时该编号原样出现在返回映射
        里），唯一区别是真实实现只需为整批 ID 建立**一次**数据库连接批次
        查询，而不是每个编号各自建连接。管理卡渲染这类"一次渲染要翻译几十个
        编号"的场景应当调用这个方法而不是循环调用 :meth:`company_label`——
        见 ``core/admin/management_card.render_management_card`` 与
        ``adapters/admin_registry.PostgresAdminQueries.company_labels`` 的
        连接数对比。空输入返回空映射。"""
        ...

    def metric_labels(self, *, metric_ids: Sequence[str]) -> Mapping[str, str]:
        """:meth:`metric_label` 的批量变体，同上一条注释同一理由——真实实现
        是文件读取而非数据库连接，批量的收益是"只读一次映射文件"而不是每个
        指标各读一次，同一份收敛姿势。空输入返回空映射。"""
        ...
