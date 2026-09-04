"""`scripts/ci/check_alembic_revisions.py` 的判定用例（Issue #53）。

这份检查的价值全在它**会变红**：一条只会通过的检查等于没有检查。所以下面每一条
都构造一份坏输入，断言它被**具体地**拒绝，而不是只跑一遍真仓库看它绿。
最后一组反过来跑真实仓库状态，防止检查因为文件结构变化而变成空转。

真库那半边（两条链建库对比、旧库未 stamp 必须失败）在
`scripts/ci/check_migration_chain.sh`，由门禁在有容器时执行，不在本文件覆盖范围。
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).parents[1]
SCRIPT = REPOSITORY_ROOT / "scripts" / "ci" / "check_alembic_revisions.py"
DSN_MODULE = REPOSITORY_ROOT / "migrations" / "alembic" / "migration_dsn.py"


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


CHECK = _load_module(SCRIPT, "alembic_revision_check_under_test")
# 纯逻辑模块，不 import alembic：这几条断言在没装 migrate extra 的机器上照样跑。
DSN = _load_module(DSN_MODULE, "migration_dsn_under_test")


def _alembic_available() -> bool:
    return importlib.util.find_spec("alembic") is not None


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
        self.assertEqual(
            CHECK.check_ini("[alembic]\nscript_location = %(here)s/migrations/alembic\n"), []
        )

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
            (source / "lingxi.egg-info" / "requires.txt").write_text(
                "alembic>=1.19\n", encoding="utf-8"
            )
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


class EmbeddedCopyTest(unittest.TestCase):
    """基线内嵌的编号 SQL 副本必须与磁盘原文逐字节相同（V-迁移-07）。

    真库那半边（两条链建库后 schema 相等）看不见这类分叉：副本里多一句 INSERT、
    GRANT，或只改注释，两边 `pg_dump --schema=public` 依然相等。而且真库那半边
    没有容器就整体不跑。
    """

    def _build(
        self, directory: str, embedded: str, on_disk: str, chain: str = "a.sql"
    ) -> list[str]:
        root = Path(directory)
        (root / "alembic" / "versions").mkdir(parents=True)
        (root / chain).write_text(on_disk, encoding="utf-8")
        revision = root / "alembic" / "versions" / "0001_baseline.py"
        revision.write_text(
            f"_SQL_A = {embedded!r}\n"
            f'CHAIN: tuple[tuple[str, str], ...] = (\n    ("{chain}", _SQL_A),\n)\n',
            encoding="utf-8",
        )
        return CHECK.embedded_copy_failures(revision, root)

    def test_identical_bytes_pass(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(
                self._build(directory, "CREATE TABLE t ();\n", "CREATE TABLE t ();\n"), []
            )

    def test_one_character_drift_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            failures = self._build(directory, "CREATE TABLE t ();\n", "CREATE TABLE tt ();\n")
        self.assertEqual(len(failures), 1)
        self.assertIn("已分叉", failures[0])

    def test_comment_only_drift_is_reported(self) -> None:
        """只改注释——真库 schema 比对对这种分叉完全沉默。"""

        with tempfile.TemporaryDirectory() as directory:
            failures = self._build(
                directory, "-- 甲\nCREATE TABLE t ();\n", "-- 乙\nCREATE TABLE t ();\n"
            )
        self.assertEqual(len(failures), 1)
        self.assertIn("已分叉", failures[0])

    def test_numbered_sql_not_covered_by_any_chain_is_reported(self) -> None:
        """编号 SQL 已冻结；磁盘上多出一个文件意味着两条链就此分叉。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "alembic" / "versions").mkdir(parents=True)
            (root / "a.sql").write_text("X\n", encoding="utf-8")
            (root / "b.sql").write_text("Y\n", encoding="utf-8")
            revision = root / "alembic" / "versions" / "0001_baseline.py"
            revision.write_text(
                '_SQL_A = "X\\n"\nCHAIN: tuple[tuple[str, str], ...] = (\n    ("a.sql", _SQL_A),\n)\n',
                encoding="utf-8",
            )
            failures = CHECK.embedded_copy_failures(revision, root)
        self.assertEqual(len(failures), 1)
        self.assertIn("b.sql", failures[0])

    def test_revision_without_chain_is_ignored(self) -> None:
        """基线之外的 revision 没有 CHAIN，本检查对它们无话可说。"""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            revision = root / "0002_later.py"
            revision.write_text("def upgrade():\n    op.create_table('t')\n", encoding="utf-8")
            self.assertEqual(CHECK.embedded_copy_failures(revision, root), [])


