"""专用授权凭据的宿主机文件保管，与数据库侧的主体登记。

``refresh_token`` 只以 Fernet 密文保存在**宿主机受控文件**，不进业务数据库；
解密密钥来自受控环境变量，与文件分开存放。数据库只保留
``feishu_delegated_subject`` 登记表（主体 ``open_id`` 与配置状态）。

一次性令牌语义：``claim_due`` 原子置位消费标记，置位后旧密文对任何读取都
不可见；``save`` 写入新凭据并清空消费标记；``refresh_consumed_at``/
``refresh_consumed_count`` 随新凭据落盘，是频率上界**唯一**的权威判据；
``revoke`` 删除文件但保留登记行。**解密失败绝不触发任何删除**：所有读取
路径先判「解不解得开」，再判「主体对不对/是否有效」，见
:class:`_Undecryptable`；密文落盘、明文只在进程内、绝不降级为明文。
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import tempfile
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from lingxi.adapters.delegated_subject_lookup import (
    DELEGATED_PURPOSE,
    registered_delegated_subject_open_id,
)
from lingxi.adapters.postgres import DEFAULT_POSTGRES_TIMEOUTS, PostgresTimeouts, connect
from lingxi.core.identity.credentials import (
    AuthorizationGrant,
    RefreshDailyLimitReached,
    RefreshMinIntervalNotElapsed,
    SecretToken,
    expiry_moment,
    rotation_deadline,
)
from lingxi.core.identity.identifiers import redact_identifier

logger = logging.getLogger(__name__)

# `DELEGATED_PURPOSE`/`registered_delegated_subject_open_id` 的定义在
# `adapters/delegated_subject_lookup.py`；这里保留 import 重新导出，既有
# 调用方继续用本模块路径不改一行。gateway 改从新模块直接 import，避免把
# 本文件独有的 Fernet 依赖一并背上（`gateway` extras 组不含 cryptography）。

# 领取后其他进程再领取的观感与数据库版一致：消费标记本身就是唯一的门。
# 保留该常量只为兼容既有调用方签名。
DEFAULT_LEASE_SECONDS = 300

#: 「按需供给」两次消费之间的最小间隔。防崩溃循环：进程反复重启时每次启动
#: 都会换一次、作废旧 ``refresh_token``；撞上"换成功但落盘失败"的窗口会
#: 永久丢失凭据。正常运行令牌寿命约 2 小时才换一次，远超这个间隔。
DEFAULT_SUPPLY_MIN_INTERVAL = timedelta(minutes=5)

#: 「按需供给」每 UTC 日至多消费次数。它是**哨兵**而不是配额：最小间隔已经
#: 把崩溃循环压到至多 12 次/小时，正常一天约 12 次远远碰不到 100；真撞上
#: 说明系统已经异常了数小时，此时停下留痕、要求人工介入，好过继续静默轮换。
DEFAULT_SUPPLY_DAILY_LIMIT = 100

# `registered_delegated_subject_open_id` 的实现在
# `adapters/delegated_subject_lookup.py`；上面的 import 已重新导出到本模块
# 命名空间，`HostFileDelegatedCredentialVault` 的 SQL 继续用同一个常量。


class _Undecryptable:
    """「文件在，但当前主密钥解不开」的哨兵。

    与「解开了、内容是空的」（``{}``）必须区分：把解密失败读成 ``{}`` 会让
    后续的主体核对/有效性判定误判成「不一致」或「无效」而**删文件**——只要
    主密钥配错，一次性 ``refresh_token`` 就会被永久删除，之后把密钥改回来
    也救不回来，只能重新走 OAuth Bridge 授权。因此判定顺序固定为**先判
    解密、再判主体**：解密不成功时唯一安全的处置是原样留着、响亮报错、
    等人工核对密钥——删是不可逆的，留是可逆的。
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - 只为日志与断言可读
        return "<凭据文件无法解密>"


UNDECRYPTABLE = _Undecryptable()

