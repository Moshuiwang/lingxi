"""宿主机外置的用户可见文案覆盖文件；不配置这个变量时行为与镜像内逐字相同。

载体沿「公司+职能→指标名」映射与 worker 运行时提示词同一先例：宿主机
``runtime-config`` 目录只读挂进容器，容器内文件路径由
``LINGXI_CONTENT_OVERRIDE_PATH`` 指定，三个常驻进程同名同值。**生效时机是进程
启动**，编辑后需重启容器；刻意不做渲染前热重载——``core`` 侧存在模块级就持有
目录的消费点，热重载只会让同一进程内两条路径读到同一句话的两个版本。

只覆盖 ``texts`` 的正文：键集合、占位符集合、错误码与 ``meta.version`` 一律不可
改。坏文件整份忽略、退回镜像内文案，并留一条结构化错误日志（只有原因码与路径，
正文不进日志）——文案不是安全边界，让一份写坏的运维文件把进程拖停是更坏的结果。
缺文件与未配变量都不是故障：删文件正是登记在案的回滚手段。
"""

from __future__ import annotations

import hashlib
import logging
import os
import stat
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path

from lingxi.config.content import (
    CONTENT_PATH,
    ContentCatalog,
    ContentError,
    ContentRenderError,
    ContentSafetyError,
    ContentValidationError,
    text_placeholders,
    validate_user_visible_text,
)

logger = logging.getLogger(__name__)

#: 外置覆盖文件的容器内路径变量名，**全仓库只在这里登记一次**：三个常驻进程读
#: 同一份文件，要么都配同一个值、要么都不配。
CONTENT_OVERRIDE_PATH_ENV = "LINGXI_CONTENT_OVERRIDE_PATH"

#: 覆盖文件的大小上限。整份文案目录本身只有几十 KB，256 KB 已经宽出一个数量级；
#: 设上限是因为这条路径由宿主机文件决定，指错到一个巨大文件时进程会在启动阶段把它
#: 整个读进内存。
MAX_OVERRIDE_BYTES = 262144

REASON_INVALID_PATH = "invalid_path"
REASON_UNREADABLE = "unreadable"
REASON_INVALID_TOML = "invalid_toml"
REASON_UNKNOWN_SECTION = "unknown_section"
REASON_UNKNOWN_KEY = "unknown_key"
REASON_INVALID_VALUE = "invalid_value"
REASON_PLACEHOLDER_MISMATCH = "placeholder_mismatch"
REASON_UNSAFE_TEXT = "unsafe_text"


class ContentOverrideError(ContentError):
    """外置覆盖文件被整份拒绝；``reason`` 是分类原因码，消息里没有文件正文。"""

    def __init__(self, reason: str, detail: str = "") -> None:
        """记下分类原因码，供日志、管理群告警与校验命令共用同一套口径。

        ``detail`` 只放不含取值的补充说明（例如"必须是绝对路径"），供人当场知道
        该怎么改；文件正文与环境变量取值一律不进消息。
        """
        super().__init__(f"外置文案覆盖文件被整份拒绝：{reason}{detail}")
        self.reason = reason


@dataclass(frozen=True)
class ContentSource:
    """本进程实际在用的内容目录及其来源事实（不含任何正文）。"""

    catalog: ContentCatalog
    digest: str
    override_path: str | None = None
    override_digest: str | None = None
    override_keys: tuple[str, ...] = ()
    rejection: str | None = None


def parse_override_path(raw: str | None) -> Path | None:
    """解释 :data:`CONTENT_OVERRIDE_PATH_ENV`：未配置或空白即 ``None``（零变化）。

    取值含空白字符、或不是绝对路径时拒绝，只报变量名、不回显取到的值（同
    ``adapters/company_function_metric_map_file.parse_metric_map_path`` 先例）。
    **必须绝对**：相对路径的含义取决于进程的工作目录，三个常驻进程与运维手里的校验
    命令各自在不同目录下跑，同一个取值会指到不同的文件。文件是否存在、形态与内容是否
    合法不在这里判定。
    """
    value = (raw or "").strip()
    if not value:
        return None
    if any(character.isspace() for character in value):
        raise ContentOverrideError(
            REASON_INVALID_PATH,
            f"（环境变量 {CONTENT_OVERRIDE_PATH_ENV} 不得包含空白字符，不回显取到的值）",
        )
    path = Path(value)
    if not path.is_absolute():
        raise ContentOverrideError(
            REASON_INVALID_PATH,
            f"（环境变量 {CONTENT_OVERRIDE_PATH_ENV} 必须是绝对路径，不回显取到的值）",
        )
    return path


