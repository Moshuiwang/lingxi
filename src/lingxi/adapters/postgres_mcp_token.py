"""MCP 访问令牌与就绪确认记录的 PostgreSQL 存取（Issue #156 / S-C-02）。

表结构与逐条理由以迁移 ``0065_mcp_token_and_sync_check`` 为准，本模块不复述。这里只
落实四件在**代码里**才成立的事：

1. **签发幂等，且绝不覆盖**：``INSERT ... ON CONFLICT DO NOTHING`` 再回读。同一个用户
   反复签发只会得到**同一份**令牌。
2. **明文只在内存里**：签发时生成 → 立即加密 → 只把密文交给 ``INSERT``。表里没有任何
   列能放明文（迁移 ``0065``），本模块也没有任何一条把明文写向数据库、日志或异常的路径。
   :class:`IssuedToken` 的 ``repr`` 里同样没有它。
3. **解密失败不放行**：:meth:`~PostgresMcpTokenStore.read_token` 让
   :class:`lingxi.adapters.mcp_token_cipher.McpTokenCipherError` 原样上抛，**不**折成
   "这个人没有令牌"。折了之后，一次主密钥配错会表现成"所有人忽然都没令牌"，
   而系统会安静地给他们重新签发一批——把一个可恢复的配置错误变成不可逆的数据破坏。
4. **每次探针尝试独立成行**：:meth:`~PostgresMcpTokenStore.record_attempt` 的
   ``attempt_no`` 由**数据库**取号，不由调用方传（理由见迁移文件头部）。

## 为什么不提供轮换

Lingxi 只在**新建**发布行时写 ``token_cipher``，更新既有行时不清空也不覆盖
（`V-权限-11`）。因此轮换一个已经发布出去的令牌在当前发布语义下**送不到消费方**：
库里换了新密文，外部表里还是旧的，用户侧表现为"某天开始问数忽然没有权限"。
要支持轮换，必须先由产品负责人决定 Lingxi 是否可以覆盖既有行的那一列；在那之前，
提供一个"能改库但送不出去"的轮换接口比没有轮换更危险。本模块因此**没有** ``rotate``。

产品负责人 2026-08-18 裁定 6（留痕见 [#203](https://github.com/Moshuiwang/lingxi/issues/203)
的决策评论）已经就**存量令牌**给出方向：既有 26 行由 lingxi 在**硬切窗口**统一重签、
覆写发布行密文，并经 Epic D 的用户环境链重投。**这不等于本模块该有 ``rotate``**——那是
一次有窗口、有重投编排的迁移动作，落点在那条硬切链上；一个随时可调的轮换接口仍然会把
新密文写进库里而送不到消费方。等硬切编排落地时，由它连同触发器一起决定改法。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from lingxi.adapters.mcp_token_cipher import McpTokenCipher, new_token
from lingxi.adapters.postgres import DEFAULT_POSTGRES_TIMEOUTS, PostgresTimeouts, connect
from lingxi.core.ids import new_id
from lingxi.core.permission.mcp_readiness import ReadinessAttempt

logger = logging.getLogger(__name__)

_UTC = timezone.utc


@dataclass(frozen=True)
class IssuedToken:
    """一次签发的结果。**明文不进 ``repr``**。

    ``secret`` 声明成 ``repr=False``：默认的 dataclass ``repr`` 会在调试器、日志、
    ``assertEqual`` 失败信息和异常上下文里把明文原样打印出来——那是凭据最常见的泄露
    路径之一，而它看上去完全无害。取用明文只走 :meth:`reveal`，让"我现在要用明文了"
    在代码里是一个能被搜到的动作。
    """

    user_id: str
    token_cipher: str
    created: bool
    secret: str = field(repr=False, default="")

    def reveal(self) -> str:
        """取出明文。调用点应当尽快用完并丢弃，不要存进任何长期对象。"""

        return self.secret


@dataclass(frozen=True)
class StoredCheck:
    """回读出来的一条就绪判定（供运维排查与用例断言）。"""

    check_id: str
    user_id: str
    permission_version: int
    attempt_no: int
    result: str
    error_code: str | None
    metric_count: int | None
    started_at: datetime
    finished_at: datetime | None


class PostgresMcpTokenStore:
    """令牌与就绪记录的读写。构造时不连接数据库，每次调用自带连接（adapters 既有惯例）。"""

    def __init__(
        self,
        dsn: str,
        *,
        cipher: McpTokenCipher,
        timeouts: PostgresTimeouts = DEFAULT_POSTGRES_TIMEOUTS,
    ) -> None:
        if not isinstance(cipher, McpTokenCipher):
            # 只接受**已经校验过主密钥**的加解密对象：允许传一个裸字符串密钥进来，
            # 等于给每个调用点各开一次绕过长度校验的口子。
            raise TypeError("令牌存储必须注入已校验主密钥的 McpTokenCipher")
        self._dsn = dsn
        self._cipher = cipher
        self._timeouts = timeouts

    # ------------------------------------------------------------------
    # 令牌
    # ------------------------------------------------------------------

    def issue_token(self, user_id: str) -> IssuedToken:
        """为该用户签发访问令牌；**已存在即返回既有那一份，绝不覆盖**。

        幂等语义的理由是产品层的：令牌一旦随**新建**发布行写进外部表格，它就是这个人
        当前能用的那一个。重新签发会让库里的密文与外部表里的不一致，而更新路径不写
        ``token_cipher``（`V-权限-11`），新值永远送不出去——用户侧表现为"某天开始问数
        忽然没有权限"。
        """

        if not isinstance(user_id, str) or not user_id.strip():
            raise ValueError("签发令牌必须指明用户")
        return self._insert_new_token(user_id, new_token(), verb="签发")

    def adopt_token(self, user_id: str, secret: str) -> IssuedToken:
        """采纳一份**已经解密**的存量令牌明文（Issue #281 改道，产品负责人 2026-08-25）。

        语义与 :meth:`issue_token` 完全相同——**已存在即返回既有那一份，绝不覆盖**：
        库里已经有这个用户的令牌行时，以库内既有那份为准，本次传入的候选明文被丢弃，
        不做比对也不做覆盖。区别只在候选明文的来源：不是本模块新生成的
        ``secrets.token_urlsafe``，而是调用方从正式表存量令牌解密出来的那一份。

        **本方法自己只校验非空，但迁移 ``0065`` 的 ``token_cipher`` CHECK 依然生效**
        （``^[A-Za-z0-9+/]{86}==$``，见该迁移文件头部）：PKCS7 把明文补到 16 字节的
        整数倍再加密，因此这条 CHECK 实际接受的是 **UTF-8 字节数落在 32–46 之间**的
        候选明文（都补到 48 字节密文 → 64 字节信封 → 88 字符 base64），不是字面意义
        "必须恰好 43 字符"——只是 :func:`~lingxi.adapters.mcp_token_cipher.new_token`
        产出的 43 字节明文恰好落在这个区间里。候选明文的字节数落在区间外时，
        ``INSERT`` 会被数据库拒绝、原样向上抛 ``psycopg`` 异常（不吞不折——采纳失败与
        签发失败同一条纪律，调用方按"本侧故障"收口）。**这不是本方法新引入的限制**：
        #281 改道评论的实测（2026-08-21，解密真实存量令牌）证实旧系统 biai-agent 的
        签发同样是 ``secrets.token_urlsafe(32)``（43 字节，落在区间内），只是与
        :meth:`issue_token` 共用同一列、同一条 CHECK 的自然结果，如实登记不隐瞒
        ——``tests/test_mcp_token_postgres.py`` 的
        ``test_adopting_a_secret_with_a_different_shape_is_rejected_by_the_database``
        真实撞过一次并钉住这条边界。
        """

        if not isinstance(user_id, str) or not user_id.strip():
            raise ValueError("采纳令牌必须指明用户")
        if not isinstance(secret, str) or not secret:
            raise ValueError("采纳令牌必须提供非空明文")
        return self._insert_new_token(user_id, secret, verb="采纳存量")

    def _insert_new_token(self, user_id: str, candidate: str, *, verb: str) -> IssuedToken:
        """``issue_token``/``adopt_token`` 共用的写入与回读：先无条件加密候选明文再
        ``ON CONFLICT DO NOTHING``，多花一次加密换来"判断与写入在同一条语句里"，两个
        并发的首次写入只会有一个胜出，另一个回读到胜出者的那一份。**先查后写**会在
        两次调用之间留一个窗口，两边各自认为自己是第一个。
        """

        candidate_cipher = self._cipher.encrypt(candidate)
        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
            cursor.execute(
                """INSERT INTO mcp_access_token (user_id, token_cipher)
                        VALUES (%s, %s)
                   ON CONFLICT (user_id) DO NOTHING""",
                (user_id, candidate_cipher),
            )
            created = cursor.rowcount == 1
            cursor.execute(
                "SELECT token_cipher FROM mcp_access_token WHERE user_id = %s", (user_id,)
            )
            row = cursor.fetchone()
        if row is None:
            # 只有 app_user 那一行在两条语句之间被删掉才会走到这里（CASCADE）。
            raise LookupError(f"{verb}令牌的目标用户不存在")
        stored_cipher = str(row[0])
        if created:
            logger.info("MCP 访问令牌已%s user=%s", verb, user_id)
            return IssuedToken(user_id, stored_cipher, True, candidate)
        # 已存在：**解密既有那一份**返回，而不是把刚生成/刚解出的明文当成结果。
        # 返回一个从没落过库的明文，会让调用方把它写进用户环境，然后永远匹配不上。
        return IssuedToken(user_id, stored_cipher, False, self._cipher.decrypt(stored_cipher))

    def token_cipher(self, user_id: str) -> str | None:
        """只取密文，**不解密**。构造发布行时用它——那条链路不需要明文。"""

        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT token_cipher FROM mcp_access_token WHERE user_id = %s", (user_id,)
            )
            row = cursor.fetchone()
        return None if row is None else str(row[0])

    def read_token(self, user_id: str) -> str | None:
        """取明文，供就绪探针使用。没有登记返回 ``None``；**解密失败原样上抛**。

        两种"取不到"必须可分辨：没登记过是"还没轮到这一步"，解不开是"密钥或数据坏了"。
        把后者也折成 ``None``，会让一次主密钥配错表现成"所有人都还没签发"。
        """

        cipher = self.token_cipher(user_id)
        return None if cipher is None else self._cipher.decrypt(cipher)

    # ------------------------------------------------------------------
    # 就绪判定记录
    # ------------------------------------------------------------------

    def record_attempt(self, attempt: ReadinessAttempt) -> str:
        """把一次就绪判定独立记一行，返回记录标识。

        ``attempt_no`` 由 ``COALESCE(MAX(attempt_no), 0) + 1`` 在数据库里取，不用
        ``attempt.attempt_no``：后者是**本轮确认内**的次序，进程重启后恢复出来的确认会
        从 1 重来，与已经落库的第一次撞上 ``UNIQUE``。库里那一列要回答的是"这个人的这一版
        权限一共判定过几次"，跨轮次连续才有意义——这张表存在的理由正是事后回答
        "十五分钟是不是一个现实的上限"。
        """

        if not isinstance(attempt, ReadinessAttempt):
            raise TypeError("就绪记录必须是 ReadinessAttempt：取值域与不变式由它保证")
        check_id = new_id("syn")
        lock_key = f"mcp_sync_check:{attempt.binding.user_id}:{attempt.binding.permission_version}"
        with connect(self._dsn, timeouts=self._timeouts) as connection:
            with connection.transaction(), connection.cursor() as cursor:
                # **取号必须串行化**。``COALESCE(MAX(attempt_no), 0) + 1`` 在
                # ``READ COMMITTED`` 下会让两个并发记账读到同一个 MAX，各自算出同一个
                # N+1，于是一个撞上 ``UNIQUE`` 中止——而它对应的那次探针**已经真的发出去
                # 了**，却没有留下任何记录。这张表存在的理由正是"事后判断十五分钟是不是
                # 现实的上限"，丢样本等于丢掉它唯一的用处。
                #
                # 用事务级 advisory lock 而不是 ``SELECT ... FOR UPDATE``：后者要有一行
                # 可锁，而"这个人还没签发令牌"是合法状态（那时 mcp_access_token 里没有
                # 行，锁不住任何东西，串行化就悄悄失效了）。advisory lock 不依赖任何行
                # 是否存在，且随事务结束自动释放，不需要显式解锁路径。
                cursor.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (lock_key,))
                cursor.execute(
                    """INSERT INTO mcp_sync_check
                         (id, user_id, permission_version, attempt_no, started_at, finished_at,
                          result, error_code, metric_count, content_expires_at)
                       SELECT %(id)s, %(user)s, %(version)s,
                              COALESCE(MAX(c.attempt_no), 0) + 1,
                              %(started)s, %(finished)s, %(result)s, %(error)s, %(metrics)s, now()
                         FROM mcp_sync_check c
                        WHERE c.user_id = %(user)s AND c.permission_version = %(version)s
                    RETURNING id""",
                    {
                        "id": check_id,
                        "user": attempt.binding.user_id,
                        "version": attempt.binding.permission_version,
                        "started": attempt.started_at,
                        "finished": attempt.finished_at,
                        "result": attempt.outcome.value,
                        "error": attempt.error_code,
                        "metrics": attempt.metric_count,
                    },
                )
                row = cursor.fetchone()
        if row is None:
            raise RuntimeError("就绪判定记录未写入")
        return str(row[0])

    def load_checks(self, user_id: str, permission_version: int) -> tuple[StoredCheck, ...]:
        """回读某个用户某一版权限的全部判定，按次序。给运维排查与用例断言用。"""

        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
            cursor.execute(
                """SELECT id, user_id, permission_version, attempt_no, result, error_code,
                          metric_count, started_at, finished_at
                     FROM mcp_sync_check
                    WHERE user_id = %s AND permission_version = %s
                    ORDER BY attempt_no""",
                (user_id, permission_version),
            )
            rows = cursor.fetchall()
        return tuple(
            StoredCheck(
                check_id=str(row[0]),
                user_id=str(row[1]),
                permission_version=int(row[2]),
                attempt_no=int(row[3]),
                result=str(row[4]),
                error_code=row[5],
                metric_count=None if row[6] is None else int(row[6]),
                started_at=row[7],
                finished_at=row[8],
            )
            for row in rows
        )

    def purge_expired_checks(self, *, now: datetime | None = None, limit: int = 500) -> int:
        """删除过了九十天上限的判定记录，返回删除条数。

        这张表**没有可识别内容列**（只有内部 ULID、权限版本、次序、时间、结论与错误码），
        因此到期处理是**删整行**而不是擦某一列——没有列可擦。行本身还有一条已经存在的
        删除路径：``user_id`` 上的 ``ON DELETE CASCADE``。
        """

        moment = now or datetime.now(_UTC)
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ValueError("到期判定时间必须带时区")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit 必须是正整数")
        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cursor:
            cursor.execute(
                """DELETE FROM mcp_sync_check
                    WHERE id IN (
                            SELECT id FROM mcp_sync_check
                             WHERE content_expires_at <= %s
                             ORDER BY content_expires_at
                             LIMIT %s
                          )""",
                (moment, limit),
            )
            purged = cursor.rowcount
        if purged:
            logger.info("就绪判定记录已到期删除 条数=%s", purged)
        return purged


def token_cipher_provider(store: PostgresMcpTokenStore) -> Any:
    """把令牌存储适配成 :class:`lingxi.adapters.query_mcp_probe.QueryMcpProbe` 的
    ``token_provider``（按 ``user_id`` 返回**明文**）。

    单独给它一个名字，是为了让"哪里会解密令牌"在仓库里可被搜索到，而不是散落成一堆
    等价的 lambda。
    """

    return store.read_token


__all__ = [
    "IssuedToken",
    "PostgresMcpTokenStore",
    "StoredCheck",
    "token_cipher_provider",
]
