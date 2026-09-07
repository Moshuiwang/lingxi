"""读取「指标中文别名 → 真实指标 ID」映射配置文件（管理命令的输入侧）。

解析实现**不在本模块**：全仓唯一一份在 :mod:`lingxi.config.metric_labels`。那里同时
供用户可见侧（欢迎卡、「当前可用范围」通知）取反向的「指标 ID → 中文名」，两个方向
读的是同一个文件、同一套形状校验，不存在"管理员那边认、用户这边不认"的漂移。本模块
保留自己的公开名，因为 ``core/admin/router.py`` 只认这一个调用点，且调用节奏与展示侧
不同（见下）。

现读不缓存：唯一调用方在每次写命令时现读，换来编辑别名表立即生效、无需重启 gateway。
展示侧按进程缓存一次（别名表随镜像发布，改它本来就要发一次版），两处节奏不同是各自
场景的取舍，见 :mod:`lingxi.config.metric_labels` 模块文档「缓存节奏」一节。

fail-open：读取或格式失败一律返回空映射，与 ``company_function_metric_map_file`` 响亮
失败的纪律刻意相反——纯展示层便利机制，一次文件损坏不该让写命令整体不可用；数据完整性
由真实指标目录与数据库约束另行把守。
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from lingxi.config.metric_labels import default_metric_alias_map_path, load_metric_alias_map


def default_admin_metric_alias_map_path() -> Path:
    """随包发布的配置文件路径。"""
    return default_metric_alias_map_path()


def load_admin_metric_alias_map(path: Path | None = None) -> Mapping[str, str]:
    """解析「别名 → 真实指标 ID」映射；读取或格式失败一律返回空映射。

    ``path`` 为 ``None`` 时落回包内默认路径。单条目形状不对就跳过而非让整份解析
    失败，判据与理由见 :func:`lingxi.config.metric_labels.load_metric_alias_map`。
    """
    return load_metric_alias_map(path)
