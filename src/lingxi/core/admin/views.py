"""只读查询命令组的返回形状：与 ``AdminQueries`` 端口签名一起构成契约。

独立成模块（不放进 ``router.py``）是为了让**只需要这些数据形状、不需要命令解析
与路由编排**的调用方——``adapters/admin_registry.py``——不必在 import 时把
``core/admin/commands.py`` 一起拖进自己的闭包。这不是洁癖：`apps/admin_bootstrap`
（随 scheduler 镜像装的一次性种子命令）只需要写入登记表，从不解析或路由任何管理
命令，如果它经由 adapter 间接 import 到 ``commands.py``，scheduler 进程的运行依赖
闭包就会平白多出一段与它实际职责无关的代码路径（`scripts/ci/check_installed_
package.py` 的 `PROCESS_RUNTIME_IMPORTS` 静态闭包检查会如实反映这条多余的边）。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class LocalPermissionOverrideView:
    """「查询用户状态」命令回显的一行当前生效本地权限覆盖：只列 ``active`` 行。

    新职位+范围授权的多行会共享 ``group_id``，管理卡按组展示/撤销；历史
    ``group_id is None`` 的行仍按行展示/撤销。``reason`` 是完整原文，是否截断
    显示是展示层决定，本视图只是忠实的 DTO，不提前做任何截断。
    """

    override_id: str
    direction: str
    company_id: str
    metric_name: str
    reason: str
    created_at: str
    # 新的职位+范围授权会把展开后的行归到同一组；旧行没有这些值，保持
    # ``None``，这样历史数据无需迁移、仍可照常展示/撤销。
    position_name: str | None = None
    company_scope: str | None = None
    group_id: str | None = None

    @property
    def permission_group_id(self) -> str | None:
        """``permission_group_id`` 的领域名；``group_id`` 保留为旧 DTO 兼容别名。"""
        return self.group_id


@dataclass(frozen=True)
class GalaxySourceSummary:
    """「查询用户状态」命令回显的银河来源权限摘要（管理卡「银河来源」这一半）。

    仅供展示，**不参与任何权限判定**——权威计算路径仍是四源合并，本类型只是把
    现算一次的结果转成一份不依赖 ``core.permission`` 类型的展示 DTO。``granted=
    False`` 时 ``companies``/``functions`` 恒为空，``reason`` 给出机器可读原因码，
    另加本类型独有的三种"算不出来"分类，两者对管理员渲染成同一种文案，原因码
    保留区别供审计/排障。``functions`` 是职能标签，不是问数 MCP 认的指标 ID——
    管理员需要精确指标 ID 的场景由「本地覆盖」分列与表单下拉满足，不依赖这里。
    """

    granted: bool
    reason: str
    companies: tuple[str, ...] = ()
    functions: tuple[str, ...] = ()
    all_companies: bool = False


@dataclass(frozen=True)
class AdminUserStatusView:
    """「查询用户状态」命令的最小必要信息——开通与账号状态，不含花名册资料。

    ``local_overrides`` 是该用户当前生效的本地权限覆盖行列表，供撤销命令的
    UX 前置——新授权按 ``group_id``，历史行按 ``override_id`` 发起撤销。

    ``galaxy_source`` 是最佳努力算出的银河来源权限摘要；``None`` 与
    ``GalaxySourceSummary(granted=False, ...)`` 的区别只在"这个字段有没有被填
    过"，两者在管理卡上渲染成同一种文案，调用方不需要分支处理。
    """

    identifier: str
    provisioning_state: str
    account_state: str
    permission_version: int
    updated_at: str
    local_overrides: tuple[LocalPermissionOverrideView, ...] = field(default_factory=tuple)
    galaxy_source: GalaxySourceSummary | None = None


@dataclass(frozen=True)
class AdminEventView:
    """「追溯/审计查询」的一行最近事件，字段与 ``apps/trace`` 已经展示的口径一致。"""

    received_at: str
    event_type: str
    handled_as: str | None
    trace_id: str


@dataclass(frozen=True)
class AdminTraceView:
    """``/admin trace <追溯号>`` 查询结果：事件时间线 + 开通状态 + 失败原因 + 任务收口 + 文档投递结果。

    非 ``None``（即 ``inbound_event`` 里至少有一条这个 ``trace_id``）时才由
    真实查询构造；查无此追溯号返回 ``None``，回复「查无此追溯号」——本视图
    不区分"表里没有"与"参数不合法"，后者已经在命令解析的 ``is_ulid`` 校验
    挡住。脱敏：不带 open_id、不带用户问题正文、不带姓名/邮箱，只含运维需要
    的状态、原因与时间事实。
    """

    trace_id: str
    event_count: int
    first_received_at: str
    last_event_type: str
    last_handled_as: str | None
    dispatched: bool
    provisioning_state: str | None
    account_state: str | None
    failure_reason: str | None
    failure_event_type: str | None
    failure_occurred_at: str | None
    # 任务收口结果：这条追溯号的入站事件所派生的**最近一个**任务。全部可空且
    # ``None`` 是精确语义——这条追溯号可能压根没有派生任务（管理命令、未开通
    # 用户、重复投递），也可能任务还在排队。脱敏姿态与本视图其余字段一致：
    # 只带状态、分类码与异常类型名，不带提问正文、模型输出或异常正文。
    task_status: str | None = None
    task_error_kind: str | None = None
    task_failure_code: str | None = None
    task_failure_signature: str | None = None
    task_ended_at: str | None = None
    # 文档投递结果：任务成功收口不代表文档也成功——文档消费在
    # gateway 独立进程执行，可能仍在排队、已降级成功、明确失败或结果不明。
    # 这三列只带状态/分类码，不带标题、正文、文档 ID 或链接；管理员凭同一个
    # trace_id 必须能分辨「问数任务成功但文档交付失败」与「正文已降级交付」。
    document_delivery_status: str | None = None
    document_delivery_last_error: str | None = None
    document_body_degraded_reason: str | None = None
