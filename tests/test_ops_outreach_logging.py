"""`scripts/ops/outreach.py` 的日志接线：真实运行时审计行必须落到标准错误。

一次性脚本是独立进程，没有配置任何处理器时 ``logging`` 只把 WARNING 及以上交给
兜底处理器，``outreach.contact_marked_reachable`` / ``contact_marked_unavailable``
这类 INFO 级审计会被默认丢弃——预发实测「送达 1、状态落库、审计零行」正是这样
来的。这里不需要真库、不需要真飞书：只证明 ``main()`` 配置过之后，审计出口发出
的 INFO 记录能在捕获到的标准错误里看到事件名。

加载方式同 `tests/test_outreach_ops.py`：`scripts/` 下的文件用
`importlib.util.spec_from_file_location` 按路径加载。
"""

from __future__ import annotations

import importlib.util
import io
import logging
import sys
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from typing import Any

from lingxi.apps.scheduler.audit import StructuredLogAuditSink
from lingxi.core.outreach.contact_reachability import (
    AUDIT_MARKED_REACHABLE,
    AUDIT_MARKED_UNAVAILABLE,
)

REPOSITORY_ROOT = Path(__file__).parents[1]
SCRIPT = REPOSITORY_ROOT / "scripts" / "ops" / "outreach.py"


def _load_script() -> Any:
    module_name = "outreach_ops_logging_under_test"
    spec = importlib.util.spec_from_file_location(module_name, SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # 先登记再执行：脚本用了 `from __future__ import annotations`，dataclass 解析
    # 延迟求值的字段注解时要在 sys.modules 里查到本模块。
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


TOOL = _load_script()


class OutreachAuditVisibilityTest(unittest.TestCase):
    """``main()`` 接好处理器之后，审计出口的 INFO 记录在标准错误可见。"""

    def setUp(self) -> None:
        self.logger = logging.getLogger("lingxi")
        self._handlers = list(self.logger.handlers)
        self._level = self.logger.level
        self.addCleanup(self._restore_logger)

    def _restore_logger(self) -> None:
        """把本用例挂上去的处理器摘干净，不给同一进程里的其他用例留副作用。"""
        for handler in self.logger.handlers:
            if handler not in self._handlers:
                handler.close()
        self.logger.handlers[:] = self._handlers
        self.logger.setLevel(self._level)

    def test_main_makes_info_audit_records_visible_on_stderr(self) -> None:
        """``main()`` 在做任何判定之前先接好处理器：这次调用因缺名单被参数拒绝
        （退出码 2、零发送、零写入），审计出口随后发出的 INFO 记录仍已有去处。

        变异锚点：把 ``main()`` 开头的 ``_configure_logging()`` 调用删掉，本用例
        应变红（事件名不再出现在标准错误里）。
        """
        errors = io.StringIO()
        with redirect_stderr(errors):
            exit_code = TOOL.main([])
            StructuredLogAuditSink().record(AUDIT_MARKED_REACHABLE)
            StructuredLogAuditSink().record(
                AUDIT_MARKED_UNAVAILABLE, error_code="feishu_code_230013"
            )
        output = errors.getvalue()

        self.assertEqual(exit_code, 2)
        self.assertIn(AUDIT_MARKED_REACHABLE, output)
        self.assertIn(AUDIT_MARKED_UNAVAILABLE, output)
        self.assertIn("INFO", output, "审计行是 INFO 级：必须以 INFO 而不是升级成 WARNING 出现")

    def test_without_the_wiring_the_same_records_go_nowhere(self) -> None:
        """对照：进程里没有处理器时，同样的 INFO 记录一行都不出现——这正是要堵的洞。

        根记录器也一并隔离：同一进程里别的用例可能给它留过处理器，那不是这个
        一次性脚本真实运行时的状态（独立进程、从没配置过日志）。
        """
        root = logging.getLogger()
        root_handlers, root_level = list(root.handlers), root.level

        def restore_root() -> None:
            root.handlers[:] = root_handlers
            root.setLevel(root_level)

        self.addCleanup(restore_root)
        root.handlers[:] = []
        root.setLevel(logging.WARNING)
        self.logger.handlers.clear()
        self.logger.setLevel(logging.NOTSET)
        errors = io.StringIO()
        with redirect_stderr(errors):
            StructuredLogAuditSink().record(AUDIT_MARKED_REACHABLE)

        self.assertNotIn(AUDIT_MARKED_REACHABLE, errors.getvalue())

    def test_repeated_configuration_keeps_a_single_handler(self) -> None:
        """同一进程内反复调用不累积处理器，否则每条审计会被打印多遍。"""
        TOOL._configure_logging(io.StringIO())
        TOOL._configure_logging(io.StringIO())

        self.assertEqual(len(self.logger.handlers), 1)
        self.assertEqual(self.logger.level, logging.INFO)


if __name__ == "__main__":
    unittest.main()
