"""外置文案覆盖文件的离线校验：``python -m lingxi.config.content_check <文件>``。

放上宿主机之前先跑一遍：退出码 ``0`` 表示这份文件会被真实进程接受，非 ``0``
表示会被整份忽略、用户仍看到镜像内文案。判据与运行时**是同一段代码**
（:func:`lingxi.config.content_override.apply_override_document`，运行时加载也只
经这一个入口），因此"这里过了、线上被拒"在结构上不可能发生。

只打印键名、原因码与人话说明，不打印文件正文——校验命令常在共享终端里跑，
输出会被复制进工单。
"""

from __future__ import annotations

import sys
from pathlib import Path

from lingxi.config.content import ContentCatalog
from lingxi.config.content_override import (
    REASON_INVALID_TOML,
    REASON_INVALID_VALUE,
    REASON_PLACEHOLDER_MISMATCH,
    REASON_UNKNOWN_KEY,
    REASON_UNKNOWN_SECTION,
    REASON_UNREADABLE,
    REASON_UNSAFE_TEXT,
    ContentOverrideError,
    apply_override_document,
    read_override_document,
)

EXIT_OK = 0
EXIT_REJECTED = 1
EXIT_USAGE = 2

_REASON_HELP = {
    REASON_UNREADABLE: "文件读不出来（权限或 I/O 错误）。",
    REASON_INVALID_TOML: "不是合法的 UTF-8 TOML，解析失败。",
    REASON_UNKNOWN_SECTION: "只允许一个 [texts] 表；meta、cards 与其它顶层键不可外置。",
    REASON_UNKNOWN_KEY: "含镜像内没有登记的文案键；外置文件不能新增键。",
    REASON_INVALID_VALUE: "某个键的值不是合法文案（非文本、为空或占位写法不合法）。",
    REASON_PLACEHOLDER_MISMATCH: "某个键的占位符集合与镜像内同键不一致，渲染时必然失败。",
    REASON_UNSAFE_TEXT: "命中内容安全校验（内部过程标识或误导性权限/等待时间表达）。",
}

_USAGE = "用法：python -m lingxi.config.content_check <覆盖文件路径>"


def main(argv: list[str] | None = None) -> int:
    """校验一份覆盖文件；合法返回 ``0``，会被整份忽略返回 ``1``，用法错误返回 ``2``。"""
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 1:
        print(_USAGE, file=sys.stderr)
        return EXIT_USAGE
    path = Path(arguments[0])
    catalog = ContentCatalog.from_file()
    try:
        parsed = read_override_document(path)
    except ContentOverrideError as error:
        return _report_rejection(error)
    if parsed is None:
        print(f"文件不存在：{path}（真实进程会按镜像内文案运行，不算故障）", file=sys.stderr)
        return EXIT_USAGE
    document, _raw = parsed
    try:
        _catalog, overrides = apply_override_document(document, catalog)
    except ContentOverrideError as error:
        return _report_rejection(error)
    print(f"通过：{len(overrides)} 个文案键会被覆盖")
    for key in sorted(overrides):
        print(f"  {key}")
    return EXIT_OK


def _report_rejection(error: ContentOverrideError) -> int:
    """打印原因码与人话说明；正文与取值一律不回显。"""
    print(f"拒绝：reason={error.reason}", file=sys.stderr)
    print(_REASON_HELP.get(error.reason, "未分类的拒绝原因。"), file=sys.stderr)
    print("放到宿主机后真实进程会整份忽略这份文件，用户仍看到镜像内文案。", file=sys.stderr)
    return EXIT_REJECTED


if __name__ == "__main__":
    sys.exit(main())
