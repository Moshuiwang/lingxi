"""`scripts/ci/check_alembic_revisions.py` 的判定用例（Issue #53）。

这份检查的价值全在它**会变红**：一条只会通过的检查等于没有检查。所以下面每一条
都构造一份坏输入，断言它被**具体地**拒绝，而不是只跑一遍真仓库看它绿。
最后一组反过来跑真实仓库状态，防止检查因为文件结构变化而变成空转。

真库那半边（两条链建库对比、旧库未 stamp 必须失败）在
`scripts/ci/check_migration_chain.sh`，由门禁在有容器时执行，不在本文件覆盖范围。
"""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "ci" / "check_alembic_revisions.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("alembic_revision_check_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


CHECK = _load_script()


class AlembicIniTest(unittest.TestCase):
    """alembic.ini 不得留下可用的默认连接串（V-迁移-05 的静态那一半）。"""

    def test_default_url_is_rejected(self) -> None:
        failures = CHECK.check_ini("[alembic]\nsqlalchemy.url = postgresql://u@h/db\n")
        self.assertEqual(len(failures), 1)
        self.assertIn("sqlalchemy.url", failures[0])

    def test_empty_url_is_accepted(self) -> None:
        """写成空值等于没写：真正危险的是**能连上**的那种默认串。"""

        self.assertEqual(CHECK.check_ini("[alembic]\nsqlalchemy.url =\n"), [])

    def test_absent_url_is_accepted(self) -> None:
        self.assertEqual(CHECK.check_ini("[alembic]\nscript_location = %(here)s/migrations/alembic\n"), [])

    def test_url_hidden_in_another_section_is_still_found(self) -> None:
        """换个小节名不该能绕过：检查按 key 找，不按小节名找。"""

        failures = CHECK.check_ini("[alembic]\n[other]\nurl = postgresql://u@h/db\n")
        self.assertEqual(len(failures), 1)
        self.assertIn("[other]", failures[0])


class RuntimeIsolationTest(unittest.TestCase):
    """迁移工具链不得进入 src/（V-迁移-04）。"""

    def test_sqlalchemy_import_in_source_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "src"
            (source / "lingxi").mkdir(parents=True)
            (source / "lingxi" / "models.py").write_text("import sqlalchemy\n", encoding="utf-8")
            failures = CHECK.check_runtime_isolation(source)
        self.assertEqual(len(failures), 1)
        self.assertIn("sqlalchemy", failures[0])
        self.assertIn("models.py:1", failures[0])

    def test_alembic_mention_in_source_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "src"
            source.mkdir(parents=True)
            (source / "runner.py").write_text("# 顺手跑一下 alembic\n", encoding="utf-8")
            failures = CHECK.check_runtime_isolation(source)
        self.assertEqual(len(failures), 1)
        self.assertIn("alembic", failures[0])

    def test_packaging_metadata_is_skipped(self) -> None:
        """`pip install .` 生成的 *.egg-info 会如实列出 migrate extra 的 alembic。

        那是声明的回声而不是运行时引用，且已被 .gitignore 覆盖；把它算成违规会让
        「在仓库里装过包」的机器永远红，于是这条检查很快会被人关掉。
        """

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "src"
            (source / "lingxi.egg-info").mkdir(parents=True)
            (source / "lingxi.egg-info" / "requires.txt").write_text("alembic>=1.19\n", encoding="utf-8")
            self.assertEqual(CHECK.check_runtime_isolation(source), [])

    def test_clean_source_passes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "src"
            source.mkdir(parents=True)
            (source / "ids.py").write_text("import psycopg\n", encoding="utf-8")
            self.assertEqual(CHECK.check_runtime_isolation(source), [])


class DowngradeShapeTest(unittest.TestCase):
    """downgrade() 不得是静默空实现（V-迁移-07 的一部分）。"""

    def _failures_for(self, body: str) -> list[str]:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "0001_probe.py"
            path.write_text(body, encoding="utf-8")
            return CHECK.downgrade_failures(path)

    def test_pass_only_is_rejected(self) -> None:
        failures = self._failures_for("def downgrade():\n    pass\n")
        self.assertEqual(len(failures), 1)
        self.assertIn("空实现", failures[0])

    def test_docstring_only_is_rejected(self) -> None:
        """只有一句文档字符串同样是空实现——最像"写过了"的那一种。"""

        failures = self._failures_for('def downgrade():\n    """暂时不需要回退。"""\n')
        self.assertEqual(len(failures), 1)
        self.assertIn("空实现", failures[0])

    def test_explicit_raise_is_accepted(self) -> None:
        body = 'def downgrade():\n    raise NotImplementedError("基线不支持回退")\n'
        self.assertEqual(self._failures_for(body), [])

    def test_real_reversal_is_accepted(self) -> None:
        self.assertEqual(self._failures_for('def downgrade():\n    op.drop_table("t")\n'), [])

    def test_unrelated_statement_without_raise_or_op_is_rejected(self) -> None:
        """既不 raise 也不动 op 的函数体，判定不了它做没做事，按拒绝处理。"""

        failures = self._failures_for("def downgrade():\n    logged = True\n")
        self.assertEqual(len(failures), 1)
        self.assertIn("判定不了", failures[0])

    def test_missing_downgrade_is_rejected(self) -> None:
        failures = self._failures_for("def upgrade():\n    op.create_table('t')\n")
        self.assertEqual(len(failures), 1)
        self.assertIn("没有 downgrade()", failures[0])


class RealRepositoryStateTest(unittest.TestCase):
    """仓库当前状态必须自洽——防止上面的构造用例与真实文件脱节。"""

    def test_repository_ini_has_no_default_url(self) -> None:
        self.assertEqual(CHECK.check_ini(CHECK.ALEMBIC_INI.read_text(encoding="utf-8")), [])

    def test_repository_source_has_no_migration_toolchain(self) -> None:
        self.assertEqual(CHECK.check_runtime_isolation(CHECK.RUNTIME_SOURCE_ROOT), [])

    def test_every_revision_file_has_a_real_downgrade(self) -> None:
        versions = CHECK.REPOSITORY_ROOT / "migrations" / "alembic" / "versions"
        revision_files = sorted(versions.glob("*.py"))
        self.assertTrue(revision_files, "versions/ 下一个 revision 都没有，检查会空转")
        for path in revision_files:
            with self.subTest(revision=path.name):
                self.assertEqual(CHECK.downgrade_failures(path), [])


if __name__ == "__main__":
    unittest.main()
