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

    def test_clock_rollback_reports_none_not_a_falsely_fresh_zero(self) -> None:
        """P1-5（独立审查）：``now() - written_at`` 为负（时钟回拨）此前会被
        ``max(0.0, ...)`` 钳到 0 秒——看起来"心跳刚刚写入"，比任何真实心跳都
        更新鲜，DB 可达性缓存会因此把回拨窗口误判为"近期已验证"，缓存反而
        永不过期。负差值必须返回 ``None``（与"从未写过心跳"同一信号），逼
        调用方退回真实探测，不能让回拨制造一份假新鲜的缓存。
        """

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            # written_at 晚于 now()：clock 提供未来的时间戳，模拟随后时钟被
            # 回拨——这是唯一能在纯逻辑测试里制造"负差值"的手段，不依赖真的
            # 睡眠等待或修改系统时钟。
            liveness.touch_liveness("worker", directory=directory, clock=lambda: 1000.0)

            age = liveness.read_liveness_age_seconds(
                "worker", directory=directory, now=lambda: 400.0
            )

        self.assertIsNone(age, "时钟回拨产生的负差值必须报 None，不能钳成 0 秒当作最新鲜")

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
            healthcheck._check_database("scheduler", 0.0, {})

    def test_unreachable_host_is_unhealthy_within_a_bounded_time(self) -> None:
        """真实网络尝试：连一个必然连不上的地址（保留地址段 + 连接工厂的默认
        超时）。断言的是"确实失败"这个结果，不是具体异常类型——连接工厂本身的
        超时边界已经由 adapters/postgres.py 与 check_deploy_contract.py 覆盖。

        显式用一个空缓存目录：这条断言测的是"真实探测确实失败"，不能被恰好
        残留在真实 ``/tmp`` 里的一份陈旧缓存戳悄悄跳过。
        """

        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict("os.environ", {"LINGXI_LIVENESS_DIR": tmp}):
                started = time.monotonic()
                with self.assertRaises(healthcheck.HealthcheckError):
                    healthcheck._check_database(
                        "scheduler",
                        60.0,
                        {
                            # TEST-NET-1（RFC 5737）：保证不可路由，连接会超时或
                            # 立即拒绝，不会误连到真实数据库。
                            "LINGXI_POSTGRES_DSN": "postgresql://u:p@192.0.2.1:5",
                        },
                    )
        # 连接工厂的 connect_timeout 默认 5 秒；给测试留够裕量但仍然是"有界"。
        self.assertLess(time.monotonic() - started, 15.0)

    def test_gateway_role_reads_its_own_prefixed_variable(self) -> None:
        """gateway 与 scheduler/worker 的 DSN 变量名不同（见模块头注释），
        healthcheck 必须按角色读对应变量，读错变量会把"根本没连接"误判成别的。

        DSN 校验必须先于缓存判定发生（见 ``_check_database`` 实现注释）：这里
        故意不预置任何缓存，用来证明这条报错不依赖缓存状态。
        """

        with self.assertRaises(healthcheck.HealthcheckError) as ctx:
            healthcheck._check_database(
                "gateway", 60.0, {"LINGXI_POSTGRES_DSN": "postgresql://u:p@x/y"}
            )
        self.assertIn("LINGXI_GATEWAY_POSTGRES_DSN", str(ctx.exception))


