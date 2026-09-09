"""扩员三工具的固定输入与安全结果，不接受客户端身份。"""

from __future__ import annotations

import hashlib
import json
import re

PROTOCOL_VERSION = "2025-11-25"
TOOL_NAMES = ("list_innertest_members", "prepare_innertest_additions", "get_innertest_batch")


class InnertestError(ValueError):
    """受控业务错误，不携带原始资料。"""

    def __init__(self, code):
        """只有固定错误码进入协议响应。"""
        self.code = code
        super().__init__(code)


def envelope(code="ok", *, trace_id=None, state="ready", **details):
    """工具输出保持同一最小信封。"""
    return dict(
        ok=code == "ok",
        code=code,
        trace_id=trace_id,
        state=state,
        next_action="query_batch" if state == "pending" else "none",
        **details,
    )


def tool_schemas():
    """Schema 与运行时判定同时拒绝未声明的输入。"""
    props = (
        {
            "cursor": {"type": "string", "maxLength": 256},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 20},
        },
        {
            "request_key": {"type": "string", "minLength": 1, "maxLength": 128},
            "emails": {
                "type": "array",
                "minItems": 1,
                "maxItems": 20,
                "items": {"type": "string", "maxLength": 254},
            },
        },
        {
            "batch_id": {"type": "string", "minLength": 1, "maxLength": 128},
            "request_key": {"type": "string", "minLength": 1, "maxLength": 128},
        },
    )
    result = []
    for name, properties in zip(TOOL_NAMES, props, strict=True):
        schema = dict(type="object", properties=properties, additionalProperties=False)
        if name == TOOL_NAMES[1]:
            schema["required"] = ["request_key", "emails"]
        if name == TOOL_NAMES[2]:
            schema["oneOf"] = [{"required": ["batch_id"]}, {"required": ["request_key"]}]
        result.append(
            dict(
                name=name,
                description={
                    TOOL_NAMES[0]: "查询内测名单",
                    TOOL_NAMES[1]: "准备扩员，等待本人飞书确认",
                    TOOL_NAMES[2]: "查询本人批次",
                }[name],
                inputSchema=schema,
            )
        )
    return result


def validate_arguments(name, args):
    """原始人数先检查，规范化去重不能隐藏超限。"""
    if name not in TOOL_NAMES or not isinstance(args, dict):
        raise InnertestError("invalid_request")
    allowed = set(tool_schemas()[TOOL_NAMES.index(name)]["inputSchema"]["properties"])
    if set(args) - allowed:
        raise InnertestError("invalid_request")
    if name == TOOL_NAMES[0]:
        limit = args.get("limit", 20)
        if type(limit) is not int or not 1 <= limit <= 20:
            raise InnertestError("invalid_request")
    if name == TOOL_NAMES[1]:
        emails = args.get("emails")
        if not isinstance(emails, list) or not emails:
            raise InnertestError("invalid_request")
        if len(emails) > 20:
            raise InnertestError("too_many_targets")
        if any(
            not isinstance(v, str)
            or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", v.strip())
            or len(v) > 254
            for v in emails
        ):
            raise InnertestError("invalid_request")
        if "request_key" not in args:
            raise InnertestError("invalid_request")
    if name == TOOL_NAMES[2] and len(args) != 1:
        raise InnertestError("invalid_request")
    for key, value in args.items():
        if key not in {"emails", "limit"} and (
            not isinstance(value, str)
            or not value
            or len(value) > (256 if key == "cursor" else 128)
        ):
            raise InnertestError("invalid_request")
    return args


def normalized_intent(emails):
    """排序后的邮箱集合是业务幂等意图，独立于请求编号。"""
    values = tuple(sorted({email.strip().casefold() for email in emails}))
    digest = hashlib.sha256(json.dumps(values, separators=(",", ":")).encode()).hexdigest()
    return values, digest


def target_digest(targets):
    """确认绑定准备时完整解析快照，目标字段变化不能冒用旧卡。"""
    return hashlib.sha256(
        json.dumps(sorted(targets), ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
