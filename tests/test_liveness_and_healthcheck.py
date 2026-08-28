"""Issue #153：活性心跳文件与容器内健康检查命令。

healthcheck 必须能对"依赖不可达"和"主循环停摆"两类真实故障如实变红——本文件
用真实文件系统（``tempfile``）与真实（但故意配错/故意不启动）网络目标验证这一点，
不用 mock 掩盖判定逻辑本身。
"""

from __future__ import annotations

import io
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from postgres_schema import psycopg_available

from lingxi.apps import healthcheck, liveness

DSN = os.environ.get("LINGXI_POSTGRES_DSN")
SKIP_DB = (
    "LINGXI_POSTGRES_DSN 未设置：跳过需要真实数据库的健康检查断言"
    if not DSN
    else "LINGXI_POSTGRES_DSN 已设置但未安装 psycopg 驱动：跳过需要真实数据库的健康检查断言"
)


class LivenessFileTests(unittest.TestCase):
    def test_touch_then_read_reports_a_small_age(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            liveness.touch_liveness("worker", directory=directory)

            age = liveness.read_liveness_age_seconds("worker", directory=directory)

            self.assertIsNotNone(age)
            self.assertLess(age, 1.0)

    def test_missing_file_reports_none_not_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            age = liveness.read_liveness_age_seconds("worker", directory=Path(tmp))

        self.assertIsNone(age, "从未写过心跳时必须能与刚天真的 0 秒年龄区分开")

    def test_corrupted_file_content_is_treated_as_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            liveness.liveness_path("gateway-delivery", directory=directory).write_text(
                "not-a-number", encoding="utf-8"
            )

            age = liveness.read_liveness_age_seconds("gateway-delivery", directory=directory)

        self.assertIsNone(age)

    def test_different_roles_do_not_collide(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            liveness.touch_liveness("gateway-longconn", directory=directory, clock=lambda: 100.0)
            liveness.touch_liveness("gateway-delivery", directory=directory, clock=lambda: 200.0)

            longconn_age = liveness.read_liveness_age_seconds(
                "gateway-longconn", directory=directory, now=lambda: 210.0
            )
            delivery_age = liveness.read_liveness_age_seconds(
                "gateway-delivery", directory=directory, now=lambda: 210.0
            )

        self.assertAlmostEqual(longconn_age, 110.0)
        self.assertAlmostEqual(delivery_age, 10.0)

    def test_write_failure_does_not_raise(self) -> None:
        """活性文件写不进去不能带走主循环——``touch_liveness`` 必须吞掉写失败。"""

        unwritable_parent = Path(tempfile.mkdtemp()) / "does" / "not" / "exist"
        # 不 mkdir：目标目录不存在，写入必然失败。
        liveness.touch_liveness("worker", directory=unwritable_parent)  # 不应抛异常

    def test_lingxi_liveness_dir_env_var_overrides_the_default_location(self) -> None:
        """``LINGXI_LIVENESS_DIR`` 是 healthcheck 与主进程之间唯一的隐式约定：
        两者必须读写同一个目录（默认 ``/tmp``，与三个常驻服务的 tmpfs 挂载点
        一致）。这条断言证明该环境变量真的被两个函数一致地遵守。
        """

        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict("os.environ", {"LINGXI_LIVENESS_DIR": tmp}):
                liveness.touch_liveness("worker")
                age = liveness.read_liveness_age_seconds("worker")

        self.assertIsNotNone(age)
        self.assertLess(age, 1.0)


class HealthcheckDatabaseCheckTests(unittest.TestCase):
    """只测 ``_check_database`` 的判定分支，不依赖真实 psycopg 驱动是否装了——
    缺失/错误连接串本身就应该在真正尝试连接前后都归为"不健康"。
    """

    def test_missing_dsn_env_var_is_unhealthy(self) -> None:
        with self.assertRaises(healthcheck.HealthcheckError):
            healthcheck._check_database("scheduler", {})

    def test_unreachable_host_is_unhealthy_within_a_bounded_time(self) -> None:
        """真实网络尝试：连一个必然连不上的地址（保留地址段 + 连接工厂的默认
        超时）。断言的是"确实失败"这个结果，不是具体异常类型——连接工厂本身的
        超时边界已经由 adapters/postgres.py 与 check_deploy_contract.py 覆盖。
        """

        started = time.monotonic()
        with self.assertRaises(healthcheck.HealthcheckError):
            healthcheck._check_database(
                "scheduler",
                {
                    # TEST-NET-1（RFC 5737）：保证不可路由，连接会超时或立即拒绝，
                    # 不会误连到真实数据库。
                    "LINGXI_POSTGRES_DSN": "postgresql://u:p@192.0.2.1:5",
                },
            )
        # 连接工厂的 connect_timeout 默认 5 秒；给测试留够裕量但仍然是"有界"。
        self.assertLess(time.monotonic() - started, 15.0)

    def test_gateway_role_reads_its_own_prefixed_variable(self) -> None:
        """gateway 与 scheduler/worker 的 DSN 变量名不同（见模块头注释），
        healthcheck 必须按角色读对应变量，读错变量会把"根本没连接"误判成别的。
        """

        with self.assertRaises(healthcheck.HealthcheckError) as ctx:
            healthcheck._check_database("gateway", {"LINGXI_POSTGRES_DSN": "postgresql://u:p@x/y"})
        self.assertIn("LINGXI_GATEWAY_POSTGRES_DSN", str(ctx.exception))


class HealthcheckLivenessCheckTests(unittest.TestCase):
    def test_healthy_when_all_liveness_keys_are_fresh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict("os.environ", {"LINGXI_LIVENESS_DIR": tmp}):
                for key in healthcheck._LIVENESS_KEYS_BY_ROLE["gateway"]:
                    liveness.touch_liveness(key)
                healthcheck._check_liveness("gateway", 30.0, {})  # 不应抛异常

    def test_stale_key_on_the_gateway_delivery_side_fails_even_though_longconn_is_fresh(
        self,
    ) -> None:
        """gateway 一个进程两条循环：任一条停摆都必须让健康检查变红，不能被
        另一条仍然新鲜的心跳掩盖（模块头注释的核心取舍）。
        """

        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict("os.environ", {"LINGXI_LIVENESS_DIR": tmp}):
                liveness.touch_liveness("gateway-longconn")
                # gateway-delivery 从未写过：模拟投递消费后台线程已经死掉。
                with self.assertRaises(healthcheck.HealthcheckError) as ctx:
                    healthcheck._check_liveness("gateway", 30.0, {})
                self.assertIn("gateway-delivery", str(ctx.exception))

    def test_an_old_liveness_file_exceeding_the_threshold_fails(self) -> None:
        with patch(
            "lingxi.apps.healthcheck.read_liveness_age_seconds",
            return_value=999999.0,
        ):
            with self.assertRaises(healthcheck.HealthcheckError) as ctx:
                healthcheck._check_liveness("worker", 30.0, {})
        self.assertIn("999999.0", str(ctx.exception))

    def test_document_delivery_key_is_not_required_when_the_feature_is_unconfigured(
        self,
    ) -> None:
        """P3 顺手（Issue #341）：没配 ``LINGXI_GATEWAY_TENANT_DOMAIN`` 的 gateway
        部署里，文档投递独立消费循环压根不会被装配、永远不会写这个活性文件——
        健康检查不能因此判它不健康。"""

        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict("os.environ", {"LINGXI_LIVENESS_DIR": tmp}):
                liveness.touch_liveness("gateway-longconn")
                liveness.touch_liveness("gateway-delivery")
                healthcheck._check_liveness("gateway", 30.0, {})  # 不应抛异常

    def test_document_delivery_key_is_required_when_the_feature_is_configured(self) -> None:
        """配了这项能力时，第三条线程的活性同样必须新鲜——否则一个已经死掉的
        文档投递消费线程会被健康检查静默放过。"""

        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict("os.environ", {"LINGXI_LIVENESS_DIR": tmp}):
                liveness.touch_liveness("gateway-longconn")
                liveness.touch_liveness("gateway-delivery")
                # gateway-document-delivery 从未写过：模拟这条线程已经死掉。
                with self.assertRaises(healthcheck.HealthcheckError) as ctx:
                    healthcheck._check_liveness(
                        "gateway", 30.0, {"LINGXI_GATEWAY_TENANT_DOMAIN": "example.feishu.cn"}
                    )
                self.assertIn("gateway-document-delivery", str(ctx.exception))

    def test_document_delivery_key_healthy_when_configured_and_fresh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict("os.environ", {"LINGXI_LIVENESS_DIR": tmp}):
                liveness.touch_liveness("gateway-longconn")
                liveness.touch_liveness("gateway-delivery")
                liveness.touch_liveness("gateway-document-delivery")
                healthcheck._check_liveness(
                    "gateway", 30.0, {"LINGXI_GATEWAY_TENANT_DOMAIN": "example.feishu.cn"}
                )  # 不应抛异常


@unittest.skipUnless(DSN and psycopg_available(), SKIP_DB)
class HealthcheckCommandRealDatabaseTests(unittest.TestCase):
    """端到端：真实 ``run()`` 入口对着一个真实可达的 PostgreSQL 判健康，
    对着一个明确不可达的连接串判不健康——不是只测内部函数分支。
    """

    def test_reachable_database_and_fresh_liveness_is_exit_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"LINGXI_LIVENESS_DIR": tmp}):
                liveness.touch_liveness("scheduler")
                err = io.StringIO()
                code = healthcheck.run(
                    ["--role", "scheduler"],
                    env={"LINGXI_POSTGRES_DSN": DSN, "LINGXI_LIVENESS_DIR": tmp},
                    stderr=err,
                )
        self.assertEqual(code, 0, err.getvalue())
        self.assertIn("healthy", err.getvalue())

    def test_unreachable_database_is_exit_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            liveness.touch_liveness("scheduler", directory=Path(tmp))
            err = io.StringIO()
            code = healthcheck.run(
                ["--role", "scheduler"],
                env={
                    "LINGXI_POSTGRES_DSN": "postgresql://u:p@192.0.2.1:5/db",
                    "LINGXI_LIVENESS_DIR": tmp,
                },
                stderr=err,
            )
        self.assertEqual(code, 1)
        self.assertIn("unhealthy", err.getvalue())


if __name__ == "__main__":
    unittest.main()
