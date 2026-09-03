#!/usr/bin/env python3
"""校验**已安装的** lingxi 包完整，而不是 ``src/`` 目录完整。

测试跑的是 ``PYTHONPATH=src``，部署跑的是 ``pip install`` 出来的制品。两者会因为
新增子目录、打包配置变化或 ``__init__.py`` 遗漏而分叉，而分叉在部署时才暴露。

本检查刻意**不做条件跳过**。曾经的写法是「``import lingxi`` 成功才检查」，那样
环境坏掉会表现为静默跳过——一个看起来像通过的跳过，比不检查更危险。这里要么
通过，要么失败。

用法：先安装本包，再在**仓库目录之外**运行本脚本。

    python3 check_installed_package.py                      # 制品完整性（全部关键模块）
    python3 check_installed_package.py --process scheduler  # 追加：该进程的运行依赖真的装上了
    python3 check_installed_package.py --source-only        # 只核对源码清单（本地仓库门禁）

``--process`` 是 Issue #56 按进程拆 extras 之后加的。**`src/lingxi/` 里没有任何模块级
第三方 import，全部是函数内延迟导入**，所以「进程入口 import 成功」并不能证明它的运行
依赖装上了——只装一个空环境也照样能 import 成功。要让「某个 extra 漏声明依赖」变红，
必须显式导入第三方模块本身，这正是 ``PROCESS_RUNTIME_IMPORTS`` 的第二个元组在做的事。
"""

from __future__ import annotations

import argparse
import ast
import importlib
import importlib.util
import pathlib
import re
import sys
from collections.abc import Iterable, Mapping
from importlib.metadata import PackageNotFoundError, metadata

