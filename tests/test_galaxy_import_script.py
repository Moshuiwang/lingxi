"""导入脚本入口的最小自动化覆盖（独立复查 P2：唯一运行入口此前零覆盖）。

不需要数据库：缺 LINGXI_POSTGRES_DSN 时脚本必须在做任何事之前返回 2。
它锁住的是「脚本 import 的适配器符号与调用方式仍然存在」这一层——
改名此前只会在管理员对着真实库敲命令时暴露，从此在 CI 变红。
"""

from __future__ import annotations

import importlib.util
import io
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "import_galaxy_permission_export.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("galaxy_import_script_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class ImportScriptEntryTest(unittest.TestCase):
    def test_missing_dsn_exits_2_before_touching_anything(self) -> None:
        module = _load_script()

        saved = os.environ.pop("LINGXI_POSTGRES_DSN", None)
        try:
            captured_out, captured_err = io.StringIO(), io.StringIO()
            with redirect_stdout(captured_out), redirect_stderr(captured_err):
                code = module.main(["/nonexistent-galaxy-export-dir", "--source-label", "CI 冒烟"])
        finally:
            if saved is not None:
                os.environ["LINGXI_POSTGRES_DSN"] = saved

        self.assertEqual(code, 2)
        self.assertIn("LINGXI_POSTGRES_DSN", captured_out.getvalue() + captured_err.getvalue())


if __name__ == "__main__":
    unittest.main()
