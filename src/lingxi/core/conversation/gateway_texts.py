"""gateway 出站文案的取值口。

内容全部来自版本化内容目录；字段保留为 ``str`` 只为兼容注入式测试直接赋值。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from lingxi.config.content import (
    ContentCatalog,
    RenderedContent,
    default_content_catalog,
)

# 对外保留这个旧导出，避免调用方为读取固定提示而复制文案；正文唯一来源是内容目录。
BUSY_HINT_TEXT = default_content_catalog().text("gateway.busy_hint").text


@dataclass(frozen=True)
class GatewayTexts:
    """用户可见文案。

    文字字段保留为 ``str``，兼容现有注入式测试；默认值来自版本化内容目录。发送前通过
    ``*_content`` 方法补回键和版本，审计不记录渲染后的用户正文。
    """

    busy_hint: str = field(
        default_factory=lambda: default_content_catalog().text("gateway.busy_hint").text
    )
    # Issue #465（rc22 S-3）：同一个"话题被占用"状态的另一种真话——任务已经入队，
    # 但还没有任何 worker 领取（`task.status == 'queued'`）。默认值同样来自内容
    # 目录，保留字符串字段是为了跟 ``busy_hint`` 一样兼容既有注入式测试直接赋值。
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
        return _as_content(self.catalog, "gateway.busy_hint", self.busy_hint)

    def busy_hint_queued_content(self) -> RenderedContent:
        return _as_content(self.catalog, "gateway.busy_hint_queued", self.busy_hint_queued)

    def suspended_content(self) -> RenderedContent:
        return _as_content(self.catalog, "gateway.suspended", self.suspended)

    def queue_failed_content(self) -> RenderedContent:
        return self.catalog.text("gateway.queue_failed")


def _as_content(catalog: ContentCatalog, key: str, value: str) -> RenderedContent:
    """把兼容旧注入口的字符串包成可追溯内容；默认值仍来自目录。"""

    configured = catalog.text(key)
    if value == configured.text:
        return configured
    return RenderedContent(key=key, version=catalog.version, text=value)
