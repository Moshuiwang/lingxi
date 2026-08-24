"""``python -m lingxi.apps.admin_bootstrap``：管理员角色登记表的一次性种子命令。

[Issue #95](https://github.com/Moshuiwang/lingxi/issues/95) S-M-01 派发卡第 1 项：
「初始条目=组织资料同步的专用授权主体账号……用与 #137 专用主体相同的配置/环境识别
机制播种（启动对账或种子命令）」。形状照 ``apps/trace``/``apps/reauthorize``——都已经
是"随 scheduler 镜像装、由运维以 `docker exec` 语义手动调用的一次性受控命令"的先例
（见[代码框架第五节](../../../../docs/技术设计/代码框架.md)），不是常驻进程，不需要
compose 服务条目。

## 只做一件事：把已经登记的专用授权主体，登记进管理员角色表

本命令**不接受任何 open_id 作为命令行参数**——仓库内、命令行历史、CI 日志都不得出现
真实标识。唯一的输入来源是运行环境里已经存在的 ``feishu_delegated_subject`` 登记表
（读取用 ``registered_delegated_subject_open_id``，与 Issue #137 的专用主体识别机制完全
同一个函数，不重新发明第二套识别逻辑）；若那张表还是空的（专用授权尚未完成首次建立），
本命令响亮失败，不做任何猜测或占位写入。

## 幂等且默认只读

- 不带 ``--confirm`` 时只报告"将会做什么"，不连接数据库执行写入——避免一次误运行的
  `python -m lingxi.apps.admin_bootstrap` 在生产环境里悄悄改变状态；
- 带 ``--confirm`` 时执行 :func:`lingxi.adapters.admin_registry.seed_admin_registry_entry`，
  它本身按 ``admin_registry`` 的部分唯一索引做 ``ON CONFLICT DO NOTHING``：已存在一条
  ``active`` 登记时不重复插入、不覆盖，可以安全地重复运行（例如部署脚本按幂等假设
  多次调用）；
- 三类角色（权限管理员/运维管理员/超级管理员）**合并授予**，不提供只授予部分角色的
  命令行开关——MVP 唯一条目就是要三者一起生效，逐角色任免是 S-M-02（#96）的产品范围。

## 输出脱敏

日志与标准输出只打印 :func:`lingxi.core.identity.identifiers.redact_identifier` 处理过的
短标识（形如 ``ou_abcd…(28)``），从不打印完整 ``open_id``。
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import Callable, Mapping, Sequence, TextIO

from lingxi.core.admin.registry import AdminRegistrySeedConflict
from lingxi.core.identity.identifiers import redact_identifier

logger = logging.getLogger(__name__)

DSN_ENV_VAR = "LINGXI_POSTGRES_DSN"

#: 与迁移 ``0067`` 文档一致的脱敏标签：MVP 唯一条目对应组织资料同步的专用授权主体，
#: 不写真实姓名或团队归属。
DELEGATED_SUBJECT_LABEL = "delegated_subject"


def parse_arguments(argv: Sequence[str] = ()) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m lingxi.apps.admin_bootstrap")
    parser.add_argument(
        "--confirm",
        action="store_true",
        default=False,
        help="确认执行写入。缺省时只做只读检查并报告将会发生什么，不连接数据库写入。",
    )
    return parser.parse_args(list(argv))


def run(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    lookup_delegated_subject: Callable[[], str | None] | None = None,
    seed: Callable[[str], bool] | None = None,
) -> int:
    """执行一次种子播种。返回值是进程退出码。

    ``lookup_delegated_subject``/``seed`` 仅供测试注入；正式调用不传，分别落到
    ``registered_delegated_subject_open_id`` 与 ``seed_admin_registry_entry``——与
    业务代码同一个函数、同一套受限超时，不给这个受控命令开一条旁路。
    """

    args = parse_arguments(argv if argv is not None else sys.argv[1:])
    source: Mapping[str, str] = os.environ if env is None else env
    out = sys.stdout if stdout is None else stdout
    err = sys.stderr if stderr is None else stderr

    dsn = (source.get(DSN_ENV_VAR) or "").strip()
    if not dsn:
        print(f"缺少数据库连接串环境变量 {DSN_ENV_VAR}", file=err)
        return 1

    if lookup_delegated_subject is None:
        from lingxi.adapters.delegated_credentials import (  # noqa: PLC0415
            registered_delegated_subject_open_id,
        )

        def lookup_delegated_subject() -> str | None:
            return registered_delegated_subject_open_id(dsn)

    if seed is None:
        from lingxi.adapters.admin_registry import seed_admin_registry_entry  # noqa: PLC0415

        def seed(open_id: str) -> bool:
            # 三类角色固定合并授予：seed_admin_registry_entry 自 opus 批量审查
            # P2 修复起不再接受角色子集入参，这里没有 roles= 可传，也不需要——
            # 见该函数文档。
            return seed_admin_registry_entry(
                dsn,
                feishu_open_id=open_id,
                label=DELEGATED_SUBJECT_LABEL,
            )

    try:
        subject_open_id = lookup_delegated_subject()
    except Exception as error:  # noqa: BLE001 - 读取失败必须响亮报告，不能静默当作缺失
        print(f"读取专用授权主体登记失败：{type(error).__name__}", file=err)
        return 1

    if not subject_open_id:
        print(
            "专用授权主体尚未登记（feishu_delegated_subject 为空），无法播种管理员登记表。"
            "请先完成 Issue #137 的专用授权首次建立流程。",
            file=err,
        )
        return 1

    # `redact_identifier()` 全仓库唯一约束（`core/identity/identifiers.py`）：返回值
    # 只允许作为日志调用的参数，不得赋值给变量后挪作它用（`test_roster_audit_duty.
    # RedactedIdentifierUsageTest` 用 AST 扫描全部运行时源码强制这条）。标准输出
    # 面向操作者，不经过 logging，因此下面的 print() 一律不提及任何形式的标识——
    # 这条命令在任何一次调用里只可能涉及唯一一个专用授权主体，操作者不需要看到
    # 哪怕是脱敏形式的标识就能确认"是不是这件事"。

    if not args.confirm:
        print(
            "[只读预演] 将为专用授权主体登记权限管理员/运维管理员/超级管理员三类角色"
            f"（标签 {DELEGATED_SUBJECT_LABEL}）。加 --confirm 以实际执行写入。",
            file=out,
        )
        return 0

    try:
        inserted = seed(subject_open_id)
    except AdminRegistrySeedConflict as error:
        # "没插入"曾经被无条件当成"已经登记过、幂等成功"——但已存在的那一行可能
        # 根本不是这次意图播种的内容（opus 批量审查 P2）。这里必须响亮拒绝，不能
        # 把一次真正的不一致悄悄放过。只报字段名，不回显任何取到的值。
        print(
            "已存在一条 active 登记，但与本次意图播种的内容不一致（不一致的字段："
            + "、".join(error.mismatched_fields)
            + "），拒绝当作幂等成功。请人工核实该条登记后再决定如何处理。",
            file=err,
        )
        logger.error(
            "admin_bootstrap.seed_conflict subject=%s fields=%s",
            redact_identifier(subject_open_id),
            error.mismatched_fields,
        )
        return 1
    except Exception as error:  # noqa: BLE001 - 写入失败必须响亮报告
        print("登记写入失败，详情已记入日志。", file=err)
        logger.error(
            "admin_bootstrap.failed subject=%s error=%s",
            redact_identifier(subject_open_id),
            type(error).__name__,
        )
        return 1

    if inserted:
        print("已登记专用授权主体为管理员（三类角色）。", file=out)
        logger.info("admin_bootstrap.inserted subject=%s", redact_identifier(subject_open_id))
    else:
        print("专用授权主体已存在有效登记，未重复写入。", file=out)
        logger.info(
            "admin_bootstrap.already_registered subject=%s", redact_identifier(subject_open_id)
        )
    return 0


def main() -> int:  # pragma: no cover - 由 __main__.py 调用
    return run()


__all__: tuple[str, ...] = ("DSN_ENV_VAR", "DELEGATED_SUBJECT_LABEL", "parse_arguments", "run", "main")