_UNDECRYPTABLE_MESSAGE = (
    "凭据文件存在但当前主密钥解不开（密钥配置不一致或文件损坏）：已保留文件不做删除，"
    "请核对 LINGXI_DELEGATED_CREDENTIAL_KEY 后重试；确认文件损坏才由人工删除"
)


@dataclass(frozen=True)
class StoredCredential:
    """一条已解密领取/读取的专用授权凭据；字段含义见各成员注释。"""

    subject_open_id: str
    grant: AuthorizationGrant
    refresh_at: datetime
    expires_at: datetime
    # 文件世代号：每次 save 生成新值。轮换收尾必须携带领取时的世代号，世代
    # 不符说明期间发生了新授权——旧链的结果一律放弃，不得覆盖或删除新凭据。
    generation: str = ""
    # 领取那一刻的权威消费时刻，由 ``claim_due`` 在文件锁内生成；只有领取
    # 回来的凭据带它，``load`` 读到的是未消费的形态，因此为 ``None``。
    consumed_at: datetime | None = None
    # 「按需供给」当日消费计数——领取成功之后应当持久化的那个新值。只有
    # ``claim_due`` 的返回值带它，调用方必须原样传给 ``save(...)``：``save``
    # 每次都重建整份 payload，不传就会把当日计数悄悄清零。
    refresh_consumed_count: int | None = None


