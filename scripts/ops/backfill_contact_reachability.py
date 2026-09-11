#!/usr/bin/env python3
"""一次性回填：把四个历史来源的入站/可达证据落进 ``app_user`` 新增四列（#673）。

## 必须在 `lingxi-scheduler` 容器内运行，且脚本本体经 stdin 喂进去

与 `scripts/ops/outreach.py`/`preprovision.py` 同一形态与同一理由：`.dockerignore`
排除了 `scripts/`，镜像里没有这个文件，只有该容器持有 `LINGXI_POSTGRES_DSN`。

    docker compose --env-file deploy/.env.prod \\
      -f deploy/compose.yaml -f deploy/compose.prod.yaml \\
      exec -T scheduler python -B - --apply \\
      < scripts/ops/backfill_contact_reachability.py

不需要飞书凭据——本脚本只读写数据库，不发任何消息，因此没有 `--initiated-by`
责任人闸（那是给会产生用户可见外部动作的脚本用的，本脚本没有这类动作）。

## 四个来源

1. ``inbound_event.received_at``（按 ``user_open_id``）——最直接的入站证据。
2. ``conversation.created_at``（按 ``user_id``）——已经建过私聊主窗口或话题。
3. ``task.created_at``（按 ``user_id``）——已经问过数。
4. ``outreach_message``（按 ``recipient_open_id``，仅 ``purpose='apply'``）——
   ``delivered`` 视为可达证据；``failed`` 且 ``last_error`` 是平台明确拒绝码
   （``feishu_code_*``/``notification_failed``）才视为不可用证据。发送前检查
   （``check_*``）、传输层异常这类失败不是「联系不上」，不进负面证据。

## 幂等与中断安全

逐条历史证据按时间**升序**回放，调用与线上写入路径**同一组**
``PostgresAppUserStore.record_contact_reachable``/``record_contact_unavailable``
方法——不是本脚本另起一套 UPDATE。两个方法本身是幂等的（``COALESCE``/
``GREATEST``/"旧成功不覆盖新失败"守卫），按时间升序回放保证"同一人有多条证据
时，最后写入的就是时间上最新的那条"，与线上实时写入的语义完全一致。任何时刻
中断，已经提交的那些回放不会被撤销；重跑会把全部证据重新回放一遍，幂等操作
不会因此产生重复效果，也不会把已经被更新的线上活动推进过的状态往回拨。

## --dry-run / --apply

默认 dry-run：只读四个来源、打印每个来源命中的人数与去重后的合计人数，
**零写入**。``--apply`` 才真正回放。两档的计数来自**同一条**只读聚合查询，
因此逐项一致——不是分别用两套逻辑各算一遍再期望它们凑巧相等。
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

SOURCE_INBOUND_EVENT = "inbound_event"
SOURCE_CONVERSATION = "conversation"
SOURCE_TASK = "task"
SOURCE_OUTREACH_MESSAGE = "outreach_message"
SOURCES = (SOURCE_INBOUND_EVENT, SOURCE_CONVERSATION, SOURCE_TASK, SOURCE_OUTREACH_MESSAGE)


@dataclass(frozen=True)
class Evidence:
    """一条历史证据：谁、什么时候、正面还是负面、来自哪个来源、负面时的错误码。"""

    open_id: str
    at: datetime
    positive: bool
    source: str
    code: str | None = None


# 四个来源统一投影成同一种行形状，全部先 JOIN app_user 过滤掉查无此人的孤儿行——
# 那些行既不该计进"命中人数"，`record_*` 方法对它们也只会是一次 0 行的空写。
_EVIDENCE_SQL = """
SELECT u.feishu_open_id, ie.received_at, TRUE, %(inbound_event)s, NULL
  FROM inbound_event ie JOIN app_user u ON u.feishu_open_id = ie.user_open_id
UNION ALL
SELECT u.feishu_open_id, c.created_at, TRUE, %(conversation)s, NULL
  FROM conversation c JOIN app_user u ON u.id = c.user_id
UNION ALL
SELECT u.feishu_open_id, t.created_at, TRUE, %(task)s, NULL
  FROM task t JOIN app_user u ON u.id = t.user_id
UNION ALL
SELECT u.feishu_open_id, om.created_at, TRUE, %(outreach_message)s, NULL
  FROM outreach_message om JOIN app_user u ON u.feishu_open_id = om.recipient_open_id
 WHERE om.purpose = 'apply' AND om.status = 'delivered'
UNION ALL
SELECT u.feishu_open_id, om.created_at, FALSE, %(outreach_message)s, om.last_error
  FROM outreach_message om JOIN app_user u ON u.feishu_open_id = om.recipient_open_id
 WHERE om.purpose = 'apply' AND om.status = 'failed'
   AND (om.last_error LIKE 'feishu_code_%%' OR om.last_error = 'notification_failed')