# 逐个检查，缺哪个报哪个，不要笼统失败。
REQUIRED_MODULES = (
    # 包初始化文件也属于制品：它们决定 Python 的包边界，不能因为多数为空就从
    # 制品清单里隐身。`scheduler.__main__` 是可执行入口，不能直接 import，下面的
    # `_installed_module_location` 会只取它的已安装文件位置，不启动续期进程。
    "lingxi",
    "lingxi.adapters",
    "lingxi.apps",
    "lingxi.apps.worker",
    # Issue #153：三个常驻进程共用的活性心跳与健康检查命令。它们不是独立进程，
    # 是随镜像一起装的 `python -m lingxi.apps.healthcheck`，由 compose 的
    # `healthcheck.test` 以 `docker exec` 语义调用。
    "lingxi.apps.liveness",
    "lingxi.apps.healthcheck",
    "lingxi.apps.healthcheck.__main__",
    "lingxi.config",
    "lingxi.config.content",
    "lingxi.core",
    "lingxi.core.alerting",
    "lingxi.core.conversation",
    "lingxi.core.execution",
    "lingxi.core.identity",
    "lingxi.core.permission",
    "lingxi.core.ids",
    # 独立审查（分支 fix/291-280-user-experience 收尾）：把 QUERY_MCP_SERVER_NAME
    # 从 adapters.user_environment 挪到这个零依赖模块，避免 worker 为了一个字符串
    # 常量就要拖进整条首次开通编排的 import 闭包（见下面 PROCESS_RUNTIME_IMPORTS
    # 的 worker 闭包同名注释）。
    "lingxi.core.mcp_naming",
    "lingxi.core.identity.onboarding",
    "lingxi.core.identity.identifiers",
    "lingxi.core.identity.credentials",
    "lingxi.core.identity.org_snapshot",
    "lingxi.core.identity.first_contact",
    # Issue #89 写侧建档服务合同：判定层产出 + 花名册原值 → `app_user` 的注入口与
    # 结果分类。生产调用方是 Epic D 的正式 OnboardingRunner，装配前它不在任何进程的
    # import 闭包里，但它必须随制品发布——否则 runner 上线那天才发现 wheel 里没有它。
    "lingxi.core.identity.provisioning",
    # Epic D / S-D-02 的正式首次开通编排与它落盘用户环境的适配器。前者由
    # `apps/gateway/onboarding.py` 在函数内 import，后者同理——两者都必须随制品发布，
    # 否则「本地测试全绿但 wheel 里没有这个模块」会在部署当天才暴露（`V-部署-10`）。
    "lingxi.core.identity.onboarding_runner",
    # Trace #358 S-H-1（Issue #350 Gate G-3 裁定 Option A）纯移动拆分：14 个
    # Protocol + `EnvironmentResult` 搬进本模块，`onboarding_runner.py` 顶部
    # `from .onboarding_ports import (...)`。随 `onboarding_runner.py` 同一条
    # 发布理由——制品缺它会让 runner 装不起来。
    "lingxi.core.identity.onboarding_ports",
    # Trace #358 S-H-1（Issue #350 Gate G-3 裁定 Option A）纯移动拆分：
    # roster_row_for/draft_from_member 两个模块级纯函数搬进本模块，
    # `onboarding_runner.py` 顶部 `from .onboarding_support import (...)`。随
    # `onboarding_runner.py` 同一条发布理由；`tests/test_onboarding_runner.py:27`
    # 仍从 `onboarding_runner` 导入两者（re-export 成立）。
    "lingxi.core.identity.onboarding_support",
    # Trace #358 S-H-1（Issue #350 Gate G-3 裁定 Option A）纯移动拆分：`_Terminal`/
    # 两个异常类/`_with_reference`/两个失败工厂/全部 STATE_*、KEY_* 常量搬进本模块，
    # `onboarding_runner.py` 顶部 `from .onboarding_terminal import (...)`。随
    # `onboarding_runner.py` 同一条发布理由。
    "lingxi.core.identity.onboarding_terminal",
    # 开通链的两道失败关闭闸（rc25 S-2a，对抗审查 X-1）：`_reject_zero_galaxy_
    # without_local_grant` 从 `onboarding_runner.py` 纯移动过来，外加新增的
    # 「同邮箱已绑给另一个 user_id」闸。随 `onboarding_runner.py` 同一条发布理由。
    "lingxi.core.identity.onboarding_guards",
    # 预开通（Issue #541，rc25 S-8a）：系统触发入口的三处差异（合成事件标识、账本
    # no-op、静默投递）与邮箱→飞书身份的提前定位。`onboarding_runner.py` 与
    # `adapters/postgres_stalled_provisioning.py` 都在模块级 import 它，随
    # `onboarding_runner.py` 同一条发布理由。
    "lingxi.core.identity.preprovision",
    # 上面那道邮箱闸的只读回读口（`app_user` 规范化邮箱 → user_id）。由
    # `_build_onboarding_duty` 在函数内 import，同 `postgres_onboarding_failure`
    # 一条理由：函数内 import 证明不了它装得上，必须显式登记。
    "lingxi.adapters.postgres_email_binding",
    # 内测名单闸的纯判定层（Issue #302 S-N-01）。由同一个 `onboarding_runner.py` 里
    # 的 `build_innertest_roster_gate`（rc25 S-8a 从 `AutoOnboardingRunner` 的静态
    # 方法纯移动到本模块，因此 `apps/scheduler/onboarding.py` 改为模块级 import）与
    # `apps/scheduler/config.py` 的 `SchedulerConfig.from_env` 函数内 import，
    # 必须随制品发布，理由同上一条。
    "lingxi.core.identity.innertest_roster_gate",
    "lingxi.adapters.user_environment",
    # Epic D 闸⑥：按用户读取问数 MCP 配置的读侧适配器，配套上一条的写侧。由
    # `apps/worker/service.py` 在**模块级** import（queue 模式每个任务都要
    # 用），漏登记会直接让 worker 制品的完整性核对判红——见下面
    # PROCESS_RUNTIME_IMPORTS 的 worker 闭包。
    "lingxi.adapters.user_mcp_config",
    "lingxi.core.execution.tool_policy",
    "lingxi.core.execution.audit",
    "lingxi.core.execution.hooks",
    "lingxi.core.execution.input_safety",
    "lingxi.core.execution.message_stream",
    "lingxi.core.execution.card_stream",
    # 文档交付触发机制的纯逻辑校验（Issue #341 S-ES-2）。由 `apps/worker/turn.py`
    # 与 `apps/worker/report.py` 函数内/模块级 import，装配前不在任何进程的模块级
    # import 闭包里，但必须随制品发布，理由同上面几条只读工具白名单模块。
    "lingxi.core.execution.document_delivery",
    # 投递事件 outbox 的纯领域逻辑（Issue #151）：终态分类与解析规则，
    # 由 adapters.postgres_conversation 与 apps.worker.service 共同依赖。
    "lingxi.core.delivery",
    "lingxi.core.delivery.ports",
    "lingxi.adapters.claude_agent_hooks",
    "lingxi.core.permission.galaxy_export",
    "lingxi.core.permission.galaxy_scope",
    "lingxi.core.permission.account_match",
    "lingxi.core.permission.role_function",
    # 权限发布（Issue #156 / S-C-01）：聚合与目标行形状、发布意图消费编排在 core，
    # outbox 读写与多维表格写读回在 adapters。四个都要在制品里能 import——生产调用方
    # 是 Epic D 的 OnboardingRunner 与每日权限刷新职责，装配前它们不在任何进程的
    # import 闭包里，"本地测试全绿但 wheel 里没有这个模块"正是 V-部署-10 要挡的形状。
    "lingxi.core.permission.publish_row",
    "lingxi.core.permission.publish",
    "lingxi.adapters.postgres_permission_publish",
    "lingxi.adapters.feishu_permission_bitable",
    # MCP 令牌签发与就绪状态机（Issue #156 / S-C-02）：五路分流状态机在 core，
    # 加解密、令牌与就绪记录读写、问数 MCP 探针在 adapters。与上面四个同一姿态——
    # 生产调用方是 Epic D 的 OnboardingRunner 与每日刷新职责，本 Story 不接进程。
    "lingxi.core.permission.mcp_readiness",
    "lingxi.adapters.mcp_token_cipher",
    "lingxi.adapters.postgres_mcp_token",
    "lingxi.adapters.query_mcp_probe",
    # 存量令牌 adopt-or-issue（Issue #281 改道，Trace #304 批次 3）：只读端口在 core，
    # 飞书 bitable 读取 + 解密翻译在 adapters。生产调用方是 `lingxi-scheduler` 的首次
    # 开通编排（见下面 PROCESS_RUNTIME_IMPORTS 的 scheduler 闭包）。
    "lingxi.core.identity.stock_token_source",
    "lingxi.adapters.stock_token_bitable",
    # 权限变化通知（Issue #156 / S-C-03b）：正文渲染与「有限重试 + 审计」的发送编排在
    # core，向用户本人 open_id 的主动发送在 adapters。两者都有生产调用方——
    # `lingxi-scheduler` 的权限发布与就绪确认职责（见下面 PROCESS_RUNTIME_IMPORTS）。
    "lingxi.core.permission.notification",
    "lingxi.adapters.feishu_user_message",
    # 每日权限重算（Issue #156 / S-C-03a）：按当前有效批次读回银河快照的适配器。
    # 与上面两组不同，它**已经有生产调用方**——`lingxi-scheduler` 的每日权限重算职责
    # （见下面 PROCESS_RUNTIME_IMPORTS 的 scheduler 闭包）。
    "lingxi.adapters.postgres_galaxy_snapshot",
    "lingxi.adapters.galaxy_csv_export",
    "lingxi.adapters.galaxy_import",
    "lingxi.adapters.retention",
    "lingxi.adapters.feishu_roster_bitable",
    "lingxi.adapters.feishu_reauthorization",
    # 花名册审计日报（Issue #52）：比对与渲染在 core，基线读取与群发在 adapters。
    # 四个都要在制品里能 import——它们由 lingxi-scheduler 在运行时按需加载，
    # "本地测试全绿但 wheel 里没有这个模块"正是 V-部署-10 要挡的形状。
    "lingxi.core.identity.roster_audit",
    "lingxi.core.identity.roster_report",
    "lingxi.adapters.postgres_roster_audit",
    # 花名册持久快照（#52 的 S-B-02，D2 裁定后的新载体）：替换门槛、保旧告警事实与
    # 每日取用编排在 core，表读写在 adapters。S-B-04 起两者都在 scheduler 的运行时
    # 闭包里（见下面的 PROCESS_RUNTIME_IMPORTS）。
    "lingxi.core.identity.roster_snapshot",
    "lingxi.adapters.postgres_roster_snapshot",
    # 内测每日通报（Issue #303 S-O-01）：聚合与渲染在 core，六段真库读取在
    # adapters，均由 lingxi-scheduler 在运行时按需加载（见下面 PROCESS_RUNTIME_IMPORTS
    # 的 scheduler 闭包）——"本地测试全绿但 wheel 里没有这个模块"正是 V-部署-10 要挡
    # 的形状。判重水位持久化（Issue #325）新增独立的 watermark 适配器，读写路径
    # 分开（`postgres_daily_report` 只读，本模块只写判重标记），同样按需加载。
    "lingxi.core.daily_report",
    "lingxi.adapters.postgres_daily_report",
    "lingxi.adapters.postgres_daily_report_watermark",
    # 内测轮内容级采集（Issue #251/#304 批次 3）：凭据形状过滤、原始素材收集与
    # 记录构造在 core，落库在 adapters，均由 lingxi-worker 在运行时按开关按需
    # 加载（见下面 PROCESS_RUNTIME_IMPORTS 的 worker 闭包）——"本地测试全绿但
    # wheel 里没有这个模块"正是 V-部署-10 要挡的形状。
    "lingxi.core.innertest_content_capture",
    "lingxi.adapters.postgres_content_capture",
    # 同一张表的**到期删除**侧（对抗审查 2026-09-02 C-7）：由 lingxi-scheduler 的
    # 保留清理职责在函数内 import。刻意与写入侧分成两个模块——写入侧要
    # `ContentCaptureRecord`，那个类会把整个 `core.execution` 拉进 scheduler 的
    # import 闭包，而 scheduler 没有任何理由背上 worker 的执行层。
    "lingxi.adapters.postgres_content_capture_retention",
    # 年份接地护栏第二层（Issue #326 批次 5 卡 E）：纯逻辑判定在 core，由
    # apps/worker/service.py 模块级 import（见下面 PROCESS_RUNTIME_IMPORTS 的
    # worker 闭包）——"本地测试全绿但 wheel 里没有这个模块"同样是 V-部署-10
    # 要挡的形状。
    "lingxi.core.year_grounding_guard",
    # 花名册日报的短期令牌供给规则（Issue #215 主接线）：进程内持有者、每日频率上界与
    # 失败分类。由 lingxi-scheduler 在 `build_loop` 里模块级 import，缺了它进程起不来。
    "lingxi.core.identity.access_token_supply",
    "lingxi.adapters.feishu_group_message",
    "lingxi.adapters.role_function_map_file",
    # 「公司 + 职能 → 指标名」翻译层载体（Issue #227）：校验与翻译规则在 core，
    # 配置文件解析在 adapters。由 lingxi-scheduler 的每日权限重算职责在运行时按需
    # 加载（见下面 PROCESS_RUNTIME_IMPORTS 的 scheduler 闭包）——映射内容当前是空的，
    # 但载体本身必须在制品里，否则内容到位那天才发现 wheel 里没有加载它的代码。
    "lingxi.core.permission.metric_translation",
    "lingxi.adapters.company_function_metric_map_file",
    # 本地权限覆盖（Issue #319）：条目类型与「suppress 赢」冲突判定在 core，迁移
    # 0072 的读写在 adapters（S-P-1a 地基），四源集中合并纯函数同样在 core（S-P-3，
    # 见下一行）。**S-P-3 落地之后这三个模块已经有真实进程调用方**——
    # `permission_refresh.py`/`onboarding_runner.py` 都消费它们，见下面
    # PROCESS_RUNTIME_IMPORTS 的 scheduler 闭包同名注释；这里仍然登记是因为
    # REQUIRED_MODULES 与 PROCESS_RUNTIME_IMPORTS 各自回答不同的问题（制品完整 vs
    # 某个进程的运行时依赖装得上），两处都要有。
    "lingxi.core.permission.local_override",
    "lingxi.core.permission.position_override",
    "lingxi.adapters.postgres_local_permission",
    "lingxi.core.permission.merge_sources",
    # 存量用户首聊差集导入的纯逻辑（rc25 S-1，Issue #540）：开通编排、每日/定向重算
    # 与本地覆盖适配器都消费它（见下面 scheduler/gateway 闭包）；开通链的两步编排
    # （翻译一次 + 导入）从 onboarding_runner 拆出（体量棘轮）。
    "lingxi.core.permission.legacy_diff",
    "lingxi.core.identity.legacy_permission_import",
    # 权限发布表短期令牌供给（Issue #226）：产品负责人 2026-08-18 裁定方向 3
    # （应用身份 tenant_access_token）。方向无关外壳 table_access_token_supply 与
    # 方向实现 tenant_token_supply 都在 core（不做网络 I/O），真实 HTTP 调用在
    # adapters 的 feishu_tenant_token；三个都由 `build_loop` 装配（见下面
    # PROCESS_RUNTIME_IMPORTS 的 scheduler 闭包）。
    "lingxi.core.permission.table_access_token_supply",
    "lingxi.core.permission.tenant_token_supply",
    "lingxi.adapters.feishu_tenant_token",
    "lingxi.adapters.feishu_directory",
    "lingxi.adapters.delegated_credentials",
    "lingxi.adapters.delegated_subject_lookup",
    "lingxi.adapters.oauth_bridge_client",
    "lingxi.adapters.postgres",
    "lingxi.adapters.postgres_identity",
    "lingxi.adapters.claude_agent_session",
    # apps/ 是新增的顶层子目录：进程入口漏进制品只在部署时暴露（V-部署-10），
    # "测试全绿但 python -m 起不来"正是它的形状（Issue #37 / #16）。
    "lingxi.apps.scheduler",
    "lingxi.apps.scheduler.__main__",
    # 每日权限重算职责（Issue #156 / S-C-03a）。它由 `build_loop` 在**模块级**
    # import，因此漏登记会直接让 scheduler 起不来；仍然逐项写出来，理由同上一条。
    "lingxi.apps.scheduler.permission_refresh",
    # 权限发布消费与就绪确认职责（Issue #156 / S-C-03b），同样是模块级 import。
    "lingxi.apps.scheduler.permission_publish",
    # Trace #358 S-H-2（Issue #350 Gate G-3 裁定 Option A）纯移动拆分：就绪确认+
    # 变化通知装配（`_build_readiness_follow_up`）单独成文件而不并入
    # `permission_publish.py`——那个文件承载 `tests/test_permission_publish_duty.py::
    # NonBlockingTest` 的全文件级否定扫描（禁止出现 `sleep` 等等待类词汇），而本函数
    # 装配的通知退避是合法的 `sleep=stop.wait`，会被连坐命中。`assembly.py` 顶部
    # **模块级** import 本模块，漏登记会直接让 scheduler 起不来。
    "lingxi.apps.scheduler.permission_readiness_assembly",
    # #237：`apps/scheduler/__init__.py` 按职责边界拆成的九个子模块（#303 新增
    # daily_report）。全部由包的 `__init__.py` 在**模块级** import 以维持既有的
    # `lingxi.apps.scheduler.<名字>` 重导出契约，因此与上面两条同一姿态——漏登记
    # 会直接让 scheduler 起不来。
    "lingxi.apps.scheduler.config",
    "lingxi.apps.scheduler.audit",
    "lingxi.apps.scheduler.credential_rotation",
    "lingxi.apps.scheduler.retention",
    "lingxi.apps.scheduler.roster_audit",
    "lingxi.apps.scheduler.daily_report",
    "lingxi.apps.scheduler.loop",
    "lingxi.apps.scheduler.assembly",
    "lingxi.apps.scheduler.alerting_assembly",
    # 正式重授权是 scheduler 镜像里的**一次性**运维 job；scripts/ 被 .dockerignore
    # 排除，若这里漏掉 apps/reauthorize，源码测试仍会绿而部署 job 会在镜像内消失。
    "lingxi.apps.reauthorize",
    "lingxi.apps.reauthorize.__main__",
    # 追溯号只读查询 CLI（Issue #280 §7.2）：与 apps/reauthorize 同一姿态——scripts/
    # 被 .dockerignore 排除，一次性运维命令必须显式登记，否则源码测试全绿而部署镜像
    # 里没有它。随 scheduler 镜像一起装，由运维在容器内以 `docker exec` 语义手动调用
    # （不是常驻进程，不需要 compose 服务条目）。
    "lingxi.apps.trace",
    "lingxi.apps.trace.__main__",
    # 管理员角色登记表的一次性种子命令（Issue #95 S-M-01）：同一姿态——scripts/
    # 被 .dockerignore 排除，随 scheduler 镜像一起装，由运维在容器内以
    # `docker exec` 语义手动调用，不是常驻进程。
    "lingxi.apps.admin_bootstrap",
    "lingxi.apps.admin_bootstrap.__main__",
    # 管理员角色登记表判定/命令解析/路由（Issue #95 S-M-01）：纯逻辑，被
    # admin_bootstrap（种子命令，只用 registry）与 gateway 的管理命令面
    # （registry + commands + router）分别消费。
    "lingxi.core.admin",
    "lingxi.core.admin.registry",
    "lingxi.core.admin.commands",
    "lingxi.core.admin.router",
    "lingxi.core.admin.views",
    "lingxi.adapters.admin_registry",
    # 失败原因落库（Issue #337，S-H3-1）：`onboarding_failure` 表（迁移 0077）的
    # 唯一 PostgreSQL 落点。被两处消费——`adapters.admin_registry.
    # PostgresAdminQueries.trace_lookup`（`/admin trace` 查询，只 import
    # `fetch_failure_reason`）与 `apps.scheduler.onboarding`/
    # `apps.scheduler.stalled_provisioning`（`PostgresFailureReasonRecorder`
    # 写入方，两处各自的构造函数内 import，见下面 scheduler 闭包同名条目）。
    "lingxi.adapters.postgres_onboarding_failure",
    # 待确认操作：管理员写动作 prepare/confirm/cancel + 确认卡片/管理群通知渲染 +
    # 卡片回调编排（Issue #96 S-M-02）。全部只被 gateway 的管理命令面消费，与
    # 上面 admin_bootstrap 无关——admin_bootstrap 只播种登记表，不发起写动作。
    "lingxi.core.admin.pending_action",
    "lingxi.core.admin.notification",
    "lingxi.core.admin.card_dispatch",
    "lingxi.core.admin.card_callback",
    "lingxi.adapters.postgres_pending_action",
    "lingxi.adapters.postgres_management_card_context",
    "lingxi.adapters.feishu_admin_card",
    # 回调应答之后那批网络往返的后台执行器（#493 块 B）：确认成功后的出带外换卡、
    # 群通知、原管理卡刷新与定向重算入队搬到应答之后串行执行，回调本身不再等它们。
    "lingxi.adapters.admin_post_callback",
    # 用户权限管理卡（#439 B 档）展示层 + 指标中文别名反查（#439 A 档）的配置
    # 读取——两者均只被 gateway 的管理命令面消费，与上面确认卡片的既有归类
    # 同一姿态。
    "lingxi.core.admin.management_card",
    "lingxi.adapters.admin_metric_alias_map_file",
    # 管理卡片族共用的 CardKit JSON 拼装（按钮横排容器 + form 内 name 校验）与
    # 管理员可见展示名解析口协议（Trace #469 S-1）——被 management_card/
    # notification/card_dispatch/card_callback/router 五处消费，同一 gateway
    # 管理命令面归类。
    "lingxi.core.admin.card_layout",
    "lingxi.core.admin.display_names",
    "lingxi.apps.worker.cli",
    "lingxi.apps.worker.config",
    "lingxi.apps.worker.report",
    "lingxi.apps.worker.turn",
    "lingxi.apps.worker.service",
    # Trace #358 S-H-2（Issue #350 Gate G-3 裁定 Option A）纯移动拆分：8 个
    # 报告字段提取纯函数从 service.py 搬出。`service.py` 顶部**模块级** import
    # 本模块，`apps/worker/cli.py` 也直接 `from .service import
    # _load_task_system_prompt`（经 service.py re-export），漏登记会让两条
    # 路径都装不上。
    "lingxi.apps.worker.report_extraction",
    "lingxi.apps.worker.session_cleanup",
    "lingxi.apps.worker.__main__",
    # S4 前半（#57）新增的 gateway 进程与它的会话领域包。core/conversation/ 是
    # 新的顶层子目录，与 apps/ 当初同一个形状：漏进制品只在部署时暴露。
    "lingxi.core.conversation.commands",
    "lingxi.core.conversation.session_window",
    "lingxi.core.conversation.ports",
    "lingxi.core.conversation.pipeline",
    # Issue #65 轻审 P2-2：未开通首聊交接对账扫描，由 apps/gateway 的 main() 装配。
    "lingxi.core.conversation.onboarding_recovery",
    # 用户记忆（Issue #357 S-H3-3，D1 显式登记范围）：core.conversation.commands/
    # pipeline/ports 与 adapters.postgres_conversation._transaction 均模块级 import
    # 本模块——数据形状（`UserMemoryEntry`）、取值域常量（`MEMORY_TYPES`）与写入/
    # 提示词字符上限集中在这里，两侧调用方共用同一份事实。
    "lingxi.core.user_memory",
    # worker 侧只读拼装（Issue #357 S-H3-3 d 节）：查 user_memory、拼提示词段落，
    # 由 apps/worker/cli.py 在 queue 模式模块级 import（恒装配，不受开关控制）。
    "lingxi.adapters.postgres_user_memory",
    "lingxi.adapters.feishu_events",
    "lingxi.adapters.feishu_longconn",
    "lingxi.adapters.feishu_outbound",
    "lingxi.adapters.postgres_conversation",
    # Issue #239：按读写边界拆成包后的子模块，逐个登记（与 core.conversation
    # 拆包时同一条理由，见上方 #215 注释）。
    "lingxi.adapters.postgres_conversation._dataclasses",
    "lingxi.adapters.postgres_conversation._gateway_store",
    "lingxi.adapters.postgres_conversation._listener",
    "lingxi.adapters.postgres_conversation._queue_base",
    "lingxi.adapters.postgres_conversation._queue_gateway_delivery",
    "lingxi.adapters.postgres_conversation._queue_lifecycle",
    "lingxi.adapters.postgres_conversation._queue_outbox",
    "lingxi.adapters.postgres_conversation._queue_session_cleanup",
    "lingxi.adapters.postgres_conversation._task_queue",
    "lingxi.adapters.postgres_conversation._transaction",
    "lingxi.apps.gateway",
    "lingxi.apps.gateway.config",
    "lingxi.apps.gateway.__main__",
    # Gateway 投递消费循环（Issue #152）：CardKit 流式卡片/文本兜底 adapter 与
    # 消费循环编排，各自都在制品里必须能 import。
    "lingxi.adapters.feishu_delivery",
    "lingxi.apps.gateway.delivery",
    # 第三方飞书 SDK 连接日志的凭据脱敏（Issue #176），在 gateway main() 装配时
    # 调用，制品必须真的带上这个模块。
    "lingxi.apps.gateway.log_redaction",
    # 首次开通编排的装配（Epic D / S-D-02）：由 `apps/gateway/__init__.py` 模块级
    # import，漏登记会直接让 gateway 起不来。
    "lingxi.apps.gateway.onboarding",
    # 群聊@机器人固定引导（Issue #318，Trace #373 S-H1-2 纯移动拆出到独立模块）：
    # `GroupMentionHintThrottle`/`GroupMentionHintResponder` 原实现在
    # `apps/gateway/__init__.py`，现住在 `apps/gateway/group_mention_hint.py`；
    # 由 `apps/gateway/__init__.py` 在**模块级** import，漏登记会直接让 gateway
    # 起不来。
    "lingxi.apps.gateway.group_mention_hint",
    # 管理卡「当前状态」那一行的机器状态→产品术语翻译（Trace #521 F5，#493 P1-3）：
    # 原实现在 `apps/gateway/__init__.py`，为把"停用用户不得看到次日批处理承诺"这条
    # 判定做成可单测的纯函数而拆到 `apps/gateway/management_status.py`；由
    # `apps/gateway/__init__.py` 在**模块级** import，漏登记会直接让 gateway 起不来。
    "lingxi.apps.gateway.management_status",
    # 首次开通编排的装配（Epic D / S-D-02）：产品负责人 2026-08-18 裁定后它住在
    # scheduler，由 `apps/scheduler/__init__.py` 在函数内 import。
    "lingxi.apps.scheduler.onboarding",
    # 组织快照同步职责（Issue #250）：`apps/scheduler/__init__.py` 在**模块级**
    # import `OrgSnapshotSyncDuty`（同 permission_refresh/permission_publish 那一条
    # 理由，漏登记会直接让 scheduler 起不来）；读取编排 `feishu_org_snapshot_reader`
    # 由 `apps/scheduler/assembly.py` 在函数内 import，同 feishu_directory 的姿态。
    "lingxi.apps.scheduler.org_snapshot_sync",
    "lingxi.adapters.feishu_org_snapshot_reader",
    # 迟到就绪恢复职责（V-开通-18）：`apps/scheduler/__init__.py` 在**模块级**
    # import（同 permission_refresh/permission_publish 那一条理由，漏登记会直接
    # 让 scheduler 起不来）。它复用的适配器（postgres_permission_publish、
    # mcp_token_cipher、postgres_mcp_token、query_mcp_probe、feishu_user_message）
    # 已经因为 onboarding/permission_publish 那几节在制品清单里，不重复登记。
    "lingxi.apps.scheduler.late_readiness_recovery",
    # 外部独立审查 F1/F2/F3 修复后新增的持久化面：候选查询、「推进 active + 排通知」
    # 同事务、通知 outbox 的 claim/complete/purge，迁移 0066 建的
    # onboarding_completion_notice 表。由 `_build_late_readiness_recovery_duty` 与
    # `_build_permission_retention_duty` 在函数内 import，同上一条同一条理由。
    "lingxi.adapters.postgres_late_readiness_recovery",
    # 开通中途停摆收口职责（Issue #282，V-开通-19）：`apps/scheduler/__init__.py` 在
    # **模块级** import（同 late_readiness_recovery 那一条理由，漏登记会直接让
    # scheduler 起不来）。
    "lingxi.apps.scheduler.stalled_provisioning",
    # 停摆候选查询的持久化面：由 `_build_stalled_provisioning_duty` 在函数内
    # import，同 `postgres_late_readiness_recovery` 一条理由。
    "lingxi.adapters.postgres_stalled_provisioning",
    # 飞书 docx 交付适配器（Issue #341 S-ES-1）：建文档/写正文/授予「可管理」/协作者
    # 读回四个方法，已由 S0 探针（2026-08-27，四步全通）验证过调用形态。生产调用方
    # 是 S-ES-3 的投递链路，装配前它不在任何进程的 import 闭包里，但同
    # `lingxi.core.identity.provisioning` 一条理由——必须随制品发布，否则接线那天
    # 才发现 wheel 里没有它。
    "lingxi.adapters.feishu_docx_delivery",
    # 飞书电子表格交付适配器（Issue #354 S-H3-2）：建表/写值/授予「可管理」/协作者
    # 读回，同 `feishu_docx_delivery` 一条理由——生产调用方是同一条 S-ES-3 投递
    # 链路（`apps/gateway/document_delivery.py` 按 `delivery_type` 分派），必须
    # 随制品发布。
    "lingxi.adapters.feishu_sheets_delivery",
    # 文档投递独立消费循环（Issue #341 S-ES-3）：`apps/gateway/document_delivery.py`
    # 认领 `task_document_delivery_request` 行、驱动 S-ES-1 的四步交付，持久化面
    # 在 `adapters/postgres_document_delivery.py`。由 `apps/gateway/__init__.py`
    # 在**模块级** import（同 `apps/gateway/delivery.py` 那一条理由，漏登记会直接
    # 让 gateway 起不来）。
    "lingxi.apps.gateway.document_delivery",
    "lingxi.adapters.postgres_document_delivery",
    # 文档投递死信扫描 + 正文到期擦除职责（Issue #341 R-2/`V-投递-06`）：
    # `apps/scheduler/assembly.py` 在**模块级** import（同 late_readiness_recovery/
    # stalled_provisioning 那一条理由，漏登记会直接让 scheduler 起不来）。持久化面
    # 复用上面已经登记过的 `adapters.postgres_document_delivery`，不重复登记。
    "lingxi.apps.scheduler.document_delivery_dead_letter",
    # 管理员写动作确认执行成功后的定向单用户权限重算+发布（Issue #438）：
    # `core/admin/card_callback.py` 的 `PermissionRecomputeTrigger` 端口，由
    # `apps/gateway/__init__.py` 在函数内 import 真实实现装配进去（同该文件其余
    # "函数内 import 证明不了装得上"条目，必须随制品发布）。
    "lingxi.core.permission.targeted_recompute",
    "lingxi.adapters.postgres_targeted_recompute_lookup",
    "lingxi.adapters.postgres_permission_recompute_trigger",
)

