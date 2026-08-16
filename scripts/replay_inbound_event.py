#!/usr/bin/env python3
"""S-A-07 受控验收夹具：把同一个飞书入站事件逐字节重放 N 次（受控验证脚本，
不属于生产镜像，只在 biai-stage 或 tz 等受控环境运行）。

**用途**：Issue #57 的验收缺口——``build_supervisor(config, transport=...)`` 早已
支持注入传输层（不违反「共享外部通道同一时刻只允许一个客户端」的约束，见
AGENTS.md），但没有一个可重复运行的入口把**同一个 event_id 的完整事件体**逐字节
重放若干次去证明 ``V-接入-01``（事件级幂等去重键）/``V-接入-09``（幂等断的是
出站调用次数，不只是数据库行数）语义：第 1 次正常处理，第 2 次起必须被判定为
重复，不再重复入队、不再重复加表情或回复。``EventPipeline`` 用的是真实
``PostgresGatewayStore`` 与真实飞书出站——这正是这个脚本要验证的东西，因此不能
把它们也换成假实现。

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

**产出**：每一轮重放的处理结果摘要（``duplicate`` 标志 + 观察到的审计动作名），
逐行打印到 stdout；不打印凭据、不打印消息正文全文——摘要只包含固定的审计动作
名称字符串，取自 ``apps.gateway._LoggingAudit`` 的既有日志（那里本就不记录消息
正文，见该类文档字符串）。

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

    if len(transport.rounds) != arguments.times:
        print("警告：实际处理轮数与请求的重放次数不一致，请检查上方日志", file=sys.stderr)
        return 1
    if duplicated_rounds != expected_duplicates:
        print("警告：重复判定轮数与期望不符，幂等可能未生效，请人工核对", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
