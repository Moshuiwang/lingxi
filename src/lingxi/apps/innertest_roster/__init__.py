"""``python -m lingxi.apps.innertest_roster``：内测名单从静态切到数据库模式的受控导入工具。

随业务镜像交付、由运维以 ``docker exec`` 语义手动调用，形状照
``apps/admin_bootstrap``：默认只读，写入需要显式确认；标准输出面向操作者，
只回显调用方本就持有的邮箱，绝不打印任何飞书标识。

三个子命令：``plan``（只读预演，输出待写入集合与摘要）、``apply``（摘要须与
预演逐字相符才写，单事务落库并绑定专用授权主体）、``verify``（只读回读模式、
版本、导入摘要与成员数）。两份旧环境名单与待导入邮箱均以文件路径传入，命令行
本身不接受任何飞书标识。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import TextIO

from lingxi.core.admin.innertest import InnertestError
from lingxi.core.ids import new_id

DSN_ENV_VAR = "LINGXI_POSTGRES_DSN"


def _add_source_arguments(parser: argparse.ArgumentParser) -> None:
    """``plan``/``apply`` 共用的三份文件路径参数，一行一个值。"""
    parser.add_argument("--scope", required=True)
    parser.add_argument("--gateway-legacy-file", required=True)
    parser.add_argument("--scheduler-legacy-file", required=True)
    parser.add_argument("--emails-file", required=True)


def parse_arguments(argv: Sequence[str] = ()) -> argparse.Namespace:
    """三个子命令：``plan``、``apply``（多一个摘要参数）、``verify``。"""
    parser = argparse.ArgumentParser(prog="python -m lingxi.apps.innertest_roster")
    sub = parser.add_subparsers(dest="command", required=True)

    _add_source_arguments(sub.add_parser("plan"))

    apply_parser = sub.add_parser("apply")
    _add_source_arguments(apply_parser)
    apply_parser.add_argument("--confirm-digest", required=True)

    verify_parser = sub.add_parser("verify")
    verify_parser.add_argument("--scope", required=True)
    verify_parser.add_argument("--binding-id", default=None)

    return parser.parse_args(list(argv))


def _read_list_file(path: str, err: TextIO) -> tuple[str, ...] | None:
    """一行一个值，``#`` 起的行与空行忽略；缺文件或不可读时报错返回 ``None``。"""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as error:
        print(f"读取文件失败：{path}（{type(error).__name__}）", file=err)
        return None
    lines = (line.strip() for line in text.splitlines())
    return tuple(line for line in lines if line and not line.startswith("#"))


def _load_sources(
    args: argparse.Namespace, err: TextIO
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]] | None:
    """依次读取两份旧名单与邮箱名单；任一份失败即整体返回 ``None``，零写入。"""
    gateway = _read_list_file(args.gateway_legacy_file, err)
    scheduler = _read_list_file(args.scheduler_legacy_file, err)
    emails = _read_list_file(args.emails_file, err)
    if gateway is None or scheduler is None or emails is None:
        return None
    return gateway, scheduler, emails


def _resolve_dsn(source: Mapping[str, str], err: TextIO) -> str | None:
    """读连接串环境变量；缺失时报错并返回 ``None``。"""
    dsn = (source.get(DSN_ENV_VAR) or "").strip()
    if not dsn:
        print(f"缺少数据库连接串环境变量 {DSN_ENV_VAR}", file=err)
        return None
    return dsn


def _print_rejection(error: InnertestError, err: TextIO) -> None:
    """把拒绝码与逐条原因打给操作者；除调用方本就给出的邮箱外不回显标识。"""
    print(f"拒绝：{error.code}", file=err)
    for email, reason in getattr(error, "detail", ()):
        print(f"  - {email}: {reason}", file=err)


def _default_plan_import() -> Callable:
    from lingxi.adapters.postgres_innertest_roster import plan_roster_import

    return plan_roster_import


def _default_apply_import() -> Callable:
    from lingxi.adapters.postgres_innertest_roster import apply_roster_import

    return apply_roster_import


def _default_verify_status() -> Callable:
    from lingxi.adapters.postgres_innertest_roster import roster_import_status

    return roster_import_status


def _default_binding_status() -> Callable:
    from lingxi.adapters.postgres_innertest_roster import binding_status

    return binding_status


def _default_lookup_delegated_subject(dsn: str) -> Callable[[], str | None]:
    from lingxi.adapters.delegated_subject_lookup import registered_delegated_subject_open_id

    def lookup() -> str | None:
        return registered_delegated_subject_open_id(dsn)

    return lookup