class HostFileDelegatedCredentialVault:
    """凭据文件 + 主体登记的组合保管者。API 与数据库版前身完全一致。"""

    def __init__(
        self,
        dsn: str,
        encryption_key: str,
        credential_path: str,
        *,
        timeouts: PostgresTimeouts = DEFAULT_POSTGRES_TIMEOUTS,
    ) -> None:
        """校验加密密钥与凭据文件路径；缺少加密依赖时构造期即失败。"""
        try:
            from cryptography.fernet import Fernet, InvalidToken
        except ImportError as error:  # 绝不降级为明文保存。
            raise RuntimeError("缺少专用授权凭据的加密依赖") from error
        try:
            self._cipher = Fernet(encryption_key.encode())
        except Exception as error:
            raise ValueError("专用授权凭据的加密密钥必须是有效的 Fernet 密钥") from error
        if not credential_path or not str(credential_path).strip():
            raise ValueError("必须提供凭据文件路径（宿主机受控目录）")
        self._invalid_token = InvalidToken
        self._dsn = dsn
        self._timeouts = timeouts
        self._path = Path(credential_path)
        # 锁文件与凭据文件分开：凭据文件靠原子替换更新，锁对象必须稳定存在。
        self._lock_path = self._path.with_name(self._path.name + ".lock")

    # ---- 写入 -------------------------------------------------------------

    def save(
        self,
        *,
        subject_open_id: str,
        grant: AuthorizationGrant,
        issued_at: datetime | None = None,
        replacing_generation: str | None = None,
        expected_registered_subject_open_id: str | None = None,
        require_absent_registration: bool = False,
        refresh_consumed_at: datetime | None = None,
        refresh_consumed_count: int | None = None,
    ) -> bool:
        """登记主体并写入（或轮换）唯一一条专用授权凭据。

        两种登记 CAS 不能同时传：``expected_registered_subject_open_id``（回调
        与轮换收尾用）与 ``require_absent_registration``（首次建立用的反向
        CAS）是同一件事的两个方向，都在保存事务内让数据库判定，不是先读后写。
        ``refresh_consumed_at``/``refresh_consumed_count`` 必须原样回传
        ``claim_due`` 返回的同名字段——本方法每次都重建整份 payload，不传就
        等于清零频率上界的判据；新授权与首次建立不带这两个值，留空让新凭据
        的判据从零开始。
        """
        _validate_save_arguments(
            require_absent_registration=require_absent_registration,
            expected_registered_subject_open_id=expected_registered_subject_open_id,
            refresh_consumed_at=refresh_consumed_at,
            refresh_consumed_count=refresh_consumed_count,
        )
        moment = issued_at or datetime.now(UTC)

        with self._locked():
            if replacing_generation is not None and not self._generation_still_current(
                replacing_generation
            ):
                return False
            with (
                connect(self._dsn, timeouts=self._timeouts) as connection,
                connection.cursor() as cursor,
            ):
                if not self._register_subject_for_save(
                    cursor,
                    subject_open_id,
                    require_absent_registration=require_absent_registration,
                    expected_registered_subject_open_id=expected_registered_subject_open_id,
                ):
                    return False
                payload = _credential_payload(
                    subject_open_id,
                    grant,
                    moment,
                    refresh_consumed_at=refresh_consumed_at,
                    refresh_consumed_count=refresh_consumed_count,
                )
                self._write_encrypted(payload)
        logger.info(
            "专用授权凭据已加密写入宿主机文件 subject=%s", redact_identifier(subject_open_id)
        )
        return True

    def _generation_still_current(self, replacing_generation: str) -> bool:
        """核对磁盘上的世代号仍等于领取时的世代；解不开或已换代都放弃写回。

        解不开就核对不了代际，覆盖等于蒙着眼睛写，与「代际不符」同样放弃
        写回但不删文件；领取之后有过新授权时，旧轮换链的结果作废，
        绝不覆盖新凭据。
        """
        current = self._read_payload()
        if current is UNDECRYPTABLE:
            logger.error(_UNDECRYPTABLE_MESSAGE)
            return False
        if (current or {}).get("generation") != replacing_generation:
            logger.warning("轮换结果已过期（期间发生新授权），放弃写回")
            return False
        return True

    def _register_subject_for_save(
        self,
        cursor: Any,
        subject_open_id: str,
        *,
        require_absent_registration: bool,
        expected_registered_subject_open_id: str | None,
    ) -> bool:
        """按调用方选择的 CAS 条件登记主体；条件不满足返回 ``False``、放弃本次保存。"""
        if require_absent_registration:
            # 首次建立的 CAS：只有登记表仍为空这一插入才会命中，返回零行
            # 说明期间已经有主体登记，本次一律放弃——不覆盖、不更新。
            cursor.execute(
                """INSERT INTO feishu_delegated_subject (purpose, subject_open_id)
                   VALUES (%s, %s)
                   ON CONFLICT (purpose) DO NOTHING
                RETURNING subject_open_id""",
                (DELEGATED_PURPOSE, subject_open_id),
            )
            if cursor.fetchone() is None:
                logger.warning("首次建立时主体登记已存在，放弃写入")
                return False
        elif expected_registered_subject_open_id is not None:
            # 条件 UPDATE 是保存事务内的 CAS。它会锁住匹配的登记行；
            # 并发的主体变更若先提交，这里返回零行，绝不执行后续写入。
            cursor.execute(
                """UPDATE feishu_delegated_subject
                      SET updated_at = updated_at
                    WHERE purpose = %s AND subject_open_id = %s
                RETURNING subject_open_id""",
                (DELEGATED_PURPOSE, expected_registered_subject_open_id),
            )
            if cursor.fetchone() is None:
                logger.warning("保存前主体登记 CAS 失败，放弃写入")
                return False
        else:
            cursor.execute(
                """INSERT INTO feishu_delegated_subject (purpose, subject_open_id)
                   VALUES (%s, %s)
                   ON CONFLICT (purpose) DO UPDATE SET
                     subject_open_id = EXCLUDED.subject_open_id,
                     updated_at = now()""",
                (DELEGATED_PURPOSE, subject_open_id),
            )
        return True

    def revoke(self, *, reason: str, generation: str | None = None) -> bool:
        """删除凭据文件；数据库登记行**保留**（V-身份-02 的数据源不随撤销消失）。

        携带 ``generation`` 时只撤销**那一代**：领取之后若发生了新授权，
        旧链的失败不得把新凭据一起删掉。
        """
        with self._locked():
            existed = self._path.exists()
            if existed and generation is not None:
                current = self._read_payload()
                if current is UNDECRYPTABLE:
                    # 定向撤销要求「确认是这一代才删」；解不开就确认不了，不删（C-2）。
                    logger.error(_UNDECRYPTABLE_MESSAGE)
                    return False
                if (current or {}).get("generation") != generation:
                    logger.warning("撤销目标已被新授权取代，跳过")
                    return False
            if existed:
                self._path.unlink()
        if existed:
            logger.warning("专用授权凭据已撤销 reason=%s", reason)
        return existed

    def revoke_stale_consumed(
        self, *, max_age_seconds: int = 600, now: datetime | None = None
    ) -> bool:
        """收殓「已消费但一直没写回新凭据」的文件。

        这种文件意味着进程在续期后、落盘前死掉：旧令牌已被飞书作废，留着密文
        只会诱使未来的代码路径重放它。
        """
        moment = now or datetime.now(UTC)
        with self._locked():
            payload = self._read_payload()
            if payload is None:
                return False
            if payload is UNDECRYPTABLE:
                # 读不出 consumed_at 就判不出「消费后未落盘」，收殓无依据（C-2）。
                logger.error(_UNDECRYPTABLE_MESSAGE)
                return False
            consumed_at = _parse_moment(payload.get("consumed_at"))
            if consumed_at is None or consumed_at >= moment - timedelta(seconds=max_age_seconds):
                return False
            self._path.unlink(missing_ok=True)
        logger.error("不可恢复：发现续期后未落盘的消费中凭据，已清除，需人工重新授权")
        return True

    # ---- 读取 -------------------------------------------------------------

    def registered_subject_open_id(self) -> str | None:
        """读取正式登记的专用授权主体，不读取凭据文件。

        重授权入口用这条登记绑定回调身份；回调本身的身份只接受飞书
        ``user_info`` 回读，不能由浏览器参数提供。撤销凭据时登记行保留，
        因此失效后的恢复仍然有明确的比较对象。
        """
        return registered_delegated_subject_open_id(self._dsn, timeouts=self._timeouts)

    def load(self, *, now: datetime | None = None) -> StoredCredential | None:
        """取出当前凭据供同步使用。解密失败或已失效时撤销并返回 ``None``。"""
        moment = now or datetime.now(UTC)
        with self._locked():
            payload = self._read_payload()
        if payload is None:
            return None
        if payload is UNDECRYPTABLE:
            # **先判解密、再判主体**（C-2）：顺序反过来时，一次密钥配错会被读成
            # 「文件主体与登记不一致」并删掉密文。这里既不删文件也不连库。
            logger.error(_UNDECRYPTABLE_MESSAGE)
            return None
        if not self._subject_matches_registry(payload):
            return None
        credential = self._to_credential(payload)
        if credential is None:
            self.revoke(reason="credential_undecryptable")
            return None
        if payload.get("consumed_at"):
            # 消费中：旧令牌可能已被飞书作废，任何读取路径都不得再拿到它。
            return None
        if credential.expires_at <= moment:
            self.revoke(reason="credential_expired")
            return None
        return credential

    def claim_due(
        self,
        *,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        now: datetime | None = None,
        for_supply: bool = False,
        min_interval: timedelta = DEFAULT_SUPPLY_MIN_INTERVAL,
        daily_limit: int = DEFAULT_SUPPLY_DAILY_LIMIT,
    ) -> StoredCredential | None:
        """领取凭据并**原子置位消费标记**；消费标记置位后旧令牌不会被再次领取。

        ``for_supply=True`` 是按需续期入口：放开到期判定的同时施加两道频率
        上界——最小间隔与当日次数，两者捆在一起、不接受能让检查整体消失的
        哨兵值，判据随凭据落盘（不留进程内副本），在锁内、用锁内当前时刻、
        且在置位消费标记**之前**判定。领取成功时返回的凭据带上锁内生成的
        ``consumed_at`` 与领取后的 ``refresh_consumed_count``；轮换收尾须把
        两者原样写回新凭据，两道上界才能始终由同一份账本说了算。到期驱动
        （``for_supply=False``）不检查上界，但同样要把当日计数原样带回去。
        """
        del lease_seconds  # 消费标记取代了租期语义；参数保留以兼容调用方。
        _validate_claim_due_arguments(min_interval, daily_limit)
        with self._locked():
            # 时刻在锁内取：等锁可能跨越 UTC 午夜，锁外算好的日期会判错一整天。
            moment = now or datetime.now(UTC)
            payload = self._read_payload()
            if payload is None:
                return None
            if payload is UNDECRYPTABLE:
                # 同 `load`：解不开时不删、不连库、不领取（C-2）。
                logger.error(_UNDECRYPTABLE_MESSAGE)
                return None
            if payload.get("consumed_at") or not self._subject_matches_registry(payload):
                return None
            credential = self._claimable_credential(payload, moment)
            if credential is None:
                return None

            last_consumed_at = _parse_utc(payload.get("refresh_consumed_at"))
            count_today = _count_today(payload, last_consumed_at, moment)
            if for_supply:
                new_supply_count = _check_supply_rate_limits(
                    last_consumed_at, count_today, moment, min_interval, daily_limit
                )
            else:
                if credential.refresh_at > moment:
                    return None
                # 不做频率判据，但把当日计数原样带过去（见上方 docstring）；
                # `refresh_consumed_count`（当日次数上界）只统计 `for_supply=True`
                # 那一类消费，到期驱动的轮换由自己的到期节奏天然限速，不占用
                # 同一个计数器的配额。`refresh_consumed_at`（最小间隔判据）则
                # 对两类消费一视同仁，两者口径刻意不对称。
                new_supply_count = count_today

            payload["consumed_at"] = moment.isoformat()
            self._write_encrypted(payload)
        return replace(credential, consumed_at=moment, refresh_consumed_count=new_supply_count)

    def _claimable_credential(
        self, payload: dict[str, Any], moment: datetime
    ) -> StoredCredential | None:
        """把 payload 转成可领取的凭据；已失效或解不出来时撤销并返回 ``None``。"""
        credential = self._to_credential(payload)
        if credential is None:
            self._path.unlink(missing_ok=True)
            logger.warning("专用授权凭据已撤销 reason=credential_undecryptable")
            return None
        if credential.expires_at <= moment:
            self._path.unlink(missing_ok=True)
            logger.warning("专用授权凭据已撤销 reason=credential_expired")
            return None
        return credential

    # ---- 内部 -------------------------------------------------------------

    def _locked(self):
        return _FileLock(self._lock_path)

    def _subject_matches_registry(self, payload: dict[str, Any]) -> bool:
        """凭据文件主体必须与数据库登记一致，否则失败关闭并清除文件。

        主体 A→B 更换途中崩溃会留下「登记指向 B、文件仍是 A」：此时 A 已不在
        登记表里，双向触发器只护着 B，继续用 A 的凭据等于在防线外运行。
        清除旧文件并要求重新授权是唯一安全的恢复。
        """
        with (
            connect(self._dsn, timeouts=self._timeouts) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "SELECT subject_open_id FROM feishu_delegated_subject WHERE purpose = %s",
                (DELEGATED_PURPOSE,),
            )
            row = cursor.fetchone()
        registered = None if row is None else str(row[0])
        if registered == payload.get("subject_open_id"):
            return True
        logger.error("不可恢复：凭据文件主体与数据库登记不一致，已清除文件，需人工重新授权")
        self._path.unlink(missing_ok=True)
        return False

    def _read_payload(self) -> dict[str, Any] | _Undecryptable | None:
        """``None`` = 没有文件；:data:`UNDECRYPTABLE` = 有文件但解不开；否则是明文 payload。

        这三种情况必须由调用方**分别**处置（C-2）：只有第三种才谈得上主体核对与
        有效期判定，前两种都不构成删除凭据的依据。返回 ``{}`` 会把「解不开」伪装成
        「解开了、但什么都没有」，让下游的失败关闭分支删掉一份其实完好的密文。
        """
        try:
            blob = self._path.read_bytes()
        except FileNotFoundError:
            return None
        if not blob:
            return None
        try:
            return json.loads(self._cipher.decrypt(blob))
        except (self._invalid_token, ValueError):
            # 解密失败**不**降级成空 payload：区分不出「密钥配错」与「凭据无效」的
            # 那一刻，任何自动清理都可能销毁一份完好的一次性令牌。
            return UNDECRYPTABLE

    def _write_encrypted(self, payload: dict[str, Any]) -> None:
        blob = self._cipher.encrypt(json.dumps(payload, ensure_ascii=False).encode())
        self._path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temp_name = tempfile.mkstemp(
            dir=str(self._path.parent), prefix=self._path.name + "."
        )
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(blob)
            os.replace(temp_name, self._path)
        except BaseException:
            os.unlink(temp_name)
            raise

    def _to_credential(self, payload: dict[str, Any]) -> StoredCredential | None:
        token = payload.get("refresh_token")
        subject = payload.get("subject_open_id")
        refresh_at = _parse_moment(payload.get("refresh_at"))
        expires_at = _parse_moment(payload.get("expires_at"))
        if (
            not isinstance(token, str)
            or not token
            or not isinstance(subject, str)
            or refresh_at is None
            or expires_at is None
        ):
            return None
        remaining = max(int((expires_at - datetime.now(UTC)).total_seconds()), 1)
        return StoredCredential(
            subject_open_id=subject,
            grant=AuthorizationGrant(
                SecretToken(token), remaining, str(payload.get("scope") or "")
            ),
            refresh_at=refresh_at,
            expires_at=expires_at,
            generation=str(payload.get("generation") or ""),
        )


