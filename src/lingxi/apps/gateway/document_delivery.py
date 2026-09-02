"""Gateway 文档投递独立消费循环（Issue #341 S-ES-3，依据 #341 评论 5434520679 审定设计）。

worker 侧（``apps/worker/service.py`` 经
``adapters/postgres_conversation/_queue_outbox.py::write_terminal_event``）已经在
任务终态事务里插入了一行 ``task_document_delivery_request``（状态 ``pending``，
迁移 0074）。本模块认领这些行，驱动"建档 → 写正文 → 授予可管理 → 读回确认"
四步（S-ES-1 ``adapters/feishu_docx_delivery.py::LarkDocxDelivery``），成功后把
文档链接作为一条追加消息发给提问用户。

**独立于既有 ``DeliveryConsumer``（``apps/gateway/delivery.py``），不共用同一条
批次循环**：那条循环的职责是把已经产生的问数终态尽快送到用户面前（卡片流式、
文本兜底），轮询节奏是"越快越好"；本循环的四步里至少一步是真实飞书 HTTP 调用
（``feishu_docx_delivery.REQUEST_TIMEOUT_SECONDS`` = 20 秒/步），如果混进同一条
循环，一次建档慢查询会连带阻塞其他用户的终态卡片/文本送达——这正是设计要避免的
"建档四步阻塞他人终态送达"。两条循环各自的后台线程、各自的轮询间隔、各自的失败
隔离，只共享同一个进程内的 ``stop_event`` 与 ``run_delivery_loop``/
``delivery_thread_watchdog`` 这两个与"消费循环长什么样"无关的通用线程管理工具
（见 ``apps/gateway/__init__.py``）。

**四步的失败分类只有两种，均在 :meth:`DocumentDeliveryConsumer._process_claim`
里体现**：

1. **definite** → ``failed``——两个来源：
   （a）``adapters.feishu_docx_delivery.FeishuDocxDeliveryError(definite=True)``，
   即收到非 0 的飞书业务错误码；（b）``adapters.feishu_docx_delivery`` 四个
   动作各自的入参校验（``_require_document_id``/``_require_user_open_id``/
   ``write_paragraphs`` 的空段落检查）抛出的 ``ValueError``（P3 顺手，opus
   审查）——这些校验在**发出任何 HTTP 请求之前**就会失败，与"有副作用的调用
   结果不明"是完全不同的情形：没有请求真的发出去，重放同一份数据必然得到
   同一个结论，不存在"可能已经生效"的空间。两者都是系统已经确定、不会因为
   重试而改变结论的失败，不需要人工核对具体是哪一次调用；``last_error`` 只记
   错误分类码/异常类名，不含正文、不含凭据。**"不需要人工核对具体是哪一次
   调用"不等于"不需要被看见"**（opus 审查 R-1）：此前 :meth:`_fail` 只落
   日志，一条 ``failed`` 终态因此从不触发任何管理群告警——用户没拿到文档这件
   事只有翻日志才能发现。现在 :meth:`_fail` 与 :meth:`_uncertain` 对称，命中
   即记一条 ``document_delivery_failed`` 告警。
2. **结果不明（网络类异常、响应缺失可回读标识、读回不含目标或权限档位不对）**
   → ``uncertain``：白名单反转——只有第 1 类明确失败才归 ``failed``，
   **其余一切**（``FeishuDocxDeliveryError(definite=False)``、
   ``LookupError``、任何未预期异常、``read_members`` 结构正常但没有目标
   ``open_id`` 或档位不是 ``full_access``）都归结果不明。原因与
   ``apps/gateway/delivery.py`` 模块说明的"白名单反转（独立审核 R-1）"一致：
   飞书的四个动作里有三个（建档、写正文、授权）是有副作用的写操作，一次网络
   异常或响应解析失败**不能证明动作没有生效**，把它当成 ``failed`` 允许
   下一轮重新调用，等于可能对同一次逻辑请求重复建档/重复授权。**``uncertain``
   不自动重试**（V-交付-03：未确认成功不自动重发，转人工核对）——见
   :meth:`PostgresDocumentDeliveryStore.mark_uncertain` 与本模块
   ``_process_claim`` 的调用点，命中后只记一条告警级审计，不会被
   :meth:`PostgresDocumentDeliveryStore.claim_pending` 再次认领
   （``status='uncertain'`` 不在该方法的查询谓词范围内）。

**检查点持久化在 :meth:`_process_claim` 内部体现**：只有 ``claim.document_id``
为 ``None`` 时才调用 ``create_document``，紧接着立刻调用
``PostgresDocumentDeliveryStore.mark_document_created`` 单独提交——这是一次独立
的数据库往返，不与后续三步共享事务。如果消费进程在这一步与下一步之间崩溃，
下一次认领到同一行时 ``claim.document_id`` 已经非空，四步流程从 ``write_
paragraphs`` 续做，绝不二次调用 ``create_document``（S0 探针实测：飞书建文档
接口没有幂等键，重放会真的多建一篇孤儿文档）。

**写正文步的幂等判据（Issue #353 修复）**：``write_paragraphs``/``write_body``
同样没有幂等键——检查点恢复路径（``claim.document_id`` 进来时已经非空）如果无
条件重新调用它，会把正文再追加一遍（#328 E-S 验收发现，P3）。不能靠再加一列本地
检查点（例如"正文已写"标记）来堵这个洞：飞书写入与本地检查点提交是两次独立的
数据库/网络往返，非原子，"写正文成功了、但推进本地检查点之前进程崩溃"这个窗口
永远存在——加检查点列只能缩短这个窗口，不能封死它。因此判据必须直接问外部系统
的真实状态：只在**检查点恢复路径**（``document_id`` 是从 ``claim`` 带进来的，
不是本次调用刚建出来的）先调用 ``adapters.feishu_docx_delivery.LarkDocxDelivery.
read_body_children`` 读一次正文根 block 的现有子块，非空即跳过写正文；首次路径
（``document_id`` 本次调用才建出来，必然从未写过正文）不做这次多余的读回，行为
与修复前逐字相同。该方法已知未被真实验证的假设与后续建议见其模块文档字符串。
这条判据对官方转换路径同样成立——``write_paragraphs``/官方转换（
``convert_markdown_to_body`` + ``write_blocks`` 或 ``write_descendant_blocks``）
写的是完全相同的坐标（同一个根 block 的 ``children``），``read_body_children``
不区分"这个坐标上的内容是段落写的、扁平转换写的、还是嵌套转换写的"，因此不需要
为转换路径单独设计幂等判据。Issue #538 的嵌套块写入路径已在 stage 受控探针上
实测坐实这一点：同一篇文档 ``descendant`` 写入前根 block 子块为 0、写入后恰好
是那些一级块，嵌套块不出现在这个坐标上。

**markdown 官方转换路径的接线（迁移 0079，Issue #408 正式方案接线）**：
``task_document_delivery_request.markdown`` 非 ``None`` 才有资格走
``LarkDocxDelivery.write_body`` 的转换分支，是否真的转换由 gateway 配置
``LINGXI_DOCX_MARKDOWN_CONVERT``（装配进 ``LarkDocxDelivery`` 构造函数的
``markdown_convert_enabled``）决定；``markdown`` 为 ``None``（历史行、或登记侧
未能落上原文）**无条件**回退 :meth:`~lingxi.adapters.feishu_docx_delivery.
LarkDocxDelivery.write_paragraphs`，与转换开关是否打开无关——详见
:meth:`DocumentDeliveryConsumer._process_docx_claim` 写正文步的分支注释。转换
失败（业务错误码、结果不明、超过 ``MAX_CONVERTED_BLOCKS``）沿用本模块既有的
definite/结果不明分类，不单独处理。

**唯一的例外是「明示降级」（Issue #499，产品负责人 2026-08-31 裁定）**：转换被
飞书确定性拒绝且原因码是 ``unsupported_nested_blocks``（转换结果里出现了一个
没有任何父块认领、无处安放的块）时，``LarkDocxDelivery.write_body`` 改走纯文本
段落路径把正文写进去、并在返回的 ``WriteBodyOutcome`` 里明示降级；本模块接住
这个信号，
**必须**做三件事，缺一件这条裁定就退化成当初被明令禁止的静默降级：

1. 把原因码落进 ``task_document_delivery_request.body_degraded_reason``（迁移
   0082，:meth:`PostgresDocumentDeliveryStore.mark_body_degraded` 单独提交）
   ——补发通知路径与检查点恢复路径都是另一次进程调用，读不到这次调用的内存
   信号；
2. 成功通知改用 ``delivery.document_ready_degraded``（如实说明格式已简化及
   原因），不是普通的 ``delivery.document_ready``；
3. 记一条 ``gateway.document_delivery.body_degraded`` 结构化日志，让运维能按
   ``task_id`` 查到这次降级。

**不上告警**：降级是"交付成功、但排版被简化"，不是故障；实测命中率 18.2%
（#499 W0-1），按 ``document_delivery_failed`` 那样命中即报会把管理群刷成噪音，
反而淹没真正的失败。可观测性由上面第 1、3 两项承担（库里一列 ＋ 一条结构化
日志），与 ``_fail``/``_uncertain`` 的告警面刻意分开。

**这条降级是"拿得到"，不是"好看"**：段落路径来自 ``paragraphs`` 列，表格会被
拍平成一段长文本、``|---|`` 分隔行原样留在正文里——用户文案不得暗示格式完好，
见 ``content.toml`` 的 ``delivery.document_ready_degraded``。

**成功通知走"追加消息"，不进入任何已有话题的投递 outbox**：文档交付可能发生在
原问数任务已经确认送达很久之后（``uncertain``/``failed`` 转 ``pending`` 的
重试窗口、gateway 与 worker 各自的处理延迟），复用 ``core.execution.card_stream``
那一整套"同话题终态"语义没有意义——这里只是**另主动**发一条独立的文本消息给
这个人，复用 ``adapters.feishu_user_message.FeishuUserMessages``（与权限变化
通知同一条出站信道：``im/v1/messages`` + ``receive_id_type=open_id``）。

## 表格分支（Issue #354 S-H3-2，D2 裁定：同构 #341 文档交付路由）

同一条消费循环、同一张检查点表、同一个状态机——不新起第二条循环。
:meth:`DocumentDeliveryConsumer._process_claim` 按认领到的行的
``delivery_type`` 分派到 :meth:`_process_docx_claim`（既有逻辑，逐字未改）或
:meth:`_process_sheet_claim`（新增）：建表 → 查默认 sheet_id（纯只读，检查点
恢复路径无条件重放，不需要判据）→ 写值（``PUT`` 覆盖式接口，天然幂等，检查点
恢复路径同样无条件重放）→ 授「可管理」→ 读回确认，失败分类（definite →
``failed``、其余 → ``uncertain``）与检查点持久化（只有 ``create_spreadsheet``
成功后单独提交，绝不二次调用）的姿态与 docx 分支逐项对应，差异点见
``adapters/feishu_sheets_delivery.py`` 模块文档「与文档交付的差异点」。

## 已知边界（Trace #373 codex 外审②登记）

以下三点是 docx/sheet 共用状态机的既有架构特征，本批只登记、不改动——按当前
威胁模型（部署层单实例纪律已生效、生产不做灰度并发部署）判定为可接受，登记
只为避免后续审查重复发现同一件事：

1. **检查点没有租约 owner 判据**：:meth:`PostgresDocumentDeliveryStore.
   claim_pending` 用 ``UPDATE ... WHERE status='pending' ... RETURNING``
   配合 ``FOR UPDATE SKIP LOCKED`` 保证同一时刻的并发认领互斥，但这只在
   「认领」这一次原子语句内生效——认领成功之后的四步流程（可能跨多次真实
   HTTP 调用、数十秒量级）没有租约字段（owner + 过期时间）标记"这一行正在
   被哪一个进程处理"，行状态在流程跑完前始终停在中间态。如果真的有第二个
   gateway 实例同时在跑（本模块不假设不会发生，见
   ``postgres_document_delivery.py`` 模块文档），两个实例各自认领到的必然是
   不同行（``SKIP LOCKED`` 保证不重复认领同一行本身），但**没有代码层面的
   机制阻止两个实例同时存在**——去重实际依赖部署层单实例纪律（``deploy/
   生产部署runbook.md``「四、单实例纪律」，Trace #373 D11 决策：
   ``docs/traces/373-清仓冲刺/合同.md``），不是本模块自己强制的边界。
2. **恢复路径无条件重放写值**：写正文/写值步骤的幂等判据（见上文「写正文步
   的幂等判据」与表格分支说明）只保证"重放不会把内容越写越长/越写越错"，
   不保证"重放不会覆盖用户自己在这期间做的编辑"。如果进程在
   ``grant_full_access`` 成功（用户此刻已经拿到「可管理」权限、随时可能开始
   编辑）之后、``mark_succeeded`` 之前崩溃，恢复后的下一轮认领会依据检查点
   判据决定是否重新调用写步骤——sheets 分支的写值判据是"天然幂等、无条件
   重放"（不像 docx 分支那样先读一次现有内容判断是否跳过），因此这条恢复
   路径上，用户在崩溃-恢复窗口期间对表格做的编辑存在被下一轮无条件重放的
   写值请求覆盖的风险（秒级窗口，取决于崩溃发生的时间点与恢复调度间隔）。
3. **``read_members`` 在授权成功之后失败时终态判 ``failed``，但用户已经
   持有可管理权限**：按「失败分类只有两种」的白名单反转规则，
   ``read_members`` 返回明确的飞书业务错误码（``definite=True``）会让整条
   认领落 ``failed``；但如果这次失败发生在 ``grant_full_access`` 已经成功
   之后（读回确认这一步本身失败，不代表授权没有生效），用户此时实际已经
   对这份文档/表格拥有「可管理」权限，只是没有收到成功通知、系统记录的终态
   也是"失败"——状态机的记录与外部世界的真实状态在这一种失败形状下不一致。

三者都不是本次修复的范围（不产生新的安全或数据丢失风险，是这套「认领 + 检查点
+ 独立提交」架构本身的既有取舍），如需收紧需要新的设计决策（例如引入带租约的
认领、写步骤的乐观锁/版本号、或 ``read_members`` 失败时的补偿通知），留给后续
Trace 按需评估。
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable

from lingxi.adapters.postgres_document_delivery import (
    DELIVERY_TYPE_DOCX,
    DELIVERY_TYPE_SHEET,
    DocumentDeliveryClaim,
    PostgresDocumentDeliveryStore,
)
from lingxi.config.content import ContentCatalog, default_content_catalog

logger = logging.getLogger(__name__)

AlertCallback = Callable[[str, str], None]

#: 循环级异常告警的占位 trace_id（与 ``apps/gateway/delivery.py`` 的
#: ``LOOP_ALERT_TRACE_ID`` 同一手法，各自独立命名——两条循环互不相关，共用同一个
#: 占位符会让管理群通知分不清告警来自哪一条循环）。
LOOP_ALERT_TRACE_ID = "gateway-document-delivery-loop"

#: 一轮最多认领并处理的行数——与 ``DeliveryConsumer`` 的默认批量同量级，不是
#: 精确调校值：文档交付预期是低频动作（用户显式要求生成文档才会命中），不需要
#: 为它单独暴露一个环境变量。
DEFAULT_BATCH_LIMIT = 5


def _default_alert(kind: str, task_id: str) -> None:
    """默认告警出口：结构化日志。真实告警路由由调用方注入（同
    ``apps/gateway/delivery.py::_default_alert`` 的姿态）。
    """

    logger.error("文档投递告警 kind=%s task_id=%s", kind, task_id)


def _has_confirmed_full_access(
    members: list[dict[str, Any]], open_id: str, *, delivery_type: str = DELIVERY_TYPE_DOCX
) -> bool:
    """判定 read_members 读回结果是否确认目标 open_id 具备 full_access。

    与 ``scripts/probe_drive_folder_permissions.py`` 的 ``_member_signature`` 取值
    口径一致（``member_type``/``member_id``/``perm`` 三元组），只是这里只关心
    "有没有恰好一条命中目标 open_id 且档位是 full_access 的记录"这一个布尔结论，
    不需要整份签名。

    ``delivery_type``（Trace #373 H3 批量审查 P2-2）：``FULL_ACCESS_PERM``/
    ``OPENID_MEMBER_TYPE`` 两个常量在 ``feishu_docx_delivery`` 与
    ``feishu_sheets_delivery`` 各自独立定义（两个结构对称、不互相 import 的
    并列适配器，见 ``feishu_sheets_delivery`` 模块文档「姿态选择」）。本方法同时
    服务 docx 与 sheet 两条 ``_finalize_claim`` 路径，此前**恒从 docx 模块**导入
    这两个常量——sheet 侧若独立改动自己的取值，这里读到的仍然是 docx 侧的旧值，
    判定悄悄对不上。按 ``delivery_type`` 选取对应模块的常量，保证改 sheets 侧
    真的生效；两个模块当前取值逐字相同（``"full_access"``/``"openid"``），本次
    只是让"从哪里读"这件事对，不改变任何现有行为。
    """

    if delivery_type == DELIVERY_TYPE_SHEET:
        from lingxi.adapters.feishu_sheets_delivery import FULL_ACCESS_PERM, OPENID_MEMBER_TYPE
    else:
        from lingxi.adapters.feishu_docx_delivery import FULL_ACCESS_PERM, OPENID_MEMBER_TYPE

    return any(
        member.get("member_type") == OPENID_MEMBER_TYPE
        and member.get("member_id") == open_id
        and member.get("perm") == FULL_ACCESS_PERM
        for member in members
    )


class DocumentDeliveryConsumer:
    """一轮：回收卡住的行、清理耗尽重试预算的行、认领新行、逐行驱动到底。

    单行失败按行隔离（同 ``apps/gateway/delivery.py::DeliveryConsumer.run_once``
    的姿态）：一行的异常不得带走同一轮里的其他行；循环级查询（认领、回收）失败
    则整轮降级为"这一轮无事可做"，下一轮重来，不让一次瞬时数据库错误杀死后台
    线程本身（真正兜底见 ``apps/gateway/__init__.py`` 的 ``run_delivery_loop``/
    ``delivery_thread_watchdog``）。
    """

    def __init__(
        self,
        *,
        store: PostgresDocumentDeliveryStore,
        docx: Any,
        notifier: Any,
        sheets: Any = None,
        catalog: ContentCatalog | None = None,
        on_alert: AlertCallback | None = None,
        limit: int = DEFAULT_BATCH_LIMIT,
    ) -> None:
        self._store = store
        self._docx = docx
        # 表格分支（Issue #354 S-H3-2）：可选——单测/未装配表格能力时可以不传，
        # 只要队列里不出现 delivery_type='sheet' 的行就不会被触碰
        # （见 :meth:`_process_claim` 的分派）。真实装配见
        # :func:`assemble_document_delivery_consumer`。
        self._sheets = sheets
        self._notifier = notifier
        self._catalog = catalog or default_content_catalog()
        self._alert = on_alert or _default_alert
        self._limit = limit

    def run_once(self) -> int:
        """跑一轮，返回本轮实际认领并处理的行数。"""

        try:
            exhausted = self._store.fail_exhausted_pending()
        except Exception as error:  # noqa: BLE001 - 见类文档：只降级这一段
            logger.error("文档投递：清理耗尽重试预算的待认领行失败 error=%s", type(error).__name__)
        else:
            if exhausted > 0:
                # R-1 独立审核：这类行此前被直接丢弃——转 failed 却从不上报，管理员
                # 永远看不到"有请求耗尽重试预算被清出队列"这件事。不带具体 task_id
                # （这是一次批量转态，不是单行结果），退化为不带 trace_id 的信号
                # （见 `delivery_alert_callback` 对空/非法 task_id 的既有容错）。
                logger.error(
                    "gateway.document_delivery.attempts_exhausted count=%s", exhausted
                )
                self._alert("document_delivery_attempts_exhausted", "")

        try:
            requeued, reclaim_failed = self._store.reclaim_stale_processing()
        except Exception as error:  # noqa: BLE001 - 见类文档：只降级这一段
            logger.error("文档投递：回收卡住的处理中行失败 error=%s", type(error).__name__)
        else:
            del requeued  # 退回 pending 等下一轮重来，不是失败结果，不必上报。
            if reclaim_failed > 0:
                # 同上：回收时直接判定 failed 的那部分（重试预算已耗尽）此前同样
                # 只落日志、不上报。
                logger.error(
                    "gateway.document_delivery.reclaim_failed count=%s", reclaim_failed
                )
                self._alert("document_delivery_reclaim_failed", "")

        # P2-2（opus 审查）：补发"文档已就绪"通知排在认领新行**之前**——已经
        # succeeded 却没能通知到用户是最靠近"用户体感落空"的一类残留，不能因为
        # 本轮新到的建档请求把批量配额占满就一直排不上号。查询失败只降级这一段
        # （同上面两段），不阻塞新行的正常认领与处理。
        try:
            pending_notices = self._store.claim_unnotified_succeeded(limit=self._limit)
        except Exception as error:  # noqa: BLE001 - 见类文档：只降级这一段
            logger.error("文档投递：查询待补发通知的行失败 error=%s", type(error).__name__)
        else:
            for item in pending_notices:
                self._send_ready_notice(
                    request_id=item.id,
                    task_id=item.task_id,
                    requester_open_id=item.requester_open_id,
                    document_id=item.document_id,
                    delivery_type=item.delivery_type,
                    resource_url=item.resource_url,
                    body_degraded_reason=item.body_degraded_reason,
                )

        try:
            claims = self._store.claim_pending(limit=self._limit)
        except Exception as error:  # noqa: BLE001 - 本轮无事可做，下一轮重来
            logger.error("文档投递：认领待处理行失败 error=%s", type(error).__name__)
            return 0

        for claim in claims:
            logger.info(
                "gateway.document_delivery.claimed task_id=%s attempts=%s", claim.task_id, claim.attempts
            )
            try:
                self._process_claim(claim)
            except Exception as error:  # noqa: BLE001 - 一行失败不得带走同一轮的其他行
                logger.error(
                    "文档投递单行处理异常，本轮其余行不受影响 task_id=%s error=%s",
                    claim.task_id,
                    type(error).__name__,
                )
        return len(claims)

    def _process_claim(self, claim: DocumentDeliveryClaim) -> None:
        """按 ``claim.delivery_type`` 分派（Issue #354 S-H3-2）：docx 走
        :meth:`_process_docx_claim`（既有逻辑，逐字未改），sheet 走
        :meth:`_process_sheet_claim`（新增）。见模块文档「表格分支」一节。
        """

        if claim.delivery_type == DELIVERY_TYPE_SHEET:
            self._process_sheet_claim(claim)
        else:
            self._process_docx_claim(claim)

    def _process_docx_claim(self, claim: DocumentDeliveryClaim) -> None:
        from lingxi.adapters.feishu_docx_delivery import FeishuDocxDeliveryError
        from lingxi.adapters.postgres_document_delivery import DocumentDeliveryOwnershipLost

        document_id = claim.document_id
        # Issue #353：只有检查点恢复路径（document_id 从 claim 带进来，不是本次
        # 调用刚建出来的）才需要在写正文前多问一句"是不是已经写过了"——首次路径
        # 的 document_id 必然是全新文档，从未写过正文，不需要这次额外读回，行为
        # 与修复前逐字相同（见模块说明「写正文步的幂等判据」）。
        recovering_from_checkpoint = document_id is not None
        # Issue #499：这一行此前是否已经被判定为降级交付（迁移 0082 的
        # `body_degraded_reason`，由 `claim_pending` 一并读出）。检查点恢复路径
        # 会跳过写正文步、因此不会再产生一次 `WriteBodyOutcome`——不从 claim 里
        # 继承这个值，恢复路径发出的就是不带降级说明的"文档已生成"。
        body_degraded_reason = claim.body_degraded_reason
        try:
            if document_id is None:
                document_id = self._docx.create_document(claim.title)
                # 检查点：独立提交，不与下面三步共享事务（见模块说明）。
                self._store.mark_document_created(request_id=claim.id, document_id=document_id)
            already_has_body = recovering_from_checkpoint and bool(
                self._docx.read_body_children(document_id)
            )
            if not already_has_body:
                # Issue #408 正式方案接线：``claim.markdown`` 是否非 None 才是
                # "有没有资格走官方转换路径"的判据——是否真的转换仍然由
                # ``LarkDocxDelivery.write_body`` 内部的转换开关
                # （构造期传入的 ``markdown_convert_enabled``）决定（开关关闭
                # 时 write_body 逐字调用 write_paragraphs，等价于本分支直接调
                # write_paragraphs）。这里必须显式分两支、不能无条件都走
                # write_body(markdown=claim.markdown)：如果转换开关已经打开
                # 但这一行的 markdown 列是 NULL（历史行、或登记侧因为某种原因
                # 没能落上原文），传 None 进 write_body 会被
                # convert_markdown_to_body 判定为空正文而失败关闭——那不是
                # 这里要的结果，"markdown 列为 NULL"必须无条件回退段落路径，
                # 与转换开关状态无关（同 write_body 幂等判据一致：两条路径写的
                # 是同一个坐标）。
                if claim.markdown is not None:
                    outcome = self._docx.write_body(
                        document_id, paragraphs=list(claim.paragraphs), markdown=claim.markdown
                    )
                    # Issue #499 明示降级：`write_body` 只在
                    # `unsupported_nested_blocks` 这一个原因码上降级（其余失败仍
                    # 然向上抛，走下面既有的 definite/结果不明分类）。降级时正文
                    # **已经**写进飞书了，所以这里先把原因码单独提交成检查点、再
                    # 继续授权/读回——晚提交会被一次崩溃带走，恢复路径就再也无从
                    # 知道这一行降级过（见迁移 0082 文件头部「残留窗口如实登记」）。
                    if outcome.degraded_reason is not None:
                        body_degraded_reason = outcome.degraded_reason
                        self._store.mark_body_degraded(
                            request_id=claim.id, reason=outcome.degraded_reason
                        )
                        logger.warning(
                            "gateway.document_delivery.body_degraded task_id=%s reason=%s",
                            claim.task_id,
                            outcome.degraded_reason,
                        )
                else:
                    self._docx.write_paragraphs(document_id, list(claim.paragraphs))
            self._docx.grant_full_access(document_id, claim.requester_open_id)
            members = self._docx.read_members(document_id)
        except DocumentDeliveryOwnershipLost:
            # P1-2（opus 审查）：建档检查点提交时发现持有权已经不在本次调用手里
            # （典型：这次调用是一个被 `reclaim_stale_processing` 判定为"卡住"
            # 并回收过的慢消费者）。当场中止——不写正文、不授权、不读回、不发
            # 通知，把这一行交给真正持有它的那次调用或它已经落下的终态。
            logger.warning(
                "文档投递：建档成功但持有权已丢失，放弃本行续做 task_id=%s", claim.task_id
            )
            return
        except FeishuDocxDeliveryError as error:
            if error.definite:
                self._fail(claim, last_error=error.code)
            else:
                self._uncertain(claim, last_error=error.code)
            return
        except ValueError as error:
            # P3 顺手（opus 审查）：``adapters.feishu_docx_delivery`` 四个动作各自
            # 的入参校验（``_require_document_id``/``_require_user_open_id``/
            # ``write_paragraphs`` 的空段落检查）在**发出任何 HTTP 请求之前**就
            # 会失败，抛的是纯 ``ValueError``——这与"白名单反转"要挡的"有副作用
            # 的调用因为网络异常而结果不明"是完全不同的情形：没有任何请求真的
            # 发出去，重放同一份数据必然得到同一个 ``ValueError``，不存在"再等等
            # 说不定就好了"的空间。这类行归 ``uncertain``（V-交付-03：不自动
            # 重试，转人工核对）与归 ``failed``（同样不自动重试）在"要不要重试"
            # 这件事上结果相同，但 ``uncertain`` 会误导排查方向——它暗示"可能已经
            # 生效，需要人工核对飞书那一侧"，而这里连请求都没发出去，真正需要
            # 核对的是这一行本身的数据（``requester_open_id`` 形状不对、正文段落
            # 到了处理时点仍然是空——例如已经被 `V-投递-06` 到期擦除却仍然停在
            # 非终态，理论上不该发生但没有硬性防线保证）。
            self._fail(claim, last_error=type(error).__name__)
            return
        except Exception as error:  # noqa: BLE001 - 白名单反转（同 delivery.py R-1）：
            # 只有上面显式捕获的 definite FeishuDocxDeliveryError 与确定性入参
            # 校验错误（ValueError）才归 failed，其余一切（LookupError、未预期
            # 异常）都归结果不明——不能假设一次有副作用的调用在异常时一定没有
            # 生效。
            self._uncertain(claim, last_error=type(error).__name__)
            return

        self._finalize_claim(
            claim,
            document_id=document_id,
            members=members,
            resource_url=None,
            body_degraded_reason=body_degraded_reason,
        )

    def _process_sheet_claim(self, claim: DocumentDeliveryClaim) -> None:
        """表格分支（Issue #354 S-H3-2）：与 :meth:`_process_docx_claim` 逐项
        对应，差异点见 ``adapters/feishu_sheets_delivery.py`` 模块文档「与文档
        交付的差异点」：写值天然幂等（无条件重放，不需要 docx 那样的
        ``read_body_children`` 判据）、查默认 sheet_id 是纯只读调用（同样无条件
        重放，不需要检查点）、建表响应自带链接（与 ``document_id`` 一起随
        :meth:`mark_document_created` 落检查点，不需要 ``tenant_domain``）。
        """

        from lingxi.adapters.feishu_sheets_delivery import FeishuSheetsDeliveryError
        from lingxi.adapters.postgres_document_delivery import DocumentDeliveryOwnershipLost

        spreadsheet_token = claim.document_id
        resource_url = claim.resource_url
        try:
            if spreadsheet_token is None:
                spreadsheet_token, resource_url = self._sheets.create_spreadsheet(claim.title)
                # 检查点：独立提交，不与下面三步共享事务——同 docx 的
                # mark_document_created 姿态，只是这里额外一并落 resource_url
                # （sheet 独有：链接随建表响应一起拿到，不需要第二次调用）。
                self._store.mark_document_created(
                    request_id=claim.id, document_id=spreadsheet_token, resource_url=resource_url
                )
            sheet_id = self._sheets.get_default_sheet_id(spreadsheet_token)
            # 写值天然幂等（PUT 覆盖式接口），检查点恢复路径无条件重放——不需要
            # docx 那样先读一遍判断"是否已经写过"（见模块文档「表格分支」）。
            self._sheets.write_values(spreadsheet_token, sheet_id, [list(row) for row in claim.paragraphs])
            self._sheets.grant_full_access(spreadsheet_token, claim.requester_open_id)
            members = self._sheets.read_members(spreadsheet_token)
        except DocumentDeliveryOwnershipLost:
            logger.warning(
                "文档投递：建表成功但持有权已丢失，放弃本行续做 task_id=%s", claim.task_id
            )
            return
        except FeishuSheetsDeliveryError as error:
            if error.definite:
                self._fail(claim, last_error=error.code)
            else:
                self._uncertain(claim, last_error=error.code)
            return
        except ValueError as error:
            # 同 docx 分支的理由（见 _process_docx_claim 对应分支注释）：
            # ``adapters.feishu_sheets_delivery`` 各方法的入参校验在**发出任何
            # HTTP 请求之前**就会失败，没有"可能已经生效"的空间，归 failed。
            self._fail(claim, last_error=type(error).__name__)
            return
        except Exception as error:  # noqa: BLE001 - 白名单反转，同 docx 分支
            self._uncertain(claim, last_error=type(error).__name__)
            return

        self._finalize_claim(
            claim, document_id=spreadsheet_token, members=members, resource_url=resource_url
        )

    def _finalize_claim(
        self,
        claim: DocumentDeliveryClaim,
        *,
        document_id: str,
        members: list[dict[str, Any]],
        resource_url: str | None,
        body_degraded_reason: str | None = None,
    ) -> None:
        """docx/sheet 两条分支共用的收口：验证权限读回、落终态、发送通知
        （Issue #354 S-H3-2 从 ``_process_claim`` 提炼，行为对 docx 零变化——
        判断顺序、异常处理、日志/告警内容逐字相同，只多了 ``delivery_type``
        字段用于分派通知文案）。

        ``body_degraded_reason``（Issue #499）：只由 docx 分支传，非 ``None`` 时
        成功通知改用明示降级的文案。sheet 分支结构上恒为 ``None``（没有"markdown
        转换"这个概念，迁移 0082 的 CHECK 也在数据库层拒绝这种行），因此不传。
        """

        from lingxi.adapters.postgres_document_delivery import DocumentDeliveryOwnershipLost

        if not _has_confirmed_full_access(
            members, claim.requester_open_id, delivery_type=claim.delivery_type
        ):
            # 读回结构正常，但没有找到目标 open_id 的 full_access 记录：结果不明
            # （可能是权限还没有生效、也可能是授权那一步实际没有成功），不得判
            # succeeded（测试③锚点）。
            self._uncertain(claim, last_error="permission_not_confirmed")
            return

        try:
            self._store.mark_succeeded(request_id=claim.id)
        except DocumentDeliveryOwnershipLost:
            # 流程已经全部跑完（资源已经建好、内容已经写入、权限已经授予、读回
            # 也确认了），但落终态这一步才发现持有权已经丢失——同上，不发通知：
            # 这一行现在究竟是什么状态由真正持有它的那次调用决定，我们没有
            # 资格覆盖，也没有资格代表它去通知用户。
            logger.warning(
                "文档投递：流程已跑完但持有权已丢失，放弃写入终态与通知 task_id=%s",
                claim.task_id,
            )
            return
        except Exception as error:  # noqa: BLE001 - 落库失败仍按结果不明处理，不假装成功
            self._uncertain(claim, last_error=type(error).__name__)
            return

        logger.info(
            "gateway.document_delivery.succeeded task_id=%s attempts=%s delivery_type=%s",
            claim.task_id,
            claim.attempts,
            claim.delivery_type,
        )
        self._send_ready_notice(
            request_id=claim.id,
            task_id=claim.task_id,
            requester_open_id=claim.requester_open_id,
            document_id=document_id,
            delivery_type=claim.delivery_type,
            resource_url=resource_url,
            body_degraded_reason=body_degraded_reason,
        )

    def _fail(self, claim: DocumentDeliveryClaim, *, last_error: str) -> None:
        from lingxi.adapters.postgres_document_delivery import DocumentDeliveryOwnershipLost

        try:
            self._store.mark_failed(request_id=claim.id, last_error=last_error)
        except DocumentDeliveryOwnershipLost:
            # P1-2：这一行已经不是我们的了——另一次调用可能已经落下了不同的
            # 结论。不覆盖、不告警：告警必须描述真实发生的终态，而不是我们
            # 本来打算落的那一个。
            logger.warning(
                "文档投递：判定为 failed 但持有权已丢失，放弃写入终态与告警 task_id=%s",
                claim.task_id,
            )
            return
        except Exception as error:  # noqa: BLE001 - 记录失败不能让异常逃出本方法
            logger.error(
                "文档投递终态写入失败（failed）task_id=%s error=%s", claim.task_id, type(error).__name__
            )
        logger.error(
            "gateway.document_delivery.failed task_id=%s attempts=%s last_error=%s",
            claim.task_id,
            claim.attempts,
            last_error,
        )
        # R-1 独立审核：definite 失败此前只落日志，管理员看不到——与 `_uncertain`
        # 已有的告警对称补上；`V-交付-03`「未确认成功不自动重发」覆盖的是重试语义，
        # 不代表 failed 终态本身不需要人工核对（飞书明确拒绝仍然是"用户没拿到文档"）。
        self._alert("document_delivery_failed", claim.task_id)
        # R-1 第 3 条：此前只有 succeeded 会追加消息，用户请求生成文档失败后从未
        # 收到任何后续消息——表现成"发起之后再也没有下文"。措辞与 uncertain 区分
        # （见 content.toml「delivery.document_failed」）：failed 是确定结论，
        # 可以直接建议用户重新发起。表格分支（Issue #354 S-H3-2）按
        # ``claim.delivery_type`` 选用对称的 sheet 文案/去重前缀，docx 分支的
        # 取值逐字不变。
        # 表格分支文案带追溯号（S-H3-2 卡明确要求，姿态照 Issue #280 裁定
        # B2-4）——docx 分支的既有文案没有这个占位符，`_send_terminal_notice`
        # 只在模板真的声明了 {reference} 时才传，docx 调用点因此逐字不变。
        key, dedupe_prefix, variables = (
            ("delivery.sheet_failed", "sheet-failed", {"reference": claim.task_id})
            if claim.delivery_type == DELIVERY_TYPE_SHEET
            else ("delivery.document_failed", "document-failed", {})
        )
        self._send_terminal_notice(claim, key=key, dedupe_prefix=dedupe_prefix, template_variables=variables)

    def _uncertain(self, claim: DocumentDeliveryClaim, *, last_error: str) -> None:
        from lingxi.adapters.postgres_document_delivery import DocumentDeliveryOwnershipLost

        try:
            self._store.mark_uncertain(request_id=claim.id, last_error=last_error)
        except DocumentDeliveryOwnershipLost:
            # 同 `_fail`：持有权已经不在我们手里，不覆盖、不告警。
            logger.warning(
                "文档投递：判定为 uncertain 但持有权已丢失，放弃写入终态与告警 task_id=%s",
                claim.task_id,
            )
            return
        except Exception as error:  # noqa: BLE001 - 记录失败不能让异常逃出本方法
            logger.error(
                "文档投递终态写入失败（uncertain）task_id=%s error=%s", claim.task_id, type(error).__name__
            )
        logger.warning(
            "gateway.document_delivery.uncertain task_id=%s attempts=%s last_error=%s",
            claim.task_id,
            claim.attempts,
            last_error,
        )
        # V-交付-03：未确认成功不自动重发，转人工核对——记告警级审计。
        self._alert("document_delivery_uncertain", claim.task_id)
        # R-1 第 3 条：措辞与 failed 区分（见 content.toml
        # 「delivery.document_uncertain」）——不建议用户自行重试：uncertain 的
        # 成因可能是一次有副作用的调用（建档/写正文/授权）网络异常，无法证明
        # 没有生效，重试可能造成重复建档/重复授权，必须转人工核对。表格分支
        # （Issue #354 S-H3-2）同 `_fail` 一样按 ``claim.delivery_type`` 选用
        # 对称文案，docx 分支取值逐字不变。
        key, dedupe_prefix, variables = (
            ("delivery.sheet_uncertain", "sheet-uncertain", {"reference": claim.task_id})
            if claim.delivery_type == DELIVERY_TYPE_SHEET
            else ("delivery.document_uncertain", "document-uncertain", {})
        )
        self._send_terminal_notice(claim, key=key, dedupe_prefix=dedupe_prefix, template_variables=variables)

    def _send_ready_notice(
        self,
        *,
        request_id: str,
        task_id: str,
        requester_open_id: str,
        document_id: str,
        delivery_type: str = "docx",
        resource_url: str | None = None,
        body_degraded_reason: str | None = None,
    ) -> None:
        """成功后把文档/表格链接作为追加消息发给提问用户；失败只记日志/告警，
        不改写已经落库的 ``succeeded`` 终态——文档/表格已经建好且用户已经拿到
        权限，通知只是一次锦上添花的提醒，不是这条请求"是否成功"的判据。

        显式字段而不是接受整个 ``DocumentDeliveryClaim``（P2-2，opus 审查）：
        本方法有两个调用点——刚跑完流程的原发送路径（``claim`` 现成可用），与
        补发未确认送达通知的路径（``run_once`` 里的 :class:`UnnotifiedSuccess`，
        没有完整 claim——``title``/``paragraphs``/``attempts`` 对补发通知无关）。

        ``body_degraded_reason``（迁移 0082，Issue #499）：非 ``None`` 时选用
        ``delivery.document_ready_degraded``。两个调用点各自的来源不同——原发送
        路径来自本次 ``write_body`` 的返回值（或 claim 里继承的历史值），补发
        路径来自 :class:`~lingxi.adapters.postgres_document_delivery.
        UnnotifiedSuccess` 从库里读出的那一列；两条路径必须给出同一个结论，
        否则同一次交付会因为"第几次尝试通知"而说法不一。

        ``delivery_type``/``resource_url``（迁移 0078，Issue #354 S-H3-2）：
        docx 分支不传（保持默认值，取值/行为逐字不变，链接由
        :meth:`~lingxi.adapters.feishu_docx_delivery.LarkDocxDelivery.
        document_url` 纯本地拼接）；sheet 分支必须传非 ``None`` 的
        ``resource_url``——表格链接不做格式猜测，只用建表检查点里已经落盘的值
        （见迁移 0078 文件头部）。

        **成功才置位 ``notified_at``**：与 :meth:`PostgresDocumentDeliveryStore.
        mark_notified` 的幂等闸配合，补发路径（
        :meth:`DocumentDeliveryConsumer.run_once`）下一轮会自然跳过已经确认送达
        的行，不需要在这里额外判断"这是不是补发"。
        """

        try:
            if delivery_type == DELIVERY_TYPE_SHEET:
                if not isinstance(resource_url, str) or not resource_url:
                    # 结构性不应发生：sheet 分支的 resource_url 与 document_id
                    # 在同一次 mark_document_created 调用里一起落盘（见
                    # _process_sheet_claim）。响亮失败而不是猜测/拼一个链接。
                    raise LookupError("sheet 分支缺少可用的 resource_url")
                url = resource_url
                content_key = "delivery.sheet_ready"
                dedupe_prefix = "sheet-ready"
            else:
                url = self._docx.document_url(document_id)
                # Issue #499 明示降级：正文被降级成纯文本段落路径写入时，用户
                # 拿到的排版与他本该拿到的不同——必须用如实说明"格式已简化"的
                # 那条文案，不能沿用普通就绪文案。
                content_key = (
                    "delivery.document_ready_degraded"
                    if body_degraded_reason is not None
                    else "delivery.document_ready"
                )
                # 去重前缀刻意**两条文案共用**：原发送与补发是同一条通知的两次
                # 尝试，不是两条独立通知。分开前缀会让"第一次发普通文案失败、
                # 补发时才读到降级列"这种时序发出两条消息给同一个人。
                dedupe_prefix = "document-ready"
            content = self._catalog.text(content_key, url=url)
            self._notifier.send_text(
                open_id=requester_open_id,
                text=content.text,
                dedupe_key=f"{dedupe_prefix}:{request_id}",
            )
            self._store.mark_notified(request_id=request_id)
        except Exception as error:  # noqa: BLE001 - 通知失败不得回滚已经确认的交付结果
            logger.error(
                "文档投递完成通知发送失败，交付结果不受影响 task_id=%s error=%s",
                task_id,
                type(error).__name__,
            )
            self._alert("document_delivery_notice_failed", task_id)

    def _send_terminal_notice(
        self,
        claim: DocumentDeliveryClaim,
        *,
        key: str,
        dedupe_prefix: str,
        template_variables: dict[str, Any] | None = None,
    ) -> None:
        """failed/uncertain 终态后把固定文案作为追加消息发给提问用户（opus 审查
        R-1 第 3 条）；失败只记日志/告警，不改写已经落库的终态——同
        :meth:`_send_ready_notice` 的姿态：通知是终态判定之后锦上添花的告知，
        不是终态本身的一部分。``dedupe_prefix`` 与 ``document-ready``（成功那一路
        的既有前缀）各自独立，三种终态各自的通知不会互相去重掉。

        ``template_variables``（Issue #354 S-H3-2）：``ContentCatalog.text`` 要求
        调用方变量集合与模板变量集合逐一相等，多传/少传都会报错——docx 分支的
        两个既有 key（``delivery.document_failed``/``uncertain``）没有任何占位
        变量，调用点因此不传（默认 ``None`` → 空字典 → 与改动前逐字相同的
        ``self._catalog.text(key)`` 调用）；sheet 分支的 key 带 ``{reference}``，
        由调用方（``_fail``/``_uncertain``）显式传 ``{"reference": claim.
        task_id}``。
        """

        try:
            content = self._catalog.text(key, **(template_variables or {}))
            self._notifier.send_text(
                open_id=claim.requester_open_id,
                text=content.text,
                dedupe_key=f"{dedupe_prefix}:{claim.id}",
            )
        except Exception as error:  # noqa: BLE001 - 通知失败不得回滚已经落库的终态
            logger.error(
                "文档投递终态通知发送失败 task_id=%s key=%s error=%s",
                claim.task_id,
                key,
                type(error).__name__,
            )
            self._alert("document_delivery_notice_failed", claim.task_id)

    def run_forever(
        self,
        *,
        stop: threading.Event,
        poll_interval_seconds: float,
        heartbeat: Callable[[], None] | None = None,
        on_tick: Callable[[], None] | None = None,
    ) -> None:
        """长期循环：每轮之间固定等待一个轮询间隔（同
        ``apps/gateway/delivery.py::DeliveryConsumer.run_forever`` 的姿态）。
        """

        while not stop.is_set():
            if heartbeat is not None:
                try:
                    heartbeat()
                except Exception as error:  # noqa: BLE001 - 心跳失败不能带走投递职责
                    logger.error("文档投递心跳记录失败 error=%s", type(error).__name__)
            if on_tick is not None:
                try:
                    on_tick()
                except Exception as error:  # noqa: BLE001 - 告警自身失败不能带走投递职责
                    logger.error("文档投递告警状态机推进失败 error=%s", type(error).__name__)
            try:
                self.run_once()
            except Exception as error:  # noqa: BLE001 - 一轮异常不得带走整条循环
                logger.error("文档投递循环级异常，本轮降级后继续 error=%s", type(error).__name__)
            stop.wait(poll_interval_seconds)
        logger.info("文档投递消费循环已停止")


def assemble_document_delivery_consumer(
    config: Any, *, store: Any = None, alerting_duty: Any = None
) -> DocumentDeliveryConsumer | None:
    """装配文档/表格投递独立消费循环（Issue #341 S-ES-3；Issue #354 S-H3-2 新增
    表格分支的装配）。

    ``config.tenant_domain`` 未配置时返回 ``None``——循环整体不注册（docx 与
    sheet 共用同一条循环，同一个失败关闭开关，见模块文档「表格分支」一节），
    与既有 ``roster_audit.duty_not_registered`` 等姿态一致（调用方
    ``apps/gateway/__init__.py::main`` 据此决定要不要起第二条后台线程），不会
    用一个猜测的域名硬跑，也不会尝试装配任何飞书客户端或数据库连接。
    ``LarkSheetsDelivery`` 本身不需要 ``tenant_domain``（见该模块文档「与文档
    交付的差异点」第 1 条），这里仍然复用同一个开关只是为了不新增第二个装配
    条件——两条分支本就共用同一条循环，拆开判断徒增复杂度。
    """

    if config.tenant_domain is None:
        logger.info("gateway.document_delivery.duty_not_registered reason=missing_tenant_domain")
        return None

    from lingxi.adapters.feishu_docx_delivery import LarkDocxDelivery
    from lingxi.adapters.feishu_sheets_delivery import LarkSheetsDelivery
    from lingxi.adapters.feishu_tenant_token import FeishuTenantTokenClient
    from lingxi.adapters.feishu_user_message import FeishuUserMessages
    from lingxi.core.permission.tenant_token_supply import TenantAccessTokenSupply

    #: 与管理群/权限变化通知/花名册日报各自独立前缀同一纪律——同一个
    #: ``im/v1/messages`` 接口下，每条独立投递语义必须有自己的去重命名空间
    #: （见 ``adapters/feishu_group_message.py`` 的 ``uuid_prefix`` 文档）。
    #: 17 + 32 = 49 字符，在飞书 50 字符上限内。
    notice_uuid_prefix = "lingxi-doc-ready-"

    tenant_access_token = TenantAccessTokenSupply(
        fetch=FeishuTenantTokenClient(
            base_url=config.feishu_base_url,
            app_id=config.app_id,
            app_secret=str(config.app_secret),
        ).fetch,
    )
    docx = LarkDocxDelivery(
        base_url=config.feishu_base_url,
        tenant_access_token=tenant_access_token,
        tenant_domain=config.tenant_domain,
        # Issue #408 正式方案接线：装配层把已经读好的布尔值传进去
        # （adapters/ 不直接读 os.environ，见代码框架「三、横切约定」）。
        markdown_convert_enabled=config.markdown_convert_enabled,
    )
    sheets = LarkSheetsDelivery(
        base_url=config.feishu_base_url,
        tenant_access_token=tenant_access_token,
    )
    notifier = FeishuUserMessages(
        base_url=config.feishu_base_url,
        app_id=config.app_id,
        app_secret=str(config.app_secret),
        uuid_prefix=notice_uuid_prefix,
    )
    return DocumentDeliveryConsumer(
        store=store
        or PostgresDocumentDeliveryStore(str(config.postgres_dsn), timeouts=config.postgres_timeouts),
        docx=docx,
        sheets=sheets,
        notifier=notifier,
        on_alert=alerting_duty.delivery_alert_callback() if alerting_duty is not None else None,
    )


__all__ = [
    "DEFAULT_BATCH_LIMIT",
    "LOOP_ALERT_TRACE_ID",
    "DocumentDeliveryConsumer",
    "assemble_document_delivery_consumer",
]