# 源码树里仍保留的 Bot-Test / 历史受控验证资产。它们不是正式用户路径的漏项，但正式
# 制品关键模块清单必须明确写出不纳入的理由。这张表是固定政策，不是任意模块的逃生口；
# `check_module_manifests` 会对它逐项核对。2026-08-23 #146 清退后已没有专属进程组会
# 装配这些模块（曾经的 `bot-test` extra 随其消费者一并删除）；它们随基础安装一起
# 存在于制品里，只是不属于任何进程的运行时 import 闭包。
MODULE_MANIFEST_EXEMPTIONS: dict[str, str] = {
    "lingxi.adapters.feishu_bitable_association": "Bot-Test 历史测试资产，不纳入正式用户路径清单",
}

# 这是与上面实际登记表**独立维护**的批准快照。键集和理由全文都故意重复写在这里，
# 不能从 `MODULE_MANIFEST_EXEMPTIONS` 派生；否则新增/改写源码登记会把“漂移检查”变成
# 同一字面量的自引用，永远不会变红。正式变更豁免时必须同时审查并更新这两份冻结数据。
_FROZEN_MODULE_MANIFEST_EXEMPTION_KEYS = frozenset(
    {
        "lingxi.adapters.feishu_bitable_association",
    }
)
_FROZEN_MODULE_MANIFEST_EXEMPTION_REASONS: dict[str, str] = {
    "lingxi.adapters.feishu_bitable_association": "Bot-Test 历史测试资产，不纳入正式用户路径清单",
}

# 这个文件没有 `if __name__ == "__main__"` 保护，直接 import 会启动常驻 scheduler。
# 它仍必须出现在制品清单和 scheduler 的静态依赖闭包里，只是运行时检查改用 find_spec。
_NON_IMPORTABLE_MODULES = frozenset({"lingxi.apps.scheduler.__main__"})

# 随包发布的数据文件：模块导入成功不代表数据文件进了 wheel（后者要靠
# pyproject.toml 的 package-data 声明）。缺失时角色职能会整列变成「未映射」，或让
# 正式用户路径在部署后失去版本化内容目录。
REQUIRED_PACKAGE_DATA = (
    ("lingxi.config", "galaxy_role_function_map.toml"),
    ("lingxi.config", "company_function_metric_map.toml"),
    ("lingxi.config", "content.toml"),
)

_INSTALL_MARKERS = ("site-packages", "dist-packages")

