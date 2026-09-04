"""管理员可见文案的人性化展示解析口。

管理卡、确认卡、终态卡、群通知与 ``core/admin/router.py`` 的文本回复共用同一份
"把内部标识翻译成人类可读文本"的需求：飞书 ``open_id`` 翻译成「姓名（邮箱）」、
银河公司编号翻译成「中文名（编号）」、指标 ID 翻译成中文别名。四处调用面各自
独立声明自己需要的端口，但契约完全相同，只定义一次供四处 import 同一个
Protocol；真实实现见 ``adapters/admin_registry.PostgresAdminQueries``。

## 安全边界：``user_label`` 绝不返回 open_id 本身

合同「管理员可见文案零 ou_/lpo_/pac_」是结构性要求：真实实现查不到对应用户时
必须退化为通用占位（如"该用户"），不能把入参 ``open_id`` 原样拼进返回值。
``company_label``/``metric_label`` 没有这条限制——公司编号与指标 ID 是业务
代码，查不到中文名时原样展示编号本身是可接受的兜底。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol


class AdminDisplayNames(Protocol):
    """管理员可见展示名解析口：只读，不做任何权限判断。

    是否应当展示这条信息由调用方决定，本接口只负责"把已经决定要展示的标识
    翻译成什么文本"。
    """

    def user_label(self, *, open_id: str) -> str:
        """把飞书 ``open_id`` 翻译成「姓名（邮箱）」。

        只有姓名或只有邮箱时展示那一个；两者都缺失（含查无此用户）时返回通用
        占位，绝不返回 ``open_id`` 本身。
        """
        ...

    def company_label(self, *, company_id: str) -> str:
        """把银河公司编号翻译成「中文名（编号）」。

        数据源 ``galaxy_country.name_cn``，按当前有效银河批次现读；查无中文名
        时原样返回 ``company_id``——公司编号是业务代码，不是需要隐藏的内部
        系统标识。
        """
        ...

    def metric_label(self, *, metric_id: str) -> str:
        """把指标 ID 反查成中文别名（``config/admin_metric_alias_map.toml``）。

        查无别名时原样返回 ``metric_id``，理由同 :meth:`company_label`。
        """
        ...

    def company_labels(self, *, company_ids: Sequence[str]) -> Mapping[str, str]:
        """:meth:`company_label` 的批量变体（连接风暴收敛）。

        一次调用翻译一组公司编号，语义与逐个调用 :meth:`company_label` 完全
        等价，唯一区别是真实实现只需为整批 ID 建立一次数据库连接批次查询，
        而不是每个编号各自建连接。管理卡渲染这类"一次渲染要翻译几十个编号"的
        场景应当调用这个方法而不是循环调用 :meth:`company_label`。空输入
        返回空映射。
        """
        ...

    def metric_labels(self, *, metric_ids: Sequence[str]) -> Mapping[str, str]:
        """:meth:`metric_label` 的批量变体，同一份收敛姿势。

        真实实现是文件读取而非数据库连接，批量的收益是"只读一次映射文件"而
        不是每个指标各读一次。空输入返回空映射。
        """
        ...