def read_override_document(path: Path) -> tuple[Mapping[str, object], bytes] | None:
    """读并解析覆盖文件；文件不存在返回 ``None``。

    "文件不存在"与"文件坏了"必须分开：前者是运维删文件回滚后的正常状态，后者
    要告警。二者混成一种状态会让回滚每次都刷一条假告警，真告警随之被无视。

    **先看形态再读**：目录、FIFO 与设备文件都不是一份文案，其中 FIFO 一旦打开就会
    把启动阻塞在那里；超过 :data:`MAX_OVERRIDE_BYTES` 的文件同样整份拒绝，不把它读
    进内存。``stat`` 跟随符号链接，因此指向普通文件的软链是允许的（运维用软链切换
    版本是既有做法）。
    """
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ContentOverrideError(REASON_UNREADABLE) from error
    if not stat.S_ISREG(info.st_mode):
        raise ContentOverrideError(REASON_INVALID_PATH, "（不是一个普通文件）")
    if info.st_size > MAX_OVERRIDE_BYTES:
        raise ContentOverrideError(REASON_INVALID_PATH, f"（超过 {MAX_OVERRIDE_BYTES} 字节上限）")
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ContentOverrideError(REASON_UNREADABLE) from error
    try:
        return tomllib.loads(raw.decode("utf-8")), raw
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ContentOverrideError(REASON_INVALID_TOML) from error


def build_override_texts(document: Mapping[str, object], catalog: ContentCatalog) -> dict[str, str]:
    """按分类判据校验一份覆盖文档，返回可叠加的正文映射。

    任何一条不合格都整份拒绝，不做"跳过坏的那条、用剩下的"——部分生效会让运维
    以为改动全部落地，而用户看到的是两版文案的混合。
    """
    if set(document) - {"texts"}:
        raise ContentOverrideError(REASON_UNKNOWN_SECTION)
    raw_texts = document.get("texts", {})
    if not isinstance(raw_texts, Mapping):
        raise ContentOverrideError(REASON_UNKNOWN_SECTION)
    return {
        str(key): _checked_override(str(key), value, catalog) for key, value in raw_texts.items()
    }


def _checked_override(key: str, value: object, catalog: ContentCatalog) -> str:
    """单条覆盖的四类判据：键、值形状、内容安全、占位符集合。

    占位符集合用镜像内同一个解析器现算两边再比对，不信任任何登记表——rc25 抓到
    的 P1 正是"占位符没被传值导致渲染必然失败且一次性标志被烧掉"。
    """
    base = catalog.text_template(key)
    if base is None:
        raise ContentOverrideError(REASON_UNKNOWN_KEY)
    if not isinstance(value, str):
        raise ContentOverrideError(REASON_INVALID_VALUE)
    try:
        validate_user_visible_text(value)
    except ContentSafetyError as error:
        raise ContentOverrideError(REASON_UNSAFE_TEXT) from error
    try:
        placeholders = text_placeholders(key, value)
    except ContentValidationError as error:
        raise ContentOverrideError(REASON_INVALID_VALUE) from error
    if placeholders != text_placeholders(key, base):
        raise ContentOverrideError(REASON_PLACEHOLDER_MISMATCH)
    return value


def apply_override_document(
    document: Mapping[str, object], catalog: ContentCatalog
) -> tuple[ContentCatalog, dict[str, str]]:
    """校验一份覆盖文档并叠加到目录上；失败抛 :class:`ContentOverrideError`。

    **校验命令与运行时唯一的共同入口**。分类判据在 :func:`build_override_texts`，
    目录自身的失败关闭校验（空正文、占位写法、内容安全）在
    ``ContentCatalog.with_text_overrides``——两段都必须过，谁少跑一段就会出现
    "校验命令说合法、进程判不合法"这种运维无法自证的状态。
    """
    overrides = build_override_texts(document, catalog)
    try:
        return catalog.with_text_overrides(overrides), overrides
    except ContentRenderError as error:
        raise ContentOverrideError(REASON_PLACEHOLDER_MISMATCH) from error
    except ContentError as error:
        raise ContentOverrideError(REASON_INVALID_VALUE) from error