# 按进程分组的运行时依赖（Issue #56）。键与 pyproject.toml 的
# ``[project.optional-dependencies]`` 组名一一对应；值是
# （该进程要导入的 lingxi 模块, 该进程运行时真正需要的第三方模块）。
#
# 第三方那一列是从进程入口逐个追 import 链得到的，不是照抄 pyproject——照抄的话
# 这个检查就永远不会红。CI 在**每个 extra 各自的干净环境**里跑对应的一项，
# 见 .github/workflows/ci.yml 的 `Epic Full / extras` 矩阵。
#
# 第一列还包含沿途所有存在的父包 `__init__`（`lingxi`、`lingxi.core` 等）：Python
# 导入任何子模块前都会先执行这些父包，`process_source_closure` 会把它们一并纳入
# 闭包（Issue #116）。`lingxi.core.conversation` 的 `__init__.py` 本身 re-export
# 了 `commands` / `pipeline` / `session_window`，所以只导入 `...conversation.ports`
# 的 worker 实际上也会连带加载这三个子模块——这是加固前真实存在的登记缺口，
# 不是补一个理论场景。
PROCESS_RUNTIME_IMPORTS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "scheduler": (
        # `scheduler.__main__` 在模块级 `raise SystemExit(main())`（没有 __name__ 卫语句），
        # 运行时检查只用 find_spec 定位它；静态闭包仍必须把这个真正的启动入口登记。
        # 花名册审计日报（#52）的两个 adapter 由 `build_loop` **在函数内** import。
        # 函数内 import 意味着"进程能起来"证明不了"这两个模块装得上"——正是 #29 之后
        # 建立的防漂移机制在这里的缺口：不列进来，extras 那条干净环境的腿永远不会红。
        (
            "lingxi",
            "lingxi.apps",
            "lingxi.apps.scheduler",
            "lingxi.apps.scheduler.__main__",
            # 每日权限重算职责（Issue #156 / S-C-03a）与它拉进来的那一串：重算链路是
            # 「花名册快照 → 银河当前批次 → 匹配 → 聚合 → 发布行 → 权限决定」，因此
            # `core.permission` 整组、发布 outbox 与令牌读取口都进了 scheduler 的运行时
            # 闭包。这是它们**第一次**有真实进程调用方，此前只随制品发布。
            "lingxi.apps.scheduler.permission_refresh",
            # 权限发布消费与就绪确认（Issue #156 / S-C-03b）：它把 S-C-01 的发布执行器、
            # S-C-02 的就绪状态机与探针、以及权限变化通知全部接进了本进程，因此发布表
            # 传输、问数 MCP 探针与用户私聊出站三个 adapter 也进了运行时闭包。
            "lingxi.apps.scheduler.permission_publish",
            # Trace #358 S-H-2 纯移动拆分：`assembly.py` 顶部**模块级** import
            # 本模块（理由见 REQUIRED_MODULES 同名条目——避免 `permission_publish.py`
            # 的全文件级 NonBlockingTest 否定扫描连坐命中合法的通知退避 `sleep`）。
            "lingxi.apps.scheduler.permission_readiness_assembly",
            # 首次开通编排（Epic D / S-D-02）：`build_loop` 在函数内 import 本模块与
            # 它的适配器，因此必须显式登记——不列进来，extras 那条干净环境的腿永远
            # 不会红（与本文件其余「函数内 import」条目同一条理由）。
            "lingxi.apps.scheduler.onboarding",
            "lingxi.core.identity.onboarding_runner",
            # Trace #358 S-H-1（Issue #350 Gate G-3 裁定 Option A）纯移动拆分：
            # `onboarding_runner.py` 顶部**模块级** import 本模块，因此它随
            # `onboarding_runner.py` 一起进了 scheduler 的静态 import 闭包
            # （`process_source_closure` 会自动展开，登记只是把它显式写出来）。
            "lingxi.core.identity.onboarding_ports",
            # Trace #358 S-H-1，同上一条理由：`onboarding_runner.py` 顶部同样
            # **模块级** import 了本模块。
            "lingxi.core.identity.onboarding_support",
            "lingxi.core.identity.onboarding_terminal",
            # 存量令牌 adopt-or-issue（Issue #281 改道，Trace #304 批次 3）：
            # `build_loop` 模块级 import `build_stock_token_source`（`apps/scheduler/
            # onboarding.py`），它在函数内 import 只读端口的飞书 bitable 适配器与
            # `McpTokenCipher`——两者都不在模块级，必须显式登记（与本节其余
            # "函数内 import 证明不了装得上"条目同一条理由）。
            "lingxi.core.identity.stock_token_source",
            "lingxi.adapters.stock_token_bitable",
            # 内测名单闸（Issue #302 S-N-01）：`apps/scheduler/onboarding.py` 模块级
            # import `build_innertest_roster_gate`（rc25 S-8a 之前是经
            # `AutoOnboardingRunner` 的静态方法函数内 import）；`SchedulerConfig.
            # from_env` 解析 `LINGXI_INNERTEST_ROSTER_OPEN_IDS` 时仍是函数内 import。
            "lingxi.core.identity.innertest_roster_gate",
            # 开通链的两道失败关闭闸（rc25 S-2a，对抗审查 X-1）：`onboarding_
            # runner.py` 模块级 import，随它一起进 scheduler 的运行时闭包。
            "lingxi.core.identity.onboarding_guards",
            # 预开通（Issue #541）：`onboarding_runner.py` 与停摆候选查询都在模块级
            # import，随它们一起进 scheduler 的运行时闭包。
            "lingxi.core.identity.preprovision",
            "lingxi.core.identity.provisioning",
            "lingxi.adapters.postgres_identity",
            # 「同邮箱已绑给另一个 user_id」的只读回读口（rc25 S-2a，对抗审查
            # X-1）：`_build_onboarding_duty` 在函数内 import，与上一行同一条
            # "函数内 import 证明不了装得上"的理由。
            "lingxi.adapters.postgres_email_binding",
            "lingxi.adapters.user_environment",
            # 失败原因落库（Issue #337，S-H3-1）：`_build_onboarding_duty`
            # （`apps/scheduler/onboarding.py`）与 `_build_stalled_provisioning_
            # duty`（`apps/scheduler/stalled_provisioning.py`）各自在函数内
            # import `PostgresFailureReasonRecorder`，两个调用点都不在模块级，
            # 必须显式登记。
            "lingxi.adapters.postgres_onboarding_failure",
            # 组织快照同步（Issue #250）：`apps/scheduler/__init__.py` 模块级 import
            # `OrgSnapshotSyncDuty`；读取编排 `feishu_org_snapshot_reader` 由
            # `_build_org_snapshot_sync_duty` 函数内 import，与 onboarding 一节
            # 同一条"函数内 import 证明不了装得上"的理由。
            "lingxi.apps.scheduler.org_snapshot_sync",
            "lingxi.adapters.feishu_org_snapshot_reader",
            # 迟到就绪恢复职责（V-开通-18）：`apps/scheduler/__init__.py` 模块级
            # import，与 org_snapshot_sync 一节同一条"漏登记会直接让 scheduler
            # 起不来"的理由。它的持久化面（`_build_late_readiness_recovery_duty`
            # 与 `_build_permission_retention_duty` 在函数内 import）同样必须登记，
            # 理由与 `feishu_org_snapshot_reader` 那一行相同。
            "lingxi.apps.scheduler.late_readiness_recovery",
            "lingxi.adapters.postgres_late_readiness_recovery",
            # 开通中途停摆收口职责（Issue #282，V-开通-19）：`apps/scheduler/
            # __init__.py` 模块级 import，与 late_readiness_recovery 一节同一条
            # "漏登记会直接让 scheduler 起不来"的理由；它的候选查询持久化面
            # （`_build_stalled_provisioning_duty` 在函数内 import）同样必须登记。
            "lingxi.apps.scheduler.stalled_provisioning",
            "lingxi.adapters.postgres_stalled_provisioning",
            # 文档投递死信扫描 + 正文到期擦除职责（Issue #341 R-2/`V-投递-06`）：
            # `apps/scheduler/assembly.py` 模块级 import，与 late_readiness_recovery/
            # stalled_provisioning 一节同一条"漏登记会直接让 scheduler 起不来"的
            # 理由；它复用的持久化面（`adapters.postgres_document_delivery`）已经
            # 因为 gateway 那一节在制品清单里登记过，这里是 scheduler 进程**自己
            # 的**闭包，两个进程各自独立登记，互不代替。
            "lingxi.apps.scheduler.document_delivery_dead_letter",
            "lingxi.adapters.postgres_document_delivery",
            # #237：`apps/scheduler/__init__.py` 按职责边界拆成的九个子模块（#303
            # 新增 daily_report），全部由包的 `__init__.py` 在模块级 import（维持
            # 既有的 `lingxi.apps.scheduler.<名字>` 重导出契约），因此进程起来时
            # 这九个必然已经被 import 过一遍。
            "lingxi.apps.scheduler.config",
            "lingxi.apps.scheduler.audit",
            "lingxi.apps.scheduler.credential_rotation",
            "lingxi.apps.scheduler.retention",
            # 内测采集九十天到期删除（对抗审查 2026-09-02 C-7）：
            # `_build_content_capture_retention_duty` 函数内 import，与
            # `role_function_map_file` 一节同一条理由——函数内 import 也是进程真实
            # 依赖，制品少了它这条清理职责会在第一轮就抛 ImportError。
            "lingxi.adapters.postgres_content_capture_retention",
            "lingxi.apps.scheduler.roster_audit",
            "lingxi.apps.scheduler.daily_report",
            "lingxi.apps.scheduler.loop",
            "lingxi.apps.scheduler.assembly",
            "lingxi.apps.scheduler.alerting_assembly",
            "lingxi.adapters.feishu_permission_bitable",
            "lingxi.adapters.feishu_user_message",
            "lingxi.adapters.query_mcp_probe",
            "lingxi.core.permission.notification",
            "lingxi.adapters.postgres_galaxy_snapshot",
            "lingxi.adapters.galaxy_import",
            "lingxi.adapters.postgres_permission_publish",
            "lingxi.adapters.mcp_token_cipher",
            "lingxi.adapters.postgres_mcp_token",
            "lingxi.adapters.role_function_map_file",
            # 「公司 + 职能 → 指标名」翻译层载体（Issue #227）：`permission_refresh.py`
            # 模块级 import 翻译规则，`_build_permission_refresh_duty` 函数内 import
            # 配置文件加载器（与 `role_function_map_file` 同一条理由：函数内 import
            # 证明不了"这个模块装得上"）。
            "lingxi.core.permission.metric_translation",
            "lingxi.adapters.company_function_metric_map_file",
            # 四源聚合集中合并（Issue #319 S-P-3）：`permission_refresh.py` 与
            # `onboarding_runner.py` 都模块级 import 本地覆盖的纯逻辑
            # （`resolve_local_overrides`）与合并纯函数（`merge_permission_sources`），
            # `_build_permission_refresh_duty`/`_build_onboarding_duty` 各自函数内
            # import 真实的 Postgres 读取口——这是 `local_override`/
            # `postgres_local_permission` 这两个模块**第一次**有真实进程调用方（S-P-1a
            # 落地时随制品发布但装配前不在任何进程闭包里，见 REQUIRED_MODULES 同名
            # 注释；那条注释现在已经过期，S-P-3 之后它们确实在 scheduler 的运行时
            # 闭包里了）。
            "lingxi.core.permission.local_override",
            "lingxi.adapters.postgres_local_permission",
            "lingxi.core.permission.merge_sources",
            # 存量差集导入纯逻辑（rc25 S-1）：`onboarding_runner`/`permission_refresh`/
            # `postgres_local_permission` 模块级 import；开通链两步编排随 runner 进闭包。
            "lingxi.core.permission.legacy_diff",
            "lingxi.core.identity.legacy_permission_import",
            # 职位＋公司范围预授权（rc25 S-8b，#541）：`postgres_local_permission`
            # 模块级 import 冻结计划类型（`PositionGrantPlan`），随本地覆盖落库口
            # 一并进 scheduler 闭包——预开通脚本必须在 lingxi-scheduler 容器内运行。
            "lingxi.core.permission.position_override",
            # 权限发布表短期令牌供给（Issue #226 方向 3：应用身份）：`build_loop`
            # 模块级 import 方向无关外壳与缓存层，函数内 import 真实 HTTP 调用的
            # adapters（与 `feishu_group_message` 等其余 adapters 同一条理由）。
            "lingxi.core.permission.table_access_token_supply",
            "lingxi.core.permission.tenant_token_supply",
            "lingxi.adapters.feishu_tenant_token",
            "lingxi.core.permission",
            "lingxi.core.permission.account_match",
            "lingxi.core.permission.galaxy_export",
            "lingxi.core.permission.galaxy_scope",
            "lingxi.core.permission.mcp_readiness",
            "lingxi.core.permission.publish",
            "lingxi.core.permission.publish_row",
            "lingxi.core.permission.role_function",
            # Issue #153：main() 装配的健康检查/活性文件命令，随镜像一起装，
            # 由 compose 的 healthcheck.test 以 `docker exec` 语义调用。
            "lingxi.apps.liveness",
            "lingxi.apps.healthcheck",
            "lingxi.apps.healthcheck.__main__",
            # Issue #280 §7.2：追溯号只读查询 CLI，同一姿态——随 scheduler 镜像装，
            # 由运维在容器内以 `docker exec` 语义手动调用，不是常驻进程的一部分。
            "lingxi.apps.trace",
            "lingxi.apps.trace.__main__",
            # 管理员角色登记表种子命令（Issue #95 S-M-01），同一姿态：随 scheduler
            # 镜像装、由运维以 `docker exec` 语义手动调用。模块级 import 用到
            # `lingxi.core.admin.registry.ALL_ADMIN_ROLES`；函数内延迟 import
            # `lingxi.adapters.admin_registry`（种子写入）与 `lingxi.adapters.
            # delegated_credentials`（后者已在本闭包内，被首次开通编排用到），
            # 因此没有新增第三方依赖，只补 lingxi 模块本身。`core.admin.commands`/
            # `core.admin.router` 不在这里——它们只被 gateway 的管理命令面消费，
            # 登记在 gateway 闭包里。
            "lingxi.apps.admin_bootstrap",
            "lingxi.apps.admin_bootstrap.__main__",
            "lingxi.adapters.admin_registry",
            # #319 S-P-1b 卡 B：`adapters/admin_registry.py` 的 `PostgresAdminQueries`
            # 新增「/admin user 回显当前生效本地覆盖」，模块级 import 了
            # `PostgresLocalPermissionOverrideStore`（复用其 `effective_entries()`
            # 读路径，不重新拼一遍同样的 SQL）——这条边把
            # `adapters.postgres_local_permission` 与它引用的
            # `core.permission.local_override` 一并拉进 scheduler 闭包（`admin_
            # registry.py` 本就在这个闭包里，供 admin_bootstrap 种子写入使用）。
            # 两个模块已经在上面「四源聚合集中合并」一节登记过（S-P-3 落地更早），
            # 这里不重复登记同一个字面量——门禁对首列的重复做静态核对，一处登记
            # 已经证明"装得上"，重复只会让核对本身变得可疑。
            # #439 A 档：`PostgresAdminQueries.resolve_metric_name`（指标中文别名
            # 反查）在函数内延迟 import `admin_metric_alias_map_file`——它随
            # `admin_registry.py` 一起进了本闭包（同上"admin_bootstrap 种子写入
            # 使用"一条边），即使 admin_bootstrap 本身从不调用这个方法：静态闭包
            # 检查按模块整体核对可达性，不按函数调用路径区分。纯标准库
            # （`tomllib`），不新增第三方依赖。
            "lingxi.adapters.admin_metric_alias_map_file",
            "lingxi.core.admin",
            "lingxi.core.admin.registry",
            "lingxi.core.admin.views",
            "lingxi.config",
            "lingxi.config.content",
            "lingxi.adapters",
            "lingxi.adapters.delegated_credentials",
            # `delegated_credentials.py` 自身 import 了 `delegated_subject_lookup`
            # 做重新导出（opus 批量审查 P1 修复），因此这条边也在这两个进程的
            # 闭包里——两者本来就已经声明 cryptography，这条新增不改变实际
            # extras 依赖，只是让静态闭包清单如实反映新的 import 边。
            "lingxi.adapters.delegated_subject_lookup",
            "lingxi.adapters.feishu_directory",
            "lingxi.adapters.retention",
            "lingxi.adapters.feishu_group_message",
            "lingxi.adapters.feishu_roster_bitable",
            "lingxi.adapters.postgres_roster_audit",
            # 花名册持久快照（#52 的 S-B-02）自 S-B-04 起有了真实调用方：
            # `build_loop` 在函数内 import 它，与上面两个 adapter 同一条理由。
            "lingxi.adapters.postgres_roster_snapshot",
            # 内测每日通报（Issue #303 S-O-01）：`_build_daily_report_duty` 在函数内
            # import 真库读取口，与上面两个花名册 adapter 同一条"函数内 import 证明
            # 不了装得上"的理由。判重水位持久化（Issue #325）：同一个函数同时
            # import 了独立的 watermark 适配器，理由相同。
            "lingxi.adapters.postgres_daily_report",
            "lingxi.adapters.postgres_daily_report_watermark",
            # 管理卡权限补偿收口（Issue #493）：scheduler 在权限发布每轮末尾读取并更新
            # 管理卡上下文，跨 gateway 重启后仍能按 outbox 的真实 published 状态收口，
            # 因此该持久化 adapter 也属于 scheduler 的运行时闭包。
            "lingxi.adapters.postgres_management_card_context",
            # 上述适配器返回 ``ManagementCardContext``，其类型/构造位于 core.admin
            # 卡片模块；scheduler 虽不接收管理卡回调，但静态 import 闭包仍需如实登记
            # 这些轻量领域模块，避免制品检查把间接依赖误判为缺包。
            "lingxi.core.admin.card_layout",
            "lingxi.core.admin.display_names",
            "lingxi.core.admin.management_card",
            "lingxi.core.admin.notification",
            "lingxi.core.admin.pending_action",
            "lingxi.core.admin.card_dispatch",
            # 「本地权限覆盖活动」段（Issue #319 S-P-1c）：
            # `_build_local_override_activity_check` 在函数内 import 本地权限
            # 覆盖表的读路径，与上面两个通报 adapter 同一条"函数内 import 证明
            # 不了装得上"的理由；该 adapter 模块级 import 了
            # `core.permission.local_override` 的纯类型（`LocalPermissionOverrideEntry`/
            # `OverrideDirection`）——两者都已在「四源聚合集中合并」一节登记过，
            # 这里不重复登记同一个字面量（理由同「本地权限授权/抑制全链路」一节）。
            "lingxi.adapters.postgres",
            # 空闲会话到点清理职责（内审 P2-2）在 `build_loop` 里 import
            # `PostgresTaskQueue`；它的模块级 import 又把整个 `core.conversation`
            # 包（`__init__.py` 一次性 re-export 四个子模块）与 `core.delivery`
            # 一并拉进闭包，同样必须显式登记，理由与上面同一条注释。
            "lingxi.adapters.postgres_conversation",
            # Issue #239：按读写边界拆成包后的子模块，理由同 REQUIRED_MODULES。
            "lingxi.adapters.postgres_conversation._dataclasses",
            "lingxi.adapters.postgres_conversation._gateway_store",
            "lingxi.adapters.postgres_conversation._listener",
            "lingxi.adapters.postgres_conversation._queue_base",
            "lingxi.adapters.postgres_conversation._queue_gateway_delivery",
            "lingxi.adapters.postgres_conversation._queue_lifecycle",
            "lingxi.adapters.postgres_conversation._queue_outbox",
            "lingxi.adapters.postgres_conversation._queue_session_cleanup",
            "lingxi.adapters.postgres_conversation._task_queue",
            "lingxi.adapters.postgres_conversation._transaction",
            # 用户记忆（Issue #357 S-H3-3）：`postgres_conversation._transaction`
            # 模块级 import 本模块，`postgres_permission_publish.record_decision`
            # 的权限真变分支复用 `_transaction` 调用 `clear_user_memory`——同上面
            # "间接依赖也要显式登记"的既有理由，漏登记会直接让 scheduler 制品的
            # 完整性核对判红。
            "lingxi.core.user_memory",
            "lingxi.core",
            "lingxi.core.identity",
            # 花名册日报的短期令牌供给（Issue #215）：由 `apps/scheduler/credential_
            # rotation.py` 与 `apps/scheduler/assembly.py`（#237 拆分后的新位置）
            # 模块级 import，是常驻 scheduler 起进程就要用到的那一条。
            "lingxi.core.identity.access_token_supply",
            "lingxi.core.identity.credentials",
            # `adapters/feishu_directory.py` 自 Epic D / S-D-02 起多了一个在职状态
            # 读取口（`FeishuEmploymentReader`），它把成员详情折成
            # `core.identity.first_contact.EmploymentStatus`。scheduler 本身不用那个
            # 读取口，但它模块级 import 了同一个 adapter，因此这两个模块进了它的闭包。
            "lingxi.core.identity.first_contact",
            "lingxi.core.identity.org_snapshot",
            "lingxi.core.identity.identifiers",
            "lingxi.core.identity.roster_audit",
            "lingxi.core.identity.roster_report",
            "lingxi.core.identity.roster_snapshot",
            # 内测每日通报（Issue #303 S-O-01）：`apps/scheduler/daily_report.py`
            # 模块级 import 聚合与渲染层，与上面三个花名册 core 模块同一条"进程
            # 起来时必然已被 import 过一遍"的理由。
            "lingxi.core.daily_report",
            "lingxi.core.alerting",
            "lingxi.core.ids",
            # 独立审查（分支 fix/291-280-user-experience 收尾）：`adapters.
            # user_environment` 现在从这个零依赖模块导入 QUERY_MCP_SERVER_NAME，
            # 见 worker 闭包那条同名注释。
            "lingxi.core.mcp_naming",
            "lingxi.core.conversation",
            "lingxi.core.conversation.commands",
            "lingxi.core.conversation.onboarding_recovery",
            "lingxi.core.conversation.pipeline",
            "lingxi.core.conversation.ports",
            "lingxi.core.conversation.session_window",
            "lingxi.core.delivery",
            "lingxi.core.delivery.ports",
        ),
        # reauthorize 复用 scheduler 镜像；Bridge 的 WebSocket 依赖也必须在该制品中
        # 显式可导入，虽然常驻 scheduler 入口本身不建立 Bridge 连接。
        # `cryptography.hazmat...ciphers`：首次开通编排（Epic D / S-D-02）用
        # `McpTokenCipher`（AES-256-CBC）签发该用户的问数 MCP 令牌；`fernet` 是宿主机
        # 专用授权凭据文件那一条，两者是同一个包的不同子模块。
        (
            "cryptography.fernet",
            "cryptography.hazmat.primitives.ciphers",
            "psycopg",
            "websockets.sync.client",
        ),
    ),
    "reauthorize": (
        (
            "lingxi",
            "lingxi.apps",
            "lingxi.apps.reauthorize",
            "lingxi.apps.reauthorize.__main__",
            "lingxi.adapters",
            "lingxi.adapters.delegated_credentials",
            # `delegated_credentials.py` 自身 import 了 `delegated_subject_lookup`
            # 做重新导出（opus 批量审查 P1 修复），因此这条边也在这两个进程的
            # 闭包里——两者本来就已经声明 cryptography，这条新增不改变实际
            # extras 依赖，只是让静态闭包清单如实反映新的 import 边。
            "lingxi.adapters.delegated_subject_lookup",
            "lingxi.adapters.feishu_directory",
            "lingxi.adapters.feishu_reauthorization",
            "lingxi.adapters.oauth_bridge_client",
            "lingxi.adapters.postgres",
            "lingxi.core",
            "lingxi.core.identity",
            "lingxi.core.identity.credentials",
            # 同 scheduler 组：`adapters/feishu_directory.py` 自 Epic D / S-D-02 起
            # 多了一个在职状态读取口，它把成员详情折成
            # `core.identity.first_contact.EmploymentStatus`；重授权 job 本身不用那个
            # 读取口，但它模块级 import 了同一个 adapter。渲染文案随之一并进闭包。
            "lingxi.config",
            "lingxi.config.content",
            "lingxi.core.identity.first_contact",
            "lingxi.core.identity.org_snapshot",
            "lingxi.core.identity.identifiers",
            "lingxi.core.ids",
        ),
        ("cryptography.fernet", "psycopg", "websockets.sync.client"),
    ),
    "worker": (
        (
            "lingxi",
            "lingxi.apps",
            "lingxi.apps.worker",
            "lingxi.apps.worker.__main__",
            "lingxi.apps.worker.cli",
            "lingxi.apps.worker.config",
            "lingxi.apps.worker.report",
            "lingxi.apps.worker.turn",
            "lingxi.apps.worker.service",
            # Trace #358 S-H-2 纯移动拆分：`service.py` 顶部**模块级** import
            # 本模块（理由见 REQUIRED_MODULES 同名条目）。
            "lingxi.apps.worker.report_extraction",
            "lingxi.apps.worker.session_cleanup",
            "lingxi.apps.liveness",
            "lingxi.apps.healthcheck",
            "lingxi.apps.healthcheck.__main__",
            "lingxi.adapters",
            "lingxi.adapters.claude_agent_hooks",
            "lingxi.adapters.claude_agent_session",
            # Epic D 闸⑥：按用户读取问数 MCP 配置，由 apps/worker/service.py
            # 模块级 import（queue 模式每个任务都要用）。
            "lingxi.adapters.user_mcp_config",
            # 独立审查（分支 fix/291-280-user-experience 收尾）：Issue #291 P0 曾让
            # apps/worker/config.py 为了取 QUERY_MCP_SERVER_NAME 这一个字符串常量
            # `from lingxi.adapters.user_environment import ...`，把 adapters.
            # user_environment 顶部 import 的 `core/identity/onboarding_runner.py`
            # 整条首次开通编排闭包（身份匹配、花名册、银河、建档等约十二个模块，
            # 曾登记在这里）一并拉进了 worker 的运行时闭包——worker 是处理每一次
            # 真实用户提问的热路径进程，这条闭包与它的职责毫无关系。常量已经挪到
            # 零依赖的 `lingxi.core.mcp_naming`（不 import 任何东西），worker 现在
            # 只需要登记这一行，不再需要上面那整条开通编排链；变异存活证据见
            # `tests/test_worker_entry.py` 的
            # `test_importing_worker_config_does_not_pull_in_the_onboarding_
            # orchestration_chain`。
            "lingxi.core.mcp_naming",
            "lingxi.adapters.postgres",
            "lingxi.adapters.postgres_conversation",
            # Issue #239：按读写边界拆成包后的子模块，理由同 REQUIRED_MODULES。
            "lingxi.adapters.postgres_conversation._dataclasses",
            "lingxi.adapters.postgres_conversation._gateway_store",
            "lingxi.adapters.postgres_conversation._listener",
            "lingxi.adapters.postgres_conversation._queue_base",
            "lingxi.adapters.postgres_conversation._queue_gateway_delivery",
            "lingxi.adapters.postgres_conversation._queue_lifecycle",
            "lingxi.adapters.postgres_conversation._queue_outbox",
            "lingxi.adapters.postgres_conversation._queue_session_cleanup",
            "lingxi.adapters.postgres_conversation._task_queue",
            "lingxi.adapters.postgres_conversation._transaction",
            "lingxi.config",
            "lingxi.config.content",
            "lingxi.core",
            "lingxi.core.alerting",
            "lingxi.core.conversation",
            "lingxi.core.conversation.commands",
            "lingxi.core.conversation.onboarding_recovery",
            "lingxi.core.conversation.pipeline",
            "lingxi.core.conversation.ports",
            "lingxi.core.conversation.session_window",
            # rc25 修复包 F1：预开通首聊补一句要带真实公司/职能范围，
            # `core/conversation/pipeline.py` 因此模块级 import
            # `core.permission.notification.describe_scope` 与
            # `core.permission.publish_row.parse_permissions`（与 onboarding.completed
            # 同一来源）。两个模块及其自身的模块级依赖都是纯函数＋内容目录，不带
            # 第三方依赖，也不拖身份链；worker 不消费这条提示，只是随 pipeline 的
            # import 闭包一并载入。
            "lingxi.core.permission",
            "lingxi.core.permission.account_match",
            "lingxi.core.permission.galaxy_scope",
            "lingxi.core.permission.notification",
            "lingxi.core.permission.publish_row",
            "lingxi.core.permission.role_function",
            # 投递事件 outbox 的纯领域逻辑（Issue #151），由
            # adapters.postgres_conversation 与 apps.worker.service 共同依赖。
            "lingxi.core.delivery",
            "lingxi.core.delivery.ports",
            "lingxi.core.execution",
            "lingxi.core.execution.audit",
            # 语义化等待进度（Issue #321 方向 C）：worker 用它的
            # encode_progress_action/PROGRESS_ACTION_* 常量把工具调用阶段编码进
            # progress 事件的 content 字段，由 apps.worker.service 模块级
            # import；渲染卡片的另一半（decode_progress_action/CardStream 本身）
            # 仍只在 gateway 闭包运行。
            "lingxi.core.execution.card_stream",
            "lingxi.core.execution.hooks",
            "lingxi.core.execution.input_safety",
            "lingxi.core.execution.message_stream",
            "lingxi.core.execution.tool_policy",
            # 文档交付触发机制（Issue #341 S-ES-2），由 apps/worker/turn.py 与
            # apps/worker/report.py 模块级 import（构造 ToolPolicy 白名单合入、
            # 报告投影都需要它，不是按开关才用到的分支）。
            "lingxi.core.execution.document_delivery",
            "lingxi.core.ids",
            # 内测轮内容级采集（Issue #251/#304 批次 3），由 apps.worker.turn/
            # apps.worker.service/apps.worker.cli 模块级 import。
            "lingxi.core.innertest_content_capture",
            "lingxi.adapters.postgres_content_capture",
            # 年份接地护栏第二层（Issue #326 批次 5 卡 E）：由 apps/worker/
            # service.py 模块级 import，import 本身不依赖开关；运行时检测仅在
            # 内容采集开启（content_capture_writer 非空）时才会被调用执行。
            "lingxi.core.year_grounding_guard",
            # 用户记忆注入（Issue #357 S-H3-3 d 节）：`apps/worker/service.py` 在
            # 模块级 import `RenderedUserMemoryPrompt`（Protocol 返回类型标注），
            # `apps/worker/cli.py` 在模块级 import `PostgresUserMemoryReader`
            # （queue 模式恒装配，不像内容采集那样受开关控制），漏登记会直接让
            # worker 制品的完整性核对判红——与上面 postgres_content_capture 同一
            # 条理由。
            "lingxi.core.user_memory",
            "lingxi.adapters.postgres_user_memory",
        ),
        ("claude_agent_sdk", "psycopg"),
    ),
    "gateway": (
        (
            # 注意导入的是承载 ``main`` 的包与 ``__main__``：后者带 ``if __name__``
            # 卫语句（与 worker 同惯例），import 它不会真的把长连接跑起来。
            "lingxi",
            "lingxi.apps",
            "lingxi.apps.gateway",
            "lingxi.apps.gateway.config",
            "lingxi.apps.gateway.__main__",
            "lingxi.apps.liveness",
            "lingxi.apps.healthcheck",
            "lingxi.apps.healthcheck.__main__",
            "lingxi.config",
            "lingxi.config.content",
            "lingxi.adapters",
            "lingxi.adapters.feishu_events",
            "lingxi.adapters.feishu_longconn",
            "lingxi.adapters.feishu_outbound",
            "lingxi.adapters.postgres_conversation",
            # Issue #239：按读写边界拆成包后的子模块，理由同 REQUIRED_MODULES。
            "lingxi.adapters.postgres_conversation._dataclasses",
            "lingxi.adapters.postgres_conversation._gateway_store",
            "lingxi.adapters.postgres_conversation._listener",
            "lingxi.adapters.postgres_conversation._queue_base",
            "lingxi.adapters.postgres_conversation._queue_gateway_delivery",
            "lingxi.adapters.postgres_conversation._queue_lifecycle",
            "lingxi.adapters.postgres_conversation._queue_outbox",
            "lingxi.adapters.postgres_conversation._queue_session_cleanup",
            "lingxi.adapters.postgres_conversation._task_queue",
            "lingxi.adapters.postgres_conversation._transaction",
            "lingxi.adapters.postgres",
            # 投递消费循环（Issue #152）：CardKit/文本兜底 adapter 由
            # apps.gateway.assemble_delivery_consumer 在函数内 import；
            # apps.gateway.delivery 又在函数内 import 到它——两者都不在模块顶层，
            # 因此必须显式登记，理由与本文件其余"函数内 import"条目一致。
            "lingxi.adapters.feishu_delivery",
            "lingxi.apps.gateway.delivery",
            # 最小告警装配（Issue #153）：build_alerting_duty 在函数内 import
            # FeishuGroupMessages（只在配置了管理群时才用得到，其余时候走
            # 日志出口，但两条路径都必须能真的装得上）；FeishuGroupMessages 又
            # 模块级 import adapters.feishu_directory 的 urllib 传输，后者本身
            # 模块级依赖 core.identity.credentials（AuthorizationGrant/SecretToken
            # 类型），因此这条链一并登记，与 scheduler 组同一份依赖来源。
            "lingxi.adapters.feishu_group_message",
            "lingxi.adapters.feishu_directory",
            # 管理命令面（Issue #95 S-M-01）：build_supervisor 在函数内 import
            # PostgresAdminRegistryLookup/PostgresAdminQueries，无条件装配（不受
            # 任何 feature flag 控制，见该函数内注释），因此这条闭包必须显式登记。
            "lingxi.adapters.admin_registry",
            # `/admin trace <追溯号>`（Issue #337，S-H3-1）：`PostgresAdminQueries.
            # trace_lookup` 模块级 import `fetch_failure_reason`，随
            # `admin_registry` 一起进了 gateway 的运行时闭包。
            "lingxi.adapters.postgres_onboarding_failure",
            # 专用主体结构性出口前置（opus P3-1）：build_supervisor 在函数内 import
            # registered_delegated_subject_open_id，装配期读一次登记表把结果算成
            # 一个普通字符串交给管线（见该函数内注释）。刻意登记
            # `delegated_subject_lookup`（不是 `delegated_credentials`）：后者其余
            # 部分依赖 cryptography（Fernet），而 gateway extras 组明确不含它
            # （2026-08-18 裁定，见 `adapters/delegated_subject_lookup.py` 模块
            # 文档）；这个更小的模块本身只依赖 `adapters.postgres`，不新增
            # `core.identity.identifiers` 这条闭包。
            "lingxi.adapters.delegated_subject_lookup",
            # 内测名单闸的 gateway 侧前移一份（Issue #302 S-N-01 的纵深）：
            # build_supervisor 在函数内 import is_open_id_innertest_allowed，
            # 把 config.innertest_roster_open_ids 包成管线要的判定口。
            "lingxi.core.identity.innertest_roster_gate",
            "lingxi.core",
            "lingxi.core.admin",
            "lingxi.core.admin.registry",
            "lingxi.core.admin.commands",
            "lingxi.core.admin.router",
            "lingxi.core.admin.views",
            # 待确认操作全链路（Issue #96 S-M-02）：build_supervisor 在函数内
            # import PostgresPendingActionStore、LarkAdminCardTransport、
            # ConfirmCardDispatcher、AdminCardCallbackHandler，无条件装配（与
            # 管理命令面本身同一姿态，不受任何 feature flag 控制）；
            # make_event_handler 把 card_callback_handler 接进
            # card.action.trigger 事件分流，复用的 `parse_card_action_event`/
            # `CARD_ACTION_TRIGGER_EVENT` 来自既有的 `adapters.feishu_events`
            # 模块级 import（第 677 行已登记，不重复登记），这里只补
            # core.admin 的四个新模块与两个新 adapter。
            "lingxi.core.admin.pending_action",
            "lingxi.core.admin.notification",
            "lingxi.core.admin.card_dispatch",
            "lingxi.core.admin.card_callback",
            "lingxi.adapters.postgres_pending_action",
            "lingxi.adapters.postgres_management_card_context",
            "lingxi.adapters.feishu_admin_card",
            "lingxi.adapters.admin_post_callback",
            # 用户权限管理卡（#439 B 档）：`/admin user` 除既有文本回复外附带发送
            # 一张管理卡（`ManagementCardDispatcher`/`TomlCompanyMetricCatalog`，
            # 均在函数内由装配层按需 import，与本组其余"函数内 import 也要显式
            # 登记"条目同一姿态）。`admin_registry.PostgresAdminQueries.
            # resolve_metric_name` 同样在函数内 import `admin_metric_alias_map_
            # file`（#439 A 档指标中文别名反查）。四个新模块均为纯标准库
            # （`tomllib`），不新增第三方依赖，只补齐制品完整性登记。
            "lingxi.core.admin.management_card",
            "lingxi.adapters.admin_metric_alias_map_file",
            "lingxi.adapters.company_function_metric_map_file",
            "lingxi.core.permission.metric_translation",
            # 管理卡片族共用的 CardKit JSON 拼装与管理员可见展示名解析口协议
            # （Trace #469 S-1）：notification/management_card/card_dispatch/
            # card_callback/router 五个既登记模块的模块级 import，跟随它们
            # 一起进入 gateway 的运行时闭包。
            "lingxi.core.admin.card_layout",
            "lingxi.core.admin.display_names",
            # 本地权限授权/抑制全链路（#319 S-P-1b）：
            # adapters.postgres_pending_action 模块级 import 了
            # adapters.postgres_local_permission 的 _insert_locked/
            # DuplicateActiveOverride（confirm() 同一事务内落库本地权限覆盖行，
            # 见该模块文档「为什么拆分」），以及 core.permission.local_override
            # 的 LocalPermissionOverrideEntry/OverrideDirection（纯类型，供
            # confirm() 解析 payload 后构造要写入的条目）。
            "lingxi.adapters.postgres_local_permission",
            "lingxi.core.permission.local_override",
            "lingxi.core.permission.position_override",
            # 管理员写动作确认执行成功后的定向单用户权限重算+发布（Issue #438）：
            # `card_callback_handler` 装配处在函数内 import
            # `PermissionRecomputeAdapter`（`adapters/postgres_permission_
            # recompute_trigger.py`），无条件装配（与待确认操作全链路本身同一
            # 姿态）。它在**自己被调用时**（`.trigger()` 内）才函数内 import 一整条
            # "花名册基线 → 花名册快照 → 银河快照 → 权限聚合/合并/翻译 → 发布
            # outbox"的只读链路——这条链路此前只有 scheduler 进程真正调用过（见
            # 上面 scheduler 组同名注释），这是它**第一次**也随 gateway 进程的运行
            # 依赖闭包一起发布。
            "lingxi.core.permission.targeted_recompute",
            "lingxi.adapters.postgres_targeted_recompute_lookup",
            "lingxi.adapters.postgres_permission_recompute_trigger",
            "lingxi.core.identity.roster_audit",
            "lingxi.adapters.postgres_roster_audit",
            "lingxi.core.identity.roster_snapshot",
            "lingxi.adapters.postgres_roster_snapshot",
            "lingxi.adapters.feishu_roster_bitable",
            "lingxi.adapters.postgres_galaxy_snapshot",
            "lingxi.core.permission.galaxy_export",
            "lingxi.core.permission.galaxy_scope",
            "lingxi.adapters.galaxy_import",
            "lingxi.core.permission.account_match",
            "lingxi.core.permission.merge_sources",
            # 存量差集导入纯逻辑（rc25 S-1）：随 `adapters.postgres_local_permission`、
            # `core.permission.targeted_recompute` 进入 gateway 闭包。
            "lingxi.core.permission.legacy_diff",
            "lingxi.core.permission.publish_row",
            # rc25 修复包 F1：pipeline 渲染预开通首聊那句时用
            # `core.permission.notification.describe_scope`（公司/职能展示文本，
            # 与 onboarding.completed 同一来源）。
            "lingxi.core.permission.notification",
            "lingxi.core.permission.publish",
            "lingxi.core.permission.mcp_readiness",
            "lingxi.core.permission.role_function",
            "lingxi.adapters.postgres_permission_publish",
            "lingxi.adapters.role_function_map_file",
            "lingxi.core.alerting",
            "lingxi.core.identity",
            "lingxi.core.identity.credentials",
            # 群聊@机器人固定引导（Issue #318，Trace #373 S-H1-2 纯移动拆出到
            # `apps/gateway/group_mention_hint.py`）：`apps/gateway/__init__.py`
            # 在**模块级** import 该子模块（构造 `GroupMentionHintResponder`
            # 装配进 `build_supervisor`），该子模块自己再模块级 import
            # `redact_identifier`，把要发这条提示的 chat_id 记进日志（不是结构化
            # 审计字段，见该模块内注释与 `V-花名册-34`）。
            "lingxi.apps.gateway.group_mention_hint",
            # 管理卡状态文案翻译（Trace #521 F5，#493 P1-3）：`apps/gateway/__init__.py`
            # 模块级 import，该子模块自己再模块级 import `config.content`（版本化文案
            # 目录）与 `core.permission.targeted_recompute`（跳过原因码）——两者都已经
            # 在本组里，这里只补它自己这一条。
            "lingxi.apps.gateway.management_status",
            "lingxi.core.identity.identifiers",
            # `adapters/feishu_directory.py` 的在职状态读取口把成员详情折成
            # `core.identity.first_contact.EmploymentStatus`。gateway 本身不用那个读取口
            # （首次开通编排在 scheduler），但它模块级 import 了同一个 adapter。
            "lingxi.core.identity.first_contact",
            "lingxi.core.identity.org_snapshot",
            "lingxi.core.conversation",
            "lingxi.core.conversation.commands",
            "lingxi.core.conversation.onboarding_recovery",
            "lingxi.core.conversation.pipeline",
            "lingxi.core.conversation.ports",
            "lingxi.core.conversation.session_window",
            # 用户记忆（Issue #357 S-H3-3）：core.conversation.commands/pipeline/
            # ports 与 adapters.postgres_conversation._transaction 均模块级 import
            # 本模块（/memory 命令面数据形状），漏登记会直接让 gateway 制品的完整性
            # 核对判红——同上面几条"间接依赖也要显式登记"的既有理由。
            "lingxi.core.user_memory",
            # gateway 通过 adapters.postgres_conversation 间接依赖投递事件 outbox
            # 的纯领域逻辑（Issue #151）：任务/会话查询共用同一份 core.delivery.ports
            # 终态解析规则。apps.gateway.delivery 额外直接依赖 core.execution.*
            # ——卡片顺序、限流与失败回退（Issue #152）。
            "lingxi.core.delivery",
            "lingxi.core.delivery.ports",
            "lingxi.core.execution",
            "lingxi.core.execution.card_stream",
            "lingxi.core.ids",
            # 第三方 SDK 连接日志的凭据脱敏（Issue #176），main() 顶层 import；
            # 复用 core.execution.audit 里唯一一份查询参数脱敏纯函数，该模块又
            # 模块级依赖 core.execution.tool_policy（DenyReasonCode 等审计形状），
            # 因此这条链一并登记。
            "lingxi.apps.gateway.log_redaction",
            "lingxi.core.execution.audit",
            "lingxi.core.execution.tool_policy",
            # 首次开通在 gateway 侧只剩「记事件 + 回第一条提示」（产品负责人
            # 2026-08-18 裁定把编排整体移进 scheduler）。因此这里只有那条装配断言模块，
            # 整条判定链与它的适配器都在 scheduler 组，不在本进程的闭包里。
            "lingxi.apps.gateway.onboarding",
            # 文档投递独立消费循环（Issue #341 S-ES-3）：由 `apps/gateway/__init__.py`
            # 模块级 import `apps.gateway.document_delivery`（同 `apps.gateway.delivery`
            # 那一条理由）；`assemble_document_delivery_consumer` 在函数内 import
            # 建文档四步适配器（`feishu_docx_delivery`）、令牌供给三件套
            # （`core.identity.access_token_supply`/`core.permission`/
            # `core.permission.table_access_token_supply`/
            # `core.permission.tenant_token_supply`/`feishu_tenant_token`，与
            # scheduler 组「应用身份令牌」那条闭包同一来源）、完成通知出口
            # （`feishu_user_message`，已因 scheduler 权限变化通知在 REQUIRED_MODULES
            # 里，这里是它第一次进入 gateway 自己的运行时闭包）、以及持久化面
            # （`postgres_document_delivery`）。表格分支（Issue #354 S-H3-2）在同一个
            # 函数里同时 import 建表适配器（`feishu_sheets_delivery`），复用同一套
            # 令牌供给与持久化面，不新增闭包分支。
            "lingxi.apps.gateway.document_delivery",
            "lingxi.adapters.postgres_document_delivery",
            "lingxi.adapters.feishu_docx_delivery",
            "lingxi.adapters.feishu_sheets_delivery",
            "lingxi.adapters.feishu_tenant_token",
            "lingxi.adapters.feishu_user_message",
            "lingxi.core.identity.access_token_supply",
            "lingxi.core.permission",
            "lingxi.core.permission.table_access_token_supply",
            "lingxi.core.permission.tenant_token_supply",
        ),
        # websockets 显式列出，尽管 lark-oapi 传递携带它——理由见 pyproject.toml
        # 的 [gateway] 组注释。这里取 ``websockets.exceptions``（lark 实际 import
        # 的那个子模块）而不是顶层包：websockets 15 的顶层做了惰性导入，
        # ``import websockets`` 成功证明不了子模块装全了。
        ("lark_oapi", "psycopg", "websockets.exceptions"),
    ),
    # 2026-08-23 #146 清退：`bot-test` 进程组随其三个专属消费者
    # （feishu_onboarding/refresh_tokens/postgres_onboarding）一并删除。
    # 2026-08-24 #203 清退：`adapters/oauth_bridge.py`（原 #67 裁定保留的 E1
    # 授权基础设施参考实现，消费者复核为零后由产品负责人裁定清退）随之删除。
    # 正式重授权入口的传输层是保留件 `adapters/oauth_bridge_client.py`，其依赖
    # 已由 scheduler/reauthorize 两组声明覆盖，因此也不需要为它单独保留进程组；
    # `core.identity.onboarding` 仍由 scheduler 多处消费，长期保留。
    # 迁移作业（Issue #53）：部署时跑一次 `python -m alembic upgrade head`，不是常驻
    # 进程。**lingxi 模块那一列刻意为空**——迁移工具链不得渗入运行时代码
    # （断言 V-迁移-04：`grep -rn "sqlalchemy\|alembic" src/` 必须为空），
    # 所以这一组没有任何 lingxi 入口，只有第三方那一列要证明装得上。
    #
    # psycopg 与 alembic 并列，不是冗余：alembic 自己不依赖任何驱动，驱动由 URL 的
    # scheme 决定。少了它，`upgrade head` 在干净环境里报 ModuleNotFoundError，而
    # 这条矩阵腿是唯一会在干净环境里跑的检查（外审实测出的缺口）。
    "migrate": ((), ("alembic", "psycopg")),
}

