"""用户权限管理卡的展示层：纯函数渲染（#439 B 档，管理员操作一卡化）。

与 ``core/admin/notification.py`` 的确认卡片是**两张不同的卡**，不合并进同一张卡
（见 ``core/admin/router.AdminCommandRouter._send_management_card`` 文档、本 Story
报告"同卡二次确认"裁定一节）：产品合同「管理员处理入口与安全确认」正文明确
"待确认操作发送到发起管理员本人的飞书私聊，卡片只承担最终确认，不承担搜索、比较、
审批流或复杂信息填写"——管理卡负责"复杂信息填写"（银河来源/本地覆盖分列展示 +
新增授权/新增抑制下拉表单 + 逐行收回按钮），确认卡继续只承担"仅展示一个已经准备好
的动作 + 确认/取消"。管理卡上的每一次写意图（提交表单 / 点击收回按钮）都会转译成
一条等价的 ``/admin ...`` 命令文本，交给已有的 ``AdminCommandRouter.route()``
处理（见 ``core/admin/card_callback.py`` 新增的回调分支），复用其全部既有
``prepare()``/角色核对/自我目标防呆/确认卡发送/审计逻辑，本模块不重新实现一遍
写路径判定，只负责渲染。

只负责"展示什么"，不负责"怎么发出去"：真实 CardKit 调用住在
``adapters/feishu_admin_card.py``，本模块不 import 任何飞书 SDK，也不 import
``adapters/``（代码框架第二节），可以在没有装 SDK 的环境里测试全部渲染断言。

## 组件选择

- **公司 / 指标**：``select_static``（官方下拉组件）。选项来自真实指标目录
  （``config/company_function_metric_map.toml``，经 ``CompanyMetricCatalog`` 端口
  注入，不在本模块或调用方发明任何公司/指标数据——目录当前内容见该文件模块文档，
  由产品负责人 2026-08-19 填入，非本卡新增）。目录暂不可用（读取失败/为空）时，
  对应下拉退化为一个不可选的占位选项，而不是省略整个表单——管理员至少能看到"当前
  无法枚举，请改用文本命令"这句明确提示，不是一段视觉上缺失的空白。
- **原因**：``input``（官方输入框组件），自由文本。
- **新增授权 / 新增抑制**：两个按钮共享同一个 ``form`` 容器（飞书卡片 2.0 官方
  表单组件），提交时把公司/指标/原因三个字段随点击的按钮一起带回同一次回调
  （``form_action_type: "submit"``）。
- **逐行收回**：每条当前生效的本地覆盖各配一个独立按钮，复用已被真实 CardKit
  探针验证过的"按钮 + ``behaviors: [{type: callback, value: {...}}]``"形状（见
  ``adapters/feishu_admin_card.py`` 模块文档"2026-08-25 建卡环节已被真实探针证伪
  并修复"）——不需要额外字段输入，不放进 ``form``。

## 证据等级 1：``select_static``/``input``/``form`` 三种组件未经真实探针验证

上面引用的"已被真实探针验证过"只覆盖按钮与 markdown 两种组件；本模块新增的三种
组件字段名依据飞书卡片 2.0 公开文档《表单容器（form）》一节描述的形状，真实
网络往返、真实回调事件体是否逐字符合仍是 ``biai-stage`` L4a 受控验收范围，本
Story 未验证（见报告"未验证事项"）。这与本仓库既有先例的性质相同——2026-08-25
之前的按钮/action 容器同样是"先按文档实现、后被真实探针证伪并修复"，本模块如实
标注同一等级的未验证状态，不冒充已验证。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol, Sequence

from lingxi.core.admin.views import AdminUserStatusView, LocalPermissionOverrideView

#: 卡片按钮/表单提交回传的 ``admin_action`` 取值，供
#: ``core/admin/card_callback.py`` 新增的回调分支识别是哪一类管理卡交互。
ADMIN_ACTION_GRANT = "grant"
ADMIN_ACTION_SUPPRESS = "suppress"
ADMIN_ACTION_REVOKE = "revoke"

#: 指标/公司目录不可用时下拉唯一的占位选项——``select_static`` 需要至少一个
#: ``options`` 条目才是结构完整的组件；用一个空 ``value`` 的占位项而不是省略整个
#: 下拉，管理员点开至少能看到"当前无法枚举"这句明确提示，不是一段视觉空白。点击
#: 提交时 ``value`` 为空会被 ``card_callback.py`` 的表单处理分支当成"未选择"拒绝，
#: 不会被静默当成一个真正的公司/指标。
_CATALOG_UNAVAILABLE_PLACEHOLDER = "（当前无法枚举，请改用文本命令）"


class CompanyMetricCatalog(Protocol):
    """公司/指标下拉选项目录端口。真实实现读取
    ``config/company_function_metric_map.toml``（``adapters/
    company_function_metric_map_file.py``，已有、未改动），见
    ``adapters/feishu_admin_card.TomlCompanyMetricCatalog``。本模块不 import
    adapters/，只声明调用方需要的最小接口（代码框架第二节）。"""

    def companies(self) -> Sequence[str]: ...

    def metrics(self) -> Sequence[str]: ...


def _plain_text(content: str) -> dict[str, str]:
    return {"tag": "plain_text", "content": content}


def _select_static(*, name: str, placeholder: str, options: Sequence[str]) -> dict[str, Any]:
    option_values = list(dict.fromkeys(options)) or [_CATALOG_UNAVAILABLE_PLACEHOLDER]
    return {
        "tag": "select_static",
        "name": name,
        "placeholder": _plain_text(placeholder),
        "options": [
            {"text": _plain_text(value), "value": ("" if value == _CATALOG_UNAVAILABLE_PLACEHOLDER else value)}
            for value in option_values
        ],
    }


def _input(*, name: str, placeholder: str) -> dict[str, Any]:
    return {"tag": "input", "name": name, "placeholder": _plain_text(placeholder)}


def _callback_button(
    *, label: str, style: str, value: Mapping[str, str], form_submit: bool = False
) -> dict[str, Any]:
    button: dict[str, Any] = {
        "tag": "button",
        "text": _plain_text(label),
        "type": style,
        "behaviors": [{"type": "callback", "value": dict(value)}],
    }
    if form_submit:
        button["form_action_type"] = "submit"
    return button


def _markdown(content: str) -> dict[str, Any]:
    return {"tag": "markdown", "content": content}


def _galaxy_source_markdown(status: AdminUserStatusView) -> str:
    """与 ``core/admin/router._render_galaxy_source`` 同一段文案逻辑的卡片版
    （两处刻意不共享实现——一处是纯文本回复，一处是卡片 markdown 片段，各自的
    调用面很薄，抽公共函数换来的耦合大于收益，与 ``core/admin/notification.py``
    与 ``router.py`` 对渲染层各自维护一份的既有取舍一致）。"""

    summary = status.galaxy_source
    unavailable_reasons = {
        "roster_snapshot_unavailable",
        "galaxy_snapshot_unavailable",
        "role_function_map_unavailable",
    }
    if summary is None or summary.reason in unavailable_reasons:
        return "**银河来源**：不可用（无法计算，不代表该用户没有银河权限）"
    if not summary.granted:
        return f"**银河来源**：当前没有可用的银河权限（原因：{summary.reason}）"
    company_label = "全部公司" if summary.all_companies else "、".join(summary.companies) or "(无)"
    function_label = "、".join(summary.functions) or "(无)"
    return f"**银河来源**：公司范围 {company_label} · 职能 {function_label}（职能标签，非最终指标名）"


#: 覆盖行原因文本在管理卡上的截断长度，与 ``core/admin/router.
#: _OVERRIDE_REASON_PREVIEW_LENGTH`` 同一取值——管理卡不是审计全文检索入口，
#: 与文本回复同一纪律（该常量文档同一姿态）。
_OVERRIDE_REASON_PREVIEW_LENGTH = 20

_DIRECTION_LABEL: dict[str, str] = {"grant": "授权", "suppress": "抑制"}


def _override_row_elements(
    override: LocalPermissionOverrideView, *, display_identifier: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    direction_label = _DIRECTION_LABEL.get(override.direction, override.direction)
    reason = override.reason
    if len(reason) > _OVERRIDE_REASON_PREVIEW_LENGTH:
        reason = reason[:_OVERRIDE_REASON_PREVIEW_LENGTH] + "…"
    description = _markdown(
        f"- （{direction_label}）公司 {override.company_id} · 指标 {override.metric_name}"
        f" · 原因 {reason} · {override.created_at}"
    )
    button = _callback_button(
        label="收回",
        style="danger",
        value={
            "admin_action": ADMIN_ACTION_REVOKE,
            "override_id": override.override_id,
            "identifier": display_identifier,
        },
    )
    return description, button


def render_management_card(
    status: AdminUserStatusView, *, display_identifier: str, catalog: CompanyMetricCatalog
) -> dict[str, Any]:
    """把一次 ``/admin user`` 查询结果渲染成用户权限管理卡的 CardKit 2.0 JSON。

    ``display_identifier`` 是管理员实际输入的标识（邮箱或 open_id，见 #439 A 档
    ``resolve_identifier``）——卡片回显管理员自己刚输入的内容，不引入新的资料
    披露，也不强迫管理员在卡片上看到内部 open_id（若他查询时用的是邮箱）。

    返回值形状：``{"schema": "2.0", "config": {...}, "body": {"elements": [...]}}``，
    与 ``core/admin/notification.render_card_payload`` 的确认卡同一顶层形状
    （``elements`` 是 ``body`` 下的顶层列表，不套任何已知会被 CardKit 拒绝的容器，
    见 ``adapters/feishu_admin_card.py`` 模块文档），三个分区依次：

    1. 银河来源 + 本地覆盖（各覆盖行一对"描述 markdown + 收回按钮"）；
    2. 新增授权 / 新增抑制表单（公司/指标下拉 + 原因输入框 + 两个提交按钮）；
    3. （无）——不额外追加分隔或页脚，保持卡片精简。
    """

    try:
        companies = tuple(catalog.companies())
    except Exception:  # noqa: BLE001 - 目录不可用不得让整张卡渲染失败
        companies = ()
    try:
        metrics = tuple(catalog.metrics())
    except Exception:  # noqa: BLE001 - 同上
        metrics = ()

    elements: list[dict[str, Any]] = [
        _markdown(f"**用户权限管理卡** · 标识 {display_identifier}"),
        _markdown(_galaxy_source_markdown(status)),
        _markdown("**本地覆盖**"),
    ]
    if not status.local_overrides:
        elements.append(_markdown("无本地覆盖"))
    else:
        for override in status.local_overrides:
            description, button = _override_row_elements(
                override, display_identifier=display_identifier
            )
            elements.append(description)
            elements.append(button)

    elements.append(_markdown("**新增授权 / 新增抑制**"))
    elements.append(
        {
            "tag": "form",
            "name": "admin_manage_grant_suppress_form",
            "elements": [
                _select_static(name="company_id", placeholder="选择公司", options=companies),
                _select_static(name="metric_name", placeholder="选择指标", options=metrics),
                _input(name="reason", placeholder="填写原因"),
                _callback_button(
                    label="新增授权",
                    style="primary",
                    form_submit=True,
                    value={"admin_action": ADMIN_ACTION_GRANT, "identifier": display_identifier},
                ),
                _callback_button(
                    label="新增抑制",
                    style="danger",
                    form_submit=True,
                    value={"admin_action": ADMIN_ACTION_SUPPRESS, "identifier": display_identifier},
                ),
            ],
        }
    )

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
    内存假实现。管理卡不支持 ``update()``——它不是一次待确认操作，不需要"终态
    更新"这个概念，每次 ``/admin user`` 查询都发一张新卡，旧卡片保持原样可继续
    交互（管理员可以对同一个用户先后发起多次授权/抑制/收回，互不影响）。
    """

    def create(
        self,
        *,
        chat_id: str,
        thread_id: str | None,
        reply_to_message_id: str,
        card: dict[str, Any],
    ) -> ManagementCardCreated: ...
