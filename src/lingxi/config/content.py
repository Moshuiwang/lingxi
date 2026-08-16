"""随制品发布的中文展示内容与卡片展示字段。

内容目录只描述用户看到的文字和卡片展示字段；动作标识、跳转地址、按钮类型、权限
判断与卡片结构仍由调用方的业务代码决定。目录在加载时失败关闭，避免缺键或错误配置
退化成空白卡片、键名回显或错误提示。

``core`` 只消费这里返回的不可变 ``RenderedContent``，不读取用户正文或凭据。内容键和
版本是出站审计的追溯事实，不把渲染后的正文写入审计。
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from string import Formatter
from typing import Any

CONTENT_PATH = Path(__file__).with_name("content.toml")
CONTENT_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class ContentError(ValueError):
    """内容目录或渲染请求不满足失败关闭约束。"""


class ContentValidationError(ContentError):
    """内容目录结构不合法。"""


class ContentRenderError(ContentError):
    """调用方提供的占位变量与模板声明不一致。"""


class ContentSafetyError(ContentError):
    """用户可见内容包含内部过程或不应误导用户的固定表达。"""


# 这些键构成当前 Story 的登记表。增加一个键必须同时增加调用方与测试，不能让配置
# 文件悄悄长出没人消费的文案；调整文字本身则只需修改 content.toml。
REQUIRED_TEXT_KEYS: tuple[str, ...] = (
    "onboarding.checking",
    "onboarding.matched",
    "onboarding.completed",
    "onboarding.not_authorized",
    "onboarding.sync_timeout",
    "onboarding.internal_error",
    "onboarding.delegated_subject",
    "gateway.busy_hint",
    "gateway.suspended",
    "gateway.queue_failed",
    "gateway.delivery_expired",
    "gateway.new_session",
    "worker.action.processing",
    "worker.action.completed",
    "worker.status",
    "worker.queued_timeout",
    "worker.version_unavailable",
    "worker.running_timeout",
    "worker.max_turns",
    "worker.stopped",
    "worker.stopped_result",
    "worker.failed",
    "worker.side_effect_uncertain",
    "worker.context_too_long",
    "worker.redacted_withheld",
    "roster.report_title",
    "roster.header",
    "roster.daily_report",
    "roster.summary",
    "roster.disclaimer",
    "roster.handover_mark",
    "roster.section.changed",
    "roster.section.removed",
    "roster.section.ambiguous",
    "roster.note.changed",
    "roster.note.removed",
    "roster.note.ambiguous",
    "roster.section_heading",
    "roster.section_heading_with_note",
    "roster.entry",
    "roster.entry_handover",
    "roster.field.display_name",
    "roster.field.employee_no",
    "roster.field.email",
)

REQUIRED_CARD_KEYS: tuple[str, ...] = (
    "query.status",
    "query.result",
    "query.failure",
    "query.empty",
)

_CARD_FIELDS = frozenset({"title", "body", "button_labels"})
_UNSAFE_FIXED_MARKERS: tuple[str, ...] = (
    "权限不足",
    "无权限",
    "没有权限",
    "预计剩余",
    "剩余时间",
    "还需",
    "mcp__",
    "pretooluse",
    "posttooluse",
    "posttoolfailure",
    "tool_use_id",
    "trace_id=",
    "过程日志",
)
_PROCESS_MARKER_PATTERN = re.compile(
    r"(?:\b(?:trace_id|tool_use_id|pretooluse|posttooluse|posttoolfailure)\b|mcp__)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class RenderedContent:
    """一次已渲染的固定内容，带有可写入审计的键和版本。"""

    key: str
    version: str
    text: str


@dataclass(frozen=True)
class RenderedCard:
    """只包含展示字段的卡片结果；动作由正式卡片构造代码另行固定。"""

    key: str
    version: str
    title: str
    body: str
    button_labels: tuple[str, ...]

    @property
    def display_fields(self) -> Mapping[str, object]:
        """返回目录允许改变的展示字段，不暴露动作、URL 或权限字段。"""

        return {
            "title": self.title,
            "body": self.body,
            "button_labels": self.button_labels,
        }


@dataclass(frozen=True)
class _TextTemplate:
    key: str
    template: str
    variables: frozenset[str]


@dataclass(frozen=True)
class _CardTemplate:
    key: str
    title: _TextTemplate
    body: _TextTemplate
    button_labels: tuple[_TextTemplate, ...]
    variables: frozenset[str]


class ContentCatalog:
    """经完整校验的内容目录。实例创建后不再读取或修改目录文件。"""

    def __init__(
        self,
        *,
        version: str,
        texts: Mapping[str, _TextTemplate],
        cards: Mapping[str, _CardTemplate],
    ) -> None:
        self.version = version
        self._texts = dict(texts)
        self._cards = dict(cards)

    @classmethod
    def from_mapping(cls, document: Mapping[str, Any]) -> "ContentCatalog":
        """从 TOML 等价映射构造并完整校验目录。

        测试通过这个入口喂缺键、多余键和危险卡片字段，证明失败发生在任何渲染之前。
        错误只包含键名和字段名，不拼接变量值。
        """

        if not isinstance(document, Mapping):
            raise ContentValidationError("内容目录必须是对象")

        if set(document) != {"meta", "texts", "cards"}:
            raise ContentValidationError("内容目录顶层必须只有 meta、texts、cards")

        meta = document["meta"]
        if not isinstance(meta, Mapping) or set(meta) != {"version"}:
            raise ContentValidationError("内容目录 meta 必须只包含 version")
        version = meta.get("version")
        if not isinstance(version, str) or not version.strip() or not CONTENT_VERSION_PATTERN.fullmatch(version.strip()):
            raise ContentValidationError("内容目录 version 必须是非空的安全版本标识")
        version = version.strip()

        raw_texts = document["texts"]
        if not isinstance(raw_texts, Mapping):
            raise ContentValidationError("内容目录 texts 必须是对象")
        cls._require_exact_keys(raw_texts, REQUIRED_TEXT_KEYS, "文案")
        texts: dict[str, _TextTemplate] = {}
        for key in REQUIRED_TEXT_KEYS:
            value = raw_texts[key]
            if not isinstance(value, str):
                raise ContentValidationError(f"文案键 {key} 的值必须是文本")
            texts[key] = _make_template(key, value)

        raw_cards = document["cards"]
        if not isinstance(raw_cards, Mapping):
            raise ContentValidationError("内容目录 cards 必须是对象")
        cls._require_exact_keys(raw_cards, REQUIRED_CARD_KEYS, "卡片")
        cards: dict[str, _CardTemplate] = {}
        for key in REQUIRED_CARD_KEYS:
            raw_card = raw_cards[key]
            if not isinstance(raw_card, Mapping):
                raise ContentValidationError(f"卡片 {key} 必须是对象")
            if set(raw_card) != _CARD_FIELDS:
                raise ContentValidationError(f"卡片 {key} 只能包含 title、body、button_labels")

            title = raw_card["title"]
            body = raw_card["body"]
            labels = raw_card["button_labels"]
            if not isinstance(title, str) or not isinstance(body, str):
                raise ContentValidationError(f"卡片 {key} 的 title 和 body 必须是文本")
            if not isinstance(labels, list) or not all(isinstance(label, str) for label in labels):
                raise ContentValidationError(f"卡片 {key} 的 button_labels 必须是文本数组")

            title_template = _make_template(f"{key}.title", title)
            body_template = _make_template(f"{key}.body", body)
            label_templates = tuple(
                _make_template(f"{key}.button_labels", label) for label in labels
            )
            variables = frozenset(
                set(title_template.variables)
                | set(body_template.variables)
                | {name for label in label_templates for name in label.variables}
            )
            _validate_fixed_template_safety((title, body, *labels), key)
            cards[key] = _CardTemplate(
                key=key,
                title=title_template,
                body=body_template,
                button_labels=label_templates,
                variables=variables,
            )

        return cls(version=version, texts=texts, cards=cards)

    @classmethod
    def from_file(cls, path: Path = CONTENT_PATH) -> "ContentCatalog":
        """读取随包发布的 TOML 文件；文件损坏时不创建部分可用目录。"""

        try:
            with path.open("rb") as stream:
                document = tomllib.load(stream)
        except (OSError, tomllib.TOMLDecodeError) as error:
            raise ContentValidationError("内容目录无法加载") from error
        return cls.from_mapping(document)

    @classmethod
    def _require_exact_keys(
        cls, values: Mapping[str, Any], required: Sequence[str], kind: str
    ) -> None:
        required_set = set(required)
        actual_set = set(values)
        if actual_set != required_set:
            missing = sorted(required_set - actual_set)
            extra = sorted(actual_set - required_set)
            details: list[str] = []
            if missing:
                details.append("缺少 " + ",".join(missing))
            if extra:
                details.append("多余 " + ",".join(extra))
            raise ContentValidationError(f"{kind}键登记不完整：" + "；".join(details))

    def text(
        self,
        key: str,
        variables: Mapping[str, object] | None = None,
        **values: object,
    ) -> RenderedContent:
        """按语义键渲染文案，调用方变量集合必须与模板集合相等。"""

        template = self._texts.get(key)
        if template is None:
            raise ContentRenderError("未登记的文案键")
        provided = _merge_variables(variables, values)
        rendered = _render_template(template, provided)
        _validate_user_visible_text(rendered, internal_terms=())
        return RenderedContent(key=key, version=self.version, text=rendered)

    def card(
        self,
        key: str,
        variables: Mapping[str, object] | None = None,
        *,
        internal_terms: Sequence[str] = (),
        **values: object,
    ) -> RenderedCard:
        """渲染只含展示字段的卡片，并在出口拦截内部过程文本。"""

        template = self._cards.get(key)
        if template is None:
            raise ContentRenderError("未登记的卡片键")
        provided = _merge_variables(variables, values)
        if frozenset(provided) != template.variables:
            _raise_variable_mismatch(template.variables, frozenset(provided))

        title = _format_template(template.title.template, provided)
        body = _format_template(template.body.template, provided)
        labels = tuple(_format_template(label.template, provided) for label in template.button_labels)
        for rendered in (title, body, *labels):
            _validate_user_visible_text(rendered, internal_terms=internal_terms)
        return RenderedCard(
            key=key,
            version=self.version,
            title=title,
            body=body,
            button_labels=labels,
        )

    def text_keys(self) -> tuple[str, ...]:
        return tuple(self._texts)

    def card_keys(self) -> tuple[str, ...]:
        return tuple(self._cards)


def _make_template(key: str, template: str) -> _TextTemplate:
    if not template and key not in {"roster.note.changed"}:
        raise ContentValidationError(f"文案键 {key} 不能为空")
    variables = _template_variables(template, key)
    _validate_fixed_template_safety((template,), key)
    return _TextTemplate(key=key, template=template, variables=variables)


def _template_variables(template: str, key: str) -> frozenset[str]:
    try:
        parts = tuple(Formatter().parse(template))
    except ValueError as error:
        raise ContentValidationError(f"文案键 {key} 的占位格式不合法") from error

    variables: set[str] = set()
    for _literal, field_name, format_spec, conversion in parts:
        if field_name is None:
            continue
        if not field_name.isidentifier() or field_name.startswith("_"):
            raise ContentValidationError(f"文案键 {key} 的占位名不合法")
        if format_spec or conversion:
            raise ContentValidationError(f"文案键 {key} 不允许占位格式化指令")
        variables.add(field_name)
    return frozenset(variables)


def _merge_variables(
    variables: Mapping[str, object] | None, values: Mapping[str, object]
) -> dict[str, object]:
    merged = dict(variables or {})
    overlap = set(merged) & set(values)
    if overlap:
        raise ContentRenderError("占位变量重复提供")
    merged.update(values)
    return merged


def _render_template(template: _TextTemplate, values: Mapping[str, object]) -> str:
    provided = frozenset(values)
    if provided != template.variables:
        _raise_variable_mismatch(template.variables, provided)
    return _format_template(template.template, values)


def _raise_variable_mismatch(expected: frozenset[str], provided: frozenset[str]) -> None:
    missing = sorted(expected - provided)
    extra = sorted(provided - expected)
    details: list[str] = []
    if missing:
        details.append("缺少 " + ",".join(missing))
    if extra:
        details.append("多余 " + ",".join(extra))
    raise ContentRenderError("占位变量集合不匹配：" + "；".join(details))


def _format_template(template: str, values: Mapping[str, object]) -> str:
    # 先转成文本，避免格式化异常把调用方传入的资料值拼进错误信息。
    safe_values = {key: str(value) for key, value in values.items()}
    try:
        return template.format_map(safe_values)
    except (KeyError, ValueError) as error:  # 目录校验已覆盖，作为运行时失败关闭
        raise ContentRenderError("文案渲染失败") from error


def _validate_fixed_template_safety(templates: Sequence[str], key: str) -> None:
    for template in templates:
        try:
            _validate_user_visible_text(template, internal_terms=())
        except ContentSafetyError as error:
            raise ContentValidationError(f"内容键 {key} 含不允许的用户可见过程信息") from error


def _validate_user_visible_text(text: str, *, internal_terms: Sequence[str]) -> None:
    lowered = text.casefold()
    if _PROCESS_MARKER_PATTERN.search(text):
        raise ContentSafetyError("用户可见内容包含内部过程标识")
    markers = (*_UNSAFE_FIXED_MARKERS, *(term.casefold() for term in internal_terms if term))
    if any(marker.casefold() in lowered for marker in markers):
        raise ContentSafetyError("用户可见内容包含内部过程或误导性权限表达")


@lru_cache(maxsize=1)
def default_content_catalog() -> ContentCatalog:
    """加载当前制品的唯一内容版本；失败时不返回降级目录。"""

    return ContentCatalog.from_file()


def validate_user_visible_text(text: str, *, internal_terms: Sequence[str] = ()) -> str:
    """供未来问数卡片/文本出口复用的输出侧检查器。"""

    if not isinstance(text, str):
        raise ContentSafetyError("用户可见内容必须是文本")
    _validate_user_visible_text(text, internal_terms=internal_terms)
    return text


__all__ = [
    "CONTENT_PATH",
    "ContentCatalog",
    "ContentError",
    "ContentRenderError",
    "ContentSafetyError",
    "ContentValidationError",
    "RenderedCard",
    "RenderedContent",
    "REQUIRED_CARD_KEYS",
    "REQUIRED_TEXT_KEYS",
    "default_content_catalog",
    "validate_user_visible_text",
]
