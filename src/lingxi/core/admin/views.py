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
    """「查询用户状态」命令回显的一行当前生效本地权限覆盖（#319 S-P-1b 卡 B，
    ``/admin revoke_permission`` 的 UX 前置）：只列 ``entry_status='active'`` 行，
    数据经 ``adapters.postgres_local_permission.
    PostgresLocalPermissionOverrideStore.effective_entries``。

    新职位+范围授权的多行会共享 ``group_id``，管理卡按组展示/撤销；历史
    ``group_id is None`` 的行仍按行展示/撤销。``reason`` 是完整原文——是否截断显示是 ``core/admin/router._render_user_status``
    的展示层决定（不回显 reason 全文，截断 20 字），本视图本身只是忠实的 DTO，
    不提前做任何截断，避免把展示细节耦合进数据形状。
    """

    override_id: str
    direction: str
    company_id: str
    metric_name: str
    reason: str
    created_at: str
    # #493 新的职位+范围授权会把展开后的行归到同一组；旧行没有这些值，保持
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
    """「查询用户状态」命令回显的银河来源权限摘要（#439 B 档：管理卡「银河来源与
    本地覆盖分列展示」的「银河来源」这一半）。

    仅供展示，**不参与任何权限判定**——真实生效权限的权威计算路径仍然是
    ``core/permission/merge_sources.py`` 的四源合并，本类型只是把
    ``core/permission/publish_row.aggregate_permission`` 现算一次的结果转成一份
    不依赖 ``core.permission`` 类型的展示 DTO（``views.py`` 保持轻量、不跨模块
    耦合的既有取舍，见模块文档）。``granted=False`` 时 ``companies``/``functions``
    恒为空，``reason`` 给出机器可读原因码（与 ``PermissionAggregate.reason`` 同一
    取值域，另加本类型独有的 ``roster_snapshot_unavailable``/
    ``galaxy_snapshot_unavailable``/``role_function_map_unavailable`` 三种——
    这三种描述"算不出来"，前面几种描述"算出来了、结论是没有"，两者对管理员来说
    都渲染成同一种"无可展示的银河来源权限"，但原因码保留区别供审计/排障）。

    ``functions`` 是**职能标签**（如"运营"/"财务"），不是问数 MCP 认的指标 ID——
    这一层翻译（``core/permission/metric_translation.translate_company_functions``）
    需要额外的公司维度笛卡尔积，且其失败语义（映射未覆盖时整轮 fail-closed）是
    为"要不要真的发布"这个决策设计的，套用到"展示一下大概是什么范围"这个更宽松的
    场景意义不大；管理员真正需要精确指标 ID 的场景（发起本地覆盖）由「本地覆盖」
    分列与新增授权/抑制表单的指标下拉（真实指标目录）满足，不依赖这里的翻译结果。
    """

    granted: bool
    reason: str
    companies: tuple[str, ...] = ()
    functions: tuple[str, ...] = ()
    all_companies: bool = False


@dataclass(frozen=True)
class AdminUserStatusView:
    """「查询用户状态」命令的最小必要信息——开通与账号状态，不含花名册资料。

    ``local_overrides``（#319 S-P-1b 卡 B 新增，默认空元组保持既有构造点不用改）
    是该用户当前生效的本地权限覆盖行列表，供 ``/admin revoke_permission`` 的
    UX 前置——新授权按 ``group_id``，历史行按 ``override_id`` 发起撤销。

    ``galaxy_source``（#439 B 档新增，默认 ``None`` 保持既有构造点不用改）是最佳
    努力算出的银河来源权限摘要；``None`` 与 ``GalaxySourceSummary(granted=False,
    reason=...)`` 的区别只在"这个字段有没有被填过"，两者在管理卡上渲染成同一种
    "银河来源不可用"文案，调用方不需要分支处理。"""

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
    """``/admin trace <追溯号>`` 查询结果（Issue #337）：入站事件时间线摘要 +
    该追溯号定位到的用户当前开通状态 + 失败原因（``onboarding_failure`` 表，
    迁移 ``0077``）+ 这条追溯号对应任务的收口结果（``task`` 表，Issue #495）+
    文档投递结果（``task_document_delivery_request`` 表，Issue #499）。

    非 ``None``（即 ``inbound_event`` 里至少有一条这个 ``trace_id``）时才由
    ``adapters/admin_registry.PostgresAdminQueries.trace_lookup`` 构造；查无
    此追溯号返回 ``None``，由 ``core/admin/router._render_trace`` 回复
    「查无此追溯号」——本视图不区分"表里没有"与"参数不合法"，后者已经在
    ``commands.py`` 的 ``is_ulid`` 校验挡住。

    脱敏（Issue #337 范围条目 3）：不带 open_id、不带用户问题正文、不带姓名/
    邮箱——只含运维需要的状态、原因与时间事实，对照 ``apps/trace`` 已有的身份
    最小化默认姿态（那个 CLI 默认也不打印 open_id）。
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
    # 任务收口结果（Issue #495）：这条追溯号的入站事件所派生的**最近一个**任务
    # （``task.inbound_event_id`` = ``inbound_event.feishu_event_id``）。全部可空
    # 且 ``None`` 是精确语义——这条追溯号可能压根没有派生任务（管理命令、未开通
    # 用户、重复投递），也可能任务还在排队。``task_failure_code``/
    # ``task_failure_signature`` 由迁移 ``0080`` 落库，成功回合与不来自异常的
    # 失败在这两列上本来就是 ``NULL``。
    #
    # 脱敏姿态与本视图其余字段一致：只带状态、分类码与异常**类型名**，不带
    # 提问正文、不带模型输出、不带异常正文（`V-花名册-33`）。
    task_status: str | None = None
    task_error_kind: str | None = None
    task_failure_code: str | None = None
    task_failure_signature: str | None = None
    task_ended_at: str | None = None
    # 文档投递结果（Issue #499）：任务成功收口不代表文档也成功——文档消费在
    # gateway 独立进程执行，可能仍在排队、已降级成功、明确失败或结果不明。
    # 这三列只带状态/分类码，不带标题、正文、文档 ID 或链接；管理员凭同一个
    # trace_id 必须能分辨「问数任务成功但文档交付失败」与「正文已降级交付」。
    document_delivery_status: str | None = None
    document_delivery_last_error: str | None = None
    document_body_degraded_reason: str | None = None
