"""用户权限管理卡的展示层：纯函数渲染（职位+公司范围授权）。

与 ``core/admin/notification.py`` 的确认卡片是**两张不同的卡**，不合并：产品
合同「管理员处理入口与安全确认」明确确认卡只承担"仅展示一个已经准备好的动作 +
确认/取消"，管理卡负责查询结果展示、补充授权表单和撤销入口。每一次写意图都转译
成一条等价的 ``/admin ...`` 命令文本交给 ``AdminCommandRouter.route()``，复用其
``prepare()``/角色核对/审计逻辑，本模块不重新实现写路径判定，只负责渲染，也不
import 任何飞书 SDK 或 ``adapters/``（代码框架第二节）。

管理员可见的公司/指标/用户标识全部经 :class:`~lingxi.core.admin.display_names.
AdminDisplayNames` 翻译成人类可读文本；下拉选项的**提交值**仍是真实编号/ID，
只有展示给管理员看的 ``text`` 经过翻译。外部平台证据边界：按钮/markdown/
``column_set`` 嵌 ``form`` 的结构已被真实探针验证，真实点击后回调事件体是否
逐字符合尚未受控验收。
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
#: **公开常量**：真实点击实测坐实表单提交回调的 ``action.value`` 常常不以
#: Mapping 形态到达，``apps/gateway/__init__.py`` 的路由分流需要一个不依赖
#: ``value`` 内容的后备判据，用回调事件带回的按钮 ``action.name`` 兜底识别，
#: 这个常量必须能被外部 import。
GRANT_SUBMIT_BUTTON_NAME = "grant_submit"

#: 指标/公司目录不可用时下拉唯一的占位选项——``select_static`` 需要至少一个
#: ``options`` 条目才是结构完整的组件；用一个空 ``value`` 的占位项而不是省略整个
#: 下拉，管理员点开至少能看到"当前无法枚举"这句明确提示，不是一段视觉空白。点击
#: 提交时 ``value`` 为空会被 ``card_callback.py`` 的表单处理分支当成"未选择"拒绝，
#: 不会被静默当成一个真正的公司/指标。
_CATALOG_UNAVAILABLE_PLACEHOLDER = "（当前无法枚举，请改用文本命令）"

#: 内部标识前缀白名单，与 ``core/admin/router._INTERNAL_ID_PREFIXES`` 同一份
#: 判据、独立各自维护一份（两处调用面很薄，抽公共函数换来的耦合大于收益）。
_INTERNAL_ID_PREFIXES: tuple[str, ...] = ("ou_", "lpo_", "lpg_", "pac_")


def _safe_identifier_echo(identifier: str) -> str:
    if identifier.startswith(_INTERNAL_ID_PREFIXES):
        return "该用户"
    return identifier


class CompanyMetricCatalog(Protocol):
    """公司/指标下拉选项目录端口。

    真实实现读取 ``config/company_function_metric_map.toml``，见
    ``adapters/feishu_admin_card.TomlCompanyMetricCatalog``。本模块不 import
    adapters/，只声明调用方需要的最小接口（代码框架第二节）。
    """

    def companies(self) -> Sequence[str]:
        """全部公司编号。"""

    def metrics(self) -> Sequence[str]:
        """全部指标 ID。"""

    def positions(self) -> Sequence[str]:
        """全部银河职位名。"""


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
    """把候选值列表渲染成一个 ``select_static`` 组件。

    ``label_for``：把每个候选值翻译成管理员看到的展示文本；``value`` 字段
    永远是原始候选值本身（提交语义不变）。``None`` 时展示文本与提交值相同，
    占位选项永远不经过 ``label_for``——它本来就不是一个真实的公司/指标。
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
    element: dict[str, Any] = {
        "tag": "input",
        "name": name,
        "placeholder": _plain_text(placeholder),
    }
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
    """构造一个卡片按钮元素。

    ``name``：form 内提交按钮必须携带非空 ``name``——飞书官方错误码
    ``200530`` 明确要求（真实点击时触发，建卡请求本身不会暴露）。
    ``form_submit=True`` 但未传 ``name`` 是实现缺陷，直接 ``ValueError`` 失败
    关闭，不静默漏发这个字段。非 form 内的独立按钮不需要 ``name``。
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
    """渲染银河来源那一段 markdown 文案。

    与 ``core/admin/router._render_galaxy_source`` 同一段文案逻辑的卡片版
    （两处刻意不共享实现，调用面很薄，抽公共函数换来的耦合大于收益）。
    ``company_label_for``：调用方已经用批量端口一次性翻译好这次渲染需要的
    全部公司编号，这里只做字典查找，不再自己触发任何一次单项查询。
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
        company_label = "、".join(company_label_for(cid) for cid in summary.companies) or "(无)"
    function_label = "、".join(summary.functions) or "(无)"
    return f"**银河权限**（银河来源）：公司范围 {company_label} · 职能 {function_label}（职能标签，非最终指标名）"


