"""用户权限管理卡的展示层：纯函数渲染（#493，职位+公司范围授权）。

与 ``core/admin/notification.py`` 的确认卡片是**两张不同的卡**，不合并进同一张卡
（见 ``core/admin/router.AdminCommandRouter._send_management_card`` 文档、本 Story
报告"同卡二次确认"裁定一节）：产品合同「管理员处理入口与安全确认」正文明确
"待确认操作发送到发起管理员本人的飞书私聊，卡片只承担最终确认，不承担搜索、比较、
审批流或复杂信息填写"——管理卡负责查询结果展示、职位+公司范围补充授权表单和撤销
入口，确认卡继续只承担"仅展示一个已经准备好的动作 + 确认/取消"。新职位+范围授权
按 ``permission_group_id`` 聚合为一个职位+公司范围撤销项；只有历史无组行继续逐行撤销。
管理卡上的
每一次写意图（提交表单 / 点击撤销按钮）都会转译成
一条等价的 ``/admin ...`` 命令文本，交给已有的 ``AdminCommandRouter.route()``
处理（见 ``core/admin/card_callback.py`` 新增的回调分支），复用其全部既有
``prepare()``/角色核对/自我目标防呆/确认卡发送/审计逻辑，本模块不重新实现一遍
写路径判定，只负责渲染。

只负责"展示什么"，不负责"怎么发出去"：真实 CardKit 调用住在
``adapters/feishu_admin_card.py``，本模块不 import 任何飞书 SDK，也不 import
``adapters/``（代码框架第二节），可以在没有装 SDK 的环境里测试全部渲染断言。

## 人性化展示（Trace #469 S-1，PM 补充裁定）

管理员可见的公司/指标/用户标识全部经 :class:`~lingxi.core.admin.display_names.
AdminDisplayNames` 端口翻译成人类可读文本——公司显示「中文名（编号）」（数据源
``galaxy_country.name_cn``，按当前有效银河批次现读）、指标显示中文别名
（``config/admin_metric_alias_map.toml`` 反查）。下拉选项的**提交值**仍然是真实
公司编号/指标 ID（``catalog.companies()``/``catalog.metrics()`` 原样返回什么，
``value`` 字段就是什么）——只有展示给管理员看的 ``text`` 字段经过翻译，选中后
回传给服务端的语义不变，不需要下游再反查一次。

## 表单字段与按钮

职位和公司范围下拉、原因输入框均为必填；唯一的表单提交按钮携带非空 ``name``
（``grant_submit``），并通过 ``column_set`` 排版。独立的“取消”按钮只关闭管理卡。
撤销按钮不在表单内。旧目录假实现（无 ``positions()``）走兼容分支，该分支自
Trace #544 D-5 起**不再渲染任何写入表单**——它原先那两个按钮提交的是
``/admin grant_permission`` / ``/admin suppress_permission``，命令已撤除；生产
目录始终提供 ``positions()`` 并走新形状。

## 组件选择

- **职位 / 公司范围**：``select_static``（官方下拉组件）。职位选项来自精确角色映射；
  公司范围包含单个公司和“全部（当前实际公司数）”。目录暂不可用时退化为不可选占位项，
  不把缺失目录解释成空权限。
- **原因**：``input``（官方输入框组件），自由文本。
- **补充授权**：表单只提交职位、公司范围和原因（``form_action_type: "submit"``），
  服务端在确认事务中展开为公司×指标行。
- **撤销**：每个新授权组配一个职位+公司范围按钮；历史无组行各配一个独立按钮，复用已被真实 CardKit
  探针验证过的"按钮 + ``behaviors: [{type: callback, value: {...}}]``"形状（见
  ``adapters/feishu_admin_card.py`` 模块文档"2026-08-25 建卡环节已被真实探针证伪
  并修复"）——不需要额外字段输入，不放进 ``form``。

## 外部平台证据边界

上面引用的"已被真实探针验证过"只覆盖按钮与 markdown 两种组件；``column_set``
嵌 ``form`` 的可行性已由 W0-1 探针在真实发送侧证实（结构合法），但真实点击后
的回调事件体是否逐字符合仍是 ``biai-stage`` L4a 受控验收范围，本 Story 未验证
（见报告"未验证事项"）。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from lingxi.core.admin.card_layout import assert_unique_named_form_elements, button_row
from lingxi.core.admin.display_names import AdminDisplayNames
from lingxi.core.admin.views import AdminUserStatusView, LocalPermissionOverrideView

#: 卡片按钮/表单提交回传的 ``admin_action`` 取值，供
#: ``core/admin/card_callback.py`` 新增的回调分支识别是哪一类管理卡交互。
ADMIN_ACTION_GRANT = "grant"
ADMIN_ACTION_REVOKE = "revoke"
ADMIN_ACTION_CANCEL = "cancel"

#: 表单内提交按钮的 ``name``（飞书官方错误码 200530 要求非空且单卡唯一）。
#: 全卡只有 ``grant_submit`` 一个表单提交按钮——「屏蔽指标」按钮随
#: ``/admin suppress_permission`` 一起在 Trace #544 D-5 撤除。
#: **公开常量**（W0-1 追加结论，2026-08-30）：
#: 真实点击实测坐实——表单提交回调的 ``action.value`` 常常不以 Mapping 形态
#: 到达（缺失或需要反序列化的字符串），``apps/gateway/__init__.py`` 的路由
#: 分流因此需要一个不依赖 ``value`` 内容的后备判据，用回调事件本就会带回的
#: 按钮 ``action.name`` 兜底识别是哪一个提交按钮，这两个常量必须能被外部
#: import。
GRANT_SUBMIT_BUTTON_NAME = "grant_submit"

#: 指标/公司目录不可用时下拉唯一的占位选项——``select_static`` 需要至少一个
#: ``options`` 条目才是结构完整的组件；用一个空 ``value`` 的占位项而不是省略整个
#: 下拉，管理员点开至少能看到"当前无法枚举"这句明确提示，不是一段视觉空白。点击
#: 提交时 ``value`` 为空会被 ``card_callback.py`` 的表单处理分支当成"未选择"拒绝，
#: 不会被静默当成一个真正的公司/指标。
_CATALOG_UNAVAILABLE_PLACEHOLDER = "（当前无法枚举，请改用文本命令）"

#: 内部标识前缀白名单（Trace #469 S-1），与 ``core/admin/router.
#: _INTERNAL_ID_PREFIXES`` 同一份判据、独立各自维护一份（两处各自的调用面很薄，
#: 抽公共函数换来的耦合大于收益，与本模块其余展示层惯例一致）。
_INTERNAL_ID_PREFIXES: tuple[str, ...] = ("ou_", "lpo_", "lpg_", "pac_")


def _safe_identifier_echo(identifier: str) -> str:
    if identifier.startswith(_INTERNAL_ID_PREFIXES):
        return "该用户"
    return identifier


class CompanyMetricCatalog(Protocol):
    """公司/指标下拉选项目录端口。真实实现读取
    ``config/company_function_metric_map.toml``（``adapters/
    company_function_metric_map_file.py``，已有、未改动），见
    ``adapters/feishu_admin_card.TomlCompanyMetricCatalog``。本模块不 import
    adapters/，只声明调用方需要的最小接口（代码框架第二节）。"""

    def companies(self) -> Sequence[str]: ...

    def metrics(self) -> Sequence[str]: ...

    def positions(self) -> Sequence[str]: ...


def _plain_text(content: str) -> dict[str, str]:
    return {"tag": "plain_text", "content": content}


def _select_static(
    *,
    name: str,
    placeholder: str,
    options: Sequence[str],
    label_for: Callable[[str], str] | None = None,
    required: bool = False,
) -> dict[str, Any]:
    """``label_for``（Trace #469 S-1 新增）：把每个候选值翻译成管理员看到的
    展示文本；``value`` 字段永远是原始候选值本身（提交语义不变，见模块文档
    "人性化展示"一节）。``None`` 时展示文本与提交值相同（既有行为，占位选项
    永远不经过 ``label_for``——它本来就不是一个真实的公司/指标）。
    """

    option_values = list(dict.fromkeys(options)) or [_CATALOG_UNAVAILABLE_PLACEHOLDER]

    def _label(value: str) -> str:
        if value == _CATALOG_UNAVAILABLE_PLACEHOLDER or label_for is None:
            return value
        return label_for(value)

    element = {
        "tag": "select_static",
        "name": name,
        "placeholder": _plain_text(placeholder),
        "options": [
            {
                "text": _plain_text(_label(value)),
                "value": ("" if value == _CATALOG_UNAVAILABLE_PLACEHOLDER else value),
            }
            for value in option_values
        ],
    }
    if required:
        element["required"] = True
    return element


def _input(*, name: str, placeholder: str, required: bool = False) -> dict[str, Any]:
    element: dict[str, Any] = {"tag": "input", "name": name, "placeholder": _plain_text(placeholder)}
    if required:
        element["required"] = True
    return element


def _callback_button(
    *,
    label: str,
    style: str,
    value: Mapping[str, str],
    form_submit: bool = False,
    name: str | None = None,
) -> dict[str, Any]:
    """``name``（Trace #469 S-1，200530 修复）：form 内提交按钮必须携带非空
    ``name``——飞书官方错误码 ``200530`` 明确要求（真实点击时触发，建卡请求
    本身不会暴露，见模块文档）。``form_submit=True`` 但未传 ``name`` 是实现
    缺陷，直接 ``ValueError`` 失败关闭，不静默漏发这个字段。非 form 内的独立
    按钮（如撤销组/历史行）不需要 ``name``，``name=None`` 时不写入这个键，保持
    既有形状不变。
    """

    if form_submit and not name:
        raise ValueError("form 内提交按钮必须提供非空 name（飞书官方错误码 200530）")
    button: dict[str, Any] = {
        "tag": "button",
        "text": _plain_text(label),
        "type": style,
        "behaviors": [{"type": "callback", "value": dict(value)}],
    }
    if name:
        button["name"] = name
    if form_submit:
        button["form_action_type"] = "submit"
    return button


def _markdown(content: str) -> dict[str, Any]:
    return {"tag": "markdown", "content": content}


def _galaxy_source_markdown(
    status: AdminUserStatusView, *, company_label_for: Callable[[str], str]
) -> str:
    """与 ``core/admin/router._render_galaxy_source`` 同一段文案逻辑的卡片版
    （两处刻意不共享实现——一处是纯文本回复，一处是卡片 markdown 片段，各自的
    调用面很薄，抽公共函数换来的耦合大于收益，与 ``core/admin/notification.py``
    与 ``router.py`` 对渲染层各自维护一份的既有取舍一致）。

    ``company_label_for``（Trace #469 修复包 B，B-7）：调用方
    （:func:`render_management_card`）已经用批量端口一次性翻译好这次渲染
    需要的全部公司编号，这里只做字典查找，不再直接持有
    :class:`~lingxi.core.admin.display_names.AdminDisplayNames`、不再自己
    触发任何一次单项查询。
    """

    summary = status.galaxy_source
    unavailable_reasons = {
        "roster_snapshot_unavailable",
        "galaxy_snapshot_unavailable",
        "role_function_map_unavailable",
    }
    if summary is None or summary.reason in unavailable_reasons:
        return "**银河权限**（银河来源）：暂时读不到（不代表该用户没有权限；当前不可用）"
    if not summary.granted:
        return f"**银河权限**（银河来源）：当前没有可用的银河权限（原因：{summary.reason}）"
    if summary.all_companies:
        company_label = "全部公司"
    else:
        company_label = (
            "、".join(company_label_for(cid) for cid in summary.companies) or "(无)"
        )
    function_label = "、".join(summary.functions) or "(无)"
    return f"**银河权限**（银河来源）：公司范围 {company_label} · 职能 {function_label}（职能标签，非最终指标名）"


#: 覆盖行原因文本在管理卡上的截断长度，与 ``core/admin/router.
#: _OVERRIDE_REASON_PREVIEW_LENGTH`` 同一取值——管理卡不是审计全文检索入口，
#: 与文本回复同一纪律（该常量文档同一姿态）。
_OVERRIDE_REASON_PREVIEW_LENGTH = 20

#: 术语统一（Trace #469 S-1，PM 补充裁定第 4 条）：与 ``core/admin/
#: notification._ACTION_LABEL``、``core/admin/router._OVERRIDE_DIRECTION_LABEL``
#: 三处同步——同一操作不允许两套说法。
_DIRECTION_LABEL: dict[str, str] = {"grant": "补充授权", "suppress": "屏蔽指标"}


def _override_row_elements(
    override: LocalPermissionOverrideView,
    *,
    display_identifier: str,
    company_label_for: Callable[[str], str],
    metric_label_for: Callable[[str], str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """``company_label_for``/``metric_label_for``（Trace #469 修复包 B，
    B-7）：与 :func:`_galaxy_source_markdown` 同一姿态——调用方已经批量翻译
    好这次渲染需要的全部编号，这里只做字典查找。"""

    direction_label = _DIRECTION_LABEL.get(override.direction, override.direction)
    reason = override.reason
    if len(reason) > _OVERRIDE_REASON_PREVIEW_LENGTH:
        reason = reason[:_OVERRIDE_REASON_PREVIEW_LENGTH] + "…"
    company_label = company_label_for(override.company_id)
    metric_label = metric_label_for(override.metric_name)
    if override.position_name:
        scope_label = company_label_for(override.company_scope or override.company_id)
        description = _markdown(
            f"- （补充授权）职位 {override.position_name} · 公司范围 {scope_label}"
            f" · 指标 {metric_label} · 原因 {reason} · {override.created_at}"
        )
    else:
        description = _markdown(
            f"- （{direction_label}）公司 {company_label} · 指标 {metric_label}"
            f" · 原因 {reason} · {override.created_at}"
        )
    button = _callback_button(
        label="撤销",
        style="danger",
        value={
            "admin_action": ADMIN_ACTION_REVOKE,
            "override_id": override.override_id,
            "identifier": display_identifier,
        },
    )
    return description, button


def _permission_group_elements(
    overrides: Sequence[LocalPermissionOverrideView],
    *,
    permission_group_id: str,
    display_identifier: str,
    company_label_for: Callable[[str], str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """把一笔职位+范围授权组渲染为一个可见、可撤销的管理卡项。

    展开后的公司×指标行只用于计数和服务端的幂等核对；管理员看到并点击的是
    ``permission_group_id`` 代表的一个业务项。组 ID 只进入按钮隐藏回调值，不进入
    可见 markdown，避免把内部标识重新变成用户界面语义。
    """

    if not overrides or not permission_group_id:
        raise ValueError("职位授权组必须包含至少一行且有 permission_group_id")
    first = overrides[0]
    reason = first.reason
    if len(reason) > _OVERRIDE_REASON_PREVIEW_LENGTH:
        reason = reason[:_OVERRIDE_REASON_PREVIEW_LENGTH] + "…"
    scope_label = company_label_for(first.company_scope or first.company_id)
    position_label = first.position_name or "（未知职位）"
    description = _markdown(
        f"- （补充授权）职位 {position_label} · 公司范围 {scope_label}"
        f" · 覆盖 {len(overrides)} 项权限 · 原因 {reason} · {first.created_at}"
    )
    button = _callback_button(
        label="撤销",
        style="danger",
        value={
            "admin_action": ADMIN_ACTION_REVOKE,
            "permission_group_id": permission_group_id,
            "identifier": display_identifier,
        },
    )
    return description, button


def render_management_card(
    status: AdminUserStatusView,
    *,
    display_identifier: str,
    catalog: CompanyMetricCatalog,
    display_names: AdminDisplayNames,
    submitted: bool = False,
    dispatch_status: str | None = None,
    status_message: str | None = None,
    closed: bool = False,
) -> dict[str, Any]:
    """把一次 ``/admin user`` 查询结果渲染成用户权限管理卡的 CardKit 2.0 JSON。

    ``display_identifier`` 是管理员实际输入的标识（邮箱或 open_id，见 #439 A 档
    ``resolve_identifier``）——卡片回显管理员自己刚输入的内容，不引入新的
    资料披露。**但标识本身长得像 open_id/override_id/待确认操作内部 ID
    （``ou_``/``lpo_``/``pac_`` 前缀）时，一律退化为通用占位「该用户」**
    （Trace #469 S-1）——"管理员可见文案零 ou_"是结构性要求，不因为这是管理员
    自己刚敲进来的输入就放宽；非内部 ID 形状的输入（多数是邮箱）继续原样回显。
    这条规则只影响卡片顶部的展示行，不影响表单/按钮 ``value`` 里回传给服务端
    的 ``identifier`` 字段（那是隐藏数据，不是展示文本，必须原样传真实值，
    否则表单提交/按钮点击会路由错目标）。

    ``display_names``（Trace #469 S-1 新增，必填）：公司/指标全部经这个端口
    翻译成人类可读文本，管理员可见文案零 ou_/lpo_/pac_、零裸公司编号/裸指标
    英文 ID（本模块自身不产生 open_id/override_id 之外的其它内部标识，这些
    内部 ID 只出现在按钮的隐藏 ``value`` 字段里，供服务端识别，不出现在任何
    展示文本中）。

    **请求级批量翻译（Trace #469 修复包 B，B-7：连接风暴收敛）**：本函数只
    调用 ``display_names.company_labels``/``metric_labels`` 各**一次**，把
    这次渲染用得到的全部公司/指标编号（下拉目录全集 + 银河来源展示 + 每一行
    本地覆盖各自的公司/指标）一次性翻译好，银河来源/本地覆盖/下拉选项三处
    全部改从这份内存映射里查，不再各自触发一次单项 ``company_label``/
    ``metric_label`` 调用——真实实现（``adapters/admin_registry.
    PostgresAdminQueries.company_label``）每次调用都新建两条数据库连接，
    公司目录当前 43 个编号会让一张卡片打开约 90 条连接（审查实测坐实）；
    批量端口把这个数字收敛到与编号数量无关的常数（两条：一次取当前银河
    批次号、一次批量查 ``name_cn``）。

    返回值形状：``{"schema": "2.0", "config": {...}, "body": {"elements": [...]}}``，
    与 ``core/admin/notification.render_card_payload`` 的确认卡同一顶层形状
    （``elements`` 是 ``body`` 下的顶层列表，不套任何已知会被 CardKit 拒绝的容器，
    见 ``adapters/feishu_admin_card.py`` 模块文档），三个分区依次：

    1. 银河来源 + 本地覆盖（各覆盖行一对"描述 markdown + 撤销按钮"）；
    2. 补充授权 / 屏蔽指标表单（公司/指标下拉 + 原因输入框 + 两个横排提交按钮）；
    3. （无）——不额外追加分隔或页脚，保持卡片精简。
    """

    companies_available = True
    try:
        companies = tuple(catalog.companies())
    except Exception:  # 目录不可用不得让整张卡渲染失败
        companies = ()
        companies_available = False
    try:
        metrics = tuple(catalog.metrics())
    except Exception:  # 同上
        metrics = ()

    # #493 的产品字段是银河职位+公司范围。旧目录假实现没有 ``positions``，
    # 继续渲染旧形状只为保持已有历史卡/单测的反序列化兼容；生产目录和所有新卡
    # 均走下面的新表单，不再渲染「屏蔽指标」入口。
    try:
        positions_reader = getattr(catalog, "positions")
    except AttributeError:
        positions_reader = None
    try:
        positions = tuple(positions_reader()) if callable(positions_reader) else ()
    except Exception:  # 目录不可用仍要显示卡片
        positions = ()
    position_form = callable(positions_reader)

    galaxy_source = status.galaxy_source
    galaxy_company_ids: tuple[str, ...] = (
        ()
        if galaxy_source is None or galaxy_source.all_companies
        else tuple(galaxy_source.companies)
    )
    # 去重但保持顺序（``dict.fromkeys``，与 ``_select_static`` 既有的去重
    # 姿势一致），三个来源合并成一次批量调用的完整输入集合。
    company_ids_needed = list(
        dict.fromkeys(
            (
                *companies,
                *galaxy_company_ids,
                *(override.company_id for override in status.local_overrides),
                *(
                    override.company_scope
                    for override in status.local_overrides
                    if override.company_scope and override.company_scope != "*"
                ),
            )
        )
    )
    metric_ids_needed = list(
        dict.fromkeys((*metrics, *(override.metric_name for override in status.local_overrides)))
    )
    company_label_map = dict(display_names.company_labels(company_ids=company_ids_needed))
    metric_label_map = dict(display_names.metric_labels(metric_ids=metric_ids_needed))

    def _company_label(company_id: str) -> str:
        if company_id == "*":
            # The wildcard is a product-facing scope choice, not a real company
            # key. Keep its actual current cardinality visible wherever an
            # already-applied position grant is rendered as well as in the form
            # option; never silently collapse it to a bare "全部" label.
            if not companies_available:
                return "全部（当前公司数暂不可用）"
            return f"全部（{len(companies)} 家公司）"
        return company_label_map.get(company_id, company_id)

    def _metric_label(metric_id: str) -> str:
        return metric_label_map.get(metric_id, metric_id)

    user_label = _safe_identifier_echo(display_identifier)
    user_label_reader = getattr(display_names, "user_label", None)
    if callable(user_label_reader):
        try:
            candidate = user_label_reader(open_id=status.identifier)
        except Exception:  # display lookup must not break the card
            candidate = ""
        if isinstance(candidate, str) and candidate.strip():
            user_label = candidate.strip()

    provisioning_labels = {
        "guest": "访客（尚未开始开通）",
        "matching": "银河权限匹配中",
        "manual_review": "待人工复核",
        "provisioning": "开通中",
        "mcp_syncing": "问数权限同步中",
        "active": "已开通",
        "aborted": "开通已中止",
    }
    account_labels = {
        "enabled": "启用",
        "suspended": "已停用",
        "deleting": "删除中",
        "deleted": "已删除",
    }
    elements: list[dict[str, Any]] = [
        _markdown(
            f"**用户权限管理卡** · {user_label} · 标识 {_safe_identifier_echo(display_identifier)}"
        ),
        _markdown(
            f"**开通状态**：{provisioning_labels.get(status.provisioning_state, status.provisioning_state)}\n"
            f"**账号状态**：{account_labels.get(status.account_state, status.account_state)}"
        ),
        _markdown(_galaxy_source_markdown(status, company_label_for=_company_label)),
        _markdown("**本地覆盖**"),
    ]
    if not status.local_overrides:
        elements.append(_markdown("无本地覆盖"))
    else:
        rendered_groups: set[str] = set()
        groups: dict[str, list[LocalPermissionOverrideView]] = {}
        for override in status.local_overrides:
            if override.permission_group_id:
                groups.setdefault(override.permission_group_id, []).append(override)
        for override in status.local_overrides:
            if override.permission_group_id:
                if override.permission_group_id in rendered_groups:
                    continue
                rendered_groups.add(override.permission_group_id)
                description, button = _permission_group_elements(
                    groups[override.permission_group_id],
                    permission_group_id=override.permission_group_id,
                    display_identifier=display_identifier,
                    company_label_for=_company_label,
                )
            else:
                description, button = _override_row_elements(
                    override,
                    display_identifier=display_identifier,
                    company_label_for=_company_label,
                    metric_label_for=_metric_label,
                )
            elements.append(description)
            elements.append(button)

    if status.updated_at:
        elements.append(_markdown(f"**数据取自**：{status.updated_at}"))
    if status_message:
        elements.append(_markdown(status_message))

    if position_form:
        elements.append(_markdown("**补充授权**"))
        if closed:
            elements.append(_markdown(status_message or dispatch_status or "已关闭"))
        elif submitted:
            status_text = "已提交，请在下方确认卡片上确认（10 分钟内有效）"
            if dispatch_status:
                status_text += f"\n\n当前状态：{dispatch_status}"
            elements.append(_markdown(status_text))
        else:
            scope_options = ("*", *companies)

            def _scope_label(value: str) -> str:
                if value == "*":
                    if not companies_available:
                        return "全部（当前公司数暂不可用）"
                    return f"全部（{len(companies)} 家公司）"
                return _company_label(value)

            form_elements: list[dict[str, Any]] = [
                _select_static(
                    name="position_name",
                    placeholder="选择银河职位",
                    options=positions,
                    required=True,
                ),
                _select_static(
                    name="company_scope",
                    placeholder="选择公司范围",
                    options=scope_options,
                    label_for=_scope_label,
                    required=True,
                ),
                _input(name="reason", placeholder="填写原因", required=True),
                button_row(
                    [
                        _callback_button(
                            label="补充授权",
                            style="primary",
                            form_submit=True,
                            name=GRANT_SUBMIT_BUTTON_NAME,
                            value={"admin_action": ADMIN_ACTION_GRANT, "identifier": display_identifier},
                        )
                    ]
                ),
            ]
            assert_unique_named_form_elements(form_elements)
            elements.append(
                {
                    "tag": "form",
                    "name": "admin_manage_position_scope_form",
                    "elements": form_elements,
                }
            )
            if dispatch_status:
                # 终态（已生效/不完整/已取消）恢复表单可用，但仍把这次异步
                # 处理结果留在原卡上，避免刷新后只看到新快照而丢失结果提示。
                elements.append(_markdown(f"当前状态：{dispatch_status}"))
        if not closed and not submitted:
            elements.append(
                _callback_button(
                    label="取消",
                    style="default",
                    value={"admin_action": ADMIN_ACTION_CANCEL, "identifier": display_identifier},
                )
            )
    else:
        # Legacy renderer for cards produced by pre-#493 test/catalog implementations.
        # 「公司×指标」的补充授权/屏蔽指标表单已随 ``/admin grant_permission`` /
        # ``/admin suppress_permission`` 一起撤除（Trace #544 D-5）：那两个按钮提交
        # 的就是这两条文本命令，命令没了，按钮留着只会点出一句"入口已下线"。这条
        # 分支只在 pre-#493 假目录下出现（生产目录始终提供 ``positions()``），因此
        # 只保留一句说明，不再渲染任何写入表单——不给管理员一个按不动的按钮。
        elements.append(_markdown("**补充授权**"))
        elements.append(_markdown("当前目录不可用，暂时无法发起补充授权。"))

    return {"schema": "2.0", "config": {"update_multi": True}, "body": {"elements": elements}}


@dataclass(frozen=True)
class ManagementCardCreated:
    """建卡并作为消息发出后的结果——与 ``core/admin/notification.AdminCardCreated``
    同一形状，独立定义是为了不让本模块反向 import ``notification.py``（两张卡各自
    独立，见模块文档）。"""

    card_id: str
    message_id: str


class ManagementCardTransport(Protocol):
    """用户权限管理卡的出站端口。真实实现见
    ``adapters/feishu_admin_card.LarkAdminManagementCardTransport``；测试注入
    内存假实现。管理卡的 ``update()`` 在原卡片实体上刷新懒过期、已提交和异步
    下发状态；更新序号由持久上下文存储提供，不能由调用方硬编码。
    """

    def create(
        self,
        *,
        chat_id: str,
        thread_id: str | None,
        reply_to_message_id: str,
        card: dict[str, Any],
    ) -> ManagementCardCreated: ...

    def update(self, *, card_id: str, sequence: int, card: dict[str, Any]) -> None: ...