def _digest(*chunks: bytes) -> str:
    """内容摘要：sha256 前 12 位，形态沿 ``company_function_metric_map_file``。"""
    hasher = hashlib.sha256()
    for chunk in chunks:
        hasher.update(chunk)
    return hasher.hexdigest()[:12]


def load_content_source(override_path: Path | None) -> ContentSource:
    """建出本进程要用的内容目录：镜像内文案 + 可选的宿主机覆盖文件。

    镜像内目录本身仍失败关闭（读不出来就没有可展示的文案）；覆盖文件相反，
    任何失败都退回镜像内文案并记**一条**结构化错误日志。
    """
    base_bytes = CONTENT_PATH.read_bytes()
    base = ContentCatalog.from_file()
    base_digest = _digest(base_bytes)
    if override_path is None:
        return ContentSource(catalog=base, digest=base_digest)
    try:
        return _apply_override(base, base_bytes, override_path)
    except ContentOverrideError as error:
        logger.error(
            "外置文案覆盖文件被整份拒绝，退回镜像内文案 path=%s reason=%s（正文不进日志）",
            override_path,
            error.reason,
        )
        return ContentSource(
            catalog=base,
            digest=base_digest,
            override_path=str(override_path),
            rejection=error.reason,
        )


def _apply_override(base: ContentCatalog, base_bytes: bytes, override_path: Path) -> ContentSource:
    """读覆盖文件并叠加；缺文件按"零变化"处理，其余失败一律抛分类拒绝。"""
    parsed = read_override_document(override_path)
    if parsed is None:
        logger.info("未发现外置文案覆盖文件，按镜像内文案运行 path=%s", override_path)
        return ContentSource(
            catalog=base, digest=_digest(base_bytes), override_path=str(override_path)
        )
    document, raw = parsed
    catalog, overrides = apply_override_document(document, base)
    override_digest = _digest(raw)
    digest = _digest(base_bytes, raw)
    logger.info(
        "已加载外置文案覆盖文件 path=%s digest=%s override_digest=%s override_keys=%d",
        override_path,
        digest,
        override_digest,
        len(overrides),
    )
    return ContentSource(
        catalog=catalog,
        digest=digest,
        override_path=str(override_path),
        override_digest=override_digest,
        override_keys=tuple(sorted(overrides)),
    )


@lru_cache(maxsize=1)
def default_content_source() -> ContentSource:
    """本进程的内容目录来源，只解析一次（错误日志因此天然每进程一条）。

    **直接读 ``os.environ`` 而不是由 ``apps/`` 注入**：``core`` 侧有模块级就持有
    目录的消费点（``core/identity/roster_report``），谁先被 import 不可控，注入式
    配置会让覆盖在某些 import 次序下静默失效——那比不支持外置更糟。
    """
    try:
        path = parse_override_path(os.environ.get(CONTENT_OVERRIDE_PATH_ENV))
    except ContentOverrideError as error:
        logger.error(
            "环境变量 %s 取值不合法，按镜像内文案运行（不回显取到的值）reason=%s",
            CONTENT_OVERRIDE_PATH_ENV,
            error.reason,
        )
        # 带上拒绝原因：变量配错了与文件写坏了对运维是同一件事（用户看到的仍是镜像
        # 内文案），因此走同一条管理群告警，不静默。
        return replace(load_content_source(None), rejection=error.reason)
    return load_content_source(path)


def content_digest() -> str:
    """本进程在用的内容目录摘要（镜像内 + 覆盖文件合成，sha256 前 12 位）。"""
    return default_content_source().digest


def log_content_source(process: str) -> ContentSource:
    """进程启动时先把内容目录读出来，并记一行来源事实。

    没有这一行时，覆盖文件的加载会推迟到第一个用户请求，运维改完文件重启后无法
    在启动日志里确认"这一版确实被读到了"。
    """
    source = default_content_source()
    logger.info(
        "%s 内容目录 version=%s digest=%s override_path=%s override_keys=%d rejected=%s",
        process,
        source.catalog.version,
        source.digest,
        source.override_path,
        len(source.override_keys),
        source.rejection,
    )
    return source


__all__ = [
    "CONTENT_OVERRIDE_PATH_ENV",
    "MAX_OVERRIDE_BYTES",
    "ContentOverrideError",
    "ContentSource",
    "apply_override_document",
    "build_override_texts",
    "content_digest",
    "default_content_source",
    "load_content_source",
    "log_content_source",
    "parse_override_path",
    "read_override_document",
]
