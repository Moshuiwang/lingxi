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
- ``refresh_consumed_at`` / ``refresh_consumed_count`` 随新凭据一起落盘（Issue
  #215 起、Issue #276 改为双重上界）：日报按需消费一次性 ``refresh_token`` 之后，
  "两次消费的最小间隔"与"当日消费次数"都需要一个**进程重启也抹不掉**的判据，
  由 ``claim_due(for_supply=True)`` 在文件锁内、用锁内的当前时刻、且在置位消费标记
  之前判定。**这是频率上界唯一的权威**，进程内不留第二份账本副本；
- ``revoke`` 删除凭据文件但**保留登记行**；
- 超龄未清的消费中残留由 ``revoke_stale_consumed`` 收殓，并以「不可恢复」日志
  请求人工重新授权；
- **解密失败绝不触发任何删除**（对抗审查 2026-09-02 C-2）：所有读取路径先判
  「是不是解不开」，再判「是不是主体不对 / 是不是无效」。密钥配错与凭据无效在
  这一层长得一样，而两者的正确处置相反——一个要留着等人核对密钥，一个才谈得上
  清理。判反的代价是**永久**丢掉一次性 ``refresh_token``，只能重新走 OAuth Bridge
  授权，因此这个顺序是硬约束，见 :class:`_Undecryptable`。

吸收测试资产 ``refresh_tokens.py`` 与数据库版前身已验证的模式：密文落盘、
明文只在进程内（``SecretToken``）、缺加密依赖构造期即失败、绝不降级为明文。
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

# `DELEGATED_PURPOSE`/`registered_delegated_subject_open_id` 的定义搬到了
# `adapters/delegated_subject_lookup.py`（opus 批量审查 P1 修复，见该模块文档）——
# 这里保留 import 重新导出，本文件其余代码与既有调用方（scheduler/admin_bootstrap）
# 继续用 `from lingxi.adapters.delegated_credentials import ...` 这个路径，不改一行。
# gateway 改从新模块直接 import，避免把这个只读查询之外、本文件独有的 Fernet 依赖
# 一并背上（`pyproject.toml` 的 `gateway` extras 组明确不含 cryptography）。

# 领取后其他进程再领取的观感与数据库版一致：消费标记本身就是唯一的门。
# 保留该常量只为兼容既有调用方签名。
DEFAULT_LEASE_SECONDS = 300

#: 「按需供给」两次消费之间的最小间隔（Issue #276，产品负责人 2026-08-21 裁定默认值）。
#: 防崩溃循环：进程反复重启时每次启动都会换一次，而每次换取都作废旧 ``refresh_token``；
#: 若撞上"换成功但落盘失败"的窗口，凭据永久丢失、需产品负责人重新授权
#: （2026-08-20 事故正是这个形状）。正常运行令牌寿命约 2 小时才换一次，远超这个间隔。
DEFAULT_SUPPLY_MIN_INTERVAL = timedelta(minutes=5)

#: 「按需供给」每 UTC 日至多消费次数（Issue #276，产品负责人 2026-08-21 裁定默认值）。
#: 它是**哨兵**而不是配额：最小间隔已经把崩溃循环压到至多 12 次/小时，正常一天约
#: 12 次（2 小时寿命）远远碰不到 100；真撞上说明系统已经异常了数小时，此时停下留痕、
#: 要求人工介入，好过继续静默轮换——每一次轮换都作废旧凭据，都是一次"换成功但落盘
#: 失败"的机会。
DEFAULT_SUPPLY_DAILY_LIMIT = 100

# `registered_delegated_subject_open_id` 的实现搬到了
# `adapters/delegated_subject_lookup.py`；上面的 import 已经把它重新导出到这个
# 模块的命名空间里，本文件其余部分（`HostFileDelegatedCredentialVault` 的 INSERT/
# UPDATE/SELECT）继续用同一个 `DELEGATED_PURPOSE` 常量，未改变任何行为。


