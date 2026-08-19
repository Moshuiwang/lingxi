#!/usr/bin/env python3
"""一次性数据清理：作废「公司 + 职能 → 指标名」翻译闸（Issue #227 + P1 修复）落地
**之前**遗留在 ``publish_outbox`` 里的、未终态的权限发布意图（受控运维脚本，不属于
生产镜像，不由任何进程自动调用）。

**背景（外部独立审查 2026-08-18 坐实的 P2）**：发布执行器
（:mod:`lingxi.core.permission.publish`）经 :meth:`~lingxi.core.permission.
publish_row.PublishRow.from_fields` **原样恢复** outbox 快照，不重新翻译、也不校验
值是否为指标名。因此存在这样一条序列：#227 翻译闸落地**之前**排进 ``publish_outbox``
的授权意图，其 ``permissions`` 值仍是**职能标签**（不是翻译后的指标名）；那一轮当时
因为权限发布表短期令牌供给未接线（Issue #226）而没能真的发出去，停在 ``pending`` 或
``publishing``；#226 接上令牌供给之后，发布执行器会把这些**遗留意图原样重放**，
职能标签就会被当成指标名写进正式表——这条路径完全绕开了 #227 刚建好的翻译闸（翻译闸
只管新排的意图，管不到已经在 outbox 里躺着的旧意图）。

**为什么是一个独立脚本，不是 alembic 迁移**：本仓 ``scripts/ci/check_migration_chain.sh``
的 V-迁移-08 要求每一条非基线 revision 的 ``downgrade()`` 必须真正可逆（`pg_dump
--schema-only` 逐字节比对，见 ``scripts/ci/check_alembic_revisions.py`` 的
``downgrade_failures``）。本次清理**只做数据变更、不做任何 DDL**，"downgrade" 在数据
语义上并不存在（一旦标记为 ``superseded``，无法分辨哪些行本来该发、哪些本来该废）；
硬塞进 alembic 链要么写一个假装可逆的空操作（正是该门禁存在的理由要挡的事），要么让
``downgrade()`` 显式 ``raise``（该门禁的往返检查会真的执行 ``alembic downgrade``，
非基线 revision 上的 ``raise`` 会直接把门禁拖垮）。两条路都不诚实或不可行，因此选择
一个显式的、需要人手动执行一次的运维脚本——与本仓其余"受控验证/运维脚本不进生产镜像"
的姿态（``scripts/reauthorize_feishu_delegated.py`` 等）一致。

**什么时候跑，跑几次**：在同时部署 #227 翻译闸 + P1 修复 + #226 令牌供给的这一次发布
里，**在新版本 scheduler 进程启动之前**跑一次（标准顺序：先清理，再让新进程开始消费
outbox）。跑第二次是安全的空操作——``UPDATE ... WHERE status IN ('pending',
'publishing')`` 在没有匹配行时影响 0 行。

**安全性**：只改 Lingxi 自己的 ``publish_outbox`` 表；不连正式权限多维表格，改不了、
也不需要改任何已经真的写出去的内容。默认**演练模式**（只统计、不写），必须显式传
``--apply`` 才会真的执行 ``UPDATE``。不丢任何用户的真实权限——下一轮每日权限重算会
为每个人重新聚合、重新翻译、重新排一条内容正确的新意图（详见
``docs/技术设计/验收矩阵.md`` 的 ``V-权限-13``）。

**用法**（真实凭据只从环境变量读取）：

.. code-block:: bash

    export LINGXI_POSTGRES_DSN=...
    python3 scripts/invalidate_pretranslation_publish_intents.py          # 演练：只统计
    python3 scripts/invalidate_pretranslation_publish_intents.py --apply  # 真的执行
"""

from __future__ import annotations

import argparse
import os
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "作废 #227 翻译闸落地之前遗留在 publish_outbox 里的未终态发布意图"
            "（P2 修复）。默认只演练（统计受影响行数），传 --apply 才真的执行。"
        )
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="真的执行 UPDATE；不传时只统计会受影响的行数，不写库",
    )
    arguments = parser.parse_args(argv)

    dsn = os.environ.get("LINGXI_POSTGRES_DSN", "").strip()
    if not dsn:
        print("缺少 LINGXI_POSTGRES_DSN，无法连接数据库", file=sys.stderr)
        return 2

    from lingxi.adapters.postgres import connect

    with connect(dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM publish_outbox WHERE status IN ('pending', 'publishing')"
            )
            affected = cursor.fetchone()[0]

            if affected == 0:
                print("没有处于 pending/publishing 的遗留意图，无需清理。")
                return 0

            print(f"发现 {affected} 条处于 pending/publishing 的遗留意图。")
            if not arguments.apply:
                print("演练模式：未做任何改动。加 --apply 真的作废这些意图。")
                return 0

            with connection.transaction():
                cursor.execute(
                    "UPDATE publish_outbox SET status = 'superseded' "
                    "WHERE status IN ('pending', 'publishing')"
                )
            print(f"已将 {affected} 条遗留意图标记为 superseded（不会被发布执行器认领）。")
            print(
                "下一轮每日权限重算会为每个人重新聚合、重新翻译、重新排一条内容正确的"
                "新意图；这是预期的收敛路径，不是遗留问题。"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
