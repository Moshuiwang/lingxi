"""主动发送的编排：渲染一次、幂等落记录、失败告警并可重试（纯编排，无 I/O）。

四条不变量：**同一份渲染**——预检与正式发送走同一个 :func:`render_welcome_card`
调用点；**同名单重跑零新增**——记录表的 ``dedupe_key`` 唯一约束是幂等锚点，已经
``delivered`` 的人直接跳过、不再发一次；**重试不重复送达**——重试用同一个
``dedupe_key``，出站适配器据此折出同一个平台去重 ``uuid``；**失败不静默**——
落 ``failed`` 并沿既有告警接线报到管理群，记录仍可重试。

**认领到的记录换过收件人就不发**：同一个去重键上记着另一个 open_id，说明"这个键
对应哪个人"这件事已经变了，此时发出去等于把一张写着甲的卡片送给乙；这一条记原因
``recipient_changed``、一行都不改。

正文一个字都不进日志与记录：记录里只有收件人 open_id、内容键＋版本、样式、去重
键、时间、结果与错误码。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol

from lingxi.config.content import ContentCatalog
from lingxi.core.outreach.welcome_card import (
    DEFAULT_WELCOME_CARD_STYLE,
    WelcomeAudience,
    WelcomeCardStyle,
    render_welcome_card,
)

logger = logging.getLogger(__name__)

#: 告警通道名。带 ``_final`` 后缀是刻意的：``AlertingDuty.send_outcome_callback``
#: 据此把信号标成命中即报。主动发送是低频动作，"攒够阈值次数"在这里等于永远不报。
OUTREACH_ALERT_CHANNEL = "outreach_card_final"

#: 认领到的记录记着另一个收件人时的拒发原因。它不是一次投递失败，是"这个去重键
#: 已经属于别人"——记录一行不改，清单上按失败可见，人来决定名单是不是写错了。
REASON_RECIPIENT_CHANGED = "recipient_changed"

#: 记录的三个终态。``pending`` 是"已经落记录、还没确认送达"，重试从这里继续。
STATUS_PENDING = "pending"
STATUS_DELIVERED = "delivered"
STATUS_FAILED = "failed"


class OutreachRecordingError(RuntimeError):
    """卡片已经发出去了，但没能把它记成已送达。

    与"发送失败"必须分开：这一条已经在对方手里，把它报成失败会让人原样重跑，而重跑
    在飞书去重窗口之外会再送一张。``message_id`` 供人工按平台回读核对。
    """

    def __init__(self, *, record_id: str, message_id: str | None) -> None:
        """记下这一条的记录号与平台回读标识（两者都不含正文）。"""
        super().__init__("卡片已送达但记账失败")
        self.record_id = record_id
        self.message_id = message_id


class OutreachPurpose(str, Enum):
    """一次发送是正式送达还是预检。预检不算正式送达，但同样留记录。"""

    APPLY = "apply"
    PRECHECK = "precheck"


def outreach_dedupe_key(*, content_key: str, purpose: OutreachPurpose, subject: str) -> str:
    """一次逻辑发送的幂等键。

    ``subject`` 由装配层给：正式发送用 ``user_id``（同名单重跑落在同一个键上，
    因此零新增）；预检用「open_id + 本次运行号」（产品负责人要按样式反复预检，
    把预检也钉死成一次就等于把定稿路径堵上）。
    """
    if not content_key.strip() or not subject.strip():
        raise ValueError("幂等键必须同时绑定内容与收件人")
    return f"{content_key.strip()}:{purpose.value}:{subject.strip()}"


@dataclass(frozen=True)
class OutreachTarget:
    """一个收件人：发到哪个 open_id、他在库里是谁、他的取值输入。"""

    recipient_open_id: str
    subject: str
    audience: WelcomeAudience
    user_id: str | None = None


@dataclass(frozen=True)
class ReservedRecord:
    """记录表认领的结果：这一条是新排的，还是已经存在（可能已送达）。

    ``recipient_open_id`` 是**记录里记着的**收件人，不是本次要发给谁：两者不一致时
    这一次不发。留空表示实现没有给出这项事实（旧测试替身），按不校验处理。
    """

    record_id: str
    dedupe_key: str
    status: str
    attempts: int
    recipient_open_id: str = ""


@dataclass(frozen=True)
class OutreachOutcome:
    """一次发送的结局。**不含正文**，可直接进清单与审计。"""

    recipient_open_id: str
    purpose: OutreachPurpose
    status: str
    content_key: str
    content_version: str
    card_style: str
    skipped: bool = False
    message_id: str | None = None
    error_code: str | None = None
    content_digest: str = ""

    def audit_facts(self) -> dict[str, Any]:
        """可直接进审计的事实：没有正文，也没有姓名与邮箱。"""
        return {
            "purpose": self.purpose.value,
            "status": self.status,
            "content_key": self.content_key,
            "content_version": self.content_version,
            # 真正拿去渲染的那一份内容的摘要。它**不等于**版本号：宿主机外置覆盖
            # 文件不改版本号也不改键集合，只换正文，只有摘要能分辨这个人收到的是
            # 镜像内那一版还是覆盖之后那一版。没有注入摘要时退回版本号，因为那
            # 时确实没有第二份内容可分辨。
            "content_digest": self.content_digest or self.content_version,
            "card_style": self.card_style,
            "skipped": self.skipped,
            "error_code": self.error_code,
        }


class OutreachRecordStore(Protocol):
    """发送记录的读写口。真实实现见 ``adapters/postgres_outreach.py``。"""

    def reserve(
        self,
        *,
        recipient_open_id: str,
        user_id: str | None,
        purpose: str,
        dedupe_key: str,
        content_key: str,
        content_version: str,
        card_style: str,
    ) -> ReservedRecord:
        """认领或复用一条记录；已存在时返回它当前的状态与尝试次数。"""
        ...

    def mark_delivered(self, record_id: str, *, message_id: str | None) -> bool:
        """记成已送达（终态）；返回是否真的改到了那一行。"""
        ...

    def mark_failed(self, record_id: str, *, error: str) -> None:
        """记一次失败与错误码；这一条仍可重试。"""
        ...


class UserCardSender(Protocol):
    """向用户本人私聊发一张卡片的可注入面。实现见 ``adapters/feishu_user_card.py``。"""

    def send_card(self, *, open_id: str, card: Mapping[str, Any], dedupe_key: str) -> str | None:
        """发一张卡片，返回平台回读标识；失败时抛异常。"""
        ...


class _AuditSink(Protocol):
    def record(self, action: str, /, **fields: object) -> None: ...


def _error_code(error: BaseException) -> str:
    """失败的分类串：优先取适配器给的错误码，否则退回异常类型名。

    两者都不含正文——异常正文可能带上 open_id 或响应体（同
    ``core/permission/notification._error_code`` 的既定姿态）。
    """
    code = getattr(error, "code", None)
    if isinstance(code, str) and code.strip():
        return code.strip()[:200]
    return type(error).__name__


class OutreachDispatcher:
    """渲染 → 认领记录 → 发送 → 记账/告警。只编排注入的接口，不做 I/O。"""

    name = "主动发送"

    def __init__(
        self,
        *,
        sender: UserCardSender,
        store: OutreachRecordStore,
        audit: _AuditSink,
        send_outcome: Callable[[str, bool], Any] | None = None,
        catalog: ContentCatalog | None = None,
        style: WelcomeCardStyle = DEFAULT_WELCOME_CARD_STYLE,
        content_digest: str = "",
    ) -> None:
        """接线出站口、记录口、审计出口与告警回调。

        ``content_digest`` 是 ``catalog`` 那一份内容的摘要（镜像内 + 可能的宿主机
        覆盖文件），由装配方一次读齐后传进来；不给时审计退回内容版本号。
        """
        self._sender = sender
        self._store = store
        self._audit = audit
        self._send_outcome = send_outcome
        self._catalog = catalog
        self._style = style
        self._content_digest = content_digest

    def deliver(self, target: OutreachTarget, *, purpose: OutreachPurpose) -> OutreachOutcome:
        """把一张欢迎卡发给一个人；**不抛发送异常**，结果由返回值承载。

        渲染失败照常上抛：那是本侧缺陷，吞掉它等于让一个人静默收不到卡片；记账失败
        抛 :class:`OutreachRecordingError`——那张卡已经发出去了，调用方必须能把它与
        "没发出去"分开。
        """
        card = render_welcome_card(target.audience, style=self._style, catalog=self._catalog)
        dedupe_key = outreach_dedupe_key(
            content_key=card.content_key, purpose=purpose, subject=target.subject
        )
        record = self._store.reserve(
            recipient_open_id=target.recipient_open_id,
            user_id=target.user_id,
            purpose=purpose.value,
            dedupe_key=dedupe_key,
            content_key=card.content_key,
            content_version=card.content_version,
            card_style=card.style.value,
        )
        if record.status == STATUS_DELIVERED:
            return self._finish(target, purpose, card, STATUS_DELIVERED, skipped=True)
        if record.recipient_open_id and record.recipient_open_id != target.recipient_open_id:
            return self._finish(
                target, purpose, card, STATUS_FAILED, error_code=REASON_RECIPIENT_CHANGED
            )
        return self._send(target, purpose, card, record, dedupe_key)

    def _send(
        self,
        target: OutreachTarget,
        purpose: OutreachPurpose,
        card: Any,
        record: ReservedRecord,
        dedupe_key: str,
    ) -> OutreachOutcome:
        """真正发一次，并把结果落进记录与告警。"""
        try:
            message_id = self._sender.send_card(
                open_id=target.recipient_open_id, card=card.payload, dedupe_key=dedupe_key
            )
        except Exception as error:  # 主动发送失败不得静默
            code = _error_code(error)
            self._store.mark_failed(record.record_id, error=code)
            self._notify_send_outcome(succeeded=False)
            logger.error("主动发送失败 记录=%s error=%s", record.record_id, code)
            return self._finish(target, purpose, card, STATUS_FAILED, error_code=code)
        # 先报成功再记账：卡片已经在对方手里，告警状态机不该因为记账出问题而认为
        # 这一次投递失败。
        self._notify_send_outcome(succeeded=True)
        self._record_delivered(record, message_id)
        return self._finish(target, purpose, card, STATUS_DELIVERED, message_id=message_id)

    def _record_delivered(self, record: ReservedRecord, message_id: str | None) -> None:
        """把已经发出去的这一条记成已送达。

        写不进去时**不能**退化成"没发出去"：抛错上抛成 :class:`OutreachRecordingError`
        （调用方据此提示人工核对），无声无功（那一行已是终态或已被账号删除带走）只留
        一条审计——两者都不是投递失败。
        """
        try:
            recorded = self._store.mark_delivered(record.record_id, message_id=message_id)
        except Exception as error:
            logger.error(
                "主动发送已送达但记账失败 记录=%s error=%s",
                record.record_id,
                type(error).__name__,
            )
            raise OutreachRecordingError(
                record_id=record.record_id, message_id=message_id
            ) from error
        if recorded is False:
            self._audit.record(
                "outreach.delivery_not_recorded",
                record_id=record.record_id,
                message_id=message_id,
            )

    def _notify_send_outcome(self, *, succeeded: bool) -> None:
        """沿既有告警接线上报一次发送结果；回调本身出问题不改变发送结论。"""
        if self._send_outcome is None:
            return
        try:
            self._send_outcome(OUTREACH_ALERT_CHANNEL, succeeded)
        except Exception as error:  # 告警回调失败不能反过来打断发送收口
            logger.error("主动发送告警回调失败 error=%s", type(error).__name__)

    def _finish(
        self,
        target: OutreachTarget,
        purpose: OutreachPurpose,
        card: Any,
        status: str,
        *,
        skipped: bool = False,
        message_id: str | None = None,
        error_code: str | None = None,
    ) -> OutreachOutcome:
        """收口成一条不含正文的结局并记账。"""
        outcome = OutreachOutcome(
            recipient_open_id=target.recipient_open_id,
            purpose=purpose,
            status=status,
            content_key=card.content_key,
            content_version=card.content_version,
            card_style=card.style.value,
            skipped=skipped,
            message_id=message_id,
            error_code=error_code,
            content_digest=self._content_digest,
        )
        action = "outreach.skipped" if skipped else f"outreach.{status}"
        self._audit.record(action, **outcome.audit_facts())
        return outcome


__all__ = [
    "OUTREACH_ALERT_CHANNEL",
    "REASON_RECIPIENT_CHANGED",
    "STATUS_DELIVERED",
    "STATUS_FAILED",
    "STATUS_PENDING",
    "OutreachDispatcher",
    "OutreachOutcome",
    "OutreachPurpose",
    "OutreachRecordStore",
    "OutreachRecordingError",
    "OutreachTarget",
    "ReservedRecord",
    "UserCardSender",
    "outreach_dedupe_key",
]
