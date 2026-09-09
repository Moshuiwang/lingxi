"""受控只读追溯 CLI，保留原事件/开通视图并补充低敏任务与文档交付状态。

裸 ULID 查有效入站事件，T-ULID 只查精确任务；两者分别遵守原事件和任务保留期限。
任务投影不含问题、答案或个人资料，默认仍不显示 open_id；历史开通视图需要核对外部
身份时，仍需显式 --include-open-id。查询使用同一只读事务，不提供写入口。

本命令不知道自己被装进哪个容器：三个角色的连接串变量名不统一（见
``adapters.postgres.DSN_ENV_VAR_BY_ROLE``），因此按固定顺序逐个回退尝试，
不要求调用方另配连接串。
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from lingxi.adapters.postgres import DSN_ENV_VAR_BY_ROLE
from lingxi.adapters.task_trace_query import fetch_trace_task
from lingxi.core.task_reference import parse_reference

#: 历史默认：scheduler / worker 角色的连接串环境变量名，向后兼容按此键注入
#: 环境的旧调用方。真正的读取顺序见 :func:`_resolve_dsn`——本命令按固定顺序
#: 回退尝试，不止读这一个变量。
DSN_ENV_VAR = DSN_ENV_VAR_BY_ROLE["scheduler"]

#: 回退顺序固定：先 scheduler/worker 的历史默认变量，再 gateway 的独立前缀
#: 变量；worker 与 scheduler 同名，不需要单独再试一次。顺序不随
#: ``DSN_ENV_VAR_BY_ROLE`` 的字典遍历顺序变化。
_DSN_FALLBACK_ROLES: tuple[str, ...] = ("scheduler", "gateway")


def _resolve_dsn(source: Mapping[str, str]) -> tuple[str, tuple[str, ...]]:
    """按 :data:`_DSN_FALLBACK_ROLES` 顺序找第一个非空连接串环境变量。

    返回 ``(dsn, tried)``：命中时 ``dsn`` 非空、``tried`` 是命中之前（含命中
    本身）尝试过的变量名；全部落空时 ``dsn`` 为空串、``tried`` 是全部尝试过的
    变量名——调用方用它在失败提示里逐个列出，不需要重新猜一遍找过哪些。
    """
    tried: list[str] = []
    for role in _DSN_FALLBACK_ROLES:
        var_name = DSN_ENV_VAR_BY_ROLE[role]
        if var_name in tried:
            continue
        tried.append(var_name)
        value = (source.get(var_name) or "").strip()
        if value:
            return value, tuple(tried)
    return "", tuple(tried)


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
         WHERE trace_id = %s AND expires_at > now()
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


def _render_publish_lines(publish_rows: Sequence[_PublishRow]) -> list[str]:
    if not publish_rows:
        return ["权限发布意图: 无"]
    lines = ["权限发布意图（最近 5 条）:"]
    for row in publish_rows:
        lines.append(
            f"  - 版本={row.permission_version} 状态={row.status} "
            f"尝试次数={row.attempts} 上次结论={row.last_outcome or '(无)'} "
            f"创建于={row.created_at} 发布于={row.published_at or '(未发布)'}"
        )
    return lines


def _render_readiness_lines(readiness_rows: Sequence[_ReadinessRow]) -> list[str]:
    if not readiness_rows:
        return ["就绪探针历史: 无"]
    lines = ["就绪探针历史（最近 5 条）:"]
    for row in readiness_rows:
        lines.append(
            f"  - 版本={row.permission_version} 第{row.attempt_no}次 "
            f"结论={row.result} 错误码={row.error_code or '(无)'} "
            f"指标数={row.metric_count if row.metric_count is not None else '(无)'} "
            f"起始={row.started_at} 结束={row.finished_at or '(未结束)'}"
        )
    return lines


def _render_event(
    event: _EventRow,
    users: Mapping[str, _UserRow | None],
    publishes: Mapping[str, tuple[_PublishRow, ...]],
    readiness: Mapping[str, tuple[_ReadinessRow, ...]],
    *,
    include_open_id: bool,
) -> list[str]:
    """一条入站事件的展示行，含（若已建档）用户/发布意图/就绪探针历史。"""
    lines: list[str] = [
        "",
        f"事件标识: {event.feishu_event_id}",
        f"接收时间: {event.received_at}",
        f"事件类型: {event.event_type}",
        f"处理方式: {event.handled_as or '(未标记)'}",
    ]
    if event.onboarding_dispatched_at is None:
        lines.append("是否已认领: 否")
    else:
        lines.append(f"是否已认领: 是（{event.onboarding_dispatched_at}）")
    if include_open_id:
        lines.append(f"open_id: {event.user_open_id or '(无)'}")

    user = users.get(event.feishu_event_id)
    if user is None:
        lines.append("用户记录: 未找到（尚未建档，或该事件不带 open_id）")
        return lines

    lines.extend(
        [
            f"用户内部标识: {user.user_id}",
            f"开通状态: {user.provisioning_state}",
            f"账号状态: {user.account_state}",
            f"当前权限版本: {user.permission_version}",
            f"用户记录更新时间: {user.updated_at}",
        ]
    )
    lines.extend(_render_publish_lines(publishes.get(event.feishu_event_id, ())))
    lines.extend(_render_readiness_lines(readiness.get(event.feishu_event_id, ())))
    return lines


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
        lines.extend(
            _render_event(event, users, publishes, readiness, include_open_id=include_open_id)
        )
    return "\n".join(lines)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m lingxi.apps.trace")
    parser.add_argument("trace_id", help="原事件追溯号（ULID）或任务参考号（T-加ULID）")
    parser.add_argument(
        "--include-open-id",
        action="store_true",
        default=False,
        help="同时打印 open_id（默认不打印，见模块文档「身份最小化」）",
    )
    return parser


def _collect_trace_data(
    cursor: Any, trace_id: str
) -> tuple[
    tuple[_EventRow, ...],
    dict[str, Any],
    dict[str, tuple[_PublishRow, ...]],
    dict[str, tuple[_ReadinessRow, ...]],
]:
    """按追溯号读四张表；只为带 ``open_id`` 的事件补查用户/发布/就绪历史。"""
    events = _fetch_events(cursor, trace_id)
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
            readiness[event.feishu_event_id] = _fetch_readiness(cursor, user.user_id)
    return events, users, publishes, readiness


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

    args = _build_parser().parse_args(list(argv) if argv is not None else None)

    source = os.environ if env is None else env
    out = sys.stdout if stdout is None else stdout
    err = sys.stderr if stderr is None else stderr

    dsn, tried_vars = _resolve_dsn(source)
    if not dsn:
        print("缺少数据库连接串环境变量，已尝试：" + "、".join(tried_vars), file=err)
        return 1

    parsed = parse_reference(args.trace_id)
    if parsed is None:
        print("核查号码格式不合法", file=err)
        return 1

    connect_fn = connect
    if connect_fn is None:
        from lingxi.adapters.postgres import connect as connect_fn

    try:
        with connect_fn(dsn) as connection:
            connection.read_only = True
            with connection.cursor() as cursor:
                if parsed.reference_kind == "task":
                    data = (), {}, {}, {}
                else:
                    data = _collect_trace_data(cursor, args.trace_id)
                task = fetch_trace_task(cursor, args.trace_id)
    except Exception as error:  # 查询失败只需要区分"能不能查"
        print(f"查询失败：{type(error).__name__}", file=err)
        return 1

    print(
        _render_lookup(
            parsed.reference_kind,
            args.trace_id,
            task,
            data,
            args.include_open_id,
        ),
        file=out,
    )
    return 0


def _render_lookup(kind, reference, task, data, include_open_id):
    """任务参考号不伪造事件、开通状态或个人资料。"""
    if kind == "task":
        text = (
            f"任务参考号 {reference}"
            if task is not None
            else f"任务参考号 {reference}：查无此追溯号"
        )
    else:
        text = _render(reference, *data, include_open_id=include_open_id)
    if task is not None:
        text += "\n" + _render_task(task)
    return text


def _render_task(task: tuple) -> str:
    """与管理入口相同的低敏任务投影；没有正文和个人资料列。"""
    labels = (
        "任务状态",
        "错误分类",
        "失败码",
        "失败签名",
        "任务结束",
        "文档交付状态",
        "文档交付错误",
        "文档简化原因",
        "任务开始",
    )
    return "\n".join(f"{label}: {value}" for label, value in zip(labels, task) if value is not None)


def main() -> int:  # pragma: no cover - 由 __main__.py 与真实 CLI 调用
    """入口封装，交给 `__main__.py` 与真实 CLI 调用。"""
    return run()


__all__ = ["DSN_ENV_VAR", "run", "main"]