class _Undecryptable:
    """「文件在，但当前主密钥解不开」的哨兵（对抗审查 2026-09-02 C-2）。

    此前 ``_read_payload`` 对解密失败返回 ``{}``，于是它与「解开了、内容是空的」
    彻底同形。后果不是少读一次，而是**删文件**：``load``/``claim_due`` 拿到 ``{}``
    先走主体核对，登记表有行时 ``None != 'ou_…'`` 命中「文件主体与登记不一致」分支
    并 ``unlink`` 掉密文；登记表为空时也会走到 ``_to_credential`` 返回 ``None`` 的
    ``revoke(reason="credential_undecryptable")``。也就是说**只要主密钥配错，
    一次性 ``refresh_token`` 就在下一次扫描里被永久删除**，之后把密钥改回正确值也
    救不回来，只能请产品负责人经 OAuth Bridge 重新授权。

    因此本模块的判定顺序固定为**先判解密、再判主体**：解密不成功时不知道文件属于谁，
    也就没有任何依据说它「与登记不一致」或「无效」，唯一安全的处置是原样留着、
    响亮报错、等人工核对密钥。真要清掉的场景（确认文件损坏）由人工删除，不由一个
    配置错误代劳——删是不可逆的，留是可逆的。
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
    subject_open_id: str
    grant: AuthorizationGrant
    refresh_at: datetime
    expires_at: datetime
    # 文件世代号：每次 save 生成新值。轮换收尾（写回/撤销）必须携带领取时的
    # 世代号，世代不符说明期间发生了新授权——旧链的结果一律放弃，不得覆盖或
    # 删除新凭据（终轮 Codex）。
    generation: str = ""
    # 领取那一刻的**权威消费时刻**，由 ``claim_due`` 在文件锁内生成（Issue #215）。
    # 只有领取回来的凭据带它；``load`` 读到的是未消费的形态，因此为 ``None``。
    consumed_at: datetime | None = None
    # 「按需供给」当日消费计数——**领取成功之后应当持久化的那个新值**（Issue #276）。
    # 只有 ``claim_due`` 的返回值带它；``load`` 读到的是 ``None``。调用方必须原样传给
    # ``save(refresh_consumed_count=…)``：``save`` 每次都重建整份 payload，不传就会把
    # 当日计数悄悄清零，日上界因此形同虚设且不会有任何东西报错。
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

        ``refresh_consumed_at`` 由**消费了一次一次性 ``refresh_token`` 的调用方**
        （凭据轮换职责）显式给出，随新凭据一起落盘，成为频率上界（最小间隔）的持久判据
        （Issue #215 起、Issue #276 改为双重上界；由 :meth:`claim_due` 的 ``for_supply``
        模式在锁内判定）。它必须原样回传 ``claim_due`` 返回的那个 ``consumed_at``——
        两处用同一个锁内时刻，"何时消费过"才只有一个时钟说了算。

        ``refresh_consumed_count`` 同理，是当日消费上界的持久判据，必须原样回传
        ``claim_due`` 返回的那个 ``refresh_consumed_count``（Issue #276）。**这里不能
        留空当作"沿用旧值"**：本方法每次都会重建整份 payload（见下方 ``payload = {...}``），
        调用方不传就等于把它清零——常规到期轮换（``for_supply=False``）也要把从
        ``claim_due`` 拿到的这个字段原样传回来，否则它会把「按需供给」那条链当天已经
        积累的计数悄悄抹掉，日上界因此形同虚设且不会有任何东西报错。

        新授权与首次建立**不带**这两个值：那不是一次续期消费。刚授权完就被上界挡住会让
        运维在恢复之后还要再等，而"人工重授权当天即可恢复"是已接受的语义——留空即可
        让新凭据的最小间隔与当日计数都从零开始。判据显式传入而不是从写入路径反推，
        正是为了不让"这次写入算不算消费"变成隐式约定。
        """
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

        moment = issued_at or datetime.now(UTC)
        from lingxi.core.ids import new_ulid

        with self._locked():
            if replacing_generation is not None:
                current = self._read_payload()
                if current is UNDECRYPTABLE:
                    # 解不开就核对不了代际，覆盖等于蒙着眼睛写。与下面「代际不符」
                    # 同样放弃写回，但不删文件（C-2）。
                    logger.error(_UNDECRYPTABLE_MESSAGE)
                    return False
                current_generation = (current or {}).get("generation")
                if current_generation != replacing_generation:
                    # 领取之后有过新授权：旧轮换链的结果作废，绝不覆盖新凭据。
                    logger.warning("轮换结果已过期（期间发生新授权），放弃写回")
                    return False

            with (
                connect(self._dsn, timeouts=self._timeouts) as connection,
                connection.cursor() as cursor,
            ):
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
                    "refresh_at": rotation_deadline(
                        moment, grant.refresh_token_expires_in
                    ).isoformat(),
                    "expires_at": expiry_moment(moment, grant.refresh_token_expires_in).isoformat(),
                    "consumed_at": None,
                    # 只记"哪一刻消费了一次续期"，不记任何令牌值。
                    "refresh_consumed_at": (
                        None if refresh_consumed_at is None else refresh_consumed_at.isoformat()
                    ),
                    # 当日消费计数（Issue #276 的日上界持久判据）；只记数值，不记任何值。
                    "refresh_consumed_count": refresh_consumed_count,
                }
                self._write_encrypted(payload)
        logger.info(
            "专用授权凭据已加密写入宿主机文件 subject=%s", redact_identifier(subject_open_id)
        )
        return True

    def revoke(self, *, reason: str, generation: str | None = None) -> bool:
        """删除凭据文件；数据库登记行**保留**（V-身份-02 的数据源不随撤销消失）。

        携带 ``generation`` 时只撤销**那一代**：领取之后若发生了新授权，
        旧链的失败不得把新凭据一起删掉（终轮 Codex）。
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
        """领取凭据并**原子置位消费标记**。

        文件锁扮演数据库版 ``FOR UPDATE SKIP LOCKED`` 的角色；消费标记本身是
        防重放的门——置位之后，无论进程死在何处，旧令牌都不会被再次领取。

        ``for_supply=True`` 是**按需续期**入口（Issue #215）：日报每天要一次派生短期
        令牌，而轮换点在有效期 80%（约 5.6 天）才到。这个模式做两件**捆在一起**的事，
        没有任何参数能把它们拆开：放开到期判定，同时施加**两道频率上界**——两次消费的
        最小间隔（``min_interval``，默认 5 分钟）与当日消费次数上界（``daily_limit``，
        默认 100 次，Issue #276 产品负责人 2026-08-21 裁定解除此前"每 UTC 日至多一次"
        改为这两道）。拆得开就迟早会被拆开，而单独放开到期判定等于把一条一次性凭据
        交给一个没有任何频率约束的循环——因此 ``min_interval``/``daily_limit`` 只能
        调整**门槛的大小**，不接受能让检查整体消失的哨兵值（``None``、0 或更小）：
        ``for_supply=True`` 时两道检查**永远都跑**，可注入的只是阈值。

        两道上界的判据都是凭据自己记着的 ``refresh_consumed_at``（最近一次消费时刻）与
        ``refresh_consumed_count``（当日消费计数），**在锁内、用锁内的当前时刻判定，也在
        置位消费标记之前判定**：

        - 在锁内取"现在几点"，等锁跨过 UTC 午夜也不会把 D+1 的领取记成 D；
        - 判据随凭据落盘，因此进程重启、崩溃重启循环乃至同一宿主机上的第二个实例都
          绕不过它；这是**唯一**的频率上界，没有第二份进程内副本——副本不认识凭据代际，
          会在人工重授权之后继续拿旧账本拒绝一条全新的凭据；
        - 先判上界、后置位标记：反过来会让一次被拒的领取把凭据标成"消费中"，等于每次
          亲手制造一次凭据丢失；
        - 两道之中**先判最小间隔、再判当日上界**：前者是即时防线，后者是"持续异常数
          小时才会撞上"的哨兵，同时撞上时报"当日上界"更有信息量——重试要等到明天，
          而不是几分钟后就会被同一道上界继续拒绝。

        领取成功时返回的 :class:`StoredCredential` 带上 ``consumed_at``（**锁内生成的
        那个权威时刻**）与 ``refresh_consumed_count``（这次领取之后应有的当日计数）。
        轮换收尾必须把两者原样写回新凭据（``save(refresh_consumed_at=…,
        refresh_consumed_count=…)``），这样两道上界始终由同一个时钟、同一份账本说了算。
        到期驱动（``for_supply=False``）的领取不检查上界，但**同样要把当日计数原样
        带回去**——它自己的 ``save()`` 也会重建整份 payload，不带的话会把「按需供给」
        当天已经积累的计数悄悄清零。
        """
        del lease_seconds  # 消费标记取代了租期语义；参数保留以兼容调用方。
        if not isinstance(min_interval, timedelta) or min_interval <= timedelta(0):
            # **`<=` 而不是 `<`**（冻结候选审查 2026-08-21 的 F5）：0 是能让这道检查
            # 整体消失的哨兵值——两次消费之间"至少隔 0"对任何时刻都成立，等于把
            # `for_supply=True` 的两道上界拆成一道。上方 docstring 早就写明"不接受
            # 能让检查整体消失的哨兵值（None、0 或更小）"，此前的 `<` 只挡住了负数，
            # 与那句承诺不符。需要"几乎没有间隔"的语义（例如要把日上界从最小间隔里
            # 隔离出来单独断言）时传一个极小的正值，不是 0。
            raise ValueError("最小消费间隔必须是正的时间长度（0 会让这道上界整体失效）")
        if not isinstance(daily_limit, int) or isinstance(daily_limit, bool) or daily_limit < 1:
            raise ValueError("当日消费上界必须是正整数")
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
            if payload.get("consumed_at"):
                return None
            if not self._subject_matches_registry(payload):
                return None
            credential = self._to_credential(payload)
            if credential is None:
                self._path.unlink(missing_ok=True)
                logger.warning("专用授权凭据已撤销 reason=credential_undecryptable")
                return None
            if credential.expires_at <= moment:
                self._path.unlink(missing_ok=True)
                logger.warning("专用授权凭据已撤销 reason=credential_expired")
                return None

            last_consumed_at = _parse_utc(payload.get("refresh_consumed_at"))
            same_utc_day = (
                last_consumed_at is not None
                and last_consumed_at.date() == moment.astimezone(UTC).date()
            )
            # 当日消费计数的基线：跨了 UTC 日界（或从未消费过）就是 0，否则沿用落盘的
            # 那个值——旧凭据文件没有这个字段时按 0 处理（向后兼容）。
            count_today = (
                _parse_supply_count(payload.get("refresh_consumed_count")) if same_utc_day else 0
            )

            if for_supply:
                if last_consumed_at is not None and moment - last_consumed_at < min_interval:
                    raise RefreshMinIntervalNotElapsed(consumed_at=last_consumed_at)
                if count_today >= daily_limit:
                    raise RefreshDailyLimitReached(consumed_at=last_consumed_at)
                new_supply_count = count_today + 1
            else:
                if credential.refresh_at > moment:
                    return None
                # 不做频率判据，但把当日计数原样带过去（见上方 docstring）。
                new_supply_count = count_today
                # **刻意**（Issue #284 C 组 #9，Trace #373 D7 裁定：登记不改行为）：
                # 这一支下面 `payload["consumed_at"] = moment.isoformat()`（本方法
                # 结尾统一执行）同样会推进 `consumed_at`/`refresh_consumed_at`——
                # `for_supply=False` 的到期驱动领取与 `for_supply=True` 的按需供给
                # 共用同一处赋值，不分叉。但 `new_supply_count` 在这一支**不递增**，
                # 只原样带回 `count_today`。两道频率上界的口径因此不对称：
                # `refresh_consumed_at`（最小间隔判据）对**任何一次**成功领取都推进，
                # `refresh_consumed_count`（当日次数上界）**只统计 `for_supply=True`
                # 那一类消费**。这是有意为之，不是遗漏——`for_supply=True` 的每日
                # 上界（默认 100 次）要挡的是"崩溃重启循环把按需供给这条路径变成
                # 高频源"（见模块文档「已知并接受的残留」一节），到期驱动的轮换
                # 本身已经由自己的到期节奏（约 5.6 天一次）天然限速，不需要占用同一个
                # 计数器的配额；把它计进去反而会让按需供给的当日上界被一次完全不相关
                # 的到期轮换悄悄消耗掉一格。最小间隔判据（`refresh_consumed_at`）则
                # 对两类消费一视同仁，因为它防的是"任意一次消费之后过快再消费一次"，
                # 与是哪条路径触发的这次消费无关。

            payload["consumed_at"] = moment.isoformat()
            self._write_encrypted(payload)
        return replace(credential, consumed_at=moment, refresh_consumed_count=new_supply_count)

    # ---- 内部 -------------------------------------------------------------

    def _locked(self):
        return _FileLock(self._lock_path)

    def _subject_matches_registry(self, payload: dict[str, Any]) -> bool:
        """凭据文件主体必须与数据库登记一致，否则失败关闭并清除文件。

        主体 A→B 更换途中崩溃会留下「登记指向 B、文件仍是 A」：此时 A 已不在
        登记表里，双向触发器只护着 B，继续用 A 的凭据等于在防线外运行
        （终轮 Codex）。清除旧文件并要求重新授权是唯一安全的恢复。
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

    与 :func:`_parse_utc` 同一条方向：一份读不懂或缺这个字段的旧载荷（Issue #276
    之前落盘的凭据）不该把当日计数误判成任何非零值——那会让日上界对老凭据文件
    过早触发。写入方（本模块自己）只写非负整数或 ``None``，因此这里遇到的任何
    其他形状都是历史遗留或不受信任的读取路径，一律按"没有计数"处理。
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
