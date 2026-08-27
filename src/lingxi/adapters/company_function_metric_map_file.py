"""读取「公司 + 职能 → 指标名」翻译映射配置文件（标准库 tomllib，无新增依赖）。

文件 I/O 放在 adapters：``core.permission.metric_translation`` 只接收已解析的文档，
保证映射规则本身可以在无文件系统的单测里被完整证伪。形状照
:mod:`lingxi.adapters.role_function_map_file`（同一份三层分工：配置文件随包发布 →
本模块解析 → ``core`` 校验并使用）。

## 外置路径（Issue #320）

产品负责人 2026-08-26 就 Issue #320 裁定：指标映射表的维护人（产品负责人本人）应当
能够直接编辑这份文件，不必为一行映射改动走一次完整镜像构建发布——挂载方式与
系统提示词文件同一模式（见 ``apps/worker/service.py`` 的
``_load_task_system_prompt``）。**但生效时机不同**：系统提示词文件是 worker 每个
任务开始时现读，编辑后下一条消息即生效；本模块的读取点
``apps/scheduler/assembly.py`` 的 ``_build_permission_refresh_duty`` 在 scheduler
进程启动时只被 ``build_loop`` 调用**一次**（防止两处读文件互相漂移的刻意设计），
因此编辑外置文件后需要重启 scheduler 容器
（``docker compose --env-file deploy/.env.stage -f deploy/compose.yaml -f
deploy/compose.stage.yaml restart scheduler``，prod 同构换成
``.env.prod``/``compose.prod.yaml``；不需重建镜像）才会被读到新内容——不能用
``docker compose up -d`` 重启 scheduler：compose 配置本身未变时它判定 up-to-date 不会
重启，而这里改的是外置文件、不是 compose 配置，正是此情形；下方「加载成功时记录
内容 digest 到日志」一节的 digest 行是重启后核对"读到了哪一版"的手段。例外：
`apps/scheduler/daily_report.py`
的每日通报「未覆新指标」日检段每次现读，不受此限、无需重启。因此
:func:`load_company_function_metric_map` 从
``apps/scheduler/assembly.py`` 接收的 ``path`` 参数不再总是包内默认路径：装配层会先读
``LINGXI_COMPANY_FUNCTION_METRIC_MAP_PATH``（``apps/scheduler/config.py`` 的
``SchedulerConfig.company_function_metric_map_path``），配了就把它转成 ``Path`` 传进来，
本函数因此**优先**读那份外置文件；未配置时装配层传 ``None``，本函数落回包内默认，
逐字节保持此前行为。

**外置文件缺失或格式非法：响亮失败，不静默回落包内默认**——这是本函数一直以来的既有
语义（见下方文档字符串「空映射合法／读不出来不合法」），外置路径注入没有新开一条更
宽容的路径：调用方（`apps/scheduler/assembly.py` 的 ``_build_permission_refresh_duty``）
既有的 ``except (OSError, ValueError)`` 分支原样覆盖这条外置路径，`metric_translation_
map_unavailable` 审计记录里因此既可能是包内文件损坏，也可能是外置路径配错，两者都不
静默——运维用 `deploy/验收前部署配置清单.md` 登记的挂载方式核对即可，不需要新分支。

**加载成功时记录内容 digest 到日志**（沿用系统提示词的先例：`hashlib.sha256(...).
hexdigest()[:12]`，短摘要足以判断"内容变没变"，不需要可逆）——digest 取的是**文件原始
字节**，不是解析后的 Python 结构，这样同一份文件不论换行符、键序如何书写，只要字节
完全相同就得到相同 digest，反之亦然；产品负责人编辑外置文件、重启 scheduler 容器后
可以直接对照这一行日志确认"这次改动确实被 scheduler 读到了"，不需要额外核对手段。
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Mapping
from pathlib import Path
import tomllib

from lingxi.core.permission.metric_translation import build_company_function_metric_map

logger = logging.getLogger(__name__)

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

    ``path`` 为 ``None`` 时落回包内默认路径（此前唯一的行为）；调用方传入的显式路径
    ——不论指向包内默认还是运行时外置文件——都按**同一套**规则处理，没有专门为外置
    路径放宽的分支（模块文档「外置路径」一节）。加载成功后向日志记一行内容 digest，
    见模块文档；加载失败（本函数向上抛出之前）不记这一行——digest 只描述"读到了什么"，
    不该在没读到任何东西时也输出一个看似有效的值。
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