class MigrationDsnTest(unittest.TestCase):
    """迁移连接串的校验（V-迁移-05）。

    直接测 `migrations/alembic/migration_dsn.py` 的纯函数——它不 import alembic，
    所以这几条断言在**没装 migrate extra 的机器上照样跑**。此前它们只能通过子进程
    跑 `python -m alembic` 来验，于是在没有 alembic 的环境里表现为 FAIL 而不是 skip，
    违反代码框架第四节「无外部依赖可运行」的约定。
    """

    def test_bare_scheme_gets_the_psycopg3_driver(self) -> None:
        self.assertEqual(
            DSN.normalize_database_url("postgresql://postgres@localhost:5432/lingxi"),
            "postgresql+psycopg://postgres@localhost:5432/lingxi",
        )

    def test_explicit_psycopg_scheme_is_kept(self) -> None:
        url = "postgresql+psycopg://postgres@localhost:5432/lingxi"
        self.assertEqual(DSN.normalize_database_url(url), url)

    def test_dsn_without_database_name_is_rejected(self) -> None:
        with self.assertRaises(DSN.MigrationDsnError) as caught:
            DSN.normalize_database_url("postgresql://postgres@localhost:5432")
        self.assertIn("没有指定数据库名", str(caught.exception))

    def test_dsn_with_empty_path_is_rejected(self) -> None:
        with self.assertRaises(DSN.MigrationDsnError) as caught:
            DSN.normalize_database_url("postgresql:///")
        self.assertIn("没有指定数据库名", str(caught.exception))

    def test_dsn_with_extra_path_segments_is_rejected(self) -> None:
        with self.assertRaises(DSN.MigrationDsnError):
            DSN.normalize_database_url("postgresql://postgres@localhost:5432/a/b")

    def test_missing_and_blank_dsn_name_the_variable(self) -> None:
        for raw in (None, "", "   "):
            with self.subTest(raw=raw):
                with self.assertRaises(DSN.MigrationDsnError) as caught:
                    DSN.normalize_database_url(raw)
                self.assertIn("LINGXI_MIGRATION_DSN", str(caught.exception))

    def test_non_psycopg_driver_is_rejected(self) -> None:
        with self.assertRaises(DSN.MigrationDsnError) as caught:
            DSN.normalize_database_url("postgresql+psycopg2://postgres@localhost:5432/lingxi")
        self.assertIn("psycopg3", str(caught.exception))

    def test_error_messages_never_echo_the_connection_string(self) -> None:
        """报错不得回显连接串——它可能带口令，而门禁与部署日志都会留痕。"""

        secret = "hunter2"
        for raw in (
            f"mysql://user:{secret}@localhost:3306/lingxi",
            f"postgresql://user:{secret}@localhost:5432",
        ):
            with self.subTest(raw=raw):
                with self.assertRaises(DSN.MigrationDsnError) as caught:
                    DSN.normalize_database_url(raw)
                self.assertNotIn(secret, str(caught.exception))


@unittest.skipUnless(
    _alembic_available(), "跳过：未安装 migrate extra（alembic），env.py 接线未验证"
)
class MigrationEnvWiringTest(unittest.TestCase):
    """env.py 确实调用了上面那段校验（而不是自己另写一份）。

    纯函数用例证明逻辑对，这一条证明**它被接上了**。要真的启动 alembic，所以按
    仓库对真库测试的同款做法用 skipUnless 声明式跳过，不静默通过。
    """

    def _run(self, dsn: str) -> subprocess.CompletedProcess[str]:
        environment = dict(os.environ)
        environment["LINGXI_MIGRATION_DSN"] = dsn
        # 业务 DSN 故意留在环境里：顺带证明不会回落到它。
        environment["LINGXI_POSTGRES_DSN"] = "postgresql://postgres@localhost:5432/lingxi_test"
        return subprocess.run(
            [sys.executable, "-m", "alembic", "current"],
            cwd=Path(__file__).parents[1],
            env=environment,
            capture_output=True,
            text=True,
        )

    def test_env_rejects_dsn_without_database_name(self) -> None:
        result = self._run("postgresql://postgres@localhost:5432")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("没有指定数据库名", result.stderr + result.stdout)

    def test_env_does_not_fall_back_to_the_business_dsn(self) -> None:
        result = self._run("")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("LINGXI_MIGRATION_DSN", result.stderr + result.stdout)


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

    def test_baseline_embedded_copy_matches_disk(self) -> None:
        versions = CHECK.REPOSITORY_ROOT / "migrations" / "alembic" / "versions"
        for path in sorted(versions.glob("*.py")):
            with self.subTest(revision=path.name):
                self.assertEqual(CHECK.embedded_copy_failures(path, CHECK.MIGRATIONS_ROOT), [])


if __name__ == "__main__":
    unittest.main()
