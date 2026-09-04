"""读取「公司 + 职能 → 指标名」翻译映射配置文件（标准库 tomllib，无新增依赖）。

文件 I/O 放在 adapters：``core.permission.metric_translation`` 只接收已解析
文档，可在无文件系统的单测里完整证伪。三层分工同
:mod:`lingxi.adapters.role_function_map_file`：随包发布 → 本模块解析 →
``core`` 校验并使用。
外置路径：产品负责人可直接编辑此文件生效，无需重建镜像；但本模块只在
scheduler 启动时读一次，编辑后需显式 restart（``up -d`` 不触发）scheduler
容器才生效，`daily_report.py` 例外、每日现读。缺失或格式非法一律响亮失败，
不静默回落包内默认。
两条路径必须同源：scheduler 与三个 gateway 调用点都经
:func:`parse_metric_map_path` 注入路径（无默认值），要么都配同一份文件、
要么都不配，否则两条路径各读各的、给出不同答案。加载成功记录内容 digest
（文件原始字节 sha256 前 12 位）到日志，供重启后核对是否读到新内容。
"""

from __future__ import annotations

import hashlib
import logging
import tomllib
from collections.abc import Mapping
from pathlib import Path

from lingxi.core.permission.metric_translation import build_company_function_metric_map

logger = logging.getLogger(__name__)

_CONFIG_FILE_NAME = "company_function_metric_map.toml"


def default_company_function_metric_map_path() -> Path:
    """随包发布的配置文件路径。"""
    return Path(__file__).resolve().parents[1] / "config" / _CONFIG_FILE_NAME


#: 外置映射文件路径的环境变量名，**全仓库只在这里登记一次**：
#: ``apps/scheduler/config.py`` 与 ``apps/gateway/config.py`` 都按它读，两个进程
#: 必须指向同一份文件，见 :func:`parse_metric_map_path`。
METRIC_MAP_PATH_ENV = "LINGXI_COMPANY_FUNCTION_METRIC_MAP_PATH"


def parse_metric_map_path(raw: str | None) -> Path | None:
    """把 :data:`METRIC_MAP_PATH_ENV` 的取值解释成"这台机器该读哪一份映射"。

    唯一一处做这件事的函数：两个进程都经它解释同一个环境变量，不再各写一套
    解释逻辑（模块文档「两条路径必须同源」）。未配置/空白 → ``None``，落回
    随包默认文件——"不配置"始终合法。配了但含空白字符 → ``ValueError``，只报
    变量名、不回显取到的值。文件是否存在、内容是否合法不在这里判定，那是
    :func:`load_company_function_metric_map` 的事。
    """
    value = (raw or "").strip()
    if not value:
        return None
    if any(character.isspace() for character in value):
        raise ValueError(f"环境变量 {METRIC_MAP_PATH_ENV} 不得包含空白字符（不回显取到的值）")
    return Path(value)


def load_company_function_metric_map(
    path: Path | None = None,
) -> Mapping[str, Mapping[str, tuple[str, ...]]]:
    """解析并校验配置文件；文件缺失或格式错误一律抛错，不静默退化为空映射。

    空映射本身是合法内容（产品负责人尚未填入映射时的正常状态），这里不拒绝
    它；拒绝的是"文件读不出来"或"形状不对"——那是配置错误，悄悄当空内容处理
    会让文件损坏和"还没填"表现成同一种状态，运维无法分辨。

    ``path`` 为 ``None`` 时落回包内默认路径；显式路径不论指向包内还是外置
    文件都按同一套规则处理，没有专门放宽的分支。加载成功后记一行内容
    digest（模块文档），加载失败不记——digest 只描述"读到了什么"。
    """
    config_path = path or default_company_function_metric_map_path()
    with config_path.open("rb") as config_file:
        raw = config_file.read()
    document = tomllib.loads(raw.decode("utf-8"))
    mapping = build_company_function_metric_map(document)
    digest = hashlib.sha256(raw).hexdigest()[:12]
    logger.info(
        "已加载公司+职能→指标名翻译映射 path=%s digest=%s companies=%d",
        config_path,
        digest,
        len(mapping),
    )
    return mapping
