"""扩员三工具的固定输入与安全结果，不接受客户端身份。

工具面是登记式集合：每个工具一份 :class:`ToolSpec`（名称、说明、严格 schema、入参校验），
:class:`ToolRegistry` 把若干组登记项合成受限通道对外的全集；未登记的名字在进入业务前
拒绝。本模块只登记扩员三工具，通道全集由 :mod:`lingxi.core.admin.restricted_tools` 组合。
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from functools import partial
from typing import Any

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


@dataclass(frozen=True)
class ToolSpec:
    """一个受限通道工具的登记项；schema 与运行时校验同时拒绝未声明的输入。"""

    name: str
    description: str
    input_schema: Mapping[str, Any]
    validate: Callable[[Mapping[str, Any]], Mapping[str, Any]]

    def listing(self) -> dict[str, Any]:
        """``tools/list`` 的一项；每次复制，调用方改不了登记项。"""
        return dict(
            name=self.name,
            description=self.description,
            inputSchema=copy.deepcopy(dict(self.input_schema)),
        )


class ToolRegistry:
    """名字到登记项的固定映射：重名即拒绝，构造后不可增删。"""

    def __init__(self, specs: Iterable[ToolSpec]):
        """登记顺序就是 ``tools/list`` 的顺序。"""
        self._specs: dict[str, ToolSpec] = {}
        for spec in specs:
            if spec.name in self._specs:
                raise ValueError("受限通道工具重名：" + spec.name)
            self._specs[spec.name] = spec

    @property
    def names(self) -> tuple[str, ...]:
        """已登记的工具名。"""
        return tuple(self._specs)

    def __contains__(self, name) -> bool:
        """``tools/call`` 只认登记过的名字。"""
        return name in self._specs

    def schemas(self) -> list[dict[str, Any]]:
        """``tools/list`` 的完整结果。"""
        return [spec.listing() for spec in self._specs.values()]

    def validate(self, name, args):
        """未登记名字、非对象入参与未声明字段都在进入业务前拒绝。"""
        if not isinstance(name, str) or name not in self._specs or not isinstance(args, dict):
            raise InnertestError("invalid_request")
        spec = self._specs[name]
        if set(args) - set(spec.input_schema["properties"]):
            raise InnertestError("invalid_request")
        return spec.validate(args)


def _innertest_schemas():
    """扩员三工具的严格 schema，按 ``TOOL_NAMES`` 顺序。"""
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
        result.append(schema)
    return tuple(result)


def _validate_innertest(name, args):
    """原始人数先检查，规范化去重不能隐藏超限。"""
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


INNERTEST_TOOLS = tuple(
    ToolSpec(
        name=name,
        description=description,
        input_schema=schema,
        validate=partial(_validate_innertest, name),
    )
    for name, description, schema in zip(
        TOOL_NAMES,
        ("查询内测名单", "准备扩员，等待本人飞书确认", "查询本人批次"),
        _innertest_schemas(),
        strict=True,
    )
)

INNERTEST_REGISTRY = ToolRegistry(INNERTEST_TOOLS)


def tool_schemas():
    """扩员三工具自己的 ``tools/list`` 项；通道全集另见 ``restricted_tools``。"""
    return INNERTEST_REGISTRY.schemas()


def validate_arguments(name, args):
    """扩员三工具的入参校验，名字不在这三个之内即拒绝。"""
    return INNERTEST_REGISTRY.validate(name, args)


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
