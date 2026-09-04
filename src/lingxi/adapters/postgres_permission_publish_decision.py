"""权限决定与它的发布意图的写入路径。

从 ``postgres_permission_publish.py`` 按体量棘轮纯移动拆出（写侧
``record_decision``/``enqueue_publish`` 与消费侧
``claim_next``/``complete``/就绪确认三块自然独立，这里只搬前者）。
``_DecisionMixin`` 与 ``PostgresPermissionPublishStore`` 组合进同一个类：
``self._dsn``/``self._timeouts`` 是宿主类的属性，``self.enqueue_publish``
被 ``_record_decision_locked`` 通过 ``self`` 调用——拆分只搬动方法的物理
位置，方法查找与调用顺序同拆分前逐位相同。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from lingxi.adapters.postgres import connect
from lingxi.adapters.postgres_conversation import _Transaction as _ConversationTransaction
from lingxi.core.ids import new_id
from lingxi.core.permission.publish import (
    ACCOUNT_STATE_ENABLED,
    PermissionDecisionTransientFailure,
    PermissionGrantBlockedByAccountState,
)
from lingxi.core.permission.publish_row import (
    CREATED_FIELD_NAMES,
    PUBLISHED_FIELD_NAMES,
    REVOKED_PERMISSIONS_TEXT,
    TOKEN_CIPHER_FIELD,
    PublishRow,
    content_digest,
    is_cipher_shaped,
    permissions_digest,
)

logger = logging.getLogger(__name__)

_UTC = UTC

_ENQUEUE_PUBLISH_SQL = """INSERT INTO publish_outbox
     (id, user_id, permission_version, reason, payload, content_expires_at,
      content_digest, permissions_digest)
   VALUES (%s, %s, %s, %s, %s, now(), %s, %s)"""


class DecisionOutcome(Enum):
    """一次权限决定对发布链路的影响。两态，不合并。"""

    # 权限内容与上一条有效意图不同（或此前没有意图）：推进版本并排出一条新的发布意图。
    ENQUEUED = "enqueued"
    # 权限内容与上一条**仍然有效**的意图逐字段相同：只刷新检查时间，不推进版本、
    # 不排新意图。没有这一分支，每天一轮的权限刷新会天天写一次外部表格。
    UNCHANGED = "unchanged"


@dataclass(frozen=True)
class PermissionDecision:
    """一次权限决定落库之后的结果。"""

    outcome: DecisionOutcome
    user_id: str
    permission_version: int
    outbox_id: str | None = None
    #: 本次决定顺带清掉的「已送达、随会话保留」投递正文事件数：只有
    #: ``record_decision(clear_delivered_content=True)`` 且真的推进了版本
    #: （``outcome`` 为 ``ENQUEUED``）时才可能非零；``UNCHANGED`` 恒为 0。
    #: 默认 0 而不是 ``None``：调用方把它直接写进审计计数字段，``None``
    #: 会让那条审计行需要多一次判空。
    cleared_events: int = 0

    @property
    def enqueued(self) -> bool:
        """``True`` 当且仅当这次决定推进了版本并排出了新的发布意图。"""
        return self.outcome is DecisionOutcome.ENQUEUED


def _permissions_changed(payload: Any, digest: Any, row: PublishRow) -> bool:
    """已送达正文清理触发的专属判据。

    这次决定的 ``permissions`` 文本是否与该用户**上一条意图**（不论其
    状态）不同。刻意不复用 ``_same_content``：那个函数比较**整行**（含
    ``email``/``name``），服务 ENQUEUED/UNCHANGED 判定；这里回答的是完全
    不同的问题——"实际可用权限变了吗"，只有这个答案能决定要不要清正文。
    优先用摘要列（活过内容擦除），``digest`` 为 ``NULL`` 时退回原始 payload
    比较（迁移 0085 之前就已擦除的历史行）。
    """
    if isinstance(digest, str) and digest:
        return digest != permissions_digest(row.content_fields)
    if not isinstance(payload, Mapping):
        return True
    return str(payload.get("permissions", "")) != row.permissions


class _DecisionMixin:
    def record_decision(
        self,
        *,
        user_id: str,
        row: PublishRow,
        reason: str,
        require_enabled_account: bool,
        decided_at: datetime | None = None,
        clear_delivered_content: bool = False,
    ) -> PermissionDecision:
        """把一次权限决定与它的发布意图写进**同一个事务**。

        交给 :meth:`_record_decision_locked` 执行；输入校验见 :meth:`_validate_decision_inputs`；``require_enabled_account``
        的账号有效性核对发生在已持有的行锁内，见 :meth:`_lock_target_user`
        文档。数据库瞬时故障转译为
        :class:`~lingxi.core.permission.publish.PermissionDecisionTransientFailure`
        （事务已整体回滚），不向上传播成裸 psycopg 异常。
        """
        self._validate_decision_inputs(row, require_enabled_account)
        moment = decided_at or datetime.now(_UTC)
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ValueError("权限决定时间必须带时区")

        from psycopg.errors import OperationalError

        try:
            return self._record_decision_locked(
                user_id=user_id,
                row=row,
                reason=reason,
                moment=moment,
                require_enabled_account=require_enabled_account,
                clear_delivered_content=clear_delivered_content,
            )
        except OperationalError as error:
            raise PermissionDecisionTransientFailure(type(error).__name__) from error

    @staticmethod
    def _validate_decision_inputs(row: PublishRow, require_enabled_account: bool) -> None:
        """``require_enabled_account`` 必填，且声明必须与内容自洽。

        ``True``代表这次授权要求账号有效；``False``代表只允许撤权
        （``row.permissions`` 必须是空对象），防止未来某个调用点传错
        ``False`` 却排非空授权——守卫因此不依赖调用方声明得对，只依赖它
        声明得**自洽**。必填而不给默认值：一个"默认不检查"的开关迟早被
        新调用点忘记声明。
        """
        if not isinstance(row, PublishRow):
            raise TypeError("发布行必须是 PublishRow：字段集与文本形态由它保证")
        if not isinstance(require_enabled_account, bool):
            raise TypeError("require_enabled_account 必须显式传 True/False")
        if not require_enabled_account and row.permissions != REVOKED_PERMISSIONS_TEXT:
            raise ValueError("声明不要求账号有效的权限决定只能排撤权行（permissions 必须为空对象）")

    def _record_decision_locked(
        self,
        *,
        user_id: str,
        row: PublishRow,
        reason: str,
        moment: datetime,
        require_enabled_account: bool,
        clear_delivered_content: bool,
    ) -> PermissionDecision:
        """:meth:`record_decision` 的事务体。

        锁定用户 → 比对上一条意图 → 未变则只刷新检查时间，否则推进版本、
        排新意图、按开关顺带清送达正文。``permissions_changed`` 是独立于 ENQUEUED/UNCHANGED 的更窄比较，见
        :func:`_permissions_changed`；只有它为真时才会触发第三步的清理。
        """
        with connect(self._dsn, timeouts=self._timeouts) as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    version, _account_state = self._lock_target_user(
                        cursor,
                        user_id=user_id,
                        require_enabled_account=require_enabled_account,
                        reason=reason,
                    )
                    latest = self._load_latest_intent(cursor, user_id=user_id)
                    permissions_changed = latest is None or _permissions_changed(
                        latest[1], latest[4], row
                    )
                    unchanged = self._maybe_record_unchanged(
                        cursor,
                        user_id=user_id,
                        version=version,
                        moment=moment,
                        latest=latest,
                        row=row,
                    )
                    if unchanged is not None:
                        return unchanged

                    version += 1
                    outbox_id = self._enqueue_new_version(
                        cursor,
                        connection,
                        user_id=user_id,
                        row=row,
                        reason=reason,
                        moment=moment,
                        version=version,
                    )
                cleared_events = self._apply_clear_delivered_content(
                    connection,
                    user_id=user_id,
                    clear_delivered_content=clear_delivered_content,
                    permissions_changed=permissions_changed,
                )
        logger.info("权限发布意图已排入 user=%s version=%s reason=%s", user_id, version, reason)
        return PermissionDecision(
            DecisionOutcome.ENQUEUED, user_id, version, outbox_id, cleared_events=cleared_events
        )

    def _lock_target_user(
        self, cursor: Any, *, user_id: str, require_enabled_account: bool, reason: str
    ) -> tuple[int, str]:
        """锁定目标用户行，读出当前版本与账号状态。

        ``require_enabled_account`` 时核对账号有效。这把 ``FOR UPDATE`` 锁与管理员「停用」写入（``_confirm_locked``）用的
        是**同一行的同一把锁**，两个写入者因此必然串行，消除了"读基线时还
        enabled、落决定时已停用"的竞态，不是缩小窗口。判据只看
        ``account_state``，不带 ``provisioning_state``：首次开通时它还是
        ``provisioning``，多带一列会让首次开通全部失败——失败方向必须是
        "少写"，不能是停服级别的误伤。
        """
        cursor.execute(
            "SELECT permission_version, account_state FROM app_user WHERE id = %s FOR UPDATE",
            (user_id,),
        )
        current = cursor.fetchone()
        if current is None:
            raise LookupError("权限决定的目标用户不存在")
        version = int(current[0])
        account_state = str(current[1])
        if require_enabled_account and account_state != ACCOUNT_STATE_ENABLED:
            logger.warning(
                "账号状态不允许排出非空授权，本次权限决定整体回滚 user=%s account_state=%s reason=%s",
                user_id,
                account_state,
                reason,
            )
            raise PermissionGrantBlockedByAccountState(account_state)
        return version, account_state

    @staticmethod
    def _load_latest_intent(cursor: Any, *, user_id: str) -> tuple | None:
        """取该用户最近一条发布意图（供内容比对，不含 ``updated_at``）。"""
        cursor.execute(
            """SELECT id, payload, status, content_digest, permissions_digest
                 FROM publish_outbox
                WHERE user_id = %s
                ORDER BY permission_version DESC
                LIMIT 1""",
            (user_id,),
        )
        return cursor.fetchone()

    @staticmethod
    def _maybe_record_unchanged(
        cursor: Any,
        *,
        user_id: str,
        version: int,
        moment: datetime,
        latest: tuple | None,
        row: PublishRow,
    ) -> PermissionDecision | None:
        """内容与上一条**仍然有效**的意图逐字段相同时判 UNCHANGED，只刷新检查时间。

        「仍然有效」＝``pending``/``publishing``/``published``；已经
        ``failed``/``superseded`` 时即使内容相同也照常排新意图——内容没变
        不等于已经发布成功，把它压成"无变化"会让一次失败的发布永远没人
        再试。返回 ``None`` 表示需要继续走推进版本、排新意图的路径。
        """
        if not (
            latest is not None
            and _same_content(latest[1], latest[3], row)
            and latest[2] in ("pending", "publishing", "published")
        ):
            return None
        cursor.execute(
            "UPDATE app_user SET permission_checked_at = %s, updated_at = now() WHERE id = %s",
            (moment, user_id),
        )
        logger.info("权限无变化，不排新的发布意图 user=%s version=%s", user_id, version)
        return PermissionDecision(DecisionOutcome.UNCHANGED, user_id, version, str(latest[0]))

    def _enqueue_new_version(
        self,
        cursor: Any,
        connection: Any,
        *,
        user_id: str,
        row: PublishRow,
        reason: str,
        moment: datetime,
        version: int,
    ) -> str:
        """推进 ``app_user.permission_version`` 并排一条新发布意图，返回意图 ID。

        快照取 ``row.snapshot_fields``：有令牌就连密文一起冻结，重试因此
        能原样重放"当初决定发布的那一版"，不必在重试那一刻回查一次令牌表。
        """
        cursor.execute(
            "UPDATE app_user SET permission_version = %s, permission_checked_at = %s, "
            "updated_at = now() WHERE id = %s",
            (version, moment, user_id),
        )
        return self.enqueue_publish(
            user_id=user_id,
            reason=reason,
            payload=row.snapshot_fields,
            permission_version=version,
            tx=connection,
        )

    @staticmethod
    def _apply_clear_delivered_content(
        connection: Any, *, user_id: str, clear_delivered_content: bool, permissions_changed: bool
    ) -> int:
        """``clear_delivered_content=True`` 且权限真的变了时清空已送达正文。

        是调用方显式传入的开关，不是本方法自行判断"这是权限变化所以该
        清"：本类同时服务首次开通（没有历史会话，锁 conversation 白费）
        与每日刷新的授权/撤权路径（真正的权限变化感知），默认关闭让前者
        结构上"不碰"。清理与 version 推进、发布意图入队同一个事务，失败
        一起回滚，不留半套状态。
        """
        if not (clear_delivered_content and permissions_changed):
            return 0
        cleared_events = _ConversationTransaction(connection).clear_delivered_content_for_user(
            user_id=user_id, reason="user_cleared"
        )
        _ConversationTransaction(connection).clear_user_memory(user_id=user_id)
        return cleared_events

    def enqueue_publish(
        self,
        *,
        user_id: str,
        reason: str,
        payload: Mapping[str, str],
        permission_version: int,
        tx: Any,
    ) -> str:
        """在**调用方的事务**里排一条发布意图，返回意图标识。

        ``tx`` 必填：本方法不建连接、不提交、不回滚，事务边界完全在调用方
        手里。``payload`` 键集必须恰好是更新集或新建集之一，校验见
        :meth:`_validate_publish_payload`；进快照的是密文，不是明文——
        令牌明文与主密钥一步都不进 outbox（`V-权限-11`）。
        """
        fields = self._validate_publish_payload(
            tx=tx,
            user_id=user_id,
            reason=reason,
            payload=payload,
            permission_version=permission_version,
        )
        outbox_id = new_id("pub")
        with tx.cursor() as cursor:
            cursor.execute(
                _ENQUEUE_PUBLISH_SQL,
                (
                    outbox_id,
                    user_id,
                    permission_version,
                    reason.strip(),
                    _jsonb(fields),
                    # 摘要与快照同一条语句写入，之后不随内容擦除消失（迁移
                    # 0085）：擦掉的是邮箱与姓名，留下的是"和另一份一不
                    # 一样"这一个不可反推的答案，见 `_same_content` 文档。
                    content_digest(fields),
                    permissions_digest(fields),
                ),
            )
        return outbox_id

    @staticmethod
    def _validate_publish_payload(
        *, tx: Any, user_id: str, reason: str, payload: Mapping[str, Any], permission_version: int
    ) -> dict[str, Any]:
        """校验 :meth:`enqueue_publish` 的输入，返回规范化后的字段字典。

        **键存在就校验形状**，不是"值非 None 才校验"：一份带
        ``token_cipher: None`` 的七键快照曾经能过——它既不是合法的六键更新
        快照，也不是可用的七键新建快照，还原后会表现得像"没有令牌"。
        """
        if tx is None or not hasattr(tx, "cursor"):
            raise TypeError("enqueue_publish 必须接收调用方的事务对象（同事务合同）")
        if not user_id:
            raise ValueError("发布意图必须绑定用户")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("发布意图必须写明原因")
        if isinstance(permission_version, bool) or not isinstance(permission_version, int):
            raise ValueError("权限版本必须是整数")
        if permission_version <= 0:
            raise ValueError("权限版本必须为正：0 表示还没有过任何权限决定")
        fields = dict(payload)
        if set(fields) not in (set(PUBLISHED_FIELD_NAMES), set(CREATED_FIELD_NAMES)):
            raise ValueError("发布内容快照的字段集必须与已登记的发布字段完全一致")
        if TOKEN_CIPHER_FIELD in fields and not is_cipher_shaped(fields[TOKEN_CIPHER_FIELD]):
            # 明文长得不像密文（token_urlsafe 是 URL 安全 base64，长度也不
            # 对齐），这道形状校验就是"明文不进 outbox"的最后一关。
            raise ValueError("发布内容快照的 token_cipher 形状不合法（不回显收到的值）")
        return fields


def _same_content(payload: Any, digest: Any, row: PublishRow) -> bool:
    """上一条意图的内容与这一次的决定是否逐字段相同（``updated_at`` 不参与）。

    **优先用摘要列，摘要列活过内容擦除**：``payload`` 过了九十天会被擦成
    ``'{}'``，此后按 payload 比较必然判成"变了"。``digest`` 为 ``NULL``
    的只有迁移 0085 之前就已经被擦除的历史行，退回原来的 payload 比较。
    payload 比较前先把两边都归一成文本：payload 从 JSONB 回来时数字会变成
    Python 数字，发布行永远是文本，按类型严格相等会让无变化的刷新判成变化。
    """
    if isinstance(digest, str) and digest:
        return digest == content_digest(row.content_fields)
    if not isinstance(payload, Mapping):
        return False
    expected = row.content_fields
    return all(str(payload.get(name, "")) == value for name, value in expected.items())


def _jsonb(value: Any) -> Any:
    """把内容快照交给 psycopg 的 JSONB 适配。延迟导入：没有驱动的机器仍能 import 本模块。"""
    from psycopg.types.json import Jsonb

    return Jsonb(value)
