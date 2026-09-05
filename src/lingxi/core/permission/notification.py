"""权限变化通知：正文渲染与「有限重试 + 审计」的发送编排（纯编排）。

什么时候通知、怎么发都不在这里。本模块只回答两件事：这一次变化对用户说什么
（正文由内容目录渲染，占位变量取自真的发布出去的那一份 ``permissions``），以及
发不出去怎么办（有限次重试，仍失败就记一条可计数的审计，不转运维、不阻塞权限
生效、不改变任何发布或就绪状态）。粒度是「范围式」：只说当前可用范围，不做
diff；文案不承诺即时生效（问数 MCP 每十五分钟拉一次发布表）。

占位变量取自发布出去的那份权限文本，不是另算一遍：这让通知说的范围与消费方
实际会读到的范围是同一份数据，也让"撤权通知"结构上成立——权限文档里查不到任何
指标时只可能渲染出撤权文案。已知短板（不掩盖）：公司位默认显示编号，可选注入
:class:`CompanyNameResolver` 后按中文名展示。职能位原样展示文档里的值，因此调用方
只许传已经过翻译层的权限文本。审计只记键、版本、次数、错误码，正文一个字都不进审计。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol

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
        """渲染所用的内容目录键。"""
        return self.content.key

    @property
    def version(self) -> str:
        """渲染所用的内容版本。"""
        return self.content.version

    @property
    def text(self) -> str:
        """渲染后的通知正文。"""
        return self.content.text

    def audit_facts(self) -> dict[str, Any]:
        """可直接进审计的事实：**没有正文**。"""
        return {
            "kind": self.kind.value,
            "content_key": self.content.key,
            "content_version": self.content.version,
        }


class CompanyNameResolver(Protocol):
    """公司编号 → 中文名解析口。

    真实实现读 ``galaxy_country.name_cn``（按当前有效银河批次），与
    ``core/admin`` 侧同一份查询姿势独立各自维护——两处调用面不同，不共享实现。
    """

    def name_for(self, *, company_id: str) -> str | None:
        """返回中文名；查无对应公司时返回 ``None``。

        不是空字符串，调用方据此原样展示编号，不把"查不到"误渲染成一个空白
        公司名。
        """
        ...

    def names_for(self, *, company_ids: Sequence[str]) -> Mapping[str, str | None]:
        """:meth:`name_for` 的批量变体。

        一次调用翻译整份权限文档涉及的全部公司编号，语义与逐个调用
        :meth:`name_for` 等价，但真实实现只需建立一次批量查询，不随编号数量
        线性增长连接数。空输入返回空映射。
        """
        ...


def describe_scope(
    document: Mapping[str, Sequence[str]],
    *,
    catalog: ContentCatalog | None = None,
    company_names: CompanyNameResolver | None = None,
) -> tuple[str, str]:
    """把一份权限文档说成「公司范围, 职能范围」两串展示文本。

    公司位：出现全非通配键时一律按"全部公司"渲染，哪怕文档里还有别的键——通配
    已经覆盖了它们，并列会让用户看到自相矛盾的范围。其余情况按键排序后经
    ``company_names``（非空时）翻译成「中文名（编号）」，查不到或未注入解析口
    时原样展示编号。职能位走 ``lookup_metrics`` 的 ``company_id=None`` 分支，
    对全部键取并集是存在性判定而非范围判定，回退制不受影响。值原样透传，不做
    大小写或全半角归一——这些字符串将来是逐字匹配的指标名。
    """
    source = catalog or default_content_catalog()
    if ALL_COMPANIES_KEY in document:
        companies = source.text(CONTENT_KEY_ALL_COMPANIES).text
    else:
        company_ids = sorted(document)
        # 请求级批量翻译：一次调用翻译整份文档涉及的全部公司编号，不再对每个
        # 编号各自触发一次 CompanyNameResolver.name_for——逐个调用会让公司位
        # 较多的权限文档重演连接风暴。
        name_by_id: Mapping[str, str | None] = {}
        if company_names is not None:
            try:
                name_by_id = company_names.names_for(company_ids=company_ids)
            except Exception:  # 展示层降级：解析口本身故障不阻塞通知发送
                logger.warning(
                    "permission_notice.company_name_lookup_failed_batch company_count=%d",
                    len(company_ids),
                )
        companies = SCOPE_SEPARATOR.join(
            _company_display(company_id, name_by_id.get(company_id)) for company_id in company_ids
        )
    functions = SCOPE_SEPARATOR.join(lookup_metrics(document))
    return companies, functions


def _company_display(company_id: str, name_cn: str | None) -> str:
    """把一个公司编号与它已经解析好的中文名拼成展示串。

    批量解析与异常处理已上移到 :func:`describe_scope` 一次性完成，这里只是
    纯字符串拼接。
    """
    return f"{name_cn}（{company_id}）" if name_cn else company_id


def render_scope_notice(
    permissions: str,
    *,
    catalog: ContentCatalog | None = None,
    company_names: CompanyNameResolver | None = None,
) -> PermissionNotice:
    """把已经发布出去的那一份权限文本渲染成一条通知。

    判据只有一个：这份文档里还查不查得到任何可用项。查不到是撤权文案，查得到是
    范围更新文案，没有让调用方直接指定文案的入口——那等于允许"权限已经空了却
    发一句你还有权限"。权限文本读不懂时抛 ``ValueError``，不折成任何一种文案：
    读不懂是本侧缺陷，用一句确定的错话掩盖它比不发更糟。渲染出来的正文还要
    再过一次内容目录的用户可见性检查，恰好长得像内部过程表达的公司名或职能名
    会响亮失败，不会被发给用户。``company_names`` 缺省时保持旧行为（公司位
    展示裸编号）。
    """
    source = catalog or default_content_catalog()
    document = parse_permissions(permissions)
    if not lookup_metrics(document):
        return PermissionNotice(
            kind=NoticeKind.RANGE_REVOKED,
            content=source.text(CONTENT_KEY_RANGE_REVOKED),
        )
    companies, functions = describe_scope(document, catalog=source, company_names=company_names)
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
    """向用户本人发一条文本消息的可注入面。

    实现见 ``adapters/feishu_user_message.py``。
    """

    def send_text(self, *, open_id: str, text: str, dedupe_key: str) -> None:
        """发送一条文本消息；失败时抛异常。"""
        ...


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
        """可直接进审计的事实：没有正文。"""
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
    ``core/permission/mcp_readiness_base.McpReadinessConfirmation``：默认一个真 ``sleep``
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
        company_names: CompanyNameResolver | None = None,
        max_attempts: int = DEFAULT_NOTICE_ATTEMPTS,
        backoff_seconds: Sequence[float] = DEFAULT_NOTICE_BACKOFF_SECONDS,
    ) -> None:
        """接线发送口、审计出口、退避时钟与可选的公司名解析口。"""
        if not callable(sleep):
            raise TypeError("sleep 必须可调用：缺省会让退避在生产里静默消失")
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 1:
            raise ValueError("通知重试上限必须是正整数")
        self._sender = sender
        self._audit = audit
        self._sleep = sleep
        self._catalog = catalog or default_content_catalog()
        # 缺省 ``None`` 时 render_scope_notice 保持旧行为（公司位展示裸编号）——
        # 未接线真实解析口的既有调用点/测试不需要改动。
        self._company_names = company_names
        self._max_attempts = max_attempts
        self._backoff = tuple(float(value) for value in backoff_seconds)

    @property
    def max_attempts(self) -> int:
        """构造时确定的重试次数上限。"""
        return self._max_attempts

    def _wait_before_retry(self, attempt_no: int) -> None:
        # 退避序列比重试次数短时，最后一个间隔重复使用；序列为空则不等待。
        if attempt_no > 1 and self._backoff:
            self._sleep(self._backoff[min(attempt_no - 2, len(self._backoff) - 1)])

    def _send_once(
        self,
        *,
        open_id: str,
        text: str,
        dedupe_key: str,
        user_id: str,
        permission_version: int,
        attempt_no: int,
    ) -> str | None:
        """尝试发送一次；成功返回 ``None``，失败返回错误分类并记一条警告日志。"""
        try:
            self._sender.send_text(open_id=open_id, text=text, dedupe_key=dedupe_key)
        except Exception as error:  # 通知失败不得阻塞权限生效
            error_code = _error_code(error)
            logger.warning(
                "权限变化通知发送失败 user=%s version=%s 第%s次 error=%s",
                user_id,
                permission_version,
                attempt_no,
                error_code,
            )
            return error_code
        return None

    def _record(
        self, *, action: str, user_id: str, permission_version: int, result: NoticeResult
    ) -> None:
        self._audit.record(
            action, user=user_id, permission_version=permission_version, **result.audit_facts()
        )

    def _deliver_with_retry(
        self,
        *,
        notice: PermissionNotice,
        open_id: str,
        dedupe_key: str,
        user_id: str,
        permission_version: int,
    ) -> tuple[int, str | None]:
        """跑重试循环；返回 ``(尝试次数, 最后一次错误分类或 None)``。

        ``None`` 表示这次尝试成功，``attempts`` 即成功那一次的序号；否则
        ``attempts`` 是用满的上限。
        """
        last_error: str | None = None
        for attempt_no in range(1, self._max_attempts + 1):
            self._wait_before_retry(attempt_no)
            last_error = self._send_once(
                open_id=open_id,
                text=notice.text,
                dedupe_key=dedupe_key,
                user_id=user_id,
                permission_version=permission_version,
                attempt_no=attempt_no,
            )
            if last_error is None:
                return attempt_no, None
        return self._max_attempts, last_error

    def notify(
        self, *, user_id: str, open_id: str, permission_version: int, permissions: str
    ) -> NoticeResult:
        """发一条权限变化通知；**不抛发送异常**，结果由返回值承载。

        渲染失败（权限文本读不懂、正文撞上用户可见性检查）照常上抛：那是本侧缺陷，
        吞掉它等于把一个可修的 bug 变成"这个人偶尔收不到通知"。发送失败则被吸收，
        不阻塞权限生效；终失败只留审计与计数，不转运维、不生成待办。
        """
        notice = render_scope_notice(
            permissions, catalog=self._catalog, company_names=self._company_names
        )
        dedupe_key = notice_dedupe_key(user_id, permission_version)
        attempts, error = self._deliver_with_retry(
            notice=notice,
            open_id=open_id,
            dedupe_key=dedupe_key,
            user_id=user_id,
            permission_version=permission_version,
        )
        delivered = error is None
        result = NoticeResult(
            delivered=delivered,
            kind=notice.kind,
            content_key=notice.key,
            content_version=notice.version,
            attempts=attempts,
            error_code=error,
        )
        self._record(
            action="permission_notice.sent" if delivered else "permission_notice.failed",
            user_id=user_id,
            permission_version=permission_version,
            result=result,
        )
        if not delivered:
            logger.error(
                "权限变化通知重试用尽，权限本身不受影响 user=%s version=%s attempts=%s error=%s",
                user_id,
                permission_version,
                self._max_attempts,
                error,
            )
        return result


__all__ = [
    "CONTENT_KEY_ALL_COMPANIES",
    "CONTENT_KEY_RANGE_REVOKED",
    "CONTENT_KEY_RANGE_UPDATED",
    "CompanyNameResolver",
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