class HealthcheckDatabaseCacheTests(unittest.TestCase):
    """Issue #409：依赖可达判定的成功结果缓存——新鲜/过期/缺失三态。

    缓存必须只在真实探测成功时才被信任，且从不弱化"数据库不可达必须如实
    变红"这条硬约束：过期或缺失都必须落回真实探测。
    """

    DSN = "postgresql://u:p@192.0.2.1:5"  # TEST-NET-1，保证不可路由。

    def test_missing_cache_falls_back_to_a_real_probe_and_reports_failure(self) -> None:
        """三态之一：从未探测成功过（容器刚启动）——必须做真实探测，不能凭空
        判健康。"""

        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict("os.environ", {"LINGXI_LIVENESS_DIR": tmp}):
                with self.assertRaises(healthcheck.HealthcheckError):
                    healthcheck._check_database(
                        "scheduler", 60.0, {"LINGXI_POSTGRES_DSN": self.DSN}
                    )

    def test_fresh_cache_skips_the_real_probe(self) -> None:
        """三态之二：缓存新鲜——即使 DSN 指向一个连不上的地址，也必须信任缓存、
        不再尝试真实连接（用一个必然会失败的 DSN 恰好证明"根本没有真的去连"）。
        """

        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict("os.environ", {"LINGXI_LIVENESS_DIR": tmp}):
                liveness.touch_liveness("scheduler-db")
                # 不应抛异常：缓存年龄远小于 60s 的 TTL，真实探测被跳过。
                healthcheck._check_database("scheduler", 60.0, {"LINGXI_POSTGRES_DSN": self.DSN})

    def test_expired_cache_falls_back_to_a_real_probe(self) -> None:
        """三态之三：缓存存在但已过期——必须重新做真实探测，不能继续信任一份
        陈旧的"曾经可达"记录。"""

        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict("os.environ", {"LINGXI_LIVENESS_DIR": tmp}):
                # 戳一个纪元起点的时间戳：`read_liveness_age_seconds` 内部用的是
                # 真实 `time.time()`，因此这份缓存相对当下必然早已过期，不需要
                # 真的睡够 60 秒或去 patch 默认参数已经绑死的 `time.time` 引用。
                liveness.touch_liveness("scheduler-db", clock=lambda: 0.0)
                with self.assertRaises(healthcheck.HealthcheckError):
                    healthcheck._check_database(
                        "scheduler", 60.0, {"LINGXI_POSTGRES_DSN": self.DSN}
                    )

    def test_failed_probe_does_not_refresh_the_cache(self) -> None:
        """探测失败绝不写入缓存——不能让"故障期间"反而不再检查。"""

        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict("os.environ", {"LINGXI_LIVENESS_DIR": tmp}):
                with self.assertRaises(healthcheck.HealthcheckError):
                    healthcheck._check_database(
                        "scheduler", 60.0, {"LINGXI_POSTGRES_DSN": self.DSN}
                    )
                age = liveness.read_liveness_age_seconds("scheduler-db")
        self.assertIsNone(age, "探测失败后不应该出现任何缓存戳")

    def test_zero_ttl_disables_the_cache_even_immediately_after_a_success(self) -> None:
        """``db_cache_ttl_seconds<=0`` 必须永远做真实探测——即使缓存戳刚刚在同一
        墙钟秒内写入（age 精确为 0.0），也不能被"age <= 0"这类计时巧合误判为
        "缓存命中"。"""

        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict("os.environ", {"LINGXI_LIVENESS_DIR": tmp}):
                liveness.touch_liveness("scheduler-db")
                with self.assertRaises(healthcheck.HealthcheckError):
                    healthcheck._check_database(
                        "scheduler", 0.0, {"LINGXI_POSTGRES_DSN": self.DSN}
                    )


