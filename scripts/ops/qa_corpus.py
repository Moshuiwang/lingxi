#!/usr/bin/env python3
"""问答留存语料：读取 / 检索 / 导出 / 容量，以及窄读取角色的授予与撤销（受控运行脚本）。

## 必须在 `lingxi-scheduler` 容器内运行，且**脚本本体经 stdin 喂进去**

与 `scripts/ops/outreach.py` 同一形态、同一理由：`.dockerignore` 排除了 `scripts/`，
镜像里没有这个文件；数据库连接串与导出根只有 scheduler 容器持有。子命令与参数写在
`-` 之后：

    docker compose --env-file deploy/.env.prod \\
      -f deploy/compose.yaml -f deploy/compose.prod.yaml \\
      exec -T scheduler python -B - read --initiated-by ou_xxx --open-id ou_yyy \\
      < scripts/ops/qa_corpus.py

## 五个子命令

- `read`：按人（`--open-id`，飞书 open_id → 内部用户）、时间窗（`--since` / `--until`，
  含起不含止）、关键词（`--keyword`，问题列与用户实收正文列的子串匹配）过滤，**至少给一项**；
  `--limit` 默认 20、硬顶 200。正文只打印到标准输出，**不要在有落盘记录的会话里跑**。
- `export`：同一组过滤条件，全部命中行写成 JSON 行文件。`--out` 只能是**文件名**，文件落在
  受控导出根（环境变量 `LINGXI_QA_CORPUS_EXPORT_ROOT`，由 compose 声明在 scheduler 的
  持久卷内）正下方，`O_CREAT|O_EXCL|O_NOFOLLOW` 建 0600 文件：已存在、是符号链接、带路径
  分隔符或 `..` 一律拒绝。标准输出不作导出通道，只报文件名、行数与文件摘要。
- `stats`：行数与表总字节数，不含任何正文，不留审计。
- `grant` / `revoke`：授予 / 撤销 `--open-id` 的读取角色；发起人必须是一位生效的已登记
  管理员（三类角色全真），读取者本人**不要求**是管理员。

## 顺序（失败关闭）与退出码

鉴权 → 查询 →（导出：写文件 → 算摘要）→ **审计行提交** → 才输出 / 保留文件。任何读取
之前先过 `is_authorized_corpus_reader`：被拒时结构上没有一条语料查询发生，输出只有固定
文案。审计行写不进去：`read` 不打印任何一行，`export` 删掉刚写的文件，退出码 3。
授予 / 撤销与各自的审计行在同一事务提交，审计写不进去角色变更一起回滚。

退出码：`0` 跑完；`2` **什么都没做**（参数、鉴权、配置或目标不合格，查询本身失败也算）；
`3` 查询或文件已经发生但审计没能落下——结果已被扣住 / 文件已删除，看到 `3` 先查审计账
再决定要不要重跑。

## 审计行（运营审计账 `operation_audit`）

`corpus.read` / `corpus.export` / `corpus.reader_grant` / `corpus.reader_revoke` 各一行
「已执行」：发起人、角色快照（非管理员为空集）、执行者标签、目标种类、按人时的目标用户、
条数、摘要（读取：返回行标识有序列表的摘要，可复算「读了哪一段」；导出：文件摘要）、
证据指针（`corpus_window:<起>_<止>` 或 `corpus_export:<文件名>`）。**关键词本身不落库**，
正文与异常正文都进不去。审计账按其既有规则九十天到期，语料本身不到期。

**`--initiated-by` 是自报身份**：这道闸挡的是误操作，挡不住冒认——能在容器里执行这条
命令的人本来就能填任何一个有效 open_id；与预开通、欢迎卡两个脚本的同名闸同一性质。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

from lingxi.core.admin.operation_audit import (
    EntryPoint,
    OperationAuditEntry,
    OperationPhase,
    executor_label,
)
from lingxi.core.admin.registry import AdminRole, is_authorized_admin
from lingxi.core.ids import new_id
from lingxi.core.qa_corpus import (
    DEFAULT_READ_LIMIT,
    MAX_READ_LIMIT,
    CorpusFilter,
    checked_export_name,
    checked_read_limit,
    digest_label,
    export_evidence_ref,
    export_line,
    is_authorized_corpus_reader,
    read_result_counts,
    rows_digest,
    window_evidence_ref,
)

#: 受控导出根的环境变量名：由 compose 在 scheduler 的 `environment:` 块声明，指向持久卷内
#: 一个只有 scheduler 挂载的目录；本脚本不给默认值——没有它就不导出。
EXPORT_ROOT_VAR = "LINGXI_QA_CORPUS_EXPORT_ROOT"

#: 运营审计账上的执行者服务名（`scheduler-script:<脚本>`，同 outreach.py 的写法）、目标种类
#: 与四个操作名。
EXECUTOR_SERVICE = "scheduler-script:qa_corpus"
TARGET_KIND_CORPUS = "qa_corpus"
TARGET_KIND_READER = "qa_corpus_reader"
OPERATION_READ = "corpus.read"
OPERATION_EXPORT = "corpus.export"
OPERATION_GRANT = "corpus.reader_grant"
OPERATION_REVOKE = "corpus.reader_revoke"

#: 发起人 open_id 的形状与审计 `initiated_by` 列一致：不合形状的值在读库之前就挡住，
#: 免得查询已经发生、审计行才因形状被拒（那会变成退出码 3 而不是 2）。
_INITIATOR = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

EXIT_DONE = 0
EXIT_NOTHING_DONE = 2
EXIT_UNAUDITED = 3


class GateRejectedError(RuntimeError):
    """鉴权、参数或目标不合格：什么都没做，退出码 2。"""


class ExportRootError(RuntimeError):
    """导出根不可用或不受控：零写入，退出码 2。"""


# ---------------------------------------------------------------- 注入点（单测替换真实数据库）


def resolve_readers(dsn: str) -> Any:
    """窄读取角色表的读写口；单独成函数是给单测一个注入点。"""
    from lingxi.adapters.postgres_qa_corpus import PostgresQaCorpusReaders

    return PostgresQaCorpusReaders(dsn)


def resolve_admin_registry_lookup(dsn: str) -> Any:
    """读 ``admin_registry`` 的只读查询对象；单独成函数是给单测一个注入点。"""
    from lingxi.adapters.admin_registry import PostgresAdminRegistryLookup

    return PostgresAdminRegistryLookup(dsn)


def resolve_corpus(dsn: str) -> Any:
    """语料表的查询口；单独成函数是给单测一个注入点。"""
    from lingxi.adapters.postgres_qa_corpus import PostgresQaCorpus

    return PostgresQaCorpus(dsn)


def resolve_operation_audit(dsn: str) -> Any:
    """运营审计账的自开连接写口；单独成函数是给单测一个注入点。"""
    from lingxi.adapters.postgres_operation_audit import PostgresOperationAudit

    return PostgresOperationAudit(dsn)


def resolve_user_id(dsn: str, open_id: str) -> str | None:
    """飞书 open_id → ``app_user.id``；查无此人返回 ``None``。复用既有的同一条只读查询。"""
    from lingxi.adapters.postgres_targeted_recompute_lookup import resolve_open_id_target

    return resolve_open_id_target(dsn, open_id)


def record_audit_in_transaction(connection: Any, entry: OperationAuditEntry) -> str:
    """在调用方事务内写一行审计；授予 / 撤销用它与角色变更同事务提交。"""
    from lingxi.adapters.postgres_operation_audit import record_operation_audit

    return record_operation_audit(connection, entry)


# ---------------------------------------------------------------- 闸门


def actor_roles_snapshot(lookup: Any, open_id: str) -> frozenset[AdminRole]:
    """审计行里的角色快照：从登记表现读，没有 active 条目就是空集，不从闸门结论反推。"""
    entry = lookup.active_entry(open_id=open_id)
    return entry.roles if entry is not None else frozenset()


def reject_reader(readers: Any, open_id: str) -> str | None:
    """读取闸：返回拒绝理由，``None`` 表示这位发起人是生效的读取者。

    登记表读不出来一律失败关闭——分辨不出「不是读取者」与「库暂时读不到」时，放行的那
    一侧是整张明文语料表。
    """
    try:
        entry = readers.entry(open_id)
    except Exception as error:  # 登记表读不出来一律失败关闭
        return f"读取角色登记表不可读，未做任何操作：{type(error).__name__}"
    if not is_authorized_corpus_reader(entry):
        return "--initiated-by 给出的 open_id 不是生效的语料读取者，未做任何操作。"
    return None


def reject_admin(lookup: Any, open_id: str) -> str | None:
    """授予 / 撤销闸：发起人必须是一位生效的已登记管理员，判据与其它受控脚本同一条。"""
    try:
        authorized = is_authorized_admin(lookup.active_entry(open_id=open_id))
    except Exception as error:  # 登记表读不出来一律失败关闭
        return f"管理员登记表不可读，未做任何操作：{type(error).__name__}"
    if not authorized:
        return (
            "--initiated-by 给出的 open_id 不是一位生效的已登记管理员"
            "（admin_registry 里没有 active 条目，或三类角色没有全部授予），未做任何操作。"
        )
    return None


# ---------------------------------------------------------------- 参数


def parse_moment(text: str) -> datetime:
    """ISO 8601 时刻或日期；不带时区一律按 UTC 解释（生产主机时钟即 UTC），不猜本地时区。"""
    try:
        moment = datetime.fromisoformat(text.strip())
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"时刻必须是 ISO 8601 形式，收到 {text!r}") from error
    if moment.tzinfo is None or moment.utcoffset() is None:
        moment = moment.replace(tzinfo=UTC)
    return moment


def positive_limit(value: str) -> int:
    """``--limit`` 只接受 1..硬顶 的整数；写错就在解析阶段收口成退出码 2。"""
    try:
        return checked_read_limit(int(value))
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            f"--limit 必须是 1 到 {MAX_READ_LIMIT} 之间的整数，收到 {value!r}"
        ) from error


def _add_filter_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--open-id", default=None, help="按人：该用户的飞书 open_id")
    parser.add_argument("--since", type=parse_moment, default=None, help="时间窗起点（含）")
    parser.add_argument("--until", type=parse_moment, default=None, help="时间窗终点（不含）")
    parser.add_argument("--keyword", default=None, help="关键词：问题与实收正文的子串匹配")


_CLI_DESCRIPTION = (
    "问答留存语料的读取 / 检索 / 导出 / 容量与读取角色授予。"
    " 【硬约束】本脚本必须在 lingxi-scheduler 容器内运行，且脚本本体经 stdin 喂进去"
    "（... exec -T scheduler python -B - <子命令> ... < scripts/ops/qa_corpus.py）。"
    " 每次读取与导出各留一行审计；审计写不进去就不输出、不保留文件。"
)


def build_parser() -> argparse.ArgumentParser:
    """五个子命令；关闭前缀缩写——对一个会打印明文语料的命令，手滑半个词不等于授权。"""
    parser = argparse.ArgumentParser(description=_CLI_DESCRIPTION, allow_abbrev=False)
    parser.add_argument("--dsn", default=None, help="PostgreSQL DSN；缺省读 LINGXI_POSTGRES_DSN")
    parser.add_argument(
        "--initiated-by",
        required=True,
        dest="initiated_by",
        help="本次操作的发起人飞书 open_id，落进审计行；read / export 须是生效的语料读取者，"
        "stats 接受生效读取者或生效管理员，grant / revoke 须是生效的已登记管理员",
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="<子命令>")
    read = commands.add_parser("read", allow_abbrev=False, help="读取并打印到标准输出")
    _add_filter_arguments(read)
    read.add_argument(
        "--limit",
        type=positive_limit,
        default=DEFAULT_READ_LIMIT,
        help=f"最多打印多少行（默认 {DEFAULT_READ_LIMIT}，硬顶 {MAX_READ_LIMIT}）",
    )
    export = commands.add_parser("export", allow_abbrev=False, help="导出为 JSON 行文件")
    _add_filter_arguments(export)
    export.add_argument(
        "--out", required=True, help="文件名（不是路径），落在受控导出根正下方，须以 .jsonl 结尾"
    )
    commands.add_parser("stats", allow_abbrev=False, help="行数与表总字节数，不含正文")
    grant = commands.add_parser("grant", allow_abbrev=False, help="授予读取角色")
    grant.add_argument("--open-id", required=True, help="被授予者的飞书 open_id")
    grant.add_argument("--label", required=True, help="角色化标签（不是姓名），例如 corpus-reader")
    revoke = commands.add_parser("revoke", allow_abbrev=False, help="撤销读取角色")
    revoke.add_argument("--open-id", required=True, help="被撤销者的飞书 open_id")
    return parser


def build_filter(arguments: argparse.Namespace, *, user_id: str | None) -> CorpusFilter:
    """把命令行参数合成过滤条件；形状不合法由核心模块拒绝。"""
    keyword = arguments.keyword.strip() if arguments.keyword is not None else None
    return CorpusFilter(
        user_id=user_id, since=arguments.since, until=arguments.until, keyword=keyword
    )


# ---------------------------------------------------------------- 审计条目


def _executed_entry(
    *,
    operation: str,
    run_id: str,
    initiated_by: str,
    actor_roles: frozenset[AdminRole],
    result_code: str = "ok",
    **fields: Any,
) -> OperationAuditEntry:
    return OperationAuditEntry(
        operation_id=run_id,
        operation=operation,
        phase=OperationPhase.EXECUTED,
        initiated_by=initiated_by,
        actor_roles=actor_roles,
        entry_point=EntryPoint.OPS_SCRIPT,
        executor=executor_label(EXECUTOR_SERVICE, run_id=run_id),
        result_code=result_code,
        **fields,
    )


def read_audit_entry(
    *,
    run_id: str,
    initiated_by: str,
    actor_roles: frozenset[AdminRole],
    chosen: CorpusFilter,
    rows: Sequence[Any],
) -> OperationAuditEntry:
    """``corpus.read`` 一行：条数、返回行标识的有序摘要、按人时的目标用户、时间窗指针。"""
    return _executed_entry(
        operation=OPERATION_READ,
        run_id=run_id,
        initiated_by=initiated_by,
        actor_roles=actor_roles,
        target_kind=TARGET_KIND_CORPUS,
        target_user_id=chosen.user_id,
        target_count=len(rows),
        target_digest=rows_digest([row.id for row in rows]),
        result_counts=read_result_counts(row.record.user_id for row in rows),
        evidence_ref=window_evidence_ref(chosen.since, chosen.until),
    )


def export_audit_entry(
    *,
    run_id: str,
    initiated_by: str,
    actor_roles: frozenset[AdminRole],
    chosen: CorpusFilter,
    name: str,
    row_count: int,
    user_count: int,
    file_hexdigest: str,
) -> OperationAuditEntry:
    """``corpus.export`` 一行：条数、文件摘要、按人时的目标用户、文件名指针。"""
    return _executed_entry(
        operation=OPERATION_EXPORT,
        run_id=run_id,
        initiated_by=initiated_by,
        actor_roles=actor_roles,
        target_kind=TARGET_KIND_CORPUS,
        target_user_id=chosen.user_id,
        target_count=row_count,
        target_digest=digest_label(file_hexdigest),
        result_counts={"rows": row_count, "users": user_count},
        evidence_ref=export_evidence_ref(name),
    )


def reader_audit_entry(
    *,
    operation: str,
    run_id: str,
    initiated_by: str,
    actor_roles: frozenset[AdminRole],
    entry: Any,
    result_code: str,
) -> OperationAuditEntry:
    """``corpus.reader_grant`` / ``corpus.reader_revoke`` 一行：目标是登记行，指针指向它。"""
    return _executed_entry(
        operation=operation,
        run_id=run_id,
        initiated_by=initiated_by,
        actor_roles=actor_roles,
        result_code=result_code,
        target_kind=TARGET_KIND_READER,
        target_user_id=entry.feishu_open_id,
        target_count=1,
        evidence_ref=f"qa_corpus_reader:{entry.id}",
    )


# ---------------------------------------------------------------- 受控导出根


def export_root_from_environment(environment: Any = os.environ) -> Path:
    """受控导出根：必须由环境给出且是绝对路径；没有就不导出。"""
    raw = environment.get(EXPORT_ROOT_VAR, "")
    if not raw.strip():
        raise ExportRootError(f"未设置 {EXPORT_ROOT_VAR}，没有受控导出根，不导出。")
    root = Path(raw)
    if not root.is_absolute():
        raise ExportRootError(f"{EXPORT_ROOT_VAR} 必须是绝对路径，不导出。")
    return root


def open_export_root(root: Path) -> int:
    """打开导出根目录并返回其文件描述符；不存在则以 0700 建出来。

    ``O_DIRECTORY|O_NOFOLLOW``：根本身是符号链接就拒绝——受控的是这个目录，不是它指向
    的任何地方。属主必须是当前用户、组与其他用户不得有任何权限，否则「落地即受控」不成立。
    """
    try:
        os.mkdir(root, 0o700)
    except FileExistsError:
        pass
    except OSError as error:
        raise ExportRootError(f"导出根不可建：{type(error).__name__}") from None
    try:
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as error:
        raise ExportRootError(f"导出根不可打开：{type(error).__name__}") from None
    facts = os.fstat(root_fd)
    if facts.st_uid != os.geteuid() or facts.st_mode & 0o077:
        os.close(root_fd)
        raise ExportRootError("导出根的属主或权限不受控（须属于当前用户且为 0700），不导出。")
    return root_fd


def create_export_file(root_fd: int, name: str) -> int:
    """在导出根正下方新建 0600 文件；已存在、是符号链接一律拒绝，不覆盖任何东西。"""
    try:
        return os.open(
            name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=root_fd
        )
    except OSError as error:
        raise ExportRootError(f"导出文件不可建（{type(error).__name__}），零写入。") from None


def write_export(fd: int, rows: Iterable[Any]) -> tuple[int, int, str]:
    """把行写成 JSON 行并边写边算摘要；返回行数、涉及人数与十六进制摘要。"""
    hasher = hashlib.sha256()
    count = 0
    users: set[str] = set()
    with os.fdopen(fd, "wb") as handle:
        for row in rows:
            data = export_line(row_id=row.id, created_at=row.created_at, record=row.record).encode(
                "utf-8"
            )
            handle.write(data)
            hasher.update(data)
            count += 1
            users.add(row.record.user_id)
        handle.flush()
        os.fsync(handle.fileno())
    return count, len(users), hasher.hexdigest()


def _discard_export(root_fd: int, name: str) -> str:
    try:
        os.unlink(name, dir_fd=root_fd)
    except OSError as error:
        return f"导出文件未能删除（{type(error).__name__}），请手工清理 {name}。"
    return "导出文件已删除。"


# ---------------------------------------------------------------- 输出


def print_rows(rows: Sequence[Any], out: TextIO) -> None:
    """正文只走这里、只在审计提交之后被调用。"""
    for row in rows:
        record = row.record
        print(
            f"=== {row.id} {row.created_at.isoformat(timespec='seconds')}"
            f" {record.terminal_kind}/{record.user_result}",
            file=out,
        )
        print(
            f"问题（脱敏 {record.question_redaction_count} 处）：{record.question_content}",
            file=out,
        )
        print(
            f"实收（脱敏 {record.answer_delivered_redaction_count} 处）：{record.answer_delivered}",
            file=out,
        )
        print(
            f"模型原文（脱敏 {record.answer_model_raw_redaction_count} 处）：{record.answer_model_raw}",
            file=out,
        )
        payload = json.dumps(record.tool_calls_payload(), ensure_ascii=False, default=str)
        print(f"工具调用 {len(record.tool_calls)} 次：{payload}", file=out)


# ---------------------------------------------------------------- 子命令


def _target_user(dsn: str, arguments: argparse.Namespace) -> str | None:
    if arguments.open_id is None:
        return None
    user_id = resolve_user_id(dsn, arguments.open_id.strip())
    if user_id is None:
        raise GateRejectedError("--open-id 给出的用户在当前环境不存在，未做任何操作。")
    return user_id


def run_read(dsn: str, arguments: argparse.Namespace, *, initiated_by: str) -> int:
    """鉴权 → 查询 → 审计 → 才打印。"""
    rejection = reject_reader(resolve_readers(dsn), initiated_by)
    if rejection is not None:
        raise GateRejectedError(rejection)
    chosen = build_filter(arguments, user_id=_target_user(dsn, arguments))
    try:
        rows = resolve_corpus(dsn).search(chosen, limit=arguments.limit)
    except Exception as error:  # 查询没成功，什么都没读到
        raise GateRejectedError(f"语料查询失败：{type(error).__name__}") from None
    run_id = new_id("qcr")
    try:
        roles = actor_roles_snapshot(resolve_admin_registry_lookup(dsn), initiated_by)
        entry = read_audit_entry(
            run_id=run_id, initiated_by=initiated_by, actor_roles=roles, chosen=chosen, rows=rows
        )
        audit_id = resolve_operation_audit(dsn).record(entry)
    except Exception as error:  # 审计没落下，结果一律扣住
        print(f"审计行未能写入（{type(error).__name__}），本次读取结果未输出。", file=sys.stderr)
        return EXIT_UNAUDITED
    print_rows(rows, sys.stdout)
    print(
        f"已返回 {len(rows)} 行 · 操作号 {run_id} · 审计 {audit_id} · 证据 {entry.evidence_ref}",
        file=sys.stderr,
    )
    return EXIT_DONE


def run_export(
    dsn: str, arguments: argparse.Namespace, *, initiated_by: str, environment: Any = os.environ
) -> int:
    """鉴权 → 受控根与文件 → 分批写入并算摘要 → 审计 → 才保留文件并报告。"""
    rejection = reject_reader(resolve_readers(dsn), initiated_by)
    if rejection is not None:
        raise GateRejectedError(rejection)
    name = checked_export_name(arguments.out)
    root_fd = open_export_root(export_root_from_environment(environment))
    try:
        chosen = build_filter(arguments, user_id=_target_user(dsn, arguments))
        file_fd = create_export_file(root_fd, name)
        run_id = new_id("qcr")
        try:
            row_count, user_count, hexdigest = write_export(
                file_fd, resolve_corpus(dsn).iter_export(chosen)
            )
            roles = actor_roles_snapshot(resolve_admin_registry_lookup(dsn), initiated_by)
            entry = export_audit_entry(
                run_id=run_id,
                initiated_by=initiated_by,
                actor_roles=roles,
                chosen=chosen,
                name=name,
                row_count=row_count,
                user_count=user_count,
                file_hexdigest=hexdigest,
            )
            audit_id = resolve_operation_audit(dsn).record(entry)
        except Exception as error:  # 文件已建、审计没落下：文件不能留
            outcome = _discard_export(root_fd, name)
            print(f"导出未完成或审计未能写入（{type(error).__name__}），{outcome}", file=sys.stderr)
            return EXIT_UNAUDITED
    finally:
        os.close(root_fd)
    print(
        f"已导出 {row_count} 行 → {name} · {entry.target_digest} · 操作号 {run_id} · 审计 {audit_id}"
    )
    return EXIT_DONE


def run_stats(dsn: str, arguments: argparse.Namespace, *, initiated_by: str) -> int:
    """行数与字节；读取者或生效管理员都可看，不含正文、不留审计。"""
    if reject_reader(resolve_readers(dsn), initiated_by) is not None and (
        reject_admin(resolve_admin_registry_lookup(dsn), initiated_by) is not None
    ):
        raise GateRejectedError(
            "--initiated-by 既不是生效的语料读取者也不是生效管理员，未做任何操作。"
        )
    try:
        facts = resolve_corpus(dsn).stats()
    except Exception as error:  # 查询没成功
        raise GateRejectedError(f"容量查询失败：{type(error).__name__}") from None
    print(f"qa_corpus 行数 {facts.rows} · 表总字节 {facts.total_bytes}")
    return EXIT_DONE


def run_grant(dsn: str, arguments: argparse.Namespace, *, initiated_by: str) -> int:
    """管理员闸 → 授予与审计同事务 → 报告；重复授予幂等。"""
    lookup = resolve_admin_registry_lookup(dsn)
    rejection = reject_admin(lookup, initiated_by)
    if rejection is not None:
        raise GateRejectedError(rejection)
    open_id = arguments.open_id.strip()
    label = arguments.label.strip()
    if not open_id or not label:
        raise GateRejectedError("--open-id 与 --label 都不能为空白，未做任何操作。")
    run_id = new_id("qcr")
    roles = actor_roles_snapshot(lookup, initiated_by)

    def audit(connection: Any, entry: Any, created: bool) -> str:
        return record_audit_in_transaction(
            connection,
            reader_audit_entry(
                operation=OPERATION_GRANT,
                run_id=run_id,
                initiated_by=initiated_by,
                actor_roles=roles,
                entry=entry,
                result_code="granted" if created else "already_active",
            ),
        )

    try:
        entry, created, audit_id = resolve_readers(dsn).grant(
            open_id, label, granted_by=initiated_by, audit=audit
        )
    except Exception as error:  # 角色变更与审计同事务：任一失败整笔回滚，什么都没生效
        raise GateRejectedError(f"授予未生效、已回滚：{type(error).__name__}") from None
    outcome = "已授予读取角色" if created else "已是生效的读取者，未重复登记"
    print(
        f"{outcome}：{entry.feishu_open_id}（登记 {entry.id}）· 操作号 {run_id} · 审计 {audit_id}"
    )
    return EXIT_DONE


def run_revoke(dsn: str, arguments: argparse.Namespace, *, initiated_by: str) -> int:
    """管理员闸 → 撤销与审计同事务 → 报告；没有生效登记就什么都不做。"""
    lookup = resolve_admin_registry_lookup(dsn)
    rejection = reject_admin(lookup, initiated_by)
    if rejection is not None:
        raise GateRejectedError(rejection)
    open_id = arguments.open_id.strip()
    if not open_id:
        raise GateRejectedError("--open-id 不能为空白，未做任何操作。")
    run_id = new_id("qcr")
    roles = actor_roles_snapshot(lookup, initiated_by)

    def audit(connection: Any, entry: Any) -> str:
        return record_audit_in_transaction(
            connection,
            reader_audit_entry(
                operation=OPERATION_REVOKE,
                run_id=run_id,
                initiated_by=initiated_by,
                actor_roles=roles,
                entry=entry,
                result_code="revoked",
            ),
        )

    try:
        outcome = resolve_readers(dsn).revoke(open_id, audit=audit)
    except Exception as error:  # 同上：整笔回滚
        raise GateRejectedError(f"撤销未生效、已回滚：{type(error).__name__}") from None
    if outcome is None:
        raise GateRejectedError("该 open_id 没有生效的读取角色登记，未做任何操作。")
    entry, audit_id = outcome
    print(
        f"已撤销读取角色：{entry.feishu_open_id}（登记 {entry.id}）· 操作号 {run_id} · 审计 {audit_id}"
    )
    return EXIT_DONE


RUNNERS: dict[str, Callable[..., int]] = {
    "read": run_read,
    "export": run_export,
    "stats": run_stats,
    "grant": run_grant,
    "revoke": run_revoke,
}


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    initiated_by = arguments.initiated_by.strip()
    if not _INITIATOR.match(initiated_by):
        print("--initiated-by 不是合法的 open_id 形状，未做任何操作。", file=sys.stderr)
        return EXIT_NOTHING_DONE
    dsn = arguments.dsn or os.environ.get("LINGXI_POSTGRES_DSN")
    if not dsn:
        print("缺少 DSN：既未传 --dsn，也未设置环境变量 LINGXI_POSTGRES_DSN。", file=sys.stderr)
        return EXIT_NOTHING_DONE
    try:
        return RUNNERS[arguments.command](dsn, arguments, initiated_by=initiated_by)
    except (GateRejectedError, ExportRootError, ValueError) as error:
        print(f"未做任何操作：{error}", file=sys.stderr)
        return EXIT_NOTHING_DONE


if __name__ == "__main__":
    raise SystemExit(main())