def _validate_claim_due_arguments(min_interval: timedelta, daily_limit: int) -> None:
    """校验 :meth:`HostFileDelegatedCredentialVault.claim_due` 的频率上界参数。

    ``min_interval`` 用 ``<=`` 而不是 ``<`` 拒绝：0 是能让这道检查整体消失的
    哨兵值——两次消费之间"至少隔 0"对任何时刻都成立。需要"几乎没有间隔"的
    语义时传一个极小的正值，不是 0。
    """
    if not isinstance(min_interval, timedelta) or min_interval <= timedelta(0):
        raise ValueError("最小消费间隔必须是正的时间长度（0 会让这道上界整体失效）")
    if not isinstance(daily_limit, int) or isinstance(daily_limit, bool) or daily_limit < 1:
        raise ValueError("当日消费上界必须是正整数")


def _count_today(
    payload: dict[str, Any], last_consumed_at: datetime | None, moment: datetime
) -> int:
    """当日消费计数的基线：跨了 UTC 日界（或从未消费过）就是 0，否则沿用落盘值。

    旧凭据文件没有这个字段时按 0 处理（向后兼容）。
    """
    same_utc_day = (
        last_consumed_at is not None and last_consumed_at.date() == moment.astimezone(UTC).date()
    )
    return _parse_supply_count(payload.get("refresh_consumed_count")) if same_utc_day else 0


