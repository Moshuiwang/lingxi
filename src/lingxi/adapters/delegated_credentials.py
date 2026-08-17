"""专用授权凭据的宿主机文件保管，与数据库侧的主体登记。

按产品负责人 2026-08-05 对 Issue #16 的决策（选项 A）落实成文边界（数据库设计
原则 3、代码框架横切约定「凭据不进数据库」）：

- ``refresh_token`` 只以 Fernet 密文保存在**宿主机受控文件**（0600、原子替换、
  fcntl 排他锁），不进业务数据库，因此不随 Supabase 及其备份出司界；解密密钥
  仍来自受控环境变量，与文件分开存放。
- 数据库只保留 ``feishu_delegated_subject`` 登记表：主体 ``open_id`` 与配置状态。
  它是 V-身份-02 双向触发器的数据源（撤销、收殓都不动它），也回答数据库设计
  原则 3 允许保存的「是否已配置」。

一次性令牌语义（PR #48 双复查后的形态）全部平移自数据库版实现：

- ``claim_due`` 原子置位消费标记——领取去续期的那一刻起，旧密文对 ``load`` 与
  再次领取都不可见，进程崩溃也不会在租期后重放已被飞书作废的令牌；
- ``save`` 写入新凭据并清空消费标记（先登记数据库主体，让触发器把关，
  再落文件——顺序不能反，否则触发器拒绝时密文已经写盘）；
- ``revoke`` 删除凭据文件但**保留登记行**；
- 超龄未清的消费中残留由 ``revoke_stale_consumed`` 收殓，并以「不可恢复」日志
  请求人工重新授权。

吸收测试资产 ``refresh_tokens.py`` 与数据库版前身已验证的模式：密文落盘、
明文只在进程内（``SecretToken``）、缺加密依赖构造期即失败、绝不降级为明文。
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from lingxi.adapters.postgres import DEFAULT_POSTGRES_TIMEOUTS, PostgresTimeouts, connect
from lingxi.core.identity.credentials import (
    AuthorizationGrant,
    SecretToken,
    expiry_moment,
    rotation_deadline,
)
from lingxi.core.identity.identifiers import redact_identifier

logger = logging.getLogger(__name__)

# 登记表里唯一允许的用途。新增用途要走迁移，不能靠调用方传字符串。
DELEGATED_PURPOSE = "org_directory_sync"

# 领取后其他进程再领取的观感与数据库版一致：消费标记本身就是唯一的门。
# 保留该常量只为兼容既有调用方签名。
DEFAULT_LEASE_SECONDS = 300


@dataclass(frozen=True)
class StoredCredential:
    subject_open_id: str
    grant: AuthorizationGrant
    refresh_at: datetime
    expires_at: datetime
    # 文件世代号：每次 save 生成新值。轮换收尾（写回/撤销）必须携带领取时的
    # 世代号，世代不符说明期间发生了新授权——旧链的结果一律放弃，不得覆盖或
    # 删除新凭据（终轮 Codex）。
    generation: str = ""


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
    ) -> bool:
        """登记主体并写入（或轮换）唯一一条专用授权凭据。

        ``expected_registered_subject_open_id`` 用于回调和轮换收尾的原子 CAS：
        传入时，数据库事务只在登记仍等于 expected 时继续，避免一次旧读取把
        已撤销或已更换的主体写回。文件锁覆盖登记校验、世代校验和加密写入，
        因而不会在保存流程中留下旧文件覆盖窗口。

        ``require_absent_registration`` 是**首次建立专用授权主体**（Issue #137）
        用的反向 CAS：登记表必须仍然为空才写入。它与 expected 是同一件事的两
        个方向——都在保存事务内让数据库判定，而不是先读后写；因此首次建立不是
        绕过主体校验的旁路。``ON CONFLICT DO NOTHING`` 在已有登记时返回零行，
        既不覆盖也不更新既有主体（换主体属于另一条单独授权的删除动作）。

        两种 CAS 不能同时传：一次保存只能有一个判定条件，否则"到底以哪个为准"
        会变成调用方的隐式约定。

        登记为空时若磁盘上还残留旧密文，它对 ``load``/``claim_due`` 早已不可用
        （主体与登记不一致会被清除），因此首次建立按原子替换直接写新密文，不为
        这份已经失效的残留额外加一道人工确认。

        无 expected 时是初次受控写入的兼容路径；V-身份-02 的反向触发器仍在
        登记写入处把关，触发器拒绝时密文一个字节都不落盘。
        """

        if require_absent_registration and expected_registered_subject_open_id is not None:
            raise ValueError("首次建立与既有主体 CAS 不能同时使用")

        moment = issued_at or datetime.now(timezone.utc)
        from lingxi.core.ids import new_ulid

        with self._locked():
            if replacing_generation is not None:
                current = self._read_payload()
                current_generation = (current or {}).get("generation")
                if current_generation != replacing_generation:
                    # 领取之后有过新授权：旧轮换链的结果作废，绝不覆盖新凭据。
                    logger.warning("轮换结果已过期（期间发生新授权），放弃写回")
                    return False

            with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
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

                payload = {
                    "generation": new_ulid(),
                    "subject_open_id": subject_open_id,
                    "refresh_token": grant.refresh_token.reveal(),
                    "scope": grant.scope,
                    "issued_at": moment.isoformat(),
                    "refresh_at": rotation_deadline(moment, grant.refresh_token_expires_in).isoformat(),
                    "expires_at": expiry_moment(moment, grant.refresh_token_expires_in).isoformat(),
                    "consumed_at": None,
                }
                self._write_encrypted(payload)
        logger.info("专用授权凭据已加密写入宿主机文件 subject=%s", redact_identifier(subject_open_id))
        return True

    def revoke(self, *, reason: str, generation: str | None = None) -> bool:
        """删除凭据文件；数据库登记行**保留**（V-身份-02 的数据源不随撤销消失）。

        携带 ``generation`` 时只撤销**那一代**：领取之后若发生了新授权，
        旧链的失败不得把新凭据一起删掉（终轮 Codex）。"""

        with self._locked():
            existed = self._path.exists()
            if existed and generation is not None:
                current = self._read_payload()
                if (current or {}).get("generation") != generation:
                    logger.warning("撤销目标已被新授权取代，跳过")
                    return False
            if existed:
                self._path.unlink()
        if existed:
            logger.warning("专用授权凭据已撤销 reason=%s", reason)
        return existed

    def revoke_stale_consumed(self, *, max_age_seconds: int = 600, now: datetime | None = None) -> bool:
        """收殓「已消费但一直没写回新凭据」的文件。

        这种文件意味着进程在续期后、落盘前死掉：旧令牌已被飞书作废，留着密文
        只会诱使未来的代码路径重放它。"""

        moment = now or datetime.now(timezone.utc)
        with self._locked():
            payload = self._read_payload()
            if payload is None:
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

        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT subject_open_id FROM feishu_delegated_subject WHERE purpose = %s",
                (DELEGATED_PURPOSE,),
            )
            row = cursor.fetchone()
        if row is None or not isinstance(row[0], str) or not row[0].strip():
            return None
        return row[0].strip()

    def load(self, *, now: datetime | None = None) -> StoredCredential | None:
        """取出当前凭据供同步使用。解密失败或已失效时撤销并返回 ``None``。"""

        moment = now or datetime.now(timezone.utc)
        with self._locked():
            payload = self._read_payload()
        if payload is None:
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

    def claim_due(self, *, lease_seconds: int = DEFAULT_LEASE_SECONDS, now: datetime | None = None) -> StoredCredential | None:
        """领取到期凭据并**原子置位消费标记**。

        文件锁扮演数据库版 ``FOR UPDATE SKIP LOCKED`` 的角色；消费标记本身是
        防重放的门——置位之后，无论进程死在何处，旧令牌都不会被再次领取。
        """

        del lease_seconds  # 消费标记取代了租期语义；参数保留以兼容调用方。
        moment = now or datetime.now(timezone.utc)
        with self._locked():
            payload = self._read_payload()
            if payload is None:
                return None
            if payload.get("consumed_at"):
                return None
            if not self._subject_matches_registry(payload):
                return None
            credential = self._to_credential(payload)
            if credential is None:
                self._path.unlink(missing_ok=True)
                logger.warning("专用授权凭据已撤销 reason=credential_undecryptable")
                return None
            if credential.expires_at <= moment or credential.refresh_at > moment:
                if credential.expires_at <= moment:
                    self._path.unlink(missing_ok=True)
                    logger.warning("专用授权凭据已撤销 reason=credential_expired")
                return None
            payload["consumed_at"] = moment.isoformat()
            self._write_encrypted(payload)
        return credential

    # ---- 内部 -------------------------------------------------------------

    def _locked(self):
        return _FileLock(self._lock_path)

    def _subject_matches_registry(self, payload: dict[str, Any]) -> bool:
        """凭据文件主体必须与数据库登记一致，否则失败关闭并清除文件。

        主体 A→B 更换途中崩溃会留下「登记指向 B、文件仍是 A」：此时 A 已不在
        登记表里，双向触发器只护着 B，继续用 A 的凭据等于在防线外运行
        （终轮 Codex）。清除旧文件并要求重新授权是唯一安全的恢复。"""

        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
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

    def _read_payload(self) -> dict[str, Any] | None:
        try:
            blob = self._path.read_bytes()
        except FileNotFoundError:
            return None
        if not blob:
            return None
        try:
            return json.loads(self._cipher.decrypt(blob))
        except (self._invalid_token, ValueError):
            # 解密失败按「不可用」处理，调用方决定撤销；不留半解的内容。
            return {}

    def _write_encrypted(self, payload: dict[str, Any]) -> None:
        blob = self._cipher.encrypt(json.dumps(payload, ensure_ascii=False).encode())
        self._path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temp_name = tempfile.mkstemp(dir=str(self._path.parent), prefix=self._path.name + ".")
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
        if not isinstance(token, str) or not token or not isinstance(subject, str) or refresh_at is None or expires_at is None:
            return None
        remaining = max(int((expires_at - datetime.now(timezone.utc)).total_seconds()), 1)
        return StoredCredential(
            subject_open_id=subject,
            grant=AuthorizationGrant(SecretToken(token), remaining, str(payload.get("scope") or "")),
            refresh_at=refresh_at,
            expires_at=expires_at,
            generation=str(payload.get("generation") or ""),
        )


def _parse_moment(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


class _FileLock:
    """fcntl 排他锁：同一宿主机上的并发访问全部串行化。"""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._handle = None

    def __enter__(self) -> "_FileLock":
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = open(self._path, "a+b")  # noqa: SIM115 - 锁的生命周期由上下文管理
        os.fchmod(self._handle.fileno(), 0o600)
        fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc_info: object) -> None:
        assert self._handle is not None
        fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        self._handle.close()
        self._handle = None
