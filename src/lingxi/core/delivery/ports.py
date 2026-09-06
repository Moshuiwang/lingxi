"""投递事件的数据形状与终态解析规则。

数据库设计[「问数结果投递事件与会话保留 Outbox」]
(../../../../docs/技术设计/数据库设计.md#问数结果投递事件与会话保留-outbox) 冻结的语义在
这里落成可被单测直接证伪的纯函数：终态分类只能来自这张有限表，投递是否成功不得
改写业务结果（`V-投递-04`），到期强制覆盖为 ``delivery_expired`` 是唯一的例外路径。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class DeliveryEventType(str, Enum):
    """``task_delivery_event.event_type`` 的取值，与迁移 0059 的 CHECK 一致。"""

    STARTED = "started"
    PROGRESS = "progress"
    SAFELY_RELEASABLE_ANSWER = "safely_releasable_answer"
    TERMINAL = "terminal"


#: 只有这几类事件允许携带正文；其余事件类型的 ``content`` 必须是 ``None``
#: （数据库层的 CHECK 再确认一次，这里的常量供调用方写入前自查）。``PROGRESS``
#: 携带的是语义化进度动作码（worker 内部生成的短令牌，不是用户输入或模型
#: 输出的自由文本），同时受 ``PROGRESS_CONTENT_MAX_LENGTH`` 长度契约约束；
#: 子步骤名只来自 ``card_stream.KNOWN_QUERY_STEPS`` 这份白名单。
CONTENT_BEARING_EVENT_TYPES = frozenset(
    {
        DeliveryEventType.PROGRESS,
        DeliveryEventType.SAFELY_RELEASABLE_ANSWER,
        DeliveryEventType.TERMINAL,
    }
)

#: ``progress`` 事件 ``content`` 的长度上限（数据库层的 CHECK 同步约束）。
#: 已知形状（`card_stream.encode_progress_action` 的输出）最长约 28 字节，
#: 32 留了充裕余量，不是精确贴着已知最长值算出来的。只约束 ``PROGRESS``——
#: ``SAFELY_RELEASABLE_ANSWER``/``TERMINAL`` 携带的是用户可见的问数结果
#: 正文，篇幅由业务内容决定，不适用这条上限。
PROGRESS_CONTENT_MAX_LENGTH = 32


def assert_content_allowed(event_type: DeliveryEventType, content: str | None) -> None:
    """写入前自查：``content`` 是否被允许出现在这个 ``event_type`` 上。

    与迁移 0059/0075 的 CHECK 约束表达同一条规则的两份独立校验之一——数据库层
    是最终防线（写入方即使跳过这个函数，数据库仍会用 ``CheckViolation`` 拒绝
    违规写入），这里让调用方在真正写库前就能拿到一个可读的 ``ValueError``，
    不必等 CheckViolation 从数据库连接弹回来才发现，也不会被调用方常见的
    "写库失败只记日志、不中断任务"这类宽泛 ``except Exception`` 悄悄吞掉却
    查不出具体是哪条规则触发。
    """
    if content is None:
        return
    if event_type not in CONTENT_BEARING_EVENT_TYPES:
        allowed = sorted(item.value for item in CONTENT_BEARING_EVENT_TYPES)
        raise ValueError(f"{event_type.value} 事件不允许携带 content（仅 {allowed} 可以）")
    if event_type is DeliveryEventType.PROGRESS and len(content) > PROGRESS_CONTENT_MAX_LENGTH:
        raise ValueError(
            f"progress 事件 content 长度 {len(content)} 超过契约上限 "
            f"{PROGRESS_CONTENT_MAX_LENGTH}（应为 querying:N/composing 类内部短令牌）"
        )


class TerminalKind(str, Enum):
    """``task_delivery_event.terminal_kind`` 的取值，与迁移 0059 的 CHECK 一致。

    只有 ``terminal`` 事件携带它；表达的是 Worker 认定的**业务**结论，与是否
    已经送达到用户会话（``platform_received``）是两个独立维度（`V-投递-04`）。
    """

    SUCCESS = "success"
    FAILED = "failed"
    STOPPED = "stopped"
    REDACTED_WITHHELD = "redacted_withheld"
    TIMEOUT = "timeout"


@dataclass(frozen=True)
class ResolvedOutcome:
    """任务在投递结论落定后应当收敛到的 ``task.status`` 与 ``task.error_kind``。"""

    status: str
    error_kind: str | None


#: 业务终态到默认 ``task.status``/``error_kind`` 的映射。``error_kind`` 这里给出
#: 的是缺省值——调用方（Worker）在写 ``terminal`` 事件时通常已经算出更具体的分类
#: （例如 ``session_failed``/``context_too_long``），会覆盖这个默认值；只有
#: ``stopped``/``redacted_withheld`` 这类分类本身就等于错误码的情形才依赖默认值。
_TERMINAL_TO_OUTCOME: dict[TerminalKind, ResolvedOutcome] = {
    TerminalKind.SUCCESS: ResolvedOutcome(status="succeeded", error_kind=None),
    TerminalKind.FAILED: ResolvedOutcome(status="failed", error_kind="session_failed"),
    TerminalKind.STOPPED: ResolvedOutcome(status="stopped", error_kind="stopped"),
    TerminalKind.REDACTED_WITHHELD: ResolvedOutcome(
        status="failed", error_kind="redacted_withheld"
    ),
    TerminalKind.TIMEOUT: ResolvedOutcome(status="failed", error_kind="running_timeout"),
}

#: 二十四小时到期仍未确认送达时的强制终态（数据库设计 :594、issue 状态合同第 8 条）。
#: 无论原始业务结论是什么，都不得把任务写成用户已取得结果——这是唯一允许覆盖
#: 业务结论的路径，因此单独命名为常量而不是走 ``resolve_delivered_outcome``。
DELIVERY_EXPIRED_OUTCOME = ResolvedOutcome(status="failed", error_kind="delivery_expired")


def resolve_delivered_outcome(*, terminal_kind: str, error_kind: str | None) -> ResolvedOutcome:
    """已确认 ``platform_received`` 后，任务应当收敛到的业务终态。

    ``error_kind`` 优先取调用方在写终态事件时记录的具体分类；未提供时退回该
    ``terminal_kind`` 的默认分类。业务结论完全来自写终态事件那一刻的记录，
    投递是否成功、多久之后才确认，都不改变这里算出的结果（`V-投递-04`）。
    """
    try:
        kind = TerminalKind(terminal_kind)
    except ValueError as error:
        raise ValueError(f"未知的投递终态分类：{terminal_kind!r}") from error
    default = _TERMINAL_TO_OUTCOME[kind]
    return ResolvedOutcome(status=default.status, error_kind=error_kind or default.error_kind)


class DeliveryOperation(str, Enum):
    """会产生"用户可见外发"的操作分档。

    分档的意义在于：**「成功」在不同操作上要看的东西不一样**。建卡拿到
    ``card_id`` 才算数，发消息拿到 ``message_id`` 才算数，而流式更新/关闭本身
    不回读任何新标识——把它们混成一条"响应成功就算成功"的通用规则，就会出现
    「服务端返回空响应也被记成已送达」（`V-投递-03`）。
    """

    CARD_CREATE = "card_create"
    CARD_REPLY = "card_reply"
    CARD_UPDATE = "card_update"
    CARD_CLOSE = "card_close"
    TEXT_SEND = "text_send"
    DOCUMENT_WRITE = "document_write"
    NOTICE_SEND = "notice_send"


class DeliveryVerdict(str, Enum):
    """一次外发响应的三种裁定，与消费侧的三条处置路径一一对应。

    ``ACCEPTED`` 才允许记为已送达；``REJECTED`` 才允许清预留位、重试或降级；
    ``UNCERTAIN`` 一律保留"不明"，不得记成功、也不得当成"确定没发生"。
    """

    ACCEPTED = "accepted"
    REJECTED = "rejected"
    UNCERTAIN = "uncertain"


#: 飞书业务错误码里表示"这次调用被完整受理"的取值。``"0"`` 与 ``0`` 都出现过
#: （不同接口的 JSON 里一个是数字一个是字符串），两种都收。
DELIVERY_SUCCESS_CODES: frozenset[object] = frozenset({0, "0"})

#: 「仍在发送」——服务端已经收下这次外发并且**还在处理**，既不是完成也不是拒绝。
#: 判成明确拒绝会让消费侧清预留位并改走另一条通道，而原来那一条随后很可能真的
#: 送达，用户于是收到两份同样的结果；判成成功则会把一条尚未落地的消息记成已送达。
#: 因此它只能落 ``UNCERTAIN``（`V-投递-03`）。
DELIVERY_IN_FLIGHT_CODES: frozenset[object] = frozenset({230049, "230049"})


@dataclass(frozen=True)
class DeliveryOperationSpec:
    """一个操作的「成功要看哪个码 + 哪个必要标识 + 不明时能否安全重投」。

    ``required_identifier`` 是**响应里必须回读到**的字段名；``None`` 表示这个
    接口本来就不回读新标识（流式更新与关闭寻址的是调用方已经持有的 ``card_id``），
    此时"码成功"就是全部证据。``platform_idempotency_key`` 记的是请求里带没带
    平台侧的去重/顺序键——它是**唯一**可以据以自动重投的依据：没有键的重投等于
    "赌一次没送达"，赌错就是重复交付。
    """

    operation: DeliveryOperation
    required_identifier: str | None
    platform_idempotency_key: str | None

    @property
    def retry_safe_when_in_flight(self) -> bool:
        """在途（``230049``）时能否自动重投而不会造成重复交付。"""
        return self.platform_idempotency_key is not None


#: 按操作分档的成功码 / 必要标识表。新增一个外发操作必须在这里登记一行，
#: 否则 :func:`classify_delivery_response` 会响亮失败，而不是套用某条通用规则。
DELIVERY_OPERATIONS: dict[DeliveryOperation, DeliveryOperationSpec] = {
    # CardKit 建卡：响应带 data.card_id 才能证明卡片真的建出来了。请求体没有
    # 任何去重键，重投会建出第二张卡。
    DeliveryOperation.CARD_CREATE: DeliveryOperationSpec(
        operation=DeliveryOperation.CARD_CREATE,
        required_identifier="card_id",
        platform_idempotency_key=None,
    ),
    # 把已建好的卡片作为回复消息发出：响应带 data.message_id 才算发出去了。
    # ``im/v1/messages/:id/reply`` 的请求体没有 uuid，重投会多一条消息。
    DeliveryOperation.CARD_REPLY: DeliveryOperationSpec(
        operation=DeliveryOperation.CARD_REPLY,
        required_identifier="message_id",
        platform_idempotency_key=None,
    ),
    # 流式增量更新：不回读新标识，寻址靠调用方已持有的 card_id；整卡级
    # ``sequence`` 是平台侧的顺序键，重投同一份正文不会产生第二条可见内容。
    DeliveryOperation.CARD_UPDATE: DeliveryOperationSpec(
        operation=DeliveryOperation.CARD_UPDATE,
        required_identifier=None,
        platform_idempotency_key="sequence",
    ),
    # 关闭流式：同上。
    DeliveryOperation.CARD_CLOSE: DeliveryOperationSpec(
        operation=DeliveryOperation.CARD_CLOSE,
        required_identifier=None,
        platform_idempotency_key="sequence",
    ),
    # 文本兜底发送：响应带 data.message_id 才算发出去了，**请求体没有 uuid**
    # ——这是全表唯一"既要拿标识、又完全没有幂等键"的操作，任何形式的自动重投
    # 都可能让用户收到两条一模一样的答案。
    DeliveryOperation.TEXT_SEND: DeliveryOperationSpec(
        operation=DeliveryOperation.TEXT_SEND,
        required_identifier="message_id",
        platform_idempotency_key=None,
    ),
    # 文档/表格写入：按接口各自的回读标识逐个校验（建文档要 document_id、建表
    # 要 spreadsheet_token……），这里只登记"码必须显式成功"这条共同前提。
    DeliveryOperation.DOCUMENT_WRITE: DeliveryOperationSpec(
        operation=DeliveryOperation.DOCUMENT_WRITE,
        required_identifier=None,
        platform_idempotency_key=None,
    ),
    # 权限变化通知 / 管理群日报：请求体带 ``uuid`` 去重键，同一 dedupe_key 重投
    # 不会产生第二条消息，因此在途时可以安全重投。
    DeliveryOperation.NOTICE_SEND: DeliveryOperationSpec(
        operation=DeliveryOperation.NOTICE_SEND,
        required_identifier="message_id",
        platform_idempotency_key="uuid",
    ),
}


@dataclass(frozen=True)
class DeliveryResponseOutcome:
    """一次外发响应的裁定结果。``reason`` 是可搜的短码，不含任何正文或凭据。"""

    verdict: DeliveryVerdict
    reason: str
    retry_safe: bool = False

    @property
    def accepted(self) -> bool:
        """是否可以据此记为"平台已受理"。"""
        return self.verdict is DeliveryVerdict.ACCEPTED


_IDENTIFIER_NOT_GIVEN = object()


def classify_delivery_response(
    *,
    operation: DeliveryOperation,
    code: object,
    identifier: object = _IDENTIFIER_NOT_GIVEN,
) -> DeliveryResponseOutcome:
    """按操作分档裁定一次外发响应，**fail-closed**。

    判定顺序是刻意的：``code`` 缺失（空响应 ``{}``、HTTP 5xx 带空体）先于一切
    ——它既不是成功也不是拒绝，真实成功响应一定带 ``code=0``。其次是「仍在发送」
    这个在途码。再判明确拒绝。最后才在码成功的前提下核这个操作的必要标识：
    码成功但缺标识仍然是"不明"，因为服务端可能已经把东西发出去了，我们只是拿
    不到回执（`V-投递-03`：不得记为已送达，也不得自动重发）。
    """
    spec = DELIVERY_OPERATIONS.get(operation)
    if spec is None:
        raise ValueError(f"未登记的投递操作分档：{operation!r}")
    if code is None:
        return DeliveryResponseOutcome(verdict=DeliveryVerdict.UNCERTAIN, reason="missing_code")
    if code in DELIVERY_IN_FLIGHT_CODES:
        return DeliveryResponseOutcome(
            verdict=DeliveryVerdict.UNCERTAIN,
            reason="in_flight",
            retry_safe=spec.retry_safe_when_in_flight,
        )
    if code not in DELIVERY_SUCCESS_CODES:
        return DeliveryResponseOutcome(verdict=DeliveryVerdict.REJECTED, reason="rejected")
    if spec.required_identifier is not None:
        if identifier is _IDENTIFIER_NOT_GIVEN:
            raise ValueError(f"{operation.value} 必须核对 {spec.required_identifier} 才能裁定")
        if not isinstance(identifier, str) or not identifier:
            return DeliveryResponseOutcome(
                verdict=DeliveryVerdict.UNCERTAIN,
                reason=f"missing_{spec.required_identifier}",
            )
    return DeliveryResponseOutcome(verdict=DeliveryVerdict.ACCEPTED, reason="accepted")