def _check_supply_rate_limits(
    last_consumed_at: datetime | None,
    count_today: int,
    moment: datetime,
    min_interval: timedelta,
    daily_limit: int,
) -> int:
    """按需供给的两道频率上界：先判最小间隔、再判当日上界，返回领取后的新计数。

    先判间隔、后判当日：前者是即时防线，后者是"持续异常数小时才会撞上"的
    哨兵，同时撞上时报当日上界更有信息量——重试要等到明天，不是几分钟后
    继续被同一道上界拒绝。
    """
    if last_consumed_at is not None and moment - last_consumed_at < min_interval:
        raise RefreshMinIntervalNotElapsed(consumed_at=last_consumed_at)
    if count_today >= daily_limit:
        raise RefreshDailyLimitReached(consumed_at=last_consumed_at)
    return count_today + 1


def _validate_save_arguments(
    *,
    require_absent_registration: bool,
    expected_registered_subject_open_id: str | None,
    refresh_consumed_at: datetime | None,
    refresh_consumed_count: int | None,
) -> None:
    """校验 :meth:`HostFileDelegatedCredentialVault.save` 的入参组合与形状。"""
    if require_absent_registration and expected_registered_subject_open_id is not None:
        raise ValueError("首次建立与既有主体 CAS 不能同时使用")
    if refresh_consumed_at is not None:
        if refresh_consumed_at.tzinfo is None or refresh_consumed_at.utcoffset() is None:
            raise ValueError("续期消费时刻必须是带时区的时间")
    if refresh_consumed_count is not None:
        if (
            not isinstance(refresh_consumed_count, int)
            or isinstance(refresh_consumed_count, bool)
            or refresh_consumed_count < 0
        ):
            raise ValueError("当日消费计数必须是非负整数")


