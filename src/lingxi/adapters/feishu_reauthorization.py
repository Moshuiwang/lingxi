"""「四达文档会议助手」正式重授权入口。

本模块把一次性 state、飞书回调身份绑定和正式凭据写入编排在一起。它是受控
初始化/恢复入口，不是普通员工的开通路径；普通员工仍不经过 OAuth。

安全边界：

* state 只保存不可逆摘要、预期主体和时间元数据，文件权限为 0600，成功领取
  后立即删除，重复回调不会再次换码；
* 回调身份只取自换码后飞书 ``user_info`` 的返回，浏览器提交的参数不能声明
  自己是谁；
* 授权码、access token 和 refresh token 不进入本模块的结果、日志或 state 文件，
  只有完整的 ``AuthorizationGrant`` 才能交给正式 vault；
* 取消、换码失败、身份错绑或落盘失败都消耗当前 state，恢复动作是重新发起
  一次新的授权，不重放已经提交过的回调或一次性授权码。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlencode, urlparse

from lingxi.core.identity.credentials import AuthorizationGrant
from lingxi.core.identity.onboarding import IdentityProfile

from .delegated_credentials import _FileLock  # noqa: SLF001 - 复用同一宿主机文件锁
from .feishu_directory import AuthorizationExchange

logger = logging.getLogger(__name__)

DEFAULT_STATE_TTL_SECONDS = 10 * 60
_STATE_PATTERN = re.compile(r"[A-Za-z0-9_-]{32,256}\Z")


@dataclass(frozen=True)
class PendingAuthorization:
    """已经由受控入口发出的、尚未领取的授权状态。"""

    expected_subject_open_id: str
    expires_at: datetime


class AuthorizationStateStore(Protocol):
    def issue(
        self,
        expected_subject_open_id: str,
        *,
        ttl_seconds: int,
        now: datetime | None = None,
    ) -> tuple[str, datetime]: ...

    def claim(self, state: str, *, now: datetime | None = None) -> PendingAuthorization | None: ...


class DelegatedCredentialVault(Protocol):
    def registered_subject_open_id(self) -> str | None: ...

    def save(
        self,
        *,
        subject_open_id: str,
        grant: AuthorizationGrant,
        issued_at: datetime | None = ...,
        replacing_generation: str | None = ...,
    ) -> bool: ...


class AuthorizationCodeExchanger(Protocol):
    def exchange_authorization_code(self, code: str, *, redirect_uri: str) -> AuthorizationExchange: ...


class HostFileAuthorizationStateStore:
    """在宿主机受控文件中保存一个一次性授权状态。

    文件不保存 state 原值、授权码或任何令牌；预期主体是受控登记所需的普通
    身份字段，state 本身只以 HMAC 摘要存在。原子替换与文件锁保证进程重启、
    并发点击和写入中断都不会留下可再次领取的半条 state。
    """

    def __init__(self, path: str, integrity_key: str) -> None:
        if not isinstance(path, str) or not path.strip():
            raise ValueError("必须提供重授权 state 文件路径")
        if not isinstance(integrity_key, str) or not integrity_key.strip():
            raise ValueError("必须提供重授权 state 完整性密钥")
        self._path = Path(path)
        self._key = integrity_key.encode()
        self._lock_path = self._path.with_name(self._path.name + ".lock")

    def issue(
        self,
        expected_subject_open_id: str,
        *,
        ttl_seconds: int = DEFAULT_STATE_TTL_SECONDS,
        now: datetime | None = None,
    ) -> tuple[str, datetime]:
        subject = _required_text(expected_subject_open_id, "专用授权主体")
        if not isinstance(ttl_seconds, int) or isinstance(ttl_seconds, bool) or ttl_seconds <= 0:
            raise ValueError("重授权 state 有效期必须是正整数秒")
        moment = _aware_utc(now or datetime.now(timezone.utc), "当前时间")
        expires_at = moment + timedelta(seconds=ttl_seconds)
        state = secrets.token_urlsafe(32)
        payload = {
            "version": 1,
            "state_digest": self._digest(state),
            "expected_subject_open_id": subject,
            "issued_at": moment.isoformat(),
            "expires_at": expires_at.isoformat(),
        }
        with _FileLock(self._lock_path):
            self._write(payload)
        return state, expires_at

    def claim(self, state: str, *, now: datetime | None = None) -> PendingAuthorization | None:
        """校验并立即消费 state；任何不通过都不触发外部换码。"""

        if not isinstance(state, str) or _STATE_PATTERN.fullmatch(state) is None:
            return None
        moment = _aware_utc(now or datetime.now(timezone.utc), "当前时间")
        with _FileLock(self._lock_path):
            valid, pending = self._read()
            if not valid:
                self._path.unlink(missing_ok=True)
                return None
            if pending is None:
                return None
            if pending.expires_at <= moment:
                self._path.unlink(missing_ok=True)
                return None
            if not hmac.compare_digest(self._digest(state), self._read_state_digest()):
                return None
            # 先消费再让调用方换码：进程在外部请求期间退出时，旧 code/state 也不会
            # 被下一次入口重放；操作者重新 issue 即可恢复。
            self._path.unlink(missing_ok=True)
            return pending

    def _digest(self, value: str) -> str:
        return hmac.new(self._key, value.encode(), hashlib.sha256).hexdigest()

    def _read_state_digest(self) -> str:
        # ``claim`` 只在 `_read` 已成功后调用；再次读取同一份 JSON 不会把 state
        # 原值带进日志或调用者，且由文件锁保护。
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            value = raw.get("state_digest") if isinstance(raw, dict) else None
            return value if isinstance(value, str) else ""
        except (OSError, TypeError, ValueError):
            return ""

    def _read(self) -> tuple[bool, PendingAuthorization | None]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return True, None
        except (OSError, TypeError, ValueError):
            return False, None
        if not isinstance(raw, dict):
            return False, None
        payload = {
            "version": raw.get("version"),
            "state_digest": raw.get("state_digest"),
            "expected_subject_open_id": raw.get("expected_subject_open_id"),
            "issued_at": raw.get("issued_at"),
            "expires_at": raw.get("expires_at"),
        }
        signature = raw.get("mac")
        if not isinstance(signature, str) or not hmac.compare_digest(signature, self._mac(payload)):
            return False, None
        if payload["version"] != 1 or not isinstance(payload["state_digest"], str):
            return False, None
        subject = payload["expected_subject_open_id"]
        expires = payload["expires_at"]
        if not isinstance(subject, str) or not subject.strip() or not isinstance(expires, str):
            return False, None
        try:
            expires_at = _aware_utc(datetime.fromisoformat(expires), "state 失效时间")
        except ValueError:
            return False, None
        return True, PendingAuthorization(subject.strip(), expires_at)

    def _mac(self, payload: dict[str, Any]) -> str:
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hmac.new(self._key, canonical.encode(), hashlib.sha256).hexdigest()

    def _write(self, payload: dict[str, Any]) -> None:
        record = {**payload, "mac": self._mac(payload)}
        self._path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temp_name = tempfile.mkstemp(dir=str(self._path.parent), prefix=self._path.name + ".")
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                descriptor = -1
                json.dump(record, handle, ensure_ascii=False, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self._path)
        except BaseException:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
            raise


@dataclass(frozen=True)
class ReauthorizationStart:
    """可交给受控操作者打开的授权地址。"""

    authorization_url: str
    state: str
    expires_at: datetime


@dataclass(frozen=True)
class ReauthorizationResult:
    """不带凭据原值的、可直接交给控制层的结果。"""

    ok: bool
    code: str
    message: str
    retryable: bool


class FeishuReauthorizationEntry:
    """正式重授权编排器：发起、校验、换码、身份绑定和安全写入。"""

    def __init__(
        self,
        *,
        app_id: str,
        redirect_uri: str,
        scope: str,
        authorization_endpoint: str,
        state_store: AuthorizationStateStore,
        vault: DelegatedCredentialVault,
        exchanger: AuthorizationCodeExchanger,
        state_ttl_seconds: int = DEFAULT_STATE_TTL_SECONDS,
    ) -> None:
        self._app_id = _required_text(app_id, "飞书应用 ID")
        self._redirect_uri = _https_url(redirect_uri, "授权回跳地址")
        self._authorization_endpoint = _https_url(authorization_endpoint, "飞书授权地址")
        self._scope = _required_text(scope, "授权范围")
        if "offline_access" not in self._scope.split():
            raise ValueError("授权范围必须包含 offline_access，才能安全保存可轮换凭据")
        if not isinstance(state_ttl_seconds, int) or isinstance(state_ttl_seconds, bool) or state_ttl_seconds <= 0:
            raise ValueError("重授权 state 有效期必须是正整数秒")
        self._state_ttl_seconds = state_ttl_seconds
        self._state_store = state_store
        self._vault = vault
        self._exchanger = exchanger

    def begin(
        self,
        *,
        expected_subject_open_id: str | None = None,
        now: datetime | None = None,
    ) -> ReauthorizationStart:
        """为受控主体发起一次新授权。

        未显式传入主体时只从正式登记表读取；绝不从待回调参数或浏览器字段
        推断。新 state 会替换旧的未完成 state，使旧授权地址自然失效。
        """

        subject = expected_subject_open_id
        if subject is None:
            subject = self._vault.registered_subject_open_id()
        subject = _required_text(subject, "专用授权主体")
        state, expires_at = self._state_store.issue(
            subject,
            ttl_seconds=self._state_ttl_seconds,
            now=now,
        )
        separator = "&" if "?" in self._authorization_endpoint else "?"
        authorization_url = self._authorization_endpoint + separator + urlencode(
            {
                "client_id": self._app_id,
                "response_type": "code",
                "redirect_uri": self._redirect_uri,
                "state": state,
                "scope": self._scope,
                "prompt": "consent",
            }
        )
        return ReauthorizationStart(authorization_url, state, expires_at)

    def handle_callback(
        self,
        state: str | None,
        *,
        code: str | None = None,
        error: str | None = None,
        now: datetime | None = None,
    ) -> ReauthorizationResult:
        """处理授权回调；所有失败结果都不携带外部原始值。"""

        if not isinstance(state, str) or _STATE_PATTERN.fullmatch(state) is None:
            return self._failure("invalid_state", "授权入口已失效，请重新发起授权。")
        has_code = isinstance(code, str) and bool(code)
        has_error = isinstance(error, str) and bool(error)
        if has_code == has_error:
            return self._failure("invalid_callback", "授权回调不完整，未修改凭据，请重新发起授权。")

        pending = self._state_store.claim(state, now=now)
        if pending is None:
            return self._failure("invalid_state", "授权入口已失效，请重新发起授权。")
        if has_error:
            logger.info("正式重授权已取消，未修改凭据")
            return self._failure("cancelled", "已取消本次授权，未修改凭据，请重新发起授权。")

        try:
            exchange = self._exchanger.exchange_authorization_code(code or "", redirect_uri=self._redirect_uri)
        except Exception as caught:  # noqa: BLE001 - 外部失败只能按安全失败关闭
            logger.warning("正式重授权换码失败 error=%s", type(caught).__name__)
            return self._failure(
                "exchange_failed",
                "飞书未确认授权成功，未修改正式凭据，请重新发起授权。",
            )

        if (
            not isinstance(exchange, AuthorizationExchange)
            or not isinstance(exchange.profile, IdentityProfile)
            or not isinstance(exchange.grant, AuthorizationGrant)
        ):
            logger.warning("正式重授权换码结果不完整，未写入凭据")
            return self._failure(
                "exchange_failed",
                "飞书返回的授权资料不完整，未修改正式凭据，请重新发起授权。",
            )
        returned_subject = exchange.profile.open_id
        if not isinstance(returned_subject, str) or not hmac.compare_digest(
            returned_subject.strip(), pending.expected_subject_open_id
        ):
            logger.warning("正式重授权回调身份与发起主体不一致，未写入凭据")
            return self._failure(
                "identity_mismatch",
                "授权身份与发起主体不一致，未写入凭据，请确认使用正确的专用授权用户后重试。",
            )

        # 入口发起到回调完成之间，登记主体可能已经被另一条恢复路径替换；
        # 不允许旧 state 把新主体覆盖回去。
        try:
            registered_subject = self._vault.registered_subject_open_id()
        except Exception as caught:  # noqa: BLE001 - 不把数据库异常带到外部
            logger.warning("正式重授权读取主体登记失败 error=%s", type(caught).__name__)
            return self._failure(
                "persistence_failed",
                "无法确认正式授权主体，未修改凭据，请由运维检查后重新发起授权。",
            )
        if registered_subject is not None and not hmac.compare_digest(
            registered_subject.strip(), pending.expected_subject_open_id
        ):
            logger.warning("正式重授权期间主体登记已变化，未写入凭据")
            return self._failure(
                "subject_changed",
                "正式授权主体已变化，本次授权未写入，请重新查询后发起授权。",
            )

        try:
            saved = self._vault.save(
                subject_open_id=pending.expected_subject_open_id,
                grant=exchange.grant,
            )
        except Exception as caught:  # noqa: BLE001 - vault 必须原子失败关闭
            logger.warning("正式重授权凭据未安全保存 error=%s", type(caught).__name__)
            return self._failure(
                "persistence_failed",
                "授权已取得但未能安全保存，未使用半成品凭据，请检查后重新发起授权。",
            )
        if saved is False:
            logger.warning("正式重授权凭据未确认安全保存")
            return self._failure(
                "persistence_failed",
                "授权已取得但未能安全保存，未使用半成品凭据，请检查后重新发起授权。",
            )

        logger.info("正式重授权完成，凭据已交给正式 vault")
        return ReauthorizationResult(True, "completed", "专用授权已更新，可以继续组织目录同步。", False)

    @staticmethod
    def _failure(code: str, message: str) -> ReauthorizationResult:
        return ReauthorizationResult(False, code, message, True)


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label}不能为空")
    return value.strip()


def _aware_utc(value: datetime, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label}必须带时区")
    return value.astimezone(timezone.utc)


def _https_url(value: object, label: str) -> str:
    text = _required_text(value, label)
    parsed = urlparse(text)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username is not None or parsed.password is not None:
        raise ValueError(f"{label}必须使用不含凭据的 HTTPS 地址")
    if parsed.fragment:
        raise ValueError(f"{label}不得包含 URL fragment")
    return text