ORDER BY 1, 2
"""


def load_evidence(dsn: str) -> tuple[Evidence, ...]:
    """一次读齐四个来源的全部证据，按 open_id、时间升序；只读，不写任何东西。"""
    from lingxi.adapters.postgres import DEFAULT_POSTGRES_TIMEOUTS, PostgresTimeouts, connect

    # 聚合读跨四张表，给到本仓库业务连接允许的最宽语句超时（上限 5 秒）；正式
    # 业务路径的 3 秒预算不动，只有这条运维聚合查询单独放宽。
    timeouts = PostgresTimeouts(
        connect_timeout_seconds=DEFAULT_POSTGRES_TIMEOUTS.connect_timeout_seconds,
        statement_timeout_seconds=5,
        lock_timeout_seconds=DEFAULT_POSTGRES_TIMEOUTS.lock_timeout_seconds,
    )
    with connect(dsn, timeouts=timeouts) as connection, connection.cursor() as cursor:
        cursor.execute(
            _EVIDENCE_SQL,
            {
                "inbound_event": SOURCE_INBOUND_EVENT,
                "conversation": SOURCE_CONVERSATION,
                "task": SOURCE_TASK,
                "outreach_message": SOURCE_OUTREACH_MESSAGE,
            },
        )
        rows = cursor.fetchall()
    return tuple(
        Evidence(open_id=row[0], at=row[1], positive=bool(row[2]), source=row[3], code=row[4])
        for row in rows
    )


def source_counts(evidence: Sequence[Evidence]) -> dict[str, int]:
    """逐来源命中的人数（按 open_id 去重）；四个来源各出一份，互不合并。"""
    seen: dict[str, set[str]] = {source: set() for source in SOURCES}
    for item in evidence:
        seen[item.source].add(item.open_id)
    return {source: len(open_ids) for source, open_ids in seen.items()}


def apply_evidence(dsn: str, evidence: Sequence[Evidence]) -> int:
    """按 (open_id, 时间) 升序逐条回放；返回被回放过的人数（去重）。"""
    from lingxi.adapters.postgres_identity import PostgresAppUserStore

    store = PostgresAppUserStore(dsn)
    touched: set[str] = set()
    for item in sorted(evidence, key=lambda entry: (entry.open_id, entry.at)):
        if item.positive:
            store.record_contact_reachable(open_id=item.open_id, when=item.at)
        else:
            store.record_contact_unavailable(
                open_id=item.open_id, when=item.at, code=item.code or "unknown"
            )
        touched.add(item.open_id)
    return len(touched)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="一次性回填：把四个历史来源的入站/可达证据落进 app_user（#673）。"
        " 默认只出计数、零写入；真正回填必须显式 --apply。",
        allow_abbrev=False,
    )
    parser.add_argument("--dsn", default=None, help="PostgreSQL DSN；缺省读 LINGXI_POSTGRES_DSN")
    parser.add_argument(
        "--apply", action="store_true", help="真正回填；不传时（默认）只出计数、零写入"
    )
    return parser


def _print_counts(evidence: Sequence[Evidence]) -> int:
    counts = source_counts(evidence)
    total_people = len({item.open_id for item in evidence})
    print("四个来源命中的人数（去重；同一人可能同时被多个来源命中）：")
    for source in SOURCES:
        print(f"  - {source}: {counts[source]}")
    print(f"合计涉及 {total_people} 个不同的人。")
    return total_people


def main(argv: list[str] | None = None) -> int:
    arguments = _build_parser().parse_args(argv)
    dsn = arguments.dsn or os.environ.get("LINGXI_POSTGRES_DSN")
    if not dsn:
        print("缺少 DSN：既未传 --dsn，也未设置环境变量 LINGXI_POSTGRES_DSN。", file=sys.stderr)
        return 2

    try:
        evidence = load_evidence(dsn)
    except Exception as error:  # noqa: BLE001 - 只读聚合失败即整份拒绝，未做任何操作
        print(f"未做任何操作：{type(error).__name__}: {error}", file=sys.stderr)
        return 2

    total_people = _print_counts(evidence)
    if not arguments.apply:
        print("dry-run：以上是即将回填的计数，零写入；--apply 才真正执行。")
        return 0

    try:
        touched = apply_evidence(dsn, evidence)
    except Exception as error:  # noqa: BLE001 - 回放中途失败：已提交的那些不回滚
        print(
            f"回放中途失败：{type(error).__name__}: {error}；已经回放成功的部分不会撤销，"
            "重跑本脚本是安全的（幂等）。",
            file=sys.stderr,
        )
        return 3
    print(f"已回填：{touched} 个人的 app_user 行已按时间顺序回放全部证据。")
    if touched != total_people:
        print(
            f"警告：回填触达人数（{touched}）与计数阶段的合计人数（{total_people}）不一致，"
            "请核对是否有并发写入。",
            file=sys.stderr,
        )
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
