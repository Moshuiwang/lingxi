#!/usr/bin/env python3
"""S-A-07 受控验收夹具：只读回读一条飞书消息上的机器人表情/反应（受控验证脚本，
不属于生产镜像，只在 biai-stage 或 tz 等受控环境运行）。

**用途**：Issue #175 / #185 的验收缺口——r15/r19 真实验收里「已收到」表情在飞书
**网页端**观察不到，但网页端观察无法区分两种完全不同的失败：

1. 加表情 API 调用真的失败了（此时 gateway 审计有 ``reaction.failed``，携带飞书
   返回的 code/msg/log_id）；
2. 平台侧表情已存在，只是所用网页客户端没有渲染/没被观察到。

本脚本用飞书开放平台的**只读** reactions 列表 API
（``GET /open-apis/im/v1/messages/:message_id/reactions``，tenant token 即可）直接
回读平台事实，让上述两种情形可判定：脚本回读到 ``operator_type=app`` 的表情而
页面没有 → 渲染/观察问题；脚本回读为 0 且 gateway 日志有 ``reaction.failed`` →
API 调用失败，按日志里的 code 定位（权限、表情 key、频控等）。

**只读边界**：只调用 list 接口，不创建、不删除、不写数据库；不打印 operator 的
open_id 或任何用户标识——输出只含表情 key（``emoji_type``）、操作者类型
（``operator_type``：``app``/``user``）与计数。消息 ID 由执行者自己传入，输出里
只回显末 6 位供肉眼对齐，避免整段消息标识经复制粘贴进入工单。

**用法**（真实凭据只从环境变量读取，不出现在命令行参数里；变量名与 gateway
受控配置一致，可直接 source 同一份受控 env 文件）：

.. code-block:: bash

    export LINGXI_GATEWAY_APP_ID=...
    export LINGXI_GATEWAY_APP_SECRET=...
    PYTHONPATH=src python3 scripts/probe_message_reactions.py om_xxx

**退出码**：0 回读成功（哪怕结果是 0 个反应）；1 飞书 API 返回失败（stderr 打印
code/msg/log_id，不含凭据）；2 参数或配置错误。

**取证口径（Issue #188，2026-08-17 定稿）**——r15 曾出现「探针 0、页面有表情」的
单次不一致，排查结论：脚本无缺陷、平台最终状态一致（r15 现场消息隔日回读
``app_reactions=1``；两日 11 个采样全部与页面一致），当次 0 属读取瞬态（时机早于
反应可见，或当次目标 message_id 对应错误，二者当次证据已不可恢复）。据此验收时：

1. 对目标消息**即时 + 延时（≥60 秒）各读一次**，以延时读数为准；
2. 探针=0 且 Gateway 无 ``reaction.failed`` 时，**不得**直接判「反应缺失」——先
   延时重读，并以页面观察交叉核对；
3. 仍不一致时，先用消息列表 API 核对目标 message_id 归属（确认探的是页面观察的
   那条消息），再考虑改判。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Iterable
from typing import Any

# 出站 HTTP 超时：一次性只读探针，固定 10 秒即可；不复用 gateway 的停机预算
# 推导——这里没有停机承诺要守。
_PROBE_TIMEOUT_SECONDS = 10.0
_PAGE_SIZE = 50
# 防御性上限：分页游标异常时不允许无限翻页。
_MAX_PAGES = 20


def message_id_suffix(message_id: str) -> str:
    """只保留末 6 位供肉眼对齐，整段消息标识不进输出。

    短标识（≤6 位，真实飞书 message_id 不会这么短）无条件退化成占位符而不是
    原样回显（独立审核 F11）：这条纪律的意义在于"完整标识永远不进 stdout"，
    不在于"通常不进"。
    """

    return f"…{message_id[-6:]}" if len(message_id) > 6 else "…"


def summarize_reactions(items: Iterable[Any]) -> dict[str, Any]:
    """把 SDK 返回的 reaction 列表折叠成不含用户标识的计数摘要。

    只读取 ``reaction_type.emoji_type`` 与 ``operator.operator_type`` 两个低敏
    字段；``operator_id``/``open_id`` 一类用户标识刻意不读取，杜绝"顺手打印了
    不该打印的东西"。
    """

    total = 0
    by_key: dict[str, int] = {}
    for item in items:
        total += 1
        emoji = getattr(getattr(item, "reaction_type", None), "emoji_type", None) or "(未知)"
        operator_type = getattr(getattr(item, "operator", None), "operator_type", None) or "(未知)"
        key = f"{emoji}/{operator_type}"
        by_key[key] = by_key.get(key, 0) + 1
    return {
        "total": total,
        "by_emoji_and_operator_type": dict(sorted(by_key.items())),
        "app_reactions": sum(count for key, count in by_key.items() if key.endswith("/app")),
    }


def _list_reaction_pages(client: Any, message_id: str) -> Iterable[Any]:
    """逐页产出 reaction 条目；飞书 API 失败时抛 ``RuntimeError``（含 log_id）。"""

    from lark_oapi.api.im.v1 import ListMessageReactionRequest

    page_token: str | None = None
    for _ in range(_MAX_PAGES):
        builder = ListMessageReactionRequest.builder().message_id(message_id).page_size(_PAGE_SIZE)
        if page_token:
            builder = builder.page_token(page_token)
        response = client.im.v1.message_reaction.list(builder.build())
        if not response.success():
            raise RuntimeError(
                f"回读表情失败：code={response.code} msg={response.msg} "
                f"log_id={response.get_log_id()}"
            )
        data = response.data
        yield from getattr(data, "items", None) or []
        page_token = getattr(data, "page_token", None)
        if not getattr(data, "has_more", False) or not page_token:
            return
    raise RuntimeError(f"回读表情失败：翻页超过 {_MAX_PAGES} 页仍未结束，游标可能异常")


def main(argv: list[str] | None = None, *, client: Any = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "只读回读一条飞书消息上的表情/反应（S-A-07 r15/r19，#175/#185）。"
            "输出不含任何用户标识，只有表情 key、操作者类型与计数。"
        )
    )
    parser.add_argument("message_id", help="目标消息的 message_id（om_ 开头）")
    arguments = parser.parse_args(argv)

    message_id = arguments.message_id.strip()
    if not message_id:
        print("message_id 不能为空", file=sys.stderr)
        return 2

    if client is None:
        app_id = os.environ.get("LINGXI_GATEWAY_APP_ID", "").strip()
        app_secret = os.environ.get("LINGXI_GATEWAY_APP_SECRET", "").strip()
        if not app_id or not app_secret:
            print(
                "缺少 LINGXI_GATEWAY_APP_ID / LINGXI_GATEWAY_APP_SECRET 环境变量",
                file=sys.stderr,
            )
            return 2
        # 与重放脚本同一习惯（Issue #176）：任何可能触发真实出站的代码之前先把
        # 凭据脱敏装好。
        import logging

        logging.basicConfig(level=logging.INFO, stream=sys.stderr)
        from lingxi.apps.gateway.log_redaction import install_credential_redaction

        install_credential_redaction()

        from lingxi.adapters.feishu_outbound import build_client

        print(f"飞书应用 app_id 前缀={app_id[:10]}…", file=sys.stderr)
        client = build_client(
            app_id=app_id, app_secret=app_secret, timeout_seconds=_PROBE_TIMEOUT_SECONDS
        )

    try:
        summary = summarize_reactions(_list_reaction_pages(client, message_id))
    except Exception as error:  # noqa: BLE001 - 探针必须以文档化退出码收口（独立审核 F4）
        # lark-oapi 的传输层不包网络异常：DNS 抖动 / 超时会以 requests 异常原样
        # 穿透 list 调用，只接 RuntimeError 会让探针带着裸 traceback（可能含请求
        # URL）崩溃在文档化退出码之外，验收者无法区分"探针坏了"和"平台失败"。
        # 出口统一过自由文本脱敏并截断，RuntimeError 分支（含飞书 code/log_id）
        # 同样适用——那段文本本就不含凭据，脱敏是幂等的。
        from lingxi.core.execution.audit import redact_free_text

        print(redact_free_text(f"{type(error).__name__}: {error}")[:300], file=sys.stderr)
        return 1

    print(
        json.dumps(
            {"message_id_suffix": message_id_suffix(message_id), **summary},
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
