"""``python -m lingxi.apps.trace``：追溯号只读查询入口（Issue #280 §7.2「谁、用什么入口」）。

## 补的是哪个洞

用户看到的失败终态（``LX-ONBOARD-001``）现在带一个追溯号（``{reference}``，见
``core/identity/onboarding_runner.py`` 的 ``_KEYS_REQUIRING_REFERENCE``），但在本命令
之前，能解析这个追溯号的唯一出口是结构化日志——`apps/scheduler/audit.py` 的
``StructuredLogAuditSink.record``。按项目记忆，产品负责人没有生产宿主机访问，
「给了追溯号却没人能解析」等于没给。本命令是最小可行的解析入口：**受控只读 CLI**，
形状照 ``apps/healthcheck``、``apps/reauthorize``——两者都已经是"一个小 ``__main__.py``
+ 只读/受控逻辑"的先例（后者是双人复核写入，前者是纯只读，本命令属于后者）。

## 只读，没有任何写路径

本模块**没有 INSERT/UPDATE/DELETE**，全程只用一个只读事务读四张表。不做网页查询页、
不进管理 MCP（那是 S9 与管理 MCP Story 的范围）——见联合设计 §7.2「不做的事」。

## 身份最小化

默认**不打印** ``open_id``：只打印内部 ``user_id`` 与账号/开通状态。追溯号在多数情形下
足以定位到具体一次事件（哪一次首聊、卡在哪一步、有没有人在跑），不需要先亮出 open_id
才能诊断。需要核对具体是哪个人时，用显式 ``--include-open-id`` 打开（受控管理动作的
既有姿态：默认收紧，显式选择才放宽）。

## 能回答什么、回答不了什么

能回答：这一次事件是谁、什么时候到的、有没有被认领、当前用户 ``provisioning_state``/
``account_state``、发布意图状态、就绪探针历史。**本命令自身回答不了**
``failure_reason``——那不在本命令查询的四张表里。

**这条缺口已由 Issue #337（Trace #373 S-H3-1）在别处补上，本命令登记式不动**：
``failure_reason`` 现在同事务落进新表 ``onboarding_failure``（迁移 ``0077``），管理员
私聊 ``/admin trace <追溯号>``（``core/admin/router.py``，真实查询见
``adapters/admin_registry.PostgresAdminQueries.trace_lookup``）就能自助查到，不再需要
检索容器日志。本模块保持零变更——它仍然是给持有容器访问权限的运维用的补充诊断视图
（能看到发布意图与就绪探针历史，`/admin trace` 目前不展示这两项），不是要被
``/admin trace`` 取代；给本命令也接上 ``onboarding_failure`` 查询是可选的后续加固，
不在本次范围内。
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

#: scheduler 角色使用的连接串环境变量名，与 ``apps/healthcheck`` 的角色映射保持
#: 同一个变量（本命令按设计固定在 scheduler 容器内执行，见模块文档）。
DSN_ENV_VAR = "LINGXI_POSTGRES_DSN"


@dataclass(frozen=True)
class _EventRow:
    feishu_event_id: str
    received_at: Any
    event_type: str
    handled_as: str | None
    user_open_id: str | None
    onboarding_dispatched_at: Any


@dataclass(frozen=True)
class _UserRow:
    user_id: str
    provisioning_state: str
    account_state: str
    permission_version: int
    updated_at: Any


@dataclass(frozen=True)
class _PublishRow:
    id: str
    permission_version: int
    status: str
    attempts: int
    last_outcome: str | None
    created_at: Any
    published_at: Any


@dataclass(frozen=True)
class _ReadinessRow:
    id: str
    permission_version: int
    attempt_no: int
    started_at: Any
    finished_at: Any
    result: str
    error_code: str | None
    metric_count: int | None


def _fetch_events(cursor: Any, trace_id: str) -> tuple[_EventRow, ...]:
    cursor.execute(
        """
        SELECT feishu_event_id, received_at, event_type, handled_as,
               user_open_id, onboarding_dispatched_at
          FROM inbound_event
         WHERE trace_id = %s
         ORDER BY received_at
        """,
        (trace_id,),
    )
    return tuple(_EventRow(*row) for row in cursor.fetchall())


def _fetch_user(cursor: Any, open_id: str) -> _UserRow | None:
    cursor.execute(
        """
        SELECT id, provisioning_state, account_state, permission_version, updated_at
          FROM app_user
         WHERE feishu_open_id = %s
        """,
        (open_id,),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    return _UserRow(*row)


def _fetch_publish(cursor: Any, user_id: str) -> tuple[_PublishRow, ...]:
    cursor.execute(
        """
        SELECT id, permission_version, status, attempts, last_outcome,
               created_at, published_at
          FROM publish_outbox
         WHERE user_id = %s
         ORDER BY created_at DESC
         LIMIT 5
        """,
        (user_id,),
    )
    return tuple(_PublishRow(*row) for row in cursor.fetchall())


def _fetch_readiness(cursor: Any, user_id: str) -> tuple[_ReadinessRow, ...]:
    cursor.execute(
        """
        SELECT id, permission_version, attempt_no, started_at, finished_at,
               result, error_code, metric_count
          FROM mcp_sync_check
         WHERE user_id = %s
         ORDER BY started_at DESC
         LIMIT 5
        """,
        (user_id,),
    )
    return tuple(_ReadinessRow(*row) for row in cursor.fetchall())


def _render(
    trace_id: str,
    events: Sequence[_EventRow],
    users: Mapping[str, _UserRow | None],
    publishes: Mapping[str, tuple[_PublishRow, ...]],
    readiness: Mapping[str, tuple[_ReadinessRow, ...]],
    *,
    include_open_id: bool,
) -> str:
    if not events:
        return f"追溯号 {trace_id}：查无此追溯号"

    lines: list[str] = [f"追溯号 {trace_id}：{len(events)} 条入站事件"]
    for event in events:
        lines.append("")
        lines.append(f"事件标识: {event.feishu_event_id}")
        lines.append(f"接收时间: {event.received_at}")
        lines.append(f"事件类型: {event.event_type}")
        lines.append(f"处理方式: {event.handled_as or '(未标记)'}")
        if event.onboarding_dispatched_at is None:
            lines.append("是否已认领: 否")
        else:
            lines.append(f"是否已认领: 是（{event.onboarding_dispatched_at}）")
        if include_open_id:
            lines.append(f"open_id: {event.user_open_id or '(无)'}")

        user = users.get(event.feishu_event_id)
        if user is None:
            lines.append("用户记录: 未找到（尚未建档，或该事件不带 open_id）")
            continue

        lines.append(f"用户内部标识: {user.user_id}")
        lines.append(f"开通状态: {user.provisioning_state}")
        lines.append(f"账号状态: {user.account_state}")
        lines.append(f"当前权限版本: {user.permission_version}")
        lines.append(f"用户记录更新时间: {user.updated_at}")

        publish_rows = publishes.get(event.feishu_event_id, ())
        if publish_rows:
            lines.append("权限发布意图（最近 5 条）:")
            for row in publish_rows:
                lines.append(
                    f"  - 版本={row.permission_version} 状态={row.status} "
                    f"尝试次数={row.attempts} 上次结论={row.last_outcome or '(无)'} "
                    f"创建于={row.created_at} 发布于={row.published_at or '(未发布)'}"
                )
        else:
            lines.append("权限发布意图: 无")

        readiness_rows = readiness.get(event.feishu_event_id, ())
        if readiness_rows:
            lines.append("就绪探针历史（最近 5 条）:")
            for row in readiness_rows:
                lines.append(
                    f"  - 版本={row.permission_version} 第{row.attempt_no}次 "
                    f"结论={row.result} 错误码={row.error_code or '(无)'} "
                    f"指标数={row.metric_count if row.metric_count is not None else '(无)'} "
                    f"起始={row.started_at} 结束={row.finished_at or '(未结束)'}"
                )
        else:
            lines.append("就绪探针历史: 无")

    return "\n".join(lines)


def run(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    stdout: object = None,
    stderr: object = None,
    connect: Callable[..., Any] | None = None,
) -> int:
    """执行一次只读追溯号查询。返回值是进程退出码。

    ``connect`` 仅供测试注入（默认 ``lingxi.adapters.postgres.connect``，与业务代码
    同一个连接工厂、同一套受限超时——不给这个只读工具开一条不受超时约束的旁路）。
    """

    import os

    parser = argparse.ArgumentParser(prog="python -m lingxi.apps.trace")
    parser.add_argument("trace_id", help="要查询的追溯号（ULID）")
    parser.add_argument(
        "--include-open-id",
        action="store_true",
        default=False,
        help="同时打印 open_id（默认不打印，见模块文档「身份最小化」）",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    source = os.environ if env is None else env
    out = sys.stdout if stdout is None else stdout
    err = sys.stderr if stderr is None else stderr

    dsn = (source.get(DSN_ENV_VAR) or "").strip()
    if not dsn:
        print(f"缺少数据库连接串环境变量 {DSN_ENV_VAR}", file=err)
        return 1

    connect_fn = connect
    if connect_fn is None:
        from lingxi.adapters.postgres import connect as connect_fn  # noqa: PLC0415

    try:
        with connect_fn(dsn) as connection:
            connection.read_only = True
            with connection.cursor() as cursor:
                events = _fetch_events(cursor, args.trace_id)
                users: dict[str, Any] = {}
                publishes: dict[str, tuple[_PublishRow, ...]] = {}
                readiness: dict[str, tuple[_ReadinessRow, ...]] = {}
                for event in events:
                    if not event.user_open_id:
                        continue
                    user = _fetch_user(cursor, event.user_open_id)
                    users[event.feishu_event_id] = user
                    if user is not None:
                        publishes[event.feishu_event_id] = _fetch_publish(cursor, user.user_id)
                        readiness[event.feishu_event_id] = _fetch_readiness(
                            cursor, user.user_id
                        )
    except Exception as error:  # noqa: BLE001 - 查询失败只需要区分"能不能查"
        print(f"查询失败：{type(error).__name__}", file=err)
        return 1

    print(
        _render(
            args.trace_id,
            events,
            users,
            publishes,
            readiness,
            include_open_id=args.include_open_id,
        ),
        file=out,
    )
    return 0


def main() -> int:  # pragma: no cover - 由 __main__.py 与真实 CLI 调用
    return run()


__all__ = ["DSN_ENV_VAR", "run", "main"]