def _credential_payload(
    subject_open_id: str,
    grant: AuthorizationGrant,
    moment: datetime,
    *,
    refresh_consumed_at: datetime | None,
    refresh_consumed_count: int | None,
) -> dict[str, Any]:
    """组装即将加密落盘的完整凭据 payload；每次调用都重建整份内容，不做增量合并。"""
    from lingxi.core.ids import new_ulid

    return {
        "generation": new_ulid(),
        "subject_open_id": subject_open_id,
        "refresh_token": grant.refresh_token.reveal(),
        "scope": grant.scope,
        "issued_at": moment.isoformat(),
        "refresh_at": rotation_deadline(moment, grant.refresh_token_expires_in).isoformat(),
        "expires_at": expiry_moment(moment, grant.refresh_token_expires_in).isoformat(),
        "consumed_at": None,
        # 只记"哪一刻消费了一次续期"，不记任何令牌值。
        "refresh_consumed_at": (
            None if refresh_consumed_at is None else refresh_consumed_at.isoformat()
        ),
        # 当日消费计数（供日上界判据持久化）；只记数值，不记任何令牌值。
        "refresh_consumed_count": refresh_consumed_count,
    }


def _parse_moment(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _parse_utc(value: Any) -> datetime | None:
    """把落盘的时刻读成**带时区的 UTC 时刻**。

    频率上界按 UTC 日判定，与日报的日界一致；不带时区的历史值按 UTC 解读，因为
    写入方（凭据轮换职责）只写带时区的 UTC 时刻。读不出来时返回 ``None``＝"没有消费
    记录"，上界因此不拦——方向是刻意的：一份读不懂的旧载荷不该把凭据永久锁死。
    """
    moment = _parse_moment(value)
    if moment is None:
        return None
    if moment.tzinfo is None or moment.utcoffset() is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def _parse_supply_count(value: Any) -> int:
    """把落盘的当日消费计数读成非负整数；读不出来一律当 0（没有消费记录）。

    与 :func:`_parse_utc` 同一条方向：一份读不懂或缺这个字段的旧载荷不该把
    当日计数误判成任何非零值——那会让日上界对老凭据文件过早触发。写入方
    （本模块自己）只写非负整数或 ``None``，其他形状一律按"没有计数"处理。
    """
    if isinstance(value, bool):
        return 0
    if isinstance(value, int) and value >= 0:
        return value
    return 0


class _FileLock:
    """fcntl 排他锁：同一宿主机上的并发访问全部串行化。"""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._handle = None

    def __enter__(self) -> _FileLock:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = open(self._path, "a+b")  # 锁的生命周期由上下文管理
        os.fchmod(self._handle.fileno(), 0o600)
        fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc_info: object) -> None:
        assert self._handle is not None
        fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        self._handle.close()
        self._handle = None
