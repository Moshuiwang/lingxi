"""``python -m lingxi.apps.admin_bootstrap``：管理员角色登记表的一次性种子命令。

形状照 ``apps/trace``/``apps/reauthorize``：随 scheduler 镜像装、由运维
以 ``docker exec`` 语义手动调用的一次性受控命令，不是常驻进程。只做一件
事：把已登记的专用授权主体登记进管理员角色表。不接受任何 open_id 作为
命令行参数——仓库内、命令行历史、CI 日志都不得出现真实标识；唯一输入
来源是运行环境里已存在的 ``feishu_delegated_subject`` 登记表，那张表
还是空的时本命令响亮失败，不做任何猜测或占位写入。

幂等且默认只读：不带 ``--confirm`` 只报告将会做什么、不连接数据库写入；
``--confirm`` 执行的写入按部分唯一索引做 ``ON CONFLICT DO NOTHING``，可
安全重复运行。三类角色合并授予，不提供只授予部分角色的命令行开关。日志
与标准输出只打印脱敏后的短标识，从不打印完整 ``open_id``。
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from typing import TextIO

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


def _resolve_dsn(source: Mapping[str, str], err: TextIO) -> str | None:
    """读连接串环境变量；缺失时报错并返回 ``None``。"""
    dsn = (source.get(DSN_ENV_VAR) or "").strip()
    if not dsn:
        print(f"缺少数据库连接串环境变量 {DSN_ENV_VAR}", file=err)
        return None
    return dsn


def _build_default_lookup(dsn: str) -> Callable[[], str | None]:
    from lingxi.adapters.delegated_credentials import registered_delegated_subject_open_id

    def lookup_delegated_subject() -> str | None:
        return registered_delegated_subject_open_id(dsn)

    return lookup_delegated_subject


def _build_default_seed(dsn: str) -> Callable[[str], bool]:
    from lingxi.adapters.admin_registry import seed_admin_registry_entry

    def seed(open_id: str) -> bool:
        # 三类角色固定合并授予：seed_admin_registry_entry 不再接受角色子集
        # 入参，这里没有 roles= 可传，也不需要——见该函数文档。
        return seed_admin_registry_entry(
            dsn,
            feishu_open_id=open_id,
            label=DELEGATED_SUBJECT_LABEL,
        )

    return seed


def _resolve_subject(lookup_delegated_subject: Callable[[], str | None], err: TextIO) -> str | None:
    """取专用授权主体标识；读取失败或尚未登记都打印错误并返回 ``None``。"""
    try:
        subject_open_id = lookup_delegated_subject()
    except Exception as error:  # 读取失败必须响亮报告，不能静默当作缺失
        print(f"读取专用授权主体登记失败：{type(error).__name__}", file=err)
        return None

    if not subject_open_id:
        print(
            "专用授权主体尚未登记（feishu_delegated_subject 为空），无法播种管理员登记表。"
            "请先完成专用授权首次建立流程。",
            file=err,
        )
        return None
    return subject_open_id


def _perform_seed(
    seed: Callable[[str], bool], subject_open_id: str, *, out: TextIO, err: TextIO
) -> int:
    """执行写入并按结果打印/记账；返回进程退出码。"""
    try:
        inserted = seed(subject_open_id)
    except AdminRegistrySeedConflict as error:
        # "没插入"不能无条件当成"已经登记过、幂等成功"——已存在的那一行可能
        # 根本不是这次意图播种的内容，必须响亮拒绝，不能把一次真正的不一致
        # 悄悄放过。只报字段名，不回显任何取到的值。
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
    except Exception as error:  # 写入失败必须响亮报告
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
    业务代码同一个函数、同一套受限超时，不给这个受控命令开一条旁路。标准
    输出面向操作者、不经过 logging，因此下面的 print() 一律不提及任何
    形式的标识：本命令任一次调用只可能涉及唯一一个专用授权主体，操作者
    不需要看到脱敏标识也能确认"是不是这件事"。
    """
    args = parse_arguments(argv if argv is not None else sys.argv[1:])
    source: Mapping[str, str] = os.environ if env is None else env
    out = sys.stdout if stdout is None else stdout
    err = sys.stderr if stderr is None else stderr

    dsn = _resolve_dsn(source, err)
    if dsn is None:
        return 1

    if lookup_delegated_subject is None:
        lookup_delegated_subject = _build_default_lookup(dsn)
    if seed is None:
        seed = _build_default_seed(dsn)

    subject_open_id = _resolve_subject(lookup_delegated_subject, err)
    if subject_open_id is None:
        return 1

    if not args.confirm:
        print(
            "[只读预演] 将为专用授权主体登记权限管理员/运维管理员/超级管理员三类角色"
            f"（标签 {DELEGATED_SUBJECT_LABEL}）。加 --confirm 以实际执行写入。",
            file=out,
        )
        return 0

    return _perform_seed(seed, subject_open_id, out=out, err=err)


def main() -> int:  # pragma: no cover - 由 __main__.py 调用
    return run()


__all__: tuple[str, ...] = (
    "DSN_ENV_VAR",
    "DELEGATED_SUBJECT_LABEL",
    "parse_arguments",
    "run",
    "main",
)
