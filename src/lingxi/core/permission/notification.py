"""权限变化通知：正文渲染与「有限重试 + 审计」的发送编排（纯编排）。

[Issue #156](https://github.com/Moshuiwang/lingxi/issues/156) 的 S-C-03b。**什么时候**
通知不在这里（在 :mod:`lingxi.apps.scheduler.permission_publish`），**怎么发**也不在这里
（在 :mod:`lingxi.adapters.feishu_user_message`）。本模块只回答两件事：

1. **这一次变化对用户说什么**——正文由内容目录渲染，占位变量取自**我们真的发布出去的
   那一份 ``permissions``**；
2. **发不出去怎么办**——有限次重试，仍失败就记一条可计数的审计，**不转运维、不阻塞
   权限生效、不改变任何发布或就绪状态**。

## 触发点与文案：产品负责人 2026-08-18 裁定

留痕见 Trace [#203](https://github.com/Moshuiwang/lingxi/issues/203) 的决策评论。

- **增权在 MCP 就绪探针成功之后通知；撤权在发布读回一致之后通知**（撤权没有"就绪"
  可等）。两个触发点都在调度职责里，本模块不判断时机。
- **粒度是「范围式」**：只说"当前可用范围是什么"，不做公司级 / 指标级的 diff。
- **文案不承诺即时生效**：问数 MCP 每十五分钟拉一次发布表，因此正文说的是"已更新、
  你可以发起一次查询验证"，而不是"现在立刻可用"。
- 两条正文由产品负责人**逐字批准**，进 ``config/content.toml`` 并受版本纪律门禁约束
  （``scripts/ci/check_content_version.py``）。

## 占位变量从哪里来：**发布出去的那一份权限文本**

``{company_name}`` / ``{function_name}`` 取自 :func:`lingxi.core.permission.publish_row.
parse_permissions` 解析出来的那份文档，也就是 outbox 快照里、并且已经**逐字段读回核对
一致**的那一串。这条选择不是就近取材：

- 它让通知说的范围与消费方（问数 MCP）实际会读到的范围**是同一份数据**，而不是我们在
  另一个时刻、用另一条链路重新算一遍的结果；
- 它让"撤权通知"这件事结构上成立——权限文档里查不到任何指标时只可能渲染出撤权文案，
  想在这里渲染出一句"你还有权限"是写不出来的。

**两处已登记的短板，不掩盖**：

1. **公司位显示的是公司编号**（``boss_company_id``），不是公司中文名。发布链路上没有
   名称来源——``name_cn`` 属银河快照的展示字段，刻意不进发布行（见
   :mod:`lingxi.core.permission.publish_row` 的模块文档）。持有「全非」通配时显示
   :data:`CONTENT_KEY_ALL_COMPANIES` 那条词条，不把 ``*`` 直接给用户看。
2. **职能位显示的是发布文本里的值列表**。当前那些值还是**职能标签**，与文案里的"职能"
   一致；「公司 + 职能 → 指标名」翻译层（S-C-01 已登记的阻塞项）补上之后，这些值会变成
   **指标名**，那时这一句的措辞需要与产品负责人一起复核——翻译层那一 Story 必须回到这里。

## 失败语义：有限重试 + 审计，**绝不反向影响权限**

产品负责人 2026-08-18 裁定 4：通知失败**有限重试**，仍失败就记审计与可见计数，
**不转运维、不阻塞权限生效**。因此 :meth:`PermissionNoticeDispatcher.notify`：

- 不抛异常（发送端口的异常在这里被吸收并分类），调用方据 :class:`NoticeResult` 计数；
- 不回滚、不重置、不重排任何发布意图或就绪记录——它拿不到那些端口，在类型上就做不到；
- 重试**携带同一个去重键**（``(用户, 权限版本)``），由适配器折成飞书的投递 ``uuid``。
  能承诺的是"重试一定带同一个 uuid"这个**代码事实**；"平台一定不会重复投递"是平台行为，
  未验证（同 :func:`lingxi.adapters.feishu_group_message.delivery_uuid` 的登记）。

## 审计只记键与版本，不记正文

纪律同 :class:`lingxi.apps.scheduler.RosterAuditDuty` 的日报审计：进审计的是
``content_key`` / ``content_version`` / 用户内部标识 / 权限版本 / 尝试次数 / 错误码，
**渲染后的正文一个字都不进审计与日志**——正文里有这个人的权限范围。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Protocol

from lingxi.config.content import ContentCatalog, RenderedContent, default_content_catalog
from lingxi.core.permission.publish_row import (
    ALL_COMPANIES_KEY,
    lookup_metrics,
    parse_permissions,
)

logger = logging.getLogger(__name__)

#: 内容目录里的三个键。全模块只有这三处字面量，别处一律引用它们。
CONTENT_KEY_RANGE_UPDATED = "permission.range_updated"
CONTENT_KEY_RANGE_REVOKED = "permission.range_revoked"
#: 「全非」通配时公司位显示的词条。它不是一条消息，是一个**展示词**——批准文案的
#: ``{company_name}`` 位必须有一个可读取值，而 ``*`` 不是给人看的。
CONTENT_KEY_ALL_COMPANIES = "permission.all_companies"

#: 多个公司 / 多个职能之间的分隔符。是标点不是措辞，因此留在代码里而不是内容目录里
#: （与 ``core/identity/roster_report.py`` 对分隔符的处理同一姿态）。
SCOPE_SEPARATOR = "、"

#: 通知发送的默认重试上限与退避间隔（秒）。有限、很短、且**不是**外部规范：通知失败
#: 不阻塞任何东西，重试只是为了盖住一次瞬时抖动。间隔可注入，因此用例里一秒都不用等。
DEFAULT_NOTICE_ATTEMPTS = 3
DEFAULT_NOTICE_BACKOFF_SECONDS: tuple[float, ...] = (0.2, 1.0)


class NoticeKind(Enum):
    """两种通知。互斥，由**权限文本自己**决定，不由调用方声明。"""

    RANGE_UPDATED = "range_updated"
    RANGE_REVOKED = "range_revoked"


@dataclass(frozen=True)
class PermissionNotice:
    """一条已渲染的权限变化通知。"""

    kind: NoticeKind
    content: RenderedContent

    @property
    def key(self) -> str:
        return self.content.key

    @property
    def version(self) -> str:
        return self.content.version

    @property
    def text(self) -> str:
        return self.content.text

    def audit_facts(self) -> dict[str, Any]:
        """可直接进审计的事实：**没有正文**。"""

        return {
            "kind": self.kind.value,
            "content_key": self.content.key,
            "content_version": self.content.version,
        }


def describe_scope(
    document: Mapping[str, Sequence[str]], *, catalog: ContentCatalog | None = None
) -> tuple[str, str]:
    """把一份权限文档说成「公司范围, 职能范围」两串展示文本。

    - 公司位：出现 :data:`~lingxi.core.permission.publish_row.ALL_COMPANIES_KEY` 时一律
      按"全部公司"渲染，**哪怕文档里还有别的键**——通配已经覆盖了它们，把两者并列会让
      用户看到一句自相矛盾的范围。其余情况按键排序后拼接。
    - 职能位：走 :func:`lingxi.core.permission.publish_row.lookup_metrics` 的
      ``company_id=None`` 分支。那一支问的是"这个人到底有没有任何可用项"，对全部键取并集
      是**正确**的（它不是范围判定，是存在性判定）；范围判定的回退制一个字都没被改动。

    值原样透传，不做任何大小写折叠或全半角转换——理由同 ``publish_row`` 的「零归一」
    一节：这些字符串将来会是逐字匹配的指标名。
    """

    source = catalog or default_content_catalog()
    if ALL_COMPANIES_KEY in document:
        companies = source.text(CONTENT_KEY_ALL_COMPANIES).text
    else:
        companies = SCOPE_SEPARATOR.join(sorted(document))
    functions = SCOPE_SEPARATOR.join(lookup_metrics(document))
    return companies, functions


def render_scope_notice(
    permissions: str, *, catalog: ContentCatalog | None = None
) -> PermissionNotice:
    """把**已经发布出去的**那一份权限文本渲染成一条通知。

    判据只有一个：这份文档里还查不查得到任何可用项。查不到就是撤权文案，查得到就是
    范围更新文案。**没有让调用方直接指定文案的入口**——那等于允许"权限已经空了却发一句
    你还有权限"，或者反过来。

    权限文本读不懂时抛 ``ValueError``（来自 :func:`~lingxi.core.permission.publish_row.
    parse_permissions`），不折成任何一种文案：读不懂是本侧缺陷，用一句确定的错话掩盖它
    比不发更糟。渲染出来的正文还要再过一次内容目录的用户可见性检查（内容目录在
    :meth:`~lingxi.config.content.ContentCatalog.text` 出口做），因此一个恰好长得像内部
    过程表达的公司名或职能名会**响亮失败**，而不是被发给用户。
    """

    source = catalog or default_content_catalog()
    document = parse_permissions(permissions)
    if not lookup_metrics(document):
        return PermissionNotice(
            kind=NoticeKind.RANGE_REVOKED,
            content=source.text(CONTENT_KEY_RANGE_REVOKED),
        )
    companies, functions = describe_scope(document, catalog=source)
    return PermissionNotice(
        kind=NoticeKind.RANGE_UPDATED,
        content=source.text(
            CONTENT_KEY_RANGE_UPDATED, company_name=companies, function_name=functions
        ),
    )


def notice_dedupe_key(user_id: str, permission_version: int) -> str:
    """一次逻辑通知的去重键：``(用户, 权限版本)``。

    重试**必须**带同一个值，由适配器折成飞书的投递 ``uuid``；下一版权限则得到另一个值，
    因此"权限又变了"不会被平台当成重复投递吞掉。
    """

    if not isinstance(user_id, str) or not user_id.strip():
        raise ValueError("通知去重键必须绑定用户")
    if isinstance(permission_version, bool) or not isinstance(permission_version, int):
        raise ValueError("通知去重键必须绑定整数权限版本")
    return f"{user_id.strip()}:{permission_version}"


class UserMessageSender(Protocol):
    """向用户本人发一条文本消息的可注入面。实现见
    ``adapters/feishu_user_message.py``。"""

    def send_text(self, *, open_id: str, text: str, dedupe_key: str) -> None: ...


class _AuditSink(Protocol):
    def record(self, action: str, /, **fields: object) -> None: ...


@dataclass(frozen=True)
class NoticeResult:
    """一次通知的结果。**不含正文**，可直接进审计与报告计数。"""

    delivered: bool
    kind: NoticeKind
    content_key: str
    content_version: str
    attempts: int
    error_code: str | None = None

    def audit_facts(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "content_key": self.content_key,
            "content_version": self.content_version,
            "attempts": self.attempts,
            "error_code": self.error_code,
        }


def _error_code(error: BaseException) -> str:
    """发送失败的分类串：**优先取适配器给的错误码**，否则退回异常类型名。

    两者都不含消息正文——适配器的 ``code`` 是码不是消息（凭据边界见
    ``adapters/feishu_user_message.py``），而异常正文可能带上 open_id 或响应体。
    """

    code = getattr(error, "code", None)
    if isinstance(code, str) and code.strip():
        return code.strip()[:200]
    return type(error).__name__


class PermissionNoticeDispatcher:
    """渲染 → 发送 → 有限重试 → 记账。只编排注入的接口，不做 I/O。

    形状与 ``core/permission/publish.PermissionPublishExecutor`` 一致（编排在 ``core``，
    真正的外部调用在注入进来的对象里）。``sleep`` 是**必填**注入点，纪律同
    ``core/permission/mcp_readiness.McpReadinessConfirmation``：默认一个真 ``sleep``
    会让用例真的等，默认一个空实现又会让"退避"在生产里静默消失。装配层传的是
    ``stop.wait``，因此 ``SIGTERM`` 能立刻打断退避。
    """

    name = "权限变化通知"

    def __init__(
        self,
        *,
        sender: UserMessageSender,
        audit: _AuditSink,
        sleep: Callable[[float], Any],
        catalog: ContentCatalog | None = None,
        max_attempts: int = DEFAULT_NOTICE_ATTEMPTS,
        backoff_seconds: Sequence[float] = DEFAULT_NOTICE_BACKOFF_SECONDS,
    ) -> None:
        if not callable(sleep):
            raise TypeError("sleep 必须可调用：缺省会让退避在生产里静默消失")
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 1:
            raise ValueError("通知重试上限必须是正整数")
        self._sender = sender
        self._audit = audit
        self._sleep = sleep
        self._catalog = catalog or default_content_catalog()
        self._max_attempts = max_attempts
        self._backoff = tuple(float(value) for value in backoff_seconds)

    @property
    def max_attempts(self) -> int:
        return self._max_attempts

    def notify(
        self, *, user_id: str, open_id: str, permission_version: int, permissions: str
    ) -> NoticeResult:
        """发一条权限变化通知；**不抛发送异常**，结果由返回值承载。

        渲染失败（权限文本读不懂、正文撞上用户可见性检查）**照常上抛**：那是本侧缺陷，
        吞掉它等于把一个可修的 bug 变成"这个人偶尔收不到通知"。发送失败则被吸收——
        产品负责人 2026-08-18 裁定 4：不阻塞权限生效。
        """

        notice = render_scope_notice(permissions, catalog=self._catalog)
        dedupe_key = notice_dedupe_key(user_id, permission_version)
        last_error: str | None = None
        for attempt_no in range(1, self._max_attempts + 1):
            if attempt_no > 1:
                # 退避序列比重试次数短时，最后一个间隔重复使用；序列为空则不等待。
                if self._backoff:
                    self._sleep(self._backoff[min(attempt_no - 2, len(self._backoff) - 1)])
            try:
                self._sender.send_text(
                    open_id=open_id, text=notice.text, dedupe_key=dedupe_key
                )
            except Exception as error:  # noqa: BLE001 - 通知失败不得阻塞权限生效
                last_error = _error_code(error)
                logger.warning(
                    "权限变化通知发送失败 user=%s version=%s 第%s次 error=%s",
                    user_id,
                    permission_version,
                    attempt_no,
                    last_error,
                )
                continue
            result = NoticeResult(
                delivered=True,
                kind=notice.kind,
                content_key=notice.key,
                content_version=notice.version,
                attempts=attempt_no,
            )
            self._audit.record(
                "permission_notice.sent",
                user=user_id,
                permission_version=permission_version,
                **result.audit_facts(),
            )
            return result

        result = NoticeResult(
            delivered=False,
            kind=notice.kind,
            content_key=notice.key,
            content_version=notice.version,
            attempts=self._max_attempts,
            error_code=last_error,
        )
        # 终失败只留审计与计数：不转运维、不生成待办（裁定 4）。计数由调用方的职责
        # 报告带出去，因此"今天有几个人没收到通知"是一个能被看见的数字。
        self._audit.record(
            "permission_notice.failed",
            user=user_id,
            permission_version=permission_version,
            **result.audit_facts(),
        )
        logger.error(
            "权限变化通知重试用尽，权限本身不受影响 user=%s version=%s attempts=%s error=%s",
            user_id,
            permission_version,
            self._max_attempts,
            last_error,
        )
        return result


__all__ = [
    "CONTENT_KEY_ALL_COMPANIES",
    "CONTENT_KEY_RANGE_REVOKED",
    "CONTENT_KEY_RANGE_UPDATED",
    "DEFAULT_NOTICE_ATTEMPTS",
    "DEFAULT_NOTICE_BACKOFF_SECONDS",
    "NoticeKind",
    "NoticeResult",
    "PermissionNotice",
    "PermissionNoticeDispatcher",
    "SCOPE_SEPARATOR",
    "UserMessageSender",
    "describe_scope",
    "notice_dedupe_key",
    "render_scope_notice",
]