# 每个 extra 的源码入口。下面的静态闭包会遍历函数体内的延迟 import，反向证明
# PROCESS_RUNTIME_IMPORTS 没有漏掉某个实际会被该进程加载的 lingxi 模块。
PROCESS_SOURCE_ENTRY_POINTS: dict[str, tuple[str, ...]] = {
    # `apps.healthcheck.__main__` 追加进三个进程各自的入口集合：它不是被
    # scheduler/gateway/worker 的模块 import 到的，是随同一个镜像装的独立命令，
    # 由 compose 的 `healthcheck.test` 以 `docker exec` 语义单独调用（Issue
    # #153）。加进来才能让静态闭包真的走到它的函数内 import（`lingxi.adapters.
    # postgres`），反向证明每个进程的 extra 里健康检查命令本身也装得上。
    "scheduler": (
        "lingxi.apps.scheduler",
        "lingxi.apps.scheduler.__main__",
        "lingxi.apps.healthcheck.__main__",
        # 追溯号只读查询 CLI（Issue #280 §7.2）：同一姿态，随镜像装、独立调用，
        # 加进来才能让静态闭包真的走到它函数内的 `lingxi.adapters.postgres` 导入。
        "lingxi.apps.trace.__main__",
        # 管理员角色登记表种子命令（Issue #95 S-M-01），同一姿态：加进来才能让
        # 静态闭包走到 `run()` 函数内的 `lingxi.adapters.admin_registry`/
        # `lingxi.adapters.delegated_credentials` 延迟 import。
        "lingxi.apps.admin_bootstrap.__main__",
    ),
    "reauthorize": ("lingxi.apps.reauthorize.__main__",),
    "worker": ("lingxi.apps.worker.__main__", "lingxi.apps.healthcheck.__main__"),
    "gateway": (
        "lingxi.apps.gateway",
        "lingxi.apps.gateway.__main__",
        "lingxi.apps.healthcheck.__main__",
    ),
    # 迁移作业运行 alembic，不加载任何 lingxi 模块；这是显式边界，不是漏登记。
    "migrate": (),
}

