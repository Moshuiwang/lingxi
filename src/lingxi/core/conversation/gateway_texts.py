"""gateway 出站文案的取值口。

正文唯一来源是版本化内容目录；字段保留为 ``str``，只为兼容注入式测试直接赋值。
发送前一律经 ``*_content`` 方法补回键与内容版本，审计因此只记键和版本、不记用户正文。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from lingxi.config.content import (
    ContentCatalog,
    RenderedContent,
    default_content_catalog,
)

#: 旧导出：让调用方读取固定忙碌提示时不必复制一份文案。
BUSY_HINT_TEXT = default_content_catalog().text("gateway.busy_hint").text


@dataclass(frozen=True)
class GatewayTexts:
    """gateway 会用到的用户可见文案。

    Attributes:
        busy_hint: 话题已被占用且任务已经在处理时的提示。
        busy_hint_queued: 同一个"话题被占用"状态的另一种真话——任务已经入队，但还
            没有任何 worker 领取。
        suspended: 已停用用户的提示。
        catalog: 内容目录本身，供需要按键现渲染的调用方使用。
    """

    busy_hint: str = field(
        default_factory=lambda: default_content_catalog().text("gateway.busy_hint").text
    )
    busy_hint_queued: str = field(
        default_factory=lambda: default_content_catalog().text("gateway.busy_hint_queued").text
    )
    suspended: str = field(
        default_factory=lambda: default_content_catalog().text("gateway.suspended").text
    )
    catalog: ContentCatalog = field(
        default_factory=default_content_catalog, repr=False, compare=False
    )

    def busy_hint_content(self) -> RenderedContent:
        """忙碌提示（任务已经在处理）。"""
        return _as_content(self.catalog, "gateway.busy_hint", self.busy_hint)

    def busy_hint_queued_content(self) -> RenderedContent:
        """忙碌提示（任务还在队列里等人领取）。"""
        return _as_content(self.catalog, "gateway.busy_hint_queued", self.busy_hint_queued)

    def suspended_content(self) -> RenderedContent:
        """已停用用户的提示。"""
        return _as_content(self.catalog, "gateway.suspended", self.suspended)

    def queue_failed_content(self) -> RenderedContent:
        """入队失败的诚实提示；没有注入口，直接取目录正文。"""
        return self.catalog.text("gateway.queue_failed")


def _as_content(catalog: ContentCatalog, key: str, value: str) -> RenderedContent:
    """把兼容旧注入口的字符串包成可追溯内容；未被覆盖时仍返回目录里的那一条。"""
    configured = catalog.text(key)
    if value == configured.text:
        return configured
    return RenderedContent(key=key, version=catalog.version, text=value)
