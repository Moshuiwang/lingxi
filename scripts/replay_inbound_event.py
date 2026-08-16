#!/usr/bin/env python3
"""S-A-07 受控验收夹具：把同一个飞书入站事件逐字节重放 N 次（受控验证脚本，
不属于生产镜像，只在 biai-stage 或 tz 等受控环境运行）。

**用途**：Issue #57 的验收缺口——``build_supervisor(config, transport=...)`` 早已
支持注入传输层（不违反「共享外部通道同一时刻只允许一个客户端」的约束，见
AGENTS.md），但没有一个可重复运行的入口把**同一个 event_id 的完整事件体**逐字节
重放若干次去证明入站幂等：第 1 次正常处理，第 2 次起必须被判定为重复。
``EventPipeline`` 用的是真实 ``PostgresGatewayStore`` 与真实飞书出站——这正是这个
脚本要验证的东西，因此不能把它们也换成假实现。

**验证边界（独立审核 P3-2）——脚本的自动判定与需要人工核对的部分不是同一件事**：

- 脚本**自动判定**的只有 ``V-接入-01``（事件级幂等去重键，数据库层面的
  ``inbound_event`` 唯一约束）：判据是本次重放是否观察到
  ``apps.gateway._LoggingAudit`` 记的 ``inbound_event.duplicate`` 审计动作
  （见下方"产出"），退出码 0 表示这一条已经用真实数据库证实。
- ``V-接入-09``（幂等断的是**出站调用次数**——不重复加表情、不重复回复，不只是
  数据库行数）**脚本不做自动判定**，必须由验收执行者在飞书侧人工回读：确认
  目标话题里只出现一次表情、第 2 轮起没有产生新的回复消息。**脚本退出码 0
  不等于 V-接入-09 已经验证**——它只证明了事件在数据库层面被判成重复，不证明
  出站调用真的只发生了一次（理论上如果生产代码在去重判定与出站调用之间的顺序
  被改坏，数据库判重仍可能通过而出站被重复触发，那正是需要人工回读飞书侧才能
  抓住的场景）。

**输入**：一个 JSON 文件路径，内容是一条 ``im.message.receive_v1`` 事件的完整
envelope（飞书回调体的原始形状），由验收执行者自备。脚本只做最小字段校验，
必须包含：

- ``header.event_id``：事件唯一标识，重放脚本原样复用这同一个值
- ``header.event_type``：必须是 ``"im.message.receive_v1"``
- ``event.sender.sender_id.open_id``：发送者 open_id（任务归属的唯一来源）
- ``event.message.message_id``
- ``event.message.chat_id``
- ``event.message.chat_type``：必须是 ``"p2p"``（问数与多轮对话只服务飞书私聊，
  群聊事件在生产入口本身就会被拒绝，重放它测不出幂等）
- ``event.message.content``：飞书消息体（一段 JSON 字符串）

**用法**（真实凭据只从环境变量读取，不出现在命令行参数里）：

.. code-block:: bash

    export LINGXI_GATEWAY_APP_ID=...
    export LINGXI_GATEWAY_APP_SECRET=...
    export LINGXI_GATEWAY_POSTGRES_DSN=...
    PYTHONPATH=src python3 scripts/replay_inbound_event.py \\
        /path/to/envelope.json --times 2

**产出**：启动时先在 stderr 打一行不含凭据的目标环境摘要（数据库 host/dbname
与 app_id 前 10 位，见 ``redacted_target_summary``，独立审核 P3-1），供执行者
肉眼核对没敲错环境；随后每一轮重放的处理结果摘要（``duplicate`` 标志 + 观察到
的审计动作名）逐行打印到 stdout。全程不打印凭据、不打印消息正文全文——摘要
只包含固定的审计动作名称字符串，取自 ``apps.gateway._LoggingAudit`` 的既有
日志（那里本就不记录消息正文，见该类文档字符串）。

**处理边界**：脚本调用的是真实的 ``build_supervisor`` 装配路径，会真的往配置的
数据库写入 ``inbound_event``/``task`` 等行，也会真的调用飞书出站接口（加表情、
回复）。只在 Bot-Test 身份与受控测试数据库上运行，不对生产库、生产飞书应用执行。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import urllib.parse
from pathlib import Path
from typing import Any, Iterator

# 只依赖生产入口已经公开的两个常量，不重新发明"什么算私聊消息"这条规则——
# 与 lingxi.adapters.feishu_events 的既有定义保持单一来源。
MESSAGE_RECEIVE_EVENT = "im.message.receive_v1"
PRIVATE_CHAT_TYPE = "p2p"


def _string_at(payload: object, *path: str) -> str | None:
    """按路径取一个非空字符串字段；缺失、类型不对或空白一律返回 ``None``。"""

    node: Any = payload
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    if not isinstance(node, str):
        return None
    stripped = node.strip()
    return stripped or None


def validate_envelope(payload: object) -> None:
    """校验重放脚本要用到的最小字段集合。

    这不是生产入口 ``parse_message_event`` 的替代品——那条真实解析路径会在
    ``build_supervisor`` 装配出的管线里被完整走一遍，是这次重放本身要验证的
    对象之一。这里只做**启动前**的一次快速把关，把"验收执行者的输入文件填错
    了"和"生产入口真的按规则拒绝了这条事件"这两种失败原因分开，避免验收
    执行者对着一条来自管线深处、上下文缺失的报错猜半天。

    收集全部问题一次性抛出，而不是命中第一个就退出：验收现场改一次 JSON 文件
    比来回跑五次脚本各改一个字段省事。
    """

    if not isinstance(payload, dict):
        raise ValueError("envelope 校验失败：整个文件必须是一个 JSON 对象")

    problems: list[str] = []

    if _string_at(payload, "header", "event_id") is None:
        problems.append("header.event_id 缺失或为空")

    event_type = _string_at(payload, "header", "event_type")
    if event_type != MESSAGE_RECEIVE_EVENT:
        problems.append(
            f"header.event_type 必须是 {MESSAGE_RECEIVE_EVENT!r}（收到 {event_type!r}）"
        )

    if _string_at(payload, "event", "sender", "sender_id", "open_id") is None:
        problems.append("event.sender.sender_id.open_id 缺失或为空")

    if _string_at(payload, "event", "message", "message_id") is None:
        problems.append("event.message.message_id 缺失或为空")

    if _string_at(payload, "event", "message", "chat_id") is None:
        problems.append("event.message.chat_id 缺失或为空")

    chat_type = _string_at(payload, "event", "message", "chat_type")
    if chat_type != PRIVATE_CHAT_TYPE:
        problems.append(
            f"event.message.chat_type 必须是 {PRIVATE_CHAT_TYPE!r}"
            f"（问数与多轮对话只服务飞书私聊，收到 {chat_type!r}）"
        )

    if _string_at(payload, "event", "message", "content") is None:
        problems.append("event.message.content 缺失、为空或不是字符串")

    if problems:
        raise ValueError("envelope 校验失败：\n  - " + "\n  - ".join(problems))


def redacted_target_summary(config: Any) -> str:
    """拼一份**不含口令、不含完整 DSN**的目标环境摘要（独立审核 P3-1）。

    验收执行者在受控环境里手动敲这个脚本，环境变量填错（比如把生产 DSN 当测试
    DSN 用）不会有任何编译期或类型检查能拦住——启动时把连的是哪个 host/dbname、
    用的是哪个飞书应用（只取 app_id 前 10 位；飞书 app_id 一律 ``cli_`` 开头，
    只取 6 位的话剩下能区分的字符太少，10 位足够肉眼辨认是哪个应用，且 app_id
    本身不是机密）打出来，供肉眼核对"这是不是我以为的那个受控环境"。只取这
    两样：口令、查询参数（可能带 ``options``/其它敏感片段）与完整 DSN 一律不进
    摘要，即便执行者把这行日志贴进工单或聊天记录也不会带出凭据。

    两类畸形 DSN 都必须退化成占位符而不是让脚本在打印这行提示的时候就崩溃或
    漏出凭据（独立审核 P3-1 追加自查）：

    - **非 URL 形态**（如 libpq 的 ``host=h port=5432 ... password=x`` 关键字/
      值写法）：``urlsplit`` 解不出 ``netloc``，把整段原样塞进 ``path``——必须
      先判 ``parsed.netloc`` 是否非空，为空就整体退化成占位符，否则会把整段
      （含 ``password=...``）当成"dbname"打出来，等于没有脱敏。
    - **端口段不是合法数字**（如 ``...@host:notaport/db``）：``urlsplit`` 切
      ``netloc`` 时不校验端口内容，只有真的访问 ``.port`` 才会抛
      ``ValueError``——不能让一份肉眼核对提示因为 DSN 里一个写错的端口就把
      整个脚本带崩溃退出。
    """

    parsed = urllib.parse.urlsplit(str(config.postgres_dsn))
    if parsed.netloc:
        # 只有真的解析出 scheme://host 形态时才信任 path 是 dbname——没有
        # netloc 时 urlsplit 会把整段畸形字符串原样塞进 path，直接拿来当
        # dbname 显示等于没有脱敏，宁可整段都退化成占位符。
        host = parsed.hostname or "(无法解析 host)"
        try:
            port = f":{parsed.port}" if parsed.port else ""
        except ValueError:
            # 端口段不是合法数字：这里只是一份肉眼核对提示，解析失败就退化
            # 成占位符，不能让脚本因为一个写错的 DSN 在打印摘要这一步崩溃。
            port = ":(无法解析 port)"
        dbname = parsed.path.lstrip("/") or "(无法解析 dbname)"
    else:
        host, port, dbname = "(无法解析 host)", "", "(无法解析 dbname)"
    app_id_prefix = (config.app_id or "")[:10] or "(空)"
    return f"目标数据库={host}{port}/{dbname} 飞书应用 app_id 前缀={app_id_prefix}…"


class _AuditCapture(logging.Handler):
    """收集 ``apps.gateway._LoggingAudit`` 记的审计动作名，供拼每轮结果摘要。

    只认它固定使用的 ``"audit %s %s"`` 格式（``apps/gateway/__init__.py``），
    不解析随行的字段字典——字段里可能带 ``event_id``/``trace_id`` 这类业务
    标识，摘要不需要它们，少读一层就少一层"顺手打印了不该打印的东西"的风险。
    action 名称本身是代码里的固定字符串常量，不含消息正文（该类文档字符串
    已经声明"这里不记录消息正文"）。
    """

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.actions: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D401 - Handler 接口
        if record.name != "lingxi.apps.gateway" or record.msg != "audit %s %s":
            return
        action = record.args[0] if record.args else None
        if isinstance(action, str):
            self.actions.append(action)


class ReplayTransport:
    """按剧本把同一个事件 envelope 产出 ``times`` 次。

    形状参照 ``tests/test_gateway_longconn.py`` 的 ``ScriptedTransport``：一次
    ``stream()`` 调用代表一次"连接会话"；这里把全部重放次数放进同一次会话里
    （真实长连接场景下，同一批事件通常也确实来自同一条连接），产完即把
    ``exhausted`` 置位，供 ``supervisor.run`` 的 ``should_stop`` 收工——不依赖
    SIGTERM，重放脚本产完剧本就该优雅退出。

    ``report``：``LongConnectionSupervisor._dispatch`` 的既有回调点（真实
    ``LarkEventTransport`` 也实现了同名方法，用于回报 ack），这里借用它在每条
    事件处理完之后打印一次结果摘要——不需要改动生产入口的 ``handle_event``
    签名就能拿到"这一轮是不是被判定为重复"。
    """

    def __init__(self, envelope: dict, *, times: int, audit: _AuditCapture) -> None:
        self._envelope = envelope
        self._times = times
        self._audit = audit
        self.connects = 0
        self.exhausted = False
        self.rounds: list[dict[str, Any]] = []

    def stream(self) -> Iterator[dict]:
        self.connects += 1
        for _ in range(self._times):
            yield self._envelope
        self.exhausted = True

    def report(self, payload: dict, error: BaseException | None) -> None:
        actions = list(self._audit.actions)
        self._audit.actions.clear()
        summary = {
            "round": len(self.rounds) + 1,
            "event_id": payload.get("header", {}).get("event_id"),
            "duplicate": "inbound_event.duplicate" in actions,
            "audit_actions": actions,
            "handler_error": None if error is None else type(error).__name__,
        }
        self.rounds.append(summary)
        print(json.dumps(summary, ensure_ascii=False))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "把同一个飞书入站事件 envelope 逐字节重放 N 次，验证入站事件级幂等"
            "（V-接入-01/09）。会真实写库、真实调用飞书出站接口，只在 Bot-Test"
            "身份与受控测试数据库上运行。"
        )
    )
    parser.add_argument("envelope_path", type=Path, help="事件 envelope 的 JSON 文件路径")
    parser.add_argument(
        "--times",
        type=int,
        default=2,
        help="重放次数（默认 2）：同一个 event_id 原样重复这么多次",
    )
    arguments = parser.parse_args(argv)

    if arguments.times < 1:
        print("--times 必须是正整数", file=sys.stderr)
        return 2

    try:
        raw = arguments.envelope_path.read_text(encoding="utf-8")
    except OSError as error:
        print(f"读取 envelope 文件失败：{error}", file=sys.stderr)
        return 2

    try:
        envelope = json.loads(raw)
    except json.JSONDecodeError as error:
        print(f"envelope 不是合法 JSON：{error}", file=sys.stderr)
        return 2

    try:
        validate_envelope(envelope)
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2

    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    # Issue #176 同一习惯：任何可能触发真实连接的代码之前先把凭据脱敏装好——
    # 这里虽然不建长连接，但 build_client 用的是同一个 lark-oapi 客户端，出站
    # 调用同样可能打印带查询参数的 URL。
    from lingxi.apps.gateway.log_redaction import install_credential_redaction

    install_credential_redaction()

    from lingxi.apps.gateway import build_supervisor
    from lingxi.apps.gateway.config import GatewayConfigError, load_config

    try:
        config = load_config(os.environ)
    except GatewayConfigError as error:
        print(f"gateway 配置不可用：{error}", file=sys.stderr)
        return 2

    # 独立审核 P3-1：启动即回显不含凭据的目标环境摘要，供执行者肉眼确认没有
    # 把这条重放对着错的库/错的飞书应用敲出去。
    print(redacted_target_summary(config), file=sys.stderr)

    audit_capture = _AuditCapture()
    logging.getLogger("lingxi.apps.gateway").addHandler(audit_capture)

    transport = ReplayTransport(envelope, times=arguments.times, audit=audit_capture)
    event_id = envelope["header"]["event_id"]
    print(
        f"开始重放 event_id={event_id} 次数={arguments.times}"
        "（同一个事件体逐字节重复，验证入站幂等）",
        file=sys.stderr,
    )

    # 不传 onboarding：幂等断的是 inbound_event 这一级去重，与被重放的用户是否
    # 已经开通无关——未开通用户重复投递同样必须被判定为重复，缺省的失败关闭
    # runner 不影响这条断言。
    supervisor = build_supervisor(config, transport=transport)
    supervisor.run(should_stop=lambda: transport.exhausted)

    duplicated_rounds = sum(1 for round_ in transport.rounds if round_["duplicate"])
    expected_duplicates = arguments.times - 1
    print(
        f"重放完成：共 {len(transport.rounds)} 轮，其中 {duplicated_rounds} 轮被判定为重复"
        f"（期望：除第 1 轮外全部重复，即 {expected_duplicates} 轮）",
        file=sys.stderr,
    )
    print(
        "注意（V-接入-01 vs V-接入-09）：以上结论只覆盖 V-接入-01——事件在数据库层面"
        "被判定为重复。V-接入-09（出站只加一次表情、只回一条）本脚本不做自动判定，"
        "退出码 0 不代表它已经验证；请到飞书侧人工回读目标话题，确认第 2 轮起没有"
        "出现新的表情或回复消息。",
        file=sys.stderr,
    )

    if len(transport.rounds) != arguments.times:
        print("警告：实际处理轮数与请求的重放次数不一致，请检查上方日志", file=sys.stderr)
        return 1
    if duplicated_rounds != expected_duplicates:
        print("警告：重复判定轮数与期望不符，V-接入-01 可能未生效，请人工核对", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