PROCESS_ENTRY_EXEMPTIONS: dict[str, str] = {
    "migrate": "迁移作业只运行 alembic upgrade，不绑定 lingxi 运行时模块",
}
# 与 `PROCESS_ENTRY_EXEMPTIONS` 分开冻结，防止迁移边界理由被改写后仍与自身副本相等。
_FROZEN_PROCESS_ENTRY_EXEMPTION_KEYS = frozenset({"migrate"})
_FROZEN_PROCESS_ENTRY_EXEMPTION_REASONS: dict[str, str] = {
    "migrate": "迁移作业只运行 alembic upgrade，不绑定 lingxi 运行时模块",
}


# CI 的 extras 矩阵所在文件。用 ``__file__`` 定位而不是 cwd：本检查刻意在仓库目录
# 之外运行，但它自己始终躺在仓库里，CI 也是按绝对路径调用它的。
CI_WORKFLOW = pathlib.Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"
SOURCE_ROOT = pathlib.Path(__file__).resolve().parents[2] / "src" / "lingxi"

# 刻意不进 CI 矩阵的组。**目前为空**；往里加必须在注释里写清为什么该组不需要
# 「干净环境里装一次」的证明，否则这就成了漏加矩阵行的后门。
MATRIX_EXEMPT_EXTRAS: frozenset[str] = frozenset()