class HealthcheckEnvIsolationTests(unittest.TestCase):
    """P1-6（独立审查）：``_check_database``/``_check_liveness`` 的 ``directory``
    关键字参数必须真正被使用，不能悄悄退回真实进程 ``os.environ``——否则调用方
    （测试、或未来任何想要隔离环境跑一次判定的场景）以为自己已经传了独立目录，
    实际上仍在读写真实 ``/tmp``，两次调用会互相污染。这里不依赖真实数据库：
    用一个必然不可路由的 DSN + 预置的 DB 缓存戳证明"命中缓存就不会真的发起
    连接"，从而间接证明 `directory` 参数确实决定了缓存读到了哪个目录。
    """

    DSN = "postgresql://u:p@192.0.2.1:5"  # TEST-NET-1，保证不可路由。

    def test_check_liveness_uses_the_passed_directory_not_the_real_environment(self) -> None:
        with tempfile.TemporaryDirectory() as real_tmp, tempfile.TemporaryDirectory() as decoy_tmp:
            with patch.dict("os.environ", {"LINGXI_LIVENESS_DIR": decoy_tmp}):
                # 心跳只写在 real_tmp，只经函数参数 directory 传入，不经由
                # os.environ（它指向 decoy_tmp，那里什么心跳文件都没有）。
                for key in healthcheck._LIVENESS_KEYS_BY_ROLE["worker"]:
                    liveness.touch_liveness(key, directory=Path(real_tmp))

                # 传 directory=real_tmp：应该读到刚写的新鲜心跳，不抛异常。
                healthcheck._check_liveness("worker", 30.0, {}, directory=Path(real_tmp))

                # 不传 directory：退回真实 os.environ 指向的 decoy_tmp，那里没有
                # 任何心跳文件，必须如实报"活性文件缺失"。
                with self.assertRaises(healthcheck.HealthcheckError):
                    healthcheck._check_liveness("worker", 30.0, {})

    def test_check_database_cache_uses_the_passed_directory_not_the_real_environment(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as real_tmp, tempfile.TemporaryDirectory() as decoy_tmp:
            with patch.dict("os.environ", {"LINGXI_LIVENESS_DIR": decoy_tmp}):
                # 缓存戳只写在 real_tmp。
                liveness.touch_liveness("scheduler-db", directory=Path(real_tmp))

                # 传 directory=real_tmp：命中缓存，不会真的发起连接，不抛异常
                # ——DSN 本身指向不可路由地址，真发起连接必然失败，能不抛异常
                # 恰好证明确实是从 real_tmp 读到了这份新鲜缓存戳。
                healthcheck._check_database(
                    "scheduler", 60.0, {"LINGXI_POSTGRES_DSN": self.DSN}, directory=Path(real_tmp)
                )

                # 不传 directory：退回真实 os.environ 指向的 decoy_tmp，那里没有
                # 缓存戳，必须落回真实探测并如实失败。
                with self.assertRaises(healthcheck.HealthcheckError):
                    healthcheck._check_database(
                        "scheduler", 60.0, {"LINGXI_POSTGRES_DSN": self.DSN}
                    )

    def test_run_resolves_liveness_directory_from_the_passed_env(self) -> None:
        """``run(env=...)`` 端到端：目录只经 env 参数传入，不需要真实数据库——
        DB 缓存戳与主循环活性心跳都预先写进 ``target_tmp``，DSN 故意指向一个
        不可路由地址；如果 `run()` 正确把 `env["LINGXI_LIVENESS_DIR"]` 一路
        传到底，两段判定都会命中缓存/心跳，不会真的发起任何网络连接，`run()`
        应该判健康（退出码 0）。真实进程 os.environ 指向另一个空目录（decoy），
        证明结果不是意外借道真实环境凑巧对上的。
        """

        with tempfile.TemporaryDirectory() as target_tmp, tempfile.TemporaryDirectory() as decoy_tmp:
            with patch.dict("os.environ", {"LINGXI_LIVENESS_DIR": decoy_tmp}):
                liveness.touch_liveness("scheduler-db", directory=Path(target_tmp))
                liveness.touch_liveness("scheduler", directory=Path(target_tmp))
                err = io.StringIO()
                code = healthcheck.run(
                    ["--role", "scheduler"],
                    env={
                        "LINGXI_POSTGRES_DSN": self.DSN,
                        "LINGXI_LIVENESS_DIR": target_tmp,
                    },
                    stderr=err,
                )
        self.assertEqual(code, 0, err.getvalue())
        self.assertIn("healthy", err.getvalue())

    def test_run_does_not_fall_back_to_the_real_process_environment(self) -> None:
        """反证：把心跳/缓存戳只写进真实 os.environ 指向的目录（decoy），env
        参数指向的 target_tmp 里什么都没有——`run()` 必须如实报不健康，证明它
        没有反过来"偷看"真实 os.environ 又把结果判成健康。"""

        with tempfile.TemporaryDirectory() as target_tmp, tempfile.TemporaryDirectory() as decoy_tmp:
            with patch.dict("os.environ", {"LINGXI_LIVENESS_DIR": decoy_tmp}):
                liveness.touch_liveness("scheduler-db", directory=Path(decoy_tmp))
                liveness.touch_liveness("scheduler", directory=Path(decoy_tmp))
                err = io.StringIO()
                code = healthcheck.run(
                    ["--role", "scheduler"],
                    env={
                        "LINGXI_POSTGRES_DSN": self.DSN,
                        "LINGXI_LIVENESS_DIR": target_tmp,
                    },
                    stderr=err,
                )
        self.assertEqual(code, 1)
        self.assertIn("unhealthy", err.getvalue())


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
        """负向用例的环境隔离（P1-6，独立审查）：此前这条用例没有 `patch.dict`
        真实 `os.environ`——之所以此前一直能通过，纯粹是因为数据库不可达这一步
        先于活性检查失败，`env=` 里的 `LINGXI_LIVENESS_DIR` 传没传、有没有真正
        生效，这条用例根本无从判断，是一条"看起来在测隔离、实际什么都没测"的
        用例。补上 `patch.dict`（与上面的正向用例同一姿态）：真实进程环境指向
        一个不存在的路径，`run()` 必须仍然只认 `env=` 传入的 `tmp`，不能悄悄
        退回真实 `os.environ`——`HealthcheckEnvIsolationTests` 已经用不依赖真库
        的方式把这条不变量单独钉死；这里保留是端到端整条命令路径的真实覆盖。
        """

        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"LINGXI_LIVENESS_DIR": "/nonexistent-should-never-be-read"}):
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