def _cmd_plan(
    args: argparse.Namespace, dsn: str, out: TextIO, err: TextIO, plan_import: Callable
) -> int:
    """只读预演：加载三份文件、算摘要、打印待写入集合；不触发任何写入。"""
    sources = _load_sources(args, err)
    if sources is None:
        return 2
    gateway, scheduler, emails = sources
    try:
        plan = plan_import(
            dsn,
            scope=args.scope,
            gateway_legacy=gateway,
            scheduler_legacy=scheduler,
            emails=emails,
        )
    except InnertestError as error:
        _print_rejection(error, err)
        return 1
    except Exception as error:
        print(f"预演失败：{type(error).__name__}", file=err)
        return 1
    print(f"[只读预演] scope={args.scope} 待写入成员：{len(plan.members)} 人", file=out)
    print(f"摘要：{plan.digest}", file=out)
    for email, _open_id in plan.members:
        print(f"  - {email}", file=out)
    return 0


def _cmd_apply(
    args: argparse.Namespace,
    dsn: str,
    out: TextIO,
    err: TextIO,
    apply_import: Callable,
    lookup_delegated_subject: Callable[[], str | None],
) -> int:
    """摘要须与调用方给出的逐字相符才写；专用授权主体解析异常同样零写入。"""
    sources = _load_sources(args, err)
    if sources is None:
        return 2
    gateway, scheduler, emails = sources
    try:
        admin_open_id = lookup_delegated_subject()
    except Exception as error:
        print(f"读取专用授权主体登记失败：{type(error).__name__}", file=err)
        return 1
    binding_id = new_id("iab")
    try:
        plan = apply_import(
            dsn,
            scope=args.scope,
            gateway_legacy=gateway,
            scheduler_legacy=scheduler,
            emails=emails,
            confirm_digest=args.confirm_digest,
            admin_open_id=admin_open_id,
            binding_id=binding_id,
        )
    except InnertestError as error:
        _print_rejection(error, err)
        return 1
    except Exception as error:
        print(f"写入失败：{type(error).__name__}", file=err)
        return 1
    print(f"已写入 scope={args.scope} 成员：{len(plan.members)} 人", file=out)
    print(f"摘要：{plan.digest}", file=out)
    print(f"绑定标识：{binding_id}", file=out)
    return 0


def _cmd_verify(
    args: argparse.Namespace,
    dsn: str,
    out: TextIO,
    err: TextIO,
    verify_status: Callable,
    binding_status: Callable,
) -> int:
    """只读回读模式、版本、导入摘要与成员数；给了绑定标识则一并回读绑定状态。"""
    try:
        status = verify_status(dsn, scope=args.scope)
    except Exception as error:
        print(f"回读失败：{type(error).__name__}", file=err)
        return 1
    payload = dict(ok=True, schema_revision=1, scope=args.scope, **status)
    exit_code = 0
    if args.binding_id:
        try:
            payload["binding"] = binding_status(dsn, scope=args.scope, binding_id=args.binding_id)
        except Exception as error:
            # 调用方点名要回读绑定，那一半没读成就不是一次成功的核验；名单那半的
            # 结果照旧输出，但整体判失败，免得部署步骤靠退出码或 ok 放行。
            payload["ok"] = False
            payload["binding_error"] = type(error).__name__
            exit_code = 1
    print(json.dumps(payload, ensure_ascii=False), file=out)
    return exit_code


def run(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    lookup_delegated_subject: Callable[[], str | None] | None = None,
    plan_import: Callable | None = None,
    apply_import: Callable | None = None,
    verify_status: Callable | None = None,
    binding_status: Callable | None = None,
) -> int:
    """解析参数并分派到三个子命令；全部数据库依赖均可注入，供测试替身使用。"""
    args = parse_arguments(argv if argv is not None else sys.argv[1:])
    source: Mapping[str, str] = os.environ if env is None else env
    out = sys.stdout if stdout is None else stdout
    err = sys.stderr if stderr is None else stderr

    dsn = _resolve_dsn(source, err)
    if dsn is None:
        return 1

    if args.command == "plan":
        return _cmd_plan(args, dsn, out, err, plan_import or _default_plan_import())
    if args.command == "apply":
        lookup = lookup_delegated_subject or _default_lookup_delegated_subject(dsn)
        return _cmd_apply(args, dsn, out, err, apply_import or _default_apply_import(), lookup)
    return _cmd_verify(
        args,
        dsn,
        out,
        err,
        verify_status or _default_verify_status(),
        binding_status or _default_binding_status(),
    )


def main() -> int:  # pragma: no cover - 由 __main__.py 调用
    """入口封装，交给 `__main__.py` 调用。"""
    return run()


__all__: tuple[str, ...] = (
    "DSN_ENV_VAR",
    "parse_arguments",
    "run",
    "main",
)