# 只认单行写法 `extra: [a, b, c]`。改成多行 YAML 列表时这里会找不到而**失败**，
# 不是静默通过——找不到就当作对不上账。
_MATRIX_LINE = re.compile(r"^[ \t]*extra:[ \t]*\[([^\]]*)\]", re.MULTILINE)


def source_module_files(source_root: pathlib.Path | None = None) -> dict[str, pathlib.Path]:
    """返回 `src/lingxi/` 中每个 Python 模块的源码文件。

    包初始化文件用包名表示（例如 `__init__.py` → `lingxi.apps`），因为它们同样
    决定制品的可导入边界。清单完整性只从这个反向枚举得到基准，不从手工清单反推
    源码，所以清单少一项时一定能被发现。
    """

    root = SOURCE_ROOT if source_root is None else source_root
    if not root.is_dir():
        return {}

    found: dict[str, pathlib.Path] = {}
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root)
        parts = list(relative.parts)
        if parts[-1] == "__init__.py":
            parts = parts[:-1]
        else:
            parts[-1] = parts[-1][: -len(".py")]
        module = ".".join(["lingxi", *parts])
        found[module] = path
    return found


def source_module_names(source_root: pathlib.Path | None = None) -> set[str]:
    """返回源码树实际存在的模块名集合，供门禁和白盒测试共同使用。"""

    return set(source_module_files(source_root))


# 兼容门禁脚本中“枚举模块”的自然读法；实现只保留一份。
iter_source_modules = source_module_names


def _resolve_source_module(target: str, source_files: Mapping[str, pathlib.Path]) -> str | None:
    """把 `from lingxi.x import Symbol` 的目标归一成实际源码模块名。"""

    if not target.startswith("lingxi"):
        return None
    parts = target.split(".")
    for end in range(len(parts), 0, -1):
        candidate = ".".join(parts[:end])
        if candidate in source_files:
            return candidate
    return None


def _source_imports(module: str, source_files: Mapping[str, pathlib.Path]) -> set[str]:
    """读取一个源码模块的 lingxi import，包含函数体和相对 import。"""

    path = source_files[module]
    package = module if path.name == "__init__.py" else module.rsplit(".", 1)[0]
    found: set[str] = set()
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    def add_target(target: str) -> None:
        resolved = _resolve_source_module(target, source_files)
        if resolved is not None:
            found.add(resolved)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                add_target(alias.name)
            continue
        if not isinstance(node, ast.ImportFrom):
            continue

        if node.level:
            base = package.split(".")
            anchor = base[: len(base) - node.level + 1]
            prefix = ".".join(anchor)
            base_target = ".".join(part for part in (prefix, node.module or "") if part)
            if base_target:
                add_target(base_target)
            for alias in node.names:
                if base_target:
                    add_target(f"{base_target}.{alias.name}")
                elif prefix:
                    add_target(f"{prefix}.{alias.name}")
            continue

        if node.module:
            add_target(node.module)
            for alias in node.names:
                add_target(f"{node.module}.{alias.name}")

    return found


def _ancestor_packages(module: str, source_files: Mapping[str, pathlib.Path]) -> list[str]:
    """返回一个模块路径上、在源码树里确实存在 `__init__.py` 的父包（不含自身）。

    Python 导入 ``lingxi.core.conversation.pipeline`` 之前，会先依次执行
    ``lingxi/__init__.py``、``lingxi/core/__init__.py``、
    ``lingxi/core/conversation/__init__.py``——这些父包 `__init__` 是该模块真实会
    被加载的一部分，即便当前它们是空文件或只 import 已登记的子模块。若不把它们
    带进闭包，父包 `__init__` 日后新增未登记依赖时，`--source-only` 门禁看不见，
    只会在部署后的干净镜像里才暴露（Issue #116）。
    """

    parts = module.split(".")
    ancestors: list[str] = []
    for end in range(1, len(parts)):
        candidate = ".".join(parts[:end])
        if candidate in source_files:
            ancestors.append(candidate)
    return ancestors


def process_source_closure(
    extra: str, source_files: Mapping[str, pathlib.Path] | None = None
) -> set[str]:
    """计算一个进程入口实际会加载的 lingxi 模块闭包，含沿途所有存在的父包 `__init__`。"""

    files = source_module_files() if source_files is None else source_files
    roots = PROCESS_SOURCE_ENTRY_POINTS[extra]
    seen: set[str] = set()
    pending = list(roots)
    while pending:
        module = pending.pop()
        if module in seen or module not in files:
            continue
        seen.add(module)
        pending.extend(_source_imports(module, files) - seen)
        pending.extend(name for name in _ancestor_packages(module, files) if name not in seen)
    return seen