class HealthcheckTtlLatencyContractTests(unittest.TestCase):
    """P1-4（独立审查）：把「各角色 DB 缓存 TTL + retries×(interval+timeout) ≤
    对应 B 类（主循环停摆）最坏时延」钉成一条可执行断言——此前这条不变量只在
    `deploy/监控告警.md` 的表格里人工核对，gateway 的旧 TTL（24s，按活性阈值
    固定 80% 折算）曾经在"网格量化"效应下把真实最坏时延推到 145s、悄悄超过了
    同角色 B 类的 129s（`_compute_db_cache_ttl_seconds` 文档字符串有完整推导），
    表格却因为用了过于简化的公式一直显示"符合"。回归时（例如未来有人改动某个
    角色的活性阈值或检查间隔却忘了回头核对这条关系）直接在测试阶段炸掉，不必
    等到真实环境才发现。

    ``interval``/``timeout``/``retries`` 三个数字对着 `deploy/compose.yaml`
    三个服务各自的 `healthcheck` 小节手抄一份（与 healthcheck 模块内部
    `_HEALTHCHECK_INTERVAL_SECONDS_BY_ROLE` 各自独立登记，不是同一份变量互相
    印证——真要跑偏也能被这条断言看见，因为它同时把 compose.yaml 与 TTL 公式
    两处的假设都摆到明处）：两处都改了才算真的同步，没有自动化门禁跨文件互相
    核对，这一条纪律与 `deploy/监控告警.md`「五、时延估算」的既有免责声明
    一致。
    """

    # deploy/compose.yaml 三个服务的 healthcheck.interval / timeout / retries。
    _INTERVAL_SECONDS = {"scheduler": 30.0, "worker": 29.0, "gateway": 23.0}
    _TIMEOUT_SECONDS = {"scheduler": 10.0, "worker": 10.0, "gateway": 10.0}
    _RETRIES = {"scheduler": 3, "worker": 3, "gateway": 3}

    def test_ttl_plus_retry_budget_never_exceeds_the_liveness_stall_worst_case(self) -> None:
        """A 类公式必须带上网格量化项 ``interval``（NEW-2，独立审查复核 2026-08-29
        坐实并修复）：`_compute_db_cache_ttl_seconds` 文档字符串的推导本身是
        ``(TTL + interval) + retries×(interval+timeout)``——缓存过期只在
        healthcheck 被调用的离散节拍上被发现，不是过期那一刻就立刻触发真实探测，
        因此比"天真地把 TTL 当成精确到期时刻"多付出最多一个 ``interval``。修复前
        本断言漏了这一项（直接 ``ttl + retry_budget``），旧 gateway TTL=24s 那种
        "网格量化把最坏时延从 123s 推到 145s、悄悄超过 B 类 129s"的缺陷不会被这条
        守卫拦下——正是 P1-4 原始事故的形状，若断言本身不带这一项，它只是看起来
        钉住了这条不变量，实际上验证的是一条更宽松、通不过就不该通过的假公式。

        ``TTL=0``（禁用缓存）不加这一项：那时"每一轮都做真实探测"，退化成没有
        TTL 项的 ``retries×(interval+timeout)``，不存在"缓存过期要等下一个节拍
        才被发现"这件事，见该函数文档字符串同一节。"""

        for role, liveness_max_age in healthcheck._DEFAULT_MAX_LIVENESS_AGE_SECONDS.items():
            with self.subTest(role=role):
                interval = self._INTERVAL_SECONDS[role]
                timeout = self._TIMEOUT_SECONDS[role]
                retries = self._RETRIES[role]
                ttl = healthcheck._DEFAULT_DB_CACHE_TTL_SECONDS[role]

                retry_budget = retries * (interval + timeout)
                b_worst_case = liveness_max_age + retry_budget
                grid_quantization = interval if ttl > 0 else 0.0
                a_worst_case = ttl + grid_quantization + retry_budget

                self.assertLessEqual(
                    a_worst_case,
                    b_worst_case,
                    f"{role}: TTL({ttl})+网格量化({grid_quantization})+重试预算"
                    f"({retry_budget})={a_worst_case} 超过了 B 类最坏时延 "
                    f"{b_worst_case}——TTL 公式失去了安全余量",
                )

    def test_ttl_is_never_negative_and_zero_means_disabled_not_a_calculation_artifact(
        self,
    ) -> None:
        for role, ttl in healthcheck._DEFAULT_DB_CACHE_TTL_SECONDS.items():
            with self.subTest(role=role):
                self.assertGreaterEqual(ttl, 0.0)
                expected = max(
                    0.0,
                    healthcheck._DEFAULT_MAX_LIVENESS_AGE_SECONDS[role]
                    - 2 * healthcheck._HEALTHCHECK_INTERVAL_SECONDS_BY_ROLE[role],
                )
                self.assertEqual(ttl, expected)


if __name__ == "__main__":
    unittest.main()