#: 覆盖行原因文本在管理卡上的截断长度，与 ``core/admin/router.
#: _OVERRIDE_REASON_PREVIEW_LENGTH`` 同一取值——管理卡不是审计全文检索入口，
#: 与文本回复同一纪律（该常量文档同一姿态）。
_OVERRIDE_REASON_PREVIEW_LENGTH = 20

#: 术语统一：与 ``core/admin/notification._ACTION_LABEL``、
#: ``core/admin/router._OVERRIDE_DIRECTION_LABEL`` 三处同步，同一操作不允许
#: 两套说法。
_DIRECTION_LABEL: dict[str, str] = {"grant": "补充授权", "suppress": "屏蔽指标"}


def _override_row_elements(
    override: LocalPermissionOverrideView,
    *,
    display_identifier: str,
    company_label_for: Callable[[str], str],
    metric_label_for: Callable[[str], str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """把一行本地覆盖渲染成"描述 markdown + 撤销按钮"对。

    ``company_label_for``/``metric_label_for``：与 :func:`_galaxy_source_markdown`
    同一姿态——调用方已经批量翻译好这次渲染需要的全部编号，这里只做字典查找。
    """
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


def _resolve_catalog_data(
    catalog: CompanyMetricCatalog,
) -> tuple[tuple[str, ...], bool, tuple[str, ...], tuple[str, ...], bool]:
    """读取公司/指标/职位目录；任一读取失败都退化为空，不让整张卡渲染失败。

    旧目录假实现没有 ``positions``——继续渲染旧形状只为保持已有历史卡/单测
    的反序列化兼容；生产目录和所有新卡都提供 ``positions()`` 并走新表单。
    返回 ``(companies, companies_available, metrics, positions, position_form)``。
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
    try:
        positions_reader = getattr(catalog, "positions")
    except AttributeError:
        positions_reader = None
    try:
        positions = tuple(positions_reader()) if callable(positions_reader) else ()
    except Exception:  # 目录不可用仍要显示卡片
        positions = ()
    position_form = callable(positions_reader)
    return companies, companies_available, metrics, positions, position_form


def _batch_translate_labels(
    status: AdminUserStatusView,
    *,
    companies: Sequence[str],
    metrics: Sequence[str],
    display_names: AdminDisplayNames,
) -> tuple[dict[str, str], dict[str, str]]:
    """把这次渲染用得到的全部公司/指标编号一次性批量翻译好。

    银河来源/本地覆盖/下拉选项三处全部改从返回的映射里查，不再各自触发一次
    单项查询——真实实现每次单项查询都新建数据库连接，批量端口把连接数收敛到
    与编号数量无关的常数。
    """
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
    return company_label_map, metric_label_map


def _resolve_user_label(
    display_names: AdminDisplayNames, *, status: AdminUserStatusView, display_identifier: str
) -> str:
    """解析卡片顶部展示的用户标识。

    优先经 ``display_names.user_label`` 解析出的人类可读文本，解析不可用或
    失败时退回管理员实际输入的标识本身（内部 ID 形状仍会被
    :func:`_safe_identifier_echo` 折叠成通用占位）。
    """
    user_label = _safe_identifier_echo(display_identifier)
    user_label_reader = getattr(display_names, "user_label", None)
    if callable(user_label_reader):
        try:
            candidate = user_label_reader(open_id=status.identifier)
        except Exception:  # display lookup must not break the card
            candidate = ""
        if isinstance(candidate, str) and candidate.strip():
            user_label = candidate.strip()
    return user_label


def _render_header_elements(
    status: AdminUserStatusView,
    *,
    user_label: str,
    display_identifier: str,
    company_label_for: Callable[[str], str],
) -> list[dict[str, Any]]:
    """卡片头部三段 markdown 加上"本地覆盖"分节标题。

    依次是用户标识行、开通/账号状态行、银河来源行。
    """
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
    return [
        _markdown(
            f"**用户权限管理卡** · {user_label} · 标识 {_safe_identifier_echo(display_identifier)}"
        ),
        _markdown(
            f"**开通状态**：{provisioning_labels.get(status.provisioning_state, status.provisioning_state)}\n"
            f"**账号状态**：{account_labels.get(status.account_state, status.account_state)}"
        ),
        _markdown(_galaxy_source_markdown(status, company_label_for=company_label_for)),
        _markdown("**本地覆盖**"),
    ]


def _render_local_override_elements(
    local_overrides: Sequence[LocalPermissionOverrideView],
    *,
    display_identifier: str,
    company_label_for: Callable[[str], str],
    metric_label_for: Callable[[str], str],
) -> list[dict[str, Any]]:
    """把本地覆盖行渲染成"描述 markdown + 撤销按钮"对。

    同一 ``permission_group_id`` 的多行只渲染一个聚合撤销项，历史无组行
    各自逐行渲染。
    """
    if not local_overrides:
        return [_markdown("无本地覆盖")]
    elements: list[dict[str, Any]] = []
    rendered_groups: set[str] = set()
    groups: dict[str, list[LocalPermissionOverrideView]] = {}
    for override in local_overrides:
        if override.permission_group_id:
            groups.setdefault(override.permission_group_id, []).append(override)
    for override in local_overrides:
        if override.permission_group_id:
            if override.permission_group_id in rendered_groups:
                continue
            rendered_groups.add(override.permission_group_id)
            description, button = _permission_group_elements(
                groups[override.permission_group_id],
                permission_group_id=override.permission_group_id,
                display_identifier=display_identifier,
                company_label_for=company_label_for,
            )
        else:
            description, button = _override_row_elements(
                override,
                display_identifier=display_identifier,
                company_label_for=company_label_for,
                metric_label_for=metric_label_for,
            )
        elements.append(description)
        elements.append(button)
    return elements


def _grant_form_elements(
    *,
    positions: Sequence[str],
    companies: Sequence[str],
    companies_available: bool,
    company_label_for: Callable[[str], str],
    display_identifier: str,
) -> list[dict[str, Any]]:
    """构造补充授权表单本体：职位/公司范围下拉 + 原因输入框 + 单个提交按钮。"""
    scope_options = ("*", *companies)

    def _scope_label(value: str) -> str:
        if value == "*":
            if not companies_available:
                return "全部（当前公司数暂不可用）"
            return f"全部（{len(companies)} 家公司）"
        return company_label_for(value)

    form_elements: list[dict[str, Any]] = [
        _select_static(
            name="position_name", placeholder="选择银河职位", options=positions, required=True
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
    return [{"tag": "form", "name": "admin_manage_position_scope_form", "elements": form_elements}]


def _render_grant_section(
    *,
    positions: Sequence[str],
    companies: Sequence[str],
    companies_available: bool,
    company_label_for: Callable[[str], str],
    display_identifier: str,
    submitted: bool,
    dispatch_status: str | None,
    status_message: str | None,
    closed: bool,
) -> list[dict[str, Any]]:
    """新形状（``positions()`` 目录可用）的"补充授权"分节。

    三种互斥态——已关闭、已提交待确认、可继续编辑并提交的表单，外加一个
    非终态时才出现的取消按钮。
    """
    elements: list[dict[str, Any]] = [_markdown("**补充授权**")]
    if closed:
        elements.append(_markdown(status_message or dispatch_status or "已关闭"))
    elif submitted:
        status_text = "已提交，请在下方确认卡片上确认（10 分钟内有效）"
        if dispatch_status:
            status_text += f"\n\n当前状态：{dispatch_status}"
        elements.append(_markdown(status_text))
    else:
        elements.extend(
            _grant_form_elements(
                positions=positions,
                companies=companies,
                companies_available=companies_available,
                company_label_for=company_label_for,
                display_identifier=display_identifier,
            )
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
    return elements


def _legacy_grant_unavailable_elements() -> list[dict[str, Any]]:
    """旧假目录（无 ``positions()``）分支的"补充授权"提示。

    命令面已不存在，不渲染任何写入表单，只提示当前不可用——不给管理员一个
    按不动的按钮。
    """
    return [
        _markdown("**补充授权**"),
        _markdown("当前目录不可用，暂时无法发起补充授权。"),
    ]


def _prepare_label_resolvers(
    catalog: CompanyMetricCatalog,
    status: AdminUserStatusView,
    display_names: AdminDisplayNames,
) -> tuple[
    Callable[[str], str], Callable[[str], str], tuple[str, ...], bool, tuple[str, ...], bool
]:
    """打包目录读取 + 批量翻译，交出两个可直接调用的标签解析闭包。

    返回 ``(company_label_for, metric_label_for, companies, companies_available,
    positions, position_form)``——后四项供调用方渲染表单/统计公司数时复用，
    不需要重新读一次目录。
    """
    companies, companies_available, metrics, positions, position_form = _resolve_catalog_data(
        catalog
    )
    company_label_map, metric_label_map = _batch_translate_labels(
        status, companies=companies, metrics=metrics, display_names=display_names
    )

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

    return _company_label, _metric_label, companies, companies_available, positions, position_form


def _render_overview_elements(
    status: AdminUserStatusView,
    *,
    user_label: str,
    display_identifier: str,
    company_label_for: Callable[[str], str],
    metric_label_for: Callable[[str], str],
) -> list[dict[str, Any]]:
    """卡片头部三段 markdown 加上本地覆盖行。

    查询结果部分，写表单之前的全部只读展示内容。
    """
    elements = _render_header_elements(
        status,
        user_label=user_label,
        display_identifier=display_identifier,
        company_label_for=company_label_for,
    )
    elements.extend(
        _render_local_override_elements(
            status.local_overrides,
            display_identifier=display_identifier,
            company_label_for=company_label_for,
            metric_label_for=metric_label_for,
        )
    )
    return elements


def _render_grant_or_legacy_elements(
    *,
    position_form: bool,
    positions: Sequence[str],
    companies: Sequence[str],
    companies_available: bool,
    company_label_for: Callable[[str], str],
    display_identifier: str,
    submitted: bool,
    dispatch_status: str | None,
    status_message: str | None,
    closed: bool,
) -> list[dict[str, Any]]:
    """写"补充授权"表单部分。

    目录提供 ``positions()`` 时走新形状表单，否则走旧假目录的"当前不可用"
    提示分支。
    """
    if not position_form:
        return _legacy_grant_unavailable_elements()
    return _render_grant_section(
        positions=positions,
        companies=companies,
        companies_available=companies_available,
        company_label_for=company_label_for,
        display_identifier=display_identifier,
        submitted=submitted,
        dispatch_status=dispatch_status,
        status_message=status_message,
        closed=closed,
    )


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

    ``display_identifier`` 长得像内部 ID 时退化为通用占位「该用户」（不影响
    回传给服务端的隐藏 ``identifier`` 字段）；``display_names`` 必填，公司/
    指标/用户标识全部经此翻译成人类可读文本。返回值三个分区依次：银河来源 +
    本地覆盖、补充授权表单、（无，不额外追加分隔或页脚）。
    """
    (
        company_label_for,
        metric_label_for,
        companies,
        companies_available,
        positions,
        position_form,
    ) = _prepare_label_resolvers(catalog, status, display_names)
    user_label = _resolve_user_label(
        display_names, status=status, display_identifier=display_identifier
    )

    elements = _render_overview_elements(
        status,
        user_label=user_label,
        display_identifier=display_identifier,
        company_label_for=company_label_for,
        metric_label_for=metric_label_for,
    )
    if status.updated_at:
        elements.append(_markdown(f"**数据取自**：{status.updated_at}"))
    if status_message:
        elements.append(_markdown(status_message))
    elements.extend(
        _render_grant_or_legacy_elements(
            position_form=position_form,
            positions=positions,
            companies=companies,
            companies_available=companies_available,
            company_label_for=company_label_for,
            display_identifier=display_identifier,
            submitted=submitted,
            dispatch_status=dispatch_status,
            status_message=status_message,
            closed=closed,
        )
    )

    return {"schema": "2.0", "config": {"update_multi": True}, "body": {"elements": elements}}


@dataclass(frozen=True)
class ManagementCardCreated:
    """建卡并作为消息发出后的结果。

    与 ``core/admin/notification.AdminCardCreated`` 同一形状，独立定义是为了
    不让本模块反向 import ``notification.py``（两张卡各自独立，见模块文档）。
    """

    card_id: str
    message_id: str


class ManagementCardTransport(Protocol):
    """用户权限管理卡的出站端口。

    真实实现见 ``adapters/feishu_admin_card.LarkAdminManagementCardTransport``；
    管理卡的 ``update()`` 在原卡片实体上刷新懒过期、已提交和异步下发状态；
    更新序号由持久上下文存储提供，不能由调用方硬编码。
    """

    def create(
        self,
        *,
        chat_id: str,
        thread_id: str | None,
        reply_to_message_id: str,
        card: dict[str, Any],
    ) -> ManagementCardCreated:
        """建一张管理卡并作为消息发出。"""

    def update(self, *, card_id: str, sequence: int, card: dict[str, Any]) -> None:
        """按持久 sequence 把管理卡刷新成新内容。"""