def check_module_manifests(
    *,
    source_modules: set[str] | None = None,
    required_modules: Iterable[str] | None = None,
    process_runtime_imports: Mapping[str, tuple[tuple[str, ...], tuple[str, ...]]] | None = None,
    exemptions: Mapping[str, str] | None = None,
) -> list[str]:
    """反向核对制品清单、进程模块清单与源码实际模块。

    `REQUIRED_MODULES` 负责正式制品；`PROCESS_RUNTIME_IMPORTS` 第一列负责每个进程的
    lingxi import 闭包；源码中仅保留的 Bot-Test 资产走显式豁免。三者都从源码反向
    检查，清单漏项、陈旧项、错误豁免和进程闭包漏项都会返回失败，而不是静默通过。
    """

    files = source_module_files()
    actual_source = set(files) if source_modules is None else set(source_modules)
    required = tuple(REQUIRED_MODULES if required_modules is None else required_modules)
    process = (
        PROCESS_RUNTIME_IMPORTS if process_runtime_imports is None else process_runtime_imports
    )
    actual_exemptions = dict(
        MODULE_MANIFEST_EXEMPTIONS if exemptions is None else exemptions
    )
    failures: list[str] = []

    if not files:
        failures.append(f"{SOURCE_ROOT}：找不到 `src/lingxi/` 或其中没有 Python 模块")
        return failures

    if len(required) != len(set(required)):
        failures.append("REQUIRED_MODULES：存在重复登记，清单必须逐项且唯一")
    frozen_exemption_names = set(_FROZEN_MODULE_MANIFEST_EXEMPTION_KEYS)
    frozen_exemption_reasons = _FROZEN_MODULE_MANIFEST_EXEMPTION_REASONS
    actual_exemption_names = set(actual_exemptions)
    if set(frozen_exemption_reasons) != frozen_exemption_names:
        failures.append("模块豁免冻结键集与冻结理由全文不一致，冻结清单本身需要修复。")
    for name in sorted(actual_exemption_names - frozen_exemption_names):
        failures.append(
            f"豁免 `{name}`：不是已批准的模块豁免；不能用错误豁免掩盖制品清单漏项。"
        )
    for name in sorted(frozen_exemption_names - actual_exemption_names):
        failures.append(f"豁免 `{name}`：已批准但未登记，必须保留可审查理由。")
    for name, reason in sorted(actual_exemptions.items()):
        if not isinstance(reason, str) or not reason.strip():
            failures.append(f"豁免 `{name}`：缺少理由，不能静默忽略源码模块。")
        if name not in actual_source:
            failures.append(f"豁免 `{name}`：源码中不存在，豁免登记已陈旧。")
        elif name in frozen_exemption_names and reason != frozen_exemption_reasons.get(name):
            failures.append(f"豁免 `{name}`：理由与已批准政策不一致，不能借改名扩大豁免范围。")

    required_set = set(required)
    for name in sorted(actual_source - actual_exemption_names - required_set):
        failures.append(
            f"模块 `{name}`：存在于 src/lingxi/，但未登记进 REQUIRED_MODULES，"
            "也没有有效的显式豁免。"
        )
    for name in sorted(required_set - actual_source):
        failures.append(f"REQUIRED_MODULES：登记了不存在的模块 `{name}`。")
    for name in sorted(required_set & actual_exemption_names):
        failures.append(
            f"模块 `{name}`：同时出现在 REQUIRED_MODULES 和豁免表，归类矛盾；"
            "请保留正式制品登记或明确的资产豁免。"
        )

    expected_processes = set(PROCESS_SOURCE_ENTRY_POINTS)
    actual_processes = set(process)
    for extra in sorted(expected_processes - actual_processes):
        failures.append(f"PROCESS_RUNTIME_IMPORTS：缺少进程 `{extra}` 的模块清单。")
    for extra in sorted(actual_processes - expected_processes):
        failures.append(f"PROCESS_RUNTIME_IMPORTS：登记了未知进程 `{extra}`。")

    actual_entry_exemptions = PROCESS_ENTRY_EXEMPTIONS
    frozen_entry_names = set(_FROZEN_PROCESS_ENTRY_EXEMPTION_KEYS)
    if (
        set(_FROZEN_PROCESS_ENTRY_EXEMPTION_REASONS) != frozen_entry_names
        or set(actual_entry_exemptions) != frozen_entry_names
        or any(
            actual_entry_exemptions.get(name) != _FROZEN_PROCESS_ENTRY_EXEMPTION_REASONS.get(name)
            for name in frozen_entry_names
        )
    ):
        failures.append("PROCESS_ENTRY_EXEMPTIONS：进程入口豁免发生漂移，必须保留迁移边界理由。")

    for extra in sorted(expected_processes & actual_processes):
        lingxi_modules, _third_party_modules = process[extra]
        listed = tuple(lingxi_modules)
        listed_set = set(listed)
        if len(listed) != len(listed_set):
            failures.append(f"进程 `{extra}`：PROCESS_RUNTIME_IMPORTS 第一列存在重复模块。")

        for name in sorted(listed_set - actual_source):
            failures.append(f"进程 `{extra}`：登记了不存在的模块 `{name}`。")

        try:
            expected = process_source_closure(extra, files)
        except (OSError, SyntaxError) as error:
            failures.append(f"进程 `{extra}`：无法解析源码 import 闭包（{type(error).__name__}: {error}）。")
            expected = set()

        for name in sorted(expected - listed_set):
            failures.append(
                f"进程 `{extra}`：源码 import 闭包使用 `{name}`，但未登记进"
                " PROCESS_RUNTIME_IMPORTS 第一列。"
            )
        for name in sorted(listed_set - expected):
            failures.append(
                f"进程 `{extra}`：登记了 `{name}`，但它不在该进程的源码 import 闭包中；"
                "请移除陈旧项或补充真实入口。"
            )

        if expected and extra in PROCESS_ENTRY_EXEMPTIONS:
            failures.append(
                f"进程 `{extra}`：存在实际 lingxi import 闭包，却登记为入口豁免。"
            )
        if not expected and extra not in PROCESS_ENTRY_EXEMPTIONS:
            failures.append(
                f"进程 `{extra}`：没有模块但缺少显式 PROCESS_ENTRY_EXEMPTIONS 理由。"
            )

        for name in sorted(listed_set):
            # 2026-08-23 #146 清退：此前 `bot-test` 进程组本身合法依赖自己的豁免
            # 模块，此处曾有 `and extra != "bot-test"` 的特例放行；`bot-test` 组
            # 删除后不再有任何进程组的用法属于这种自反例外，特例随之移除。
            if name in actual_exemption_names:
                failures.append(
                    f"进程 `{extra}`：模块 `{name}` 是正式制品豁免，不能被正式进程依赖。"
                )
            elif name not in required_set and name not in actual_exemption_names:
                failures.append(
                    f"进程 `{extra}`：模块 `{name}` 不在 REQUIRED_MODULES，也不在有效豁免中。"
                )

    return failures


def _print_module_manifest_summary() -> None:
    """打印可回读的模块总数、两份清单和全部豁免。"""

    source = source_module_names()
    print(f"模块清单完整性：{len(source)} 个 src/lingxi 模块")
    print(f"  - 正式制品清单（{len(REQUIRED_MODULES)}）：{', '.join(REQUIRED_MODULES)}")
    for extra in sorted(PROCESS_RUNTIME_IMPORTS):
        lingxi_modules, _third_party_modules = PROCESS_RUNTIME_IMPORTS[extra]
        print(f"  - 进程 `{extra}` 清单（{len(lingxi_modules)}）：{', '.join(lingxi_modules) or '（无）'}")
    print(
        f"  - 制品显式豁免（{len(MODULE_MANIFEST_EXEMPTIONS)}）："
        + ", ".join(
            f"{name}（{reason}）" for name, reason in sorted(MODULE_MANIFEST_EXEMPTIONS.items())
        )
    )
    print(
        "  - 进程入口显式豁免："
        + ", ".join(
            f"{name}（{reason}）"
            for name, reason in sorted(PROCESS_ENTRY_EXEMPTIONS.items())
        )
    )


def installed_extras() -> set[str] | None:
    """已安装制品声明的 extras；读不到返回 ``None``。"""

    try:
        return set(metadata("lingxi").get_all("Provides-Extra") or [])
    except PackageNotFoundError:
        return None


def ci_matrix_extras(workflow_text: str) -> set[str] | None:
    """从 ci.yml 文本里读出 extras 矩阵；没有那一行返回 ``None``。"""

    match = _MATRIX_LINE.search(workflow_text)
    if match is None:
        return None
    return {item.strip().strip("\"'") for item in match.group(1).split(",") if item.strip()}


def check_ci_matrix(declared: set[str], workflow_text: str | None) -> list[str]:
    """每个 extra 都必须出现在 ci.yml 的 extras 矩阵里。

    只对账「pyproject ↔ 本脚本」这半边是不够的：把新组加进 pyproject 和
    ``PROCESS_RUNTIME_IMPORTS``、唯独漏掉矩阵那一行，CI 就从没在干净环境里装过
    它，而 gate 全绿——独立复查实测出过这个漏洞。这里补上另半边，让代码框架里
    「这条不靠自觉」的说法真正成立。
    """

    if workflow_text is None:
        return [f"{CI_WORKFLOW}：读不到 CI 配置，无法核对 extras 矩阵"]
    matrix = ci_matrix_extras(workflow_text)
    if matrix is None:
        return ["ci.yml：找不到 `extra: [...]` 矩阵行，extras 矩阵无法核对（改了写法就同步本脚本的正则）"]

    failures: list[str] = []
    for name in sorted(declared - matrix - MATRIX_EXEMPT_EXTRAS):
        failures.append(
            f"extra `{name}`：不在 .github/workflows/ci.yml 的 extras 矩阵里，"
            "CI 从没在干净环境里装过它。请加进 `extra: [...]`。"
        )
    for name in sorted(matrix - declared):
        failures.append(
            f"extra `{name}`：ci.yml 矩阵里有，但已安装制品没有声明它，那条矩阵腿必然失败。"
        )
    return failures


def _read_ci_workflow() -> str | None:
    try:
        return CI_WORKFLOW.read_text(encoding="utf-8")
    except OSError:
        return None


def check_declared_extras(declared: set[str]) -> list[str]:
    """已安装制品声明的 extras 必须与 ``PROCESS_RUNTIME_IMPORTS`` 一一对上。

    CI 矩阵和本脚本都是**按名字列举**的：新增一个 extra 却忘了同步，它就悄悄没有
    任何检查覆盖，而且不会有任何东西变红——正是「绿色的测试不等于会变红的测试」。
    这里读的是**已安装制品的元数据**（``Provides-Extra``）而不是 pyproject.toml：
    本检查刻意在仓库目录之外运行，回头读源码树就把这个前提丢了。
    """

    known = set(PROCESS_RUNTIME_IMPORTS)
    failures: list[str] = []
    for name in sorted(declared - known):
        failures.append(
            f"extra `{name}`：pyproject.toml 声明了它，但 PROCESS_RUNTIME_IMPORTS 没有，"
            "于是它的依赖没有任何检查覆盖。请补上本脚本的条目，"
            "并同步加进 .github/workflows/ci.yml 的 extras 矩阵。"
        )
    for name in sorted(known - declared):
        failures.append(
            f"extra `{name}`：PROCESS_RUNTIME_IMPORTS 有它，但已安装制品没有声明。"
            "pyproject.toml 可能把这一组改名或删掉了。"
        )
    return failures


def _installed_module_location(module_name: str) -> pathlib.Path:
    """返回已安装模块文件位置；对可执行入口避免执行模块代码。"""

    if module_name in _NON_IMPORTABLE_MODULES:
        spec = importlib.util.find_spec(module_name)
        if spec is None or not spec.origin or spec.origin == "built-in":
            raise ImportError(f"找不到可执行入口的安装文件（{module_name}）")
        return pathlib.Path(spec.origin)

    module = importlib.import_module(module_name)
    return pathlib.Path(module.__file__ or "")


def _check_process(name: str) -> list[str]:
    """校验某个进程 extra 的运行依赖在当前环境里真的可用。"""

    failures: list[str] = []
    lingxi_modules, third_party_modules = PROCESS_RUNTIME_IMPORTS[name]

    for module_name in lingxi_modules:
        try:
            location = _installed_module_location(module_name)
        except Exception as error:  # noqa: BLE001 - 任何导入失败都是制品问题
            failures.append(f"{name} 进程入口 {module_name}：导入失败（{type(error).__name__}: {error}）")
            continue
        if not any(marker in location.parts for marker in _INSTALL_MARKERS):
            failures.append(
                f"{name} 进程入口 {module_name}：来自 {location}，不是已安装的包。"
                "请在仓库目录之外运行本检查。"
            )

    for module_name in third_party_modules:
        try:
            importlib.import_module(module_name)
        except Exception as error:  # noqa: BLE001 - 缺依赖与导入报错都是声明问题
            failures.append(
                f"{name} 运行依赖 {module_name}：导入失败（{type(error).__name__}: {error}）。"
                f"pyproject.toml 的 [{name}] 组可能漏了它。"
            )

    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--process",
        choices=sorted(PROCESS_RUNTIME_IMPORTS),
        help="额外校验该进程 extra 的运行依赖已装上；省略时只做制品完整性检查。",
    )
    parser.add_argument(
        "--source-only",
        action="store_true",
        help="只运行源码模块清单反向对账，供 verify_repository.sh 在未安装制品的工作树中使用。",
    )
    args = parser.parse_args()

    if args.source_only and args.process:
        parser.error("--source-only 不能与 --process 同时使用")

    failures: list[str] = check_module_manifests()
    if args.source_only:
        if failures:
            print("模块清单完整性：不通过", file=sys.stderr)
            for line in failures:
                print(f"  - {line}", file=sys.stderr)
            return 1
        _print_module_manifest_summary()
        return 0

    for name in REQUIRED_MODULES:
        try:
            location = _installed_module_location(name)
        except Exception as error:  # noqa: BLE001 - 任何导入失败都是制品问题
            failures.append(f"{name}：导入失败（{type(error).__name__}: {error}）")
            continue

        if not any(marker in location.parts for marker in _INSTALL_MARKERS):
            failures.append(
                f"{name}：来自 {location}，不是已安装的包。"
                "请在仓库目录之外运行本检查，否则它只是又测了一遍源码树。"
            )

    for package_name, file_name in REQUIRED_PACKAGE_DATA:
        try:
            package = importlib.import_module(package_name)
        except Exception as error:  # noqa: BLE001 - 任何导入失败都是制品问题
            failures.append(f"{package_name}：导入失败（{type(error).__name__}: {error}）")
            continue
        data_file = pathlib.Path(package.__file__ or "").parent / file_name
        if not data_file.is_file():
            failures.append(f"{package_name}/{file_name}：数据文件不在已安装的包里")
        elif not any(marker in data_file.parts for marker in _INSTALL_MARKERS):
            failures.append(f"{package_name}/{file_name}：来自 {data_file}，不是已安装的包。")

    # 不受 --process 影响：gate 那一步（不传 --process）也要能发现「新增了 extra
    # 却没人检查它」，否则这个漏洞要等到部署才暴露。三方对账——已安装制品的
    # Provides-Extra、本脚本的 PROCESS_RUNTIME_IMPORTS、ci.yml 的 extras 矩阵——
    # 任意两边对不上都在这里失败。
    declared = installed_extras()
    if declared is None:
        failures.append("lingxi：读不到已安装制品的元数据，无法核对 extras 声明")
    else:
        failures.extend(check_declared_extras(declared))
        failures.extend(check_ci_matrix(declared, _read_ci_workflow()))

    if args.process:
        failures.extend(_check_process(args.process))

    if failures:
        print("已安装包完整性：不通过", file=sys.stderr)
        for line in failures:
            print(f"  - {line}", file=sys.stderr)
        return 1

    _print_module_manifest_summary()
    print(
        f"已安装包完整性：{len(REQUIRED_MODULES)} 个模块与 "
        f"{len(REQUIRED_PACKAGE_DATA)} 个数据文件全部来自已安装的包"
    )
    print(
        f"extras 三方对账：{len(PROCESS_RUNTIME_IMPORTS)} 组"
        f"（{', '.join(sorted(PROCESS_RUNTIME_IMPORTS))}）在制品 Provides-Extra、"
        "本脚本与 ci.yml 矩阵三处一致"
    )
    if args.process:
        lingxi_modules, third_party_modules = PROCESS_RUNTIME_IMPORTS[args.process]
        print(
            f"{args.process} 进程运行依赖：{len(lingxi_modules)} 个进程入口模块与 "
            f"{len(third_party_modules)} 个第三方模块（{', '.join(third_party_modules)}）全部可导入"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
