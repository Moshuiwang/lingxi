"""问数卡片的顺序、限流与失败回退。

这里不依赖飞书 SDK。卡片适配器只实现 ``CardTransport``，因此 L2 可以用内存假实现
验证序号、话题隔离和失败回退，L4a 再验证 CardKit 的真实字段与投递效果。

**Issue #152 起支持从持久化状态恢复（resume）**：Gateway 消费循环崩溃重启后不会重新
``start()`` 一张已经建好的卡片——它把上一次持久化的 ``card_id``/``sequence``/
``message_id``/``fallback_needed`` 作为 ``initial_*`` 参数传回来，本类从那一点继续，
不产生第二张有效卡片（`V-卡片-01`、状态合同第 7 条）。

**Issue #407 方向 B 起，卡片正文是"已走过的步骤名"追加式列表**：``update()`` 每次
调用不再用最新状态整体覆盖正文，而是把这一次信号追加成新的一行（同一状态连续
出现时原地刷新用时，不重复追加，见 ``_accumulate_step``）——正文随进度更新持续
变长，飞书官方的流式打字机效果因此天然承担"新行出现"的动态观感，本类不做任何
字符级动画模拟。这份累积状态同样要支持 resume：``initial_progress_history``
参数（``ProgressStepSnapshot`` 的有序序列）由调用方从 outbox 里已经持久化的历史
信号重建后传入，构造期原样重放一遍——Gateway 消费循环本身是无状态的，**每一轮
轮询都会重新构造一个全新的 ``CardStream``**（不止是崩溃重启才会遇到 resume），
这份参数因此不是可选的锦上添花，而是"正文不会跨轮询边界变短"的唯一依据，见
``apps/gateway/delivery.py`` 模块说明「过程流式的正文累积」。

**Issue #444 起，同一枚步骤身份持续过久会被明示为"停滞"**（产品负责人
2026-08-30 裁定「不做每秒读秒，改做不变即异常」）：``_accumulate_step`` 原地
刷新同一行时，若这枚身份已经持续超过 :data:`STALL_THRESHOLD_SECONDS`，改用
``worker.status_stalled`` 文案明示"仍停在这一步、已经多久没有新进展"，而不是
继续用看不出异常的"{action} · N 秒"措辞；换成新身份后自动恢复常规措辞，不需要
调用方感知这个状态切换。呈现层的停滞阈值取 worker 侧兜底强制刷新间隔
（``apps/worker/service.py::_PROGRESS_FALLBACK_SECONDS``，由 30 秒收紧到 12
秒）的 **2 倍**（24 秒，rc21 修复包 B 校正——原先两者相等曾造成"恰好一个
兜底周期的正常静默"被误判为停滞，见 :data:`STALL_THRESHOLD_SECONDS` 上方
注释）。

**「明确失败」与「结果不明」不是同一件事，判别用白名单而不是黑名单**（独立审核
B-1 首次修复于 2026-08-14；独立审核 R-1 于同日把判别方向从黑名单反转为白名单，
红线：重复投递）。``start()``/``finish()`` 的终态更新/``send_fallback()`` 这三处
外发调用只认一种"明确失败"：``DeliveryRejected``（定义见下方）——真实 adapter
``adapters.feishu_delivery`` **只有**在服务端已经给出完整响应、且业务错误码明确
表示拒绝（``response.success()`` 为假、``code``/``msg`` 字段可读）时才抛出它。
除它之外的**一切**异常——``lark_oapi`` 内部 JSON 解析失败
（``json.JSONDecodeError``）、响应结构缺失（``response.success()`` 为真但拿不到
``card_id``/``message_id`` 这类可回读标识）、``requests`` 的网络类异常
（``requests.exceptions.RequestException`` 及其子类，全部继承自内置
``OSError``，读超时/连接重置时抛出）、以及任何其它未预期的异常——都不能确定
服务端是否已经处理这次调用，默认归"结果不明"：三处外发原样 ``raise``，不降级、
不清空 ``_fallback_needed``——调用方（``apps.gateway.delivery``）据此不清预留位、
不重试、不改道，转入既有 ``uncertain`` 告警路径，等待人工核对是否已经送达
（issue #152 状态合同第 6 条）。本模块因此不需要 import ``requests`` 或
``lark_oapi``，只需要认识 ``DeliveryRejected`` 这一个类型——这正是白名单相对
黑名单的好处：新出现的异常类型（SDK 升级引入的新异常、此前没想到的响应形状）
默认落进"结果不明"这个更安全的分支，而不是黑名单下默认落进"明确失败"这个更
危险的分支。终态**关闭**（``finish()`` 的第二次外部调用）不受这条规则约束——
它的失败语义本身就与异常类型无关（见 ``finish()`` 文档：更新已经成功后仅关闭
失败不构成结果丢失），任何异常落进既有 ``except Exception`` 分支效果都完全
一致，无需特殊处理。
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from lingxi.config.content import (
    ContentCatalog,
    RenderedCard,
    RenderedContent,
    default_content_catalog,
)

# 语义化进度动作码（Issue #321 方向 C，产品负责人 2026-08-27 裁定；Issue #407
# 增粒度，产品负责人 2026-08-29 方向 A+B 裁定）：``apps/worker/service.py`` 编码
# 写入 ``task_delivery_event.content``，``apps/gateway/delivery.py`` 的消费循环
# 解码后交给 ``CardStream.update()``。两侧共享同一份常量，不各写一份字符串
# 字面量、悄悄漂移。
PROGRESS_ACTION_PROCESSING = "processing"
PROGRESS_ACTION_QUERYING = "querying"
PROGRESS_ACTION_COMPOSING = "composing"
# 非问数查询工具的调用（含被拒绝的越界包装调用）——Issue #407 从 COMPOSING 拆出
# 独立文案，不再与"模型正在输出文本"共用同一句话，覆盖两者交替出现时卡片长时间
# 显示同一句文案的观感（issue 背景「读秒卡住感」）。
PROGRESS_ACTION_WORKING = "working"
PROGRESS_ACTION_COMPLETED = "completed"

# 问数查询工具的已知子步骤（Issue #407 方向 A：提取步骤名信息）。这四个是真实
# 问数 MCP 注册的原生只读工具全集（``apps/worker/config.py`` 白名单前缀注释、
# ``docs/参考证据/问数MCP-list_metrics真实响应形状.md``、``core/year_grounding_
# guard.py`` 的 ``QUERY_METRIC_TOOL_NAME`` 交叉印证）。**白名单式映射**（Issue
# #407 出口安全红线）：只有落在这个集合里的子步骤名才会被编码进 progress 事件、
# 进而映射成用户可见文案；任何不在其中的字符串（模型臆造的工具名、未来协议
# 新增但本侧还未登记的工具、注入的内部标识）在 `encode_progress_action`/
# `decode_progress_action` 两处都会被静默丢弃、退回不带步骤名的通用文案——不是
# 只在其中一处过滤，两处都过滤是因为 `decode_progress_action` 的输入来自数据库
# 里已经落库的 `content` 字段，不能假设它一定是本侧刚刚编码出来的那一份（防
# 御性纵深，见模块顶部关于"结果不明"异常分类同一条纪律：宁可多判一层）。
QUERY_STEP_LIST_METRICS = "list_metrics"
QUERY_STEP_DESCRIBE_METRIC = "describe_metric"
QUERY_STEP_SEARCH_DIMENSION = "search_dimension"
QUERY_STEP_QUERY_METRIC = "query_metric"
KNOWN_QUERY_STEPS: frozenset[str] = frozenset(
    {
        QUERY_STEP_LIST_METRICS,
        QUERY_STEP_DESCRIBE_METRIC,
        QUERY_STEP_SEARCH_DIMENSION,
        QUERY_STEP_QUERY_METRIC,
    }
)

# 工具名 → 用户语文案键的白名单式映射表（Issue #407 出口安全红线）。只覆盖
# :data:`KNOWN_QUERY_STEPS` 里的四个已知子步骤；具体文案由 ``content.toml``
# 维护（产品负责人可独立调整每一句的措辞），这里只登记"哪个子步骤对应哪个
# 文案键"这份结构性映射。查不到的键（``dict.get`` 的默认分支，见
# ``CardStream._render_step_line``）一律落回既有通用文案 ``worker.action.
# querying_metrics``——这正是"未映射的工具一律显示通用文案"这条要求的具体
# 落地：即使未来协议新增了第五个查询工具、这里忘了同步登记，效果也只是退化
# 成通用文案，不会把新工具的原始名字泄漏出去。
_QUERY_STEP_ACTION_TEXT_KEYS: dict[str, str] = {
    QUERY_STEP_LIST_METRICS: "worker.action.querying_list_metrics",
    QUERY_STEP_DESCRIBE_METRIC: "worker.action.querying_describe_metric",
    QUERY_STEP_SEARCH_DIMENSION: "worker.action.querying_search_dimension",
    QUERY_STEP_QUERY_METRIC: "worker.action.querying_query_metric",
}

# 历史行的完成时措辞（Trace #469 S-1 TOP-9）：与上面 `_QUERY_STEP_ACTION_TEXT_
# KEYS` 一一对应的"已..."变体，供 `_render_step_line(completed=True)` 选用——
# 累积列表里除最后一行外，全部行代表"已经翻篇的步骤"，不该继续用"正在..."
# 这类现在时措辞。
_QUERY_STEP_ACTION_TEXT_KEYS_DONE: dict[str, str] = {
    QUERY_STEP_LIST_METRICS: "worker.action.querying_list_metrics_done",
    QUERY_STEP_DESCRIBE_METRIC: "worker.action.querying_describe_metric_done",
    QUERY_STEP_SEARCH_DIMENSION: "worker.action.querying_search_dimension_done",
    QUERY_STEP_QUERY_METRIC: "worker.action.querying_query_metric_done",
}

# 非查询类动作的完成时措辞（同上一条注释）。
_ACTION_DONE_TEXT_KEYS: dict[str, str] = {
    PROGRESS_ACTION_PROCESSING: "worker.action.processing_done",
    PROGRESS_ACTION_COMPOSING: "worker.action.composing_done",
    PROGRESS_ACTION_WORKING: "worker.action.working_done",
}
_DEFAULT_QUERYING_DONE_TEXT_KEY = "worker.action.querying_metrics_done"

# 静态防回归（P2 顺手，独立审查）：`encode_progress_action` 编码出的
# `PROGRESS_ACTION_QUERYING` 形态（``"querying:<计数>[:<子步骤名>]"``）最终会
# 落进 ``task_delivery_event.content``，迁移 0075 给这一列的 `progress` 事件
# 加了 ``char_length(content) <= 32`` 的数据库层 CHECK 约束，
# ``core/delivery/ports.py`` 的 ``PROGRESS_CONTENT_MAX_LENGTH`` 是同一条契约
# 的应用层落地（两处常量各自独立登记，互不 import，同 0075 迁移文件头注一贯
# 的"没有自动化门禁跨文件互相核对，这一条纪律"）。``ports.py`` 那边的注释
# 手算过"两位数计数时 28 字节"，但只是注释、不是可执行断言——未来谁往
# `KNOWN_QUERY_STEPS` 加一个更长的子步骤名却忘了回头核对这条 32 字节契约，
# 原本要等真实环境撞上与 0075 修复前同一形状的 `CheckViolation`（写库失败
# 只记日志、卡片静默不动，Issue #328 opus 审查 R1 的真实事故）才会被发现。
# 这里把"白名单最长子步骤名 + 两位数计数"的最坏编码长度钉成一条 import 期
# 静态断言，回归时直接炸在 import，`tests/test_worker_queue_consumer.py` 的
# `CardStreamTests` 另有一条等价的可读性更好的单测断言同一件事。
_MAX_KNOWN_QUERY_STEP_NAME_LENGTH = max(len(step) for step in KNOWN_QUERY_STEPS)
# "querying:"（9 字节）+ 两位数计数上界 "99"（2 字节）+ ":"（1 字节）+ 最长
# 子步骤名——与 `core/delivery/ports.py::PROGRESS_CONTENT_MAX_LENGTH` 注释里
# "两位数计数"这条假设一致，不是本模块凭空另设的预算。
_WORST_CASE_QUERYING_CONTENT_LENGTH = (
    len(f"{PROGRESS_ACTION_QUERYING}:") + len("99") + len(":") + _MAX_KNOWN_QUERY_STEP_NAME_LENGTH
)
assert _WORST_CASE_QUERYING_CONTENT_LENGTH <= 32, (
    "KNOWN_QUERY_STEPS 里最长子步骤名让 encode_progress_action 的最坏输出超过了"
    "迁移 0075 CHECK 与 core/delivery/ports.PROGRESS_CONTENT_MAX_LENGTH 约定的"
    "32 字节契约——新增子步骤名前必须先核对这条预算，不能只改这里不改那两处"
)


def encode_progress_action(
    action: str, *, query_count: int | None = None, query_step: str | None = None
) -> str:
    """把语义化进度状态编码进一条 progress 事件的 ``content`` 字段（worker 侧）。

    认识三种动作：``PROGRESS_ACTION_QUERYING``（必须带一个 ``>=1`` 的
    ``query_count``，渲染成"正在第 N 次查询……"；可选带一个 ``query_step``，
    只有落在 :data:`KNOWN_QUERY_STEPS` 白名单里才会被编码，否则静默丢弃、
    退回不带步骤名的通用编码——见上方模块常量注释）、``PROGRESS_ACTION_
    COMPOSING``（渲染成"正在整理与生成回答"）与 ``PROGRESS_ACTION_WORKING``
    （渲染成"正在处理其它步骤"）。``PROGRESS_ACTION_PROCESSING``——尚未发生
    任何可分类信号时的默认状态——不经过这个函数：调用方直接传 ``content=
    None``，没有语义需要编码。**不回显工具名、参数或任何查询内容**（Issue
    #321 方向 C / #407 出口安全红线的产品红线）：编码后的字符串只可能是白名单
    内的固定形状，永远不含调用方传入的原始工具名或输入正文。
    """

    if action == PROGRESS_ACTION_QUERYING:
        if query_count is None or query_count < 1:
            raise ValueError("querying 动作必须带一个 >=1 的 query_count")
        if isinstance(query_step, str) and query_step in KNOWN_QUERY_STEPS:
            return f"{PROGRESS_ACTION_QUERYING}:{query_count}:{query_step}"
        return f"{PROGRESS_ACTION_QUERYING}:{query_count}"
    if action == PROGRESS_ACTION_COMPOSING:
        return PROGRESS_ACTION_COMPOSING
    if action == PROGRESS_ACTION_WORKING:
        return PROGRESS_ACTION_WORKING
    raise ValueError(f"未知的进度动作：{action!r}")


def decode_progress_action(content: str | None) -> tuple[str, int | None, str | None]:
    """反解析（Gateway 消费侧）。返回 ``(action, query_count, query_step)``。

    任何无法识别的形状——``None``（进度事件从未携带语义、或来自旧版 worker）、
    格式不对的字符串、越界的计数、不在 :data:`KNOWN_QUERY_STEPS` 白名单里的
    步骤名——一律退回安全默认值（整体退回 ``(PROGRESS_ACTION_PROCESSING, None,
    None)``，或仅步骤名退回 ``None``）：这只是一张状态卡片要选哪句文案，宁可
    显示默认文案，也不能让一条脏数据（含蓄意构造、注入内部标识的 `content`）
    炸掉整条投递消费循环，或把不可信内容带进用户可见卡片。
    """

    if content == PROGRESS_ACTION_COMPOSING:
        return PROGRESS_ACTION_COMPOSING, None, None
    if content == PROGRESS_ACTION_WORKING:
        return PROGRESS_ACTION_WORKING, None, None
    if isinstance(content, str) and content.startswith(f"{PROGRESS_ACTION_QUERYING}:"):
        _, _, remainder = content.partition(":")
        raw_count, _, raw_step = remainder.partition(":")
        if raw_count.isdigit():
            count = int(raw_count)
            if count >= 1:
                step = raw_step if raw_step in KNOWN_QUERY_STEPS else None
                return PROGRESS_ACTION_QUERYING, count, step
    return PROGRESS_ACTION_PROCESSING, None, None


@dataclass(frozen=True)
class CardCreated:
    """建卡并把它作为消息发出后的结果。

    ``card_id`` 用于后续的流式更新/关闭；``message_id`` 是发送消息接口返回的可回读
    标识（G-CARD 实测：卡片与文本共用同一发送接口与响应结构），在卡片整个生命周期内
    只在这里产生一次，后续流式更新不产生新的 message_id。
    """

    card_id: str
    message_id: str


class DeliveryRejected(Exception):
    """服务端已经给出完整响应、并以明确的业务错误码拒绝这次外发——不是「结果不明」。

    这是本模块与 ``apps.gateway.delivery`` 唯一当作"明确失败"处理的异常类型
    （白名单，独立审核 R-1，2026-08-14）：真实 adapter（``adapters.feishu_delivery``）
    只在 ``response.success()`` 为假、且能读出 ``code``/``msg`` 字段时才抛出它。
    除它之外的任何异常——JSON 解析失败、响应缺失可回读标识、网络类异常、或任何
    未预期的异常——都不属于这个类型，因此都会落进本模块三处外发调用的默认分支
    （"结果不明"，原样 ``raise``，见模块说明）。定义在这里而不是
    ``adapters.feishu_delivery``：后者已经依赖本模块的 ``CardTransport``/
    ``TextTransport`` Protocol，若异常类型反过来定义在 adapter 侧，本模块要捕获
    它就必须反向依赖 adapter，形成循环 import；异常类型属于 ``CardTransport``/
    ``TextTransport`` 这份协议契约的一部分，放在协议定义的同一处更自然。
    """

    def __init__(
        self, message: str = "", *, code: int | str | None = None, log_id: str | None = None
    ) -> None:
        self.code = code
        self.message = message
        self.log_id = log_id
        super().__init__(message or f"服务端明确拒绝：code={code} log_id={log_id}")


class CardTransport(Protocol):
    def create(
        self,
        *,
        chat_id: str,
        thread_id: str | None,
        reply_to_message_id: str,
        card: RenderedCard,
    ) -> CardCreated: ...

    def update(self, *, card_id: str, sequence: int, card: RenderedCard) -> None: ...

    def close(self, *, card_id: str, sequence: int, card: RenderedCard) -> None: ...


class TextTransport(Protocol):
    def send_text(
        self, *, chat_id: str, thread_id: str | None, reply_to_message_id: str, text: str
    ) -> str: ...


SendOutcomeCallback = Callable[[str, bool], None]


# G-CARD 已知平台行为（独立审核 P2-5，#162 评论 5290545953/5291111636 实测）：
# 未手动关闭的流式卡片**距上次开启**（不是距上次更新）10 分钟后由平台自动关闭；
# 之后任何 ``update``/``close`` 调用都会失败。问数任务运行超过 10 分钟并不
# 罕见（硬上限 900 秒，见 ``apps/worker/config.py``），被动等到那次调用失败才
# 发现只能带来"卡片停在某个中间态、用户又收到一条不相关的文本答案"的观感。
# Issue #407 的处理方案（两种代价对比后选定"提前收口文案"）：
# - 保活刷新（试图在 10 分钟前反复调用 ``update`` 续命）在这个平台事实下**不
#   成立**——计时锚点是"上次开启"，不是"上次更新"，常规的 ``update`` 调用不会
#   重置它，因此单靠更频繁地刷新卡片无法真正避免自动关闭；真正能续命的做法是
#   主动关闭旧卡片、另开一张新卡片，但那需要新增"卡片轮换"这一整套状态（旧
#   卡片如何收尾、新卡片如何接续 sequence、Gateway 消费循环如何识别"这是同一
#   任务的第二张卡片"），实现与验证代价明显更高。
# - 提前收口文案：接近阈值时主动把这最后一帧卡片正文换成明确的"即将改用文字
#   消息"提示，并立即把 ``_fallback_needed`` 置位——后续 ``update``（progress）
#   自然直接跳过、``finish``（terminal）也直接跳过卡片，改走既有的文本兜底
#   通道（``CardStream.send_fallback``，Issue #151/#152 已验证的机制）。全部
#   改动只是复用现成的降级路径提前触发一次，不需要新增任何状态机分支，代价
#   明显更低——选定为本次实现。
# 阈值取 9 分钟（留 1 分钟安全余量）：事件驱动进度更新最短 5 秒、兜底计时最长
# 12 秒（``apps/worker/service.py`` 的 ``_PROGRESS_FALLBACK_SECONDS``，Issue
# #444 由 30 秒收紧），跨过阈值后最多 12 秒内就会有下一次 ``update`` 调用
# 触发这条提示，届时距真实的 10 分钟平台自动关闭仍有≥48 秒余量（收紧前是
# ≥30 秒），可靠地在平台真的关闭之前完成主动切换。
CARD_AUTO_CLOSE_HANDOFF_SECONDS = 540.0


class CardRateLimiter:
    """一个 worker 进程共享：单话题 500ms、全进程 50 次/秒。"""

    def __init__(self) -> None:
        self._last_by_topic: dict[str, float] = {}
        self._global_updates: deque[float] = deque()

    def allow(self, *, topic: str, now: float) -> bool:
        last = self._last_by_topic.get(topic)
        if last is not None and now - last < 0.5:
            return False
        while self._global_updates and now - self._global_updates[0] >= 1.0:
            self._global_updates.popleft()
        if len(self._global_updates) >= 50:
            return False
        self._last_by_topic[topic] = now
        self._global_updates.append(now)
        return True

    def record(self, *, topic: str, now: float) -> None:
        """无条件记一次已经发生的调用，不做节流判断、不可能返回拒绝。

        终态更新+关闭（``CardStream.finish()``）不经过 ``allow()`` 的单话题 500ms
        节流——那两帧是结果本身，被节流吞掉等于结果丢失，比不节流更糟（独立审核
        P2-2）。但全进程 50 次/秒的预算是 CardKit 的硬限制，终态这两次调用同样要
        计入，否则并发多话题同时终态时全局计数会失真、放过原本该被节流的其它
        话题。只推进全局窗口，不改动单话题的 500ms 记录——终态调用不受单话题
        节流约束，也不应该反过来去占用/影响它。
        """

        while self._global_updates and now - self._global_updates[0] >= 1.0:
            self._global_updates.popleft()
        self._global_updates.append(now)


@dataclass(frozen=True)
class CardStreamResult:
    card_id: str | None
    sequence: int
    fallback_text: bool


@dataclass(frozen=True)
class _StepRecord:
    """累积列表里的一条原始信号（Trace #469 S-1 TOP-9 重构：此前 ``_step_lines``
    直接存已经渲染好的字符串，导致"历史行"与"当前行"用的是同一句"正在..."
    措辞——一旦追加了新行，前一行本该变成"已经发生过的事"，却还停在"正在
    发生"。改成保留原始信号，交给 :meth:`CardStream._accumulated_status_card`
    在**每次渲染时**决定"这一行是不是列表里的最后一行"，只有最后一行才用
    "正在..."的现在时措辞，其余全部改用"已..."的完成时措辞——见
    :func:`CardStream._render_step_line` 的 ``completed`` 参数。
    """

    action: str
    elapsed_seconds: int
    query_count: int | None = None
    query_step: str | None = None


@dataclass(frozen=True)
class ProgressStepSnapshot:
    """一条已经持久化的进度信号，按时间顺序重放用来重建卡片正文的累积状态
    （Issue #407 方向 B）。字段与 ``decode_progress_action`` 的返回值加一个
    ``elapsed_seconds`` 对齐——``apps/gateway/delivery.py`` 从 outbox 里读出
    历史 ``progress``/``safely_releasable_answer`` 事件后解码成这个形状，见
    ``CardStream.__init__`` 的 ``initial_progress_history`` 参数。
    """

    elapsed_seconds: int
    action: str
    query_count: int | None = None
    query_step: str | None = None


# 卡片正文累积列表的行数上限（Issue #407 方向 B 的防御性上限，不是产品承诺）。
# 真实任务受 `apps/worker/config.py` 的 `MAX_TURNS_HARD_LIMIT`（30）与执行预算
# 约束，正常情况下远远不会触顶；这里只是防止极端/异常场景（例如未来某次改动
# 引入了一个永不重复的信号源）让卡片正文无界增长。触顶后丢弃最旧的行，保留
# 最近发生的步骤——用户此刻最关心的是"最近发生了什么"，不是任务刚开始时的
# 第一步。
MAX_ACCUMULATED_STEP_LINES = 40

# 停滞判定阈值（Issue #444，产品负责人 2026-08-30 裁定「不做每秒读秒，改做
# 不变即异常」；数值由 rc21 修复包 B 从 12 秒校正为 24 秒，见下方「误报双修」
# 一段）：`_accumulate_step` 把每一次信号的语义身份（action/query_count/
# query_step 三元组）与上一次比较——身份不变时只原地刷新这一行（见该方法
# 文档）。这个阈值回答"身份不变可以持续多久才算异常"：同一枚身份从第一次
# 出现到本次刷新，累计跨过这么多秒仍未换成新的身份，就判定为"停滞"，切换
# 成明示文案（``worker.status_stalled``，见 ``_render_step_line``）而不是
# 继续用看不出异常的"{action} · N 秒"措辞。
#
# **误报双修（rc21 修复包 B，opus 审查实测复现）**：本阈值原取 12 秒，与
# ``apps/worker/service.py::_PROGRESS_FALLBACK_SECONDS``（worker 侧兜底强制
# 刷新间隔，同样是 12 秒）完全相等——这个取法本身就是误报的根源：**恰好一个
# 兜底周期的静默是查询/生成回答的常态**，不是异常。真实复现：t=4 发出一次
# 问数查询、25 秒后（t=29）才返回，t=70 模型才生成完最终回答——这段时间里
# `progress_action` 身份自 t=4 起再没有变化过（工具返回本身此前不产生任何
# 信号，模型输出正文的 `assistant_message` 事件要等到接近 t=70 才出现），
# 旧的 12 秒阈值让卡片从 t=16 起就持续显示"停滞"，而这实际上是一次完全正常
# 的任务。修复分两部分（互相配合，缺一不够）：① 本阈值改为
# ``_PROGRESS_FALLBACK_SECONDS`` 的 **2 倍**（24 秒 = 2 个兜底周期）——同一枚
# 身份必须连续跨过两个兜底周期都没有换新身份，才判定异常，给恰好一个周期的
# 正常静默留出余量；② 工具返回（``apps/worker/turn.py`` 流式循环早已把 SDK
# 的 ``tool_result`` 事件转发给 ``on_stream_event``——转发路径本就存在，
# 不需要改 ``turn.py``）时，``apps/worker/service.py`` 的 ``on_stream_event``
# 新增一次进度信号，把身份切到 composing，让上面例子里 t=29 工具返回的那一
# 刻就有一次新身份出现、停滞计时随之清零，不需要一直等到模型真正开始输出
# 文字。两个常量因此**不再取相同的值**（此前的版本要求两者相等，
# 见历史版本注释），改为本常量恒等于对方的 2 倍；两处各自独立登记、互不
# import（同本文件 ``_WORST_CASE_QUERYING_CONTENT_LENGTH`` 一贯的"没有自动化
# 门禁跨文件互相核对，这一条纪律"），调整任一侧都要回头看另一侧是否仍然
# 保持这个 2 倍关系。
STALL_THRESHOLD_SECONDS = 24


class CardStream:
    """一个任务一个实例；绝不跨话题共享卡片序号或限流状态。"""

    def __init__(
        self,
        *,
        chat_id: str,
        thread_id: str | None,
        reply_to_message_id: str,
        transport: CardTransport,
        fallback: TextTransport,
        catalog: ContentCatalog | None = None,
        mark_external_side_effect: Callable[[], bool | None] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        rate_limiter: CardRateLimiter | None = None,
        on_send_outcome: SendOutcomeCallback | None = None,
        initial_card_id: str | None = None,
        initial_sequence: int = 0,
        initial_message_id: str | None = None,
        initial_fallback_needed: bool = False,
        initial_progress_history: Sequence[ProgressStepSnapshot] = (),
    ) -> None:
        self._chat_id = chat_id
        self._thread_id = thread_id
        self._reply_to_message_id = reply_to_message_id
        self._transport = transport
        self._fallback = fallback
        self._catalog = catalog or default_content_catalog()
        self._mark_external_side_effect = mark_external_side_effect
        self._monotonic = monotonic
        # resume 场景：非 None/非零表示接着一次此前已经持久化的进度继续，而不是从零建卡
        # （Issue #152、状态合同第 7 条：重启不产生第二张有效卡片）。
        self._card_id: str | None = initial_card_id
        self._sequence = initial_sequence
        self._message_id: str | None = initial_message_id
        self._fallback_needed = initial_fallback_needed
        self._last_update: float | None = None
        # 过程流式的累积正文（Issue #407 方向 B）：Gateway 消费循环按批次轮询、
        # 每一批次都会重新构造一个全新的 ``CardStream`` 实例（没有跨轮次的进程内
        # 状态），因此这份"已经走过的步骤"列表不能只活在内存里——``__init__``
        # 用调用方传入的 ``initial_progress_history``（从 outbox 里已经持久化
        # 的历史信号重建）重放一遍，找回上一轮结束时的累积状态，再由后续
        # ``update()`` 调用在这个基础上继续追加，不会出现"新一轮卡片正文突然
        # 变短"这种回退观感。``_last_step_identity`` 记录"最近一次累积的是
        # 哪一种信号"——同一种信号连续出现时（例如兜底刷新复用同一个状态）
        # 只原地刷新用时，不重复追加同一句话；换成不同信号才追加新行。
        # ``_current_step_started_at`` 记录当前这枚身份第一次出现时的
        # ``elapsed_seconds``——停滞判定（Issue #444）需要知道"这枚身份已经
        # 持续了多久"，而不是"任务总共跑了多久"，两者在有多个步骤的长任务里
        # 并不相等。resume 重放同样会正确重建这个锚点，因为回放本身就是逐条
        # 调用 ``_accumulate_step``，与实时调用走同一份逻辑。
        self._step_records: list[_StepRecord] = []
        self._last_step_identity: tuple[str, int | None, str | None] | None = None
        self._current_step_started_at: int = 0
        for snapshot in initial_progress_history:
            self._accumulate_step(
                action=snapshot.action,
                elapsed_seconds=snapshot.elapsed_seconds,
                query_count=snapshot.query_count,
                query_step=snapshot.query_step,
            )
        self._rate_limiter = rate_limiter or CardRateLimiter()
        self._on_send_outcome = on_send_outcome

    @property
    def fallback_needed(self) -> bool:
        return self._fallback_needed

    @property
    def sequence(self) -> int:
        return self._sequence

    @property
    def card_id(self) -> str | None:
        return self._card_id

    @property
    def message_id(self) -> str | None:
        """当前投递通道（卡片或文本兜底）绑定的可回读标识；尚未取得时为 ``None``。"""

        return self._message_id

    def start(self) -> None:
        """建卡并发出。resume 场景下（已经有 ``card_id`` 或已经降级）直接不做事——
        调用方按持久化状态决定要不要调用这一步，这里的判断只是防御性幂等。
        """

        if self._card_id is not None or self._fallback_needed:
            return
        # 初始占位文案（"正在处理 · 0 秒"）刻意**不**计入累积步骤列表（Issue
        # #407 方向 B）：它在这一刻还没有对应任何持久化的 progress 事件（只是
        # 建卡时的固定起点），Gateway 侧的历史重建（`initial_progress_history`）
        # 只能从 outbox 里真实存在的信号重放——如果这里把它算进
        # `_step_lines`，下一轮轮询重新构造的 `CardStream` 无法找回这一行，
        # 会让正文出现"重启后少一行"的回退观感。真正的第一条累积行来自第一次
        # 真实信号触发的 `update()`。
        card = self._catalog.card(
            "query.status",
            status=self._render_step_line(action=PROGRESS_ACTION_PROCESSING, elapsed_seconds=0),
        )
        try:
            self._before_external()
            created = self._transport.create(
                chat_id=self._chat_id,
                thread_id=self._thread_id,
                reply_to_message_id=self._reply_to_message_id,
                card=card,
            )
            self._card_id = created.card_id
            self._message_id = created.message_id
            self._notify_send("card_non_final", True)
            self._last_update = self._monotonic()
            # 创建本身就是该话题的首帧，后续更新也要遵守 500ms 间隔。
            self._rate_limiter.allow(topic=self._topic, now=self._last_update)
        except DeliveryRejected:
            # 明确失败（白名单，独立审核 R-1）：卡片路径统一走同话题文本回退。
            self._notify_send("card_non_final", False)
            self._fallback_needed = True
        except Exception:  # 结果不明：建卡是否已经在服务端生效不可知，
            # 不能假装明确失败——原样抛给调用方，不降级、不清预留位、不重试。
            raise

    def update(
        self,
        *,
        elapsed_seconds: int,
        action: str = PROGRESS_ACTION_PROCESSING,
        query_count: int | None = None,
        query_step: str | None = None,
    ) -> None:
        if self._card_id is None or self._fallback_needed:
            return
        if elapsed_seconds >= CARD_AUTO_CLOSE_HANDOFF_SECONDS:
            # G-CARD 10 分钟自动关闭提前收口（Issue #407，见 ``CARD_AUTO_CLOSE_
            # HANDOFF_SECONDS`` 上方的完整取舍说明）：这一帧不再渲染常规进度
            # 文案，改写成明确的"即将改用文字消息"提示，并立即降级——调用方
            # （Gateway）之后的每一次 ``update``/``finish`` 都会走既有的文本
            # 兜底路径，不需要任何新增分支。**只会触发一次**：`_emit_handoff_
            # notice()` 的 `finally` 块无条件把 `_fallback_needed` 置位，本方法
            # 顶部的早退分支从此拦住这个实例后续的每一次调用——不需要单独的
            # "是否已经通知过"标记，复用既有的降级状态就是这份"只发一次"保证
            # 的唯一来源，见该方法文档。
            self._emit_handoff_notice()
            return
        now = self._monotonic()
        if not self._rate_limiter.allow(topic=self._topic, now=now):
            return
        self._accumulate_step(
            action=action,
            elapsed_seconds=max(0, elapsed_seconds),
            query_count=query_count,
            query_step=query_step,
        )
        card = self._accumulated_status_card()
        self._sequence += 1
        try:
            self._before_external()
            self._transport.update(card_id=self._card_id, sequence=self._sequence, card=card)
            self._notify_send("card_non_final", True)
            self._last_update = now
        except Exception:
            self._notify_send("card_non_final", False)
            self._fallback_needed = True

    def _accumulate_step(
        self,
        *,
        action: str,
        elapsed_seconds: int,
        query_count: int | None = None,
        query_step: str | None = None,
    ) -> None:
        """过程流式（Issue #407 方向 B）：把这一次信号记进"已走过的步骤"列表。

        识别一次信号是不是"新步骤"，只看语义身份 ``(action, query_count,
        query_step)`` 是否与上一次不同——问数查询的 ``query_count`` 递增
        （即使 ``query_step`` 相同）也算新步骤，因为"第几次查询"本身就是
        Issue #321/#407 要求的"有业务含义的文字变化"。身份不变时（例如兜底
        刷新复用同一个状态、还没有任何新信号）只原地刷新这一行，不重复追加
        同一句话——否则长任务会在同一个阶段反复刷屏一模一样的行，与"只到
        步骤名深度"的产品意图背道而驰。

        只有 ``PROGRESS_ACTION_QUERYING`` 才把 ``query_count``/``query_step``
        计入身份——其它动作即使调用方误传了这两个参数也不区分，防止调用方
        传参不一致时把同一个 composing/working 状态错误地拆成多行。

        **停滞判定（Issue #444）**：身份不变时，同时算出这枚身份已经持续了
        多久（``elapsed_seconds`` 减去它第一次出现时的 ``elapsed_seconds``，
        即 ``_current_step_started_at``）。跨过 :data:`STALL_THRESHOLD_
        SECONDS` 才把这个"已持续秒数"交给 ``_render_step_line`` 切换成停滞
        明示文案；没跨过时仍然只是原地刷新总用时的旧措辞——避免任务刚好在
        两次事件驱动更新之间、只经过一个兜底周期的正常静默就被误判成异常
        （rc21 修复包 B：本阈值恒等于 ``_PROGRESS_FALLBACK_SECONDS`` 的 2
        倍，见 :data:`STALL_THRESHOLD_SECONDS` 上方「误报双修」注释）。
        换成新身份时清零这个锚点，只有新身份自己持续够久才会再次触发。
        """

        if action == PROGRESS_ACTION_QUERYING:
            identity = (action, query_count, query_step)
        else:
            identity = (action, None, None)
        is_repeat = bool(self._step_records) and self._last_step_identity == identity
        record = _StepRecord(
            action=action,
            elapsed_seconds=elapsed_seconds,
            query_count=query_count,
            query_step=query_step,
        )
        if is_repeat:
            self._step_records[-1] = record
        else:
            self._current_step_started_at = elapsed_seconds
            self._step_records.append(record)
            if len(self._step_records) > MAX_ACCUMULATED_STEP_LINES:
                del self._step_records[0]
        self._last_step_identity = identity

    def _accumulated_status_card(self) -> RenderedCard:
        """把当前累积的步骤列表渲染成一张 ``query.status`` 卡片（Issue #407
        方向 B）：正文是"已走过的步骤名"按时间顺序换行追加，飞书官方的流式
        打字机效果天然承担"新行逐字出现"的动态观感，这里只负责让正文本身
        持续变长，不做任何字符级动画模拟。

        **每次调用都重新渲染全部行**（Trace #469 S-1 TOP-9）：只有列表里
        **最后一行**（当前正在发生的步骤）用"正在..."的现在时措辞，其余
        历史行一律改用"已..."的完成时措辞——此前 ``_step_lines`` 直接缓存
        已经渲染好的字符串，一条历史行永远停在它被追加那一刻的现在时措辞，
        读起来像"过去发生的事現在还在发生"。停滞判定（``STALL_THRESHOLD_
        SECONDS``）只对最后一行计算——历史行代表"已经翻篇的步骤"，不存在
        "停滞"这个概念。
        """

        last_index = len(self._step_records) - 1
        lines: list[str] = []
        for index, record in enumerate(self._step_records):
            if index == last_index:
                duration = max(0, record.elapsed_seconds - self._current_step_started_at)
                stalled_seconds = duration if duration >= STALL_THRESHOLD_SECONDS else None
                lines.append(
                    self._render_step_line(
                        action=record.action,
                        elapsed_seconds=record.elapsed_seconds,
                        query_count=record.query_count,
                        query_step=record.query_step,
                        stalled_seconds=stalled_seconds,
                        completed=False,
                    )
                )
            else:
                lines.append(
                    self._render_step_line(
                        action=record.action,
                        elapsed_seconds=record.elapsed_seconds,
                        query_count=record.query_count,
                        query_step=record.query_step,
                        completed=True,
                    )
                )
        return self._catalog.card("query.status", status="\n".join(lines))

    def _emit_handoff_notice(self) -> None:
        """接近 G-CARD 10 分钟自动关闭阈值时的最后一帧卡片更新（Issue #407）。

        刻意不经过 ``CardRateLimiter.allow()`` 的单话题 500ms 节流——这是一次
        性的关键提示（同一实例生命周期最多触发一次，见 ``update()`` 顶部早退
        分支与本方法 ``finally`` 里 ``_fallback_needed`` 置位的说明），被节流
        吞掉就等于用户永远看不到这句解释；与 ``finish()`` 的终态两帧同一
        姿态，改用 ``record()`` 无条件记入全进程 50 次/秒预算。发送本身失败
        （任何异常）不改变"接下来改走文本通道"这个既定动作——``finally`` 保证
        ``_fallback_needed`` 一定被置位，不需要调用方感知这次调用是否真的
        成功送达；即使这一帧没有送达，卡片本身也已经停在上一帧，后续文本
        终态仍会正常送达最终答案，不构成结果丢失。
        """

        now = self._monotonic()
        card = self._status_card_handoff_notice()
        self._sequence += 1
        try:
            self._before_external()
            self._rate_limiter.record(topic=self._topic, now=now)
            self._transport.update(card_id=self._card_id, sequence=self._sequence, card=card)
            self._notify_send("card_non_final", True)
            self._last_update = now
        except Exception:  # 见方法文档：发送失败不改变既定降级动作
            self._notify_send("card_non_final", False)
        finally:
            self._fallback_needed = True

    def finish(
        self,
        *,
        result: str | None = None,
        failure: RenderedContent | None = None,
        elapsed_seconds: int = 0,
    ) -> None:
        """终态更新 + 关闭。**这两步的失败语义并不对称**（独立审核 P1-2）：

        - 终态 **更新** 失败：完整答案还没有确定写进卡片，用户看不到结果——必须
          整体降级为文本兜底，否则结果丢失。
        - 终态更新已经成功之后，仅 **关闭** 失败：答案已经在卡片里对用户可见，
          结果没有丢——按 G-CARD 实测（未手动关闭的流式卡片距上次开启 10 分钟后
          由平台自动关闭），关闭失败本身不构成"结果丢失"，只是收尾没做完。此时
          绝不能再触发文本兜底：那样只会让同一条答案在同一话题里出现两遍，是
          比"卡片没关"更差的用户体验，也直接违反"不得同时形成卡片终态与重复
          文本终态"（`V-卡片-03`）。因此关闭失败**不**置位 ``_fallback_needed``。
        """

        if self._card_id is None or self._fallback_needed:
            return
        if failure is None:
            if result:
                completed = self._catalog.text(
                    "worker.status",
                    action=self._catalog.text("worker.action.completed").text,
                    elapsed_seconds=max(0, elapsed_seconds),
                ).text
                body = f"{completed}\n{result}"
                # `result` 是模型生成的终态正文（Issue #322）：worker 出口安全
                # （`constrain_output`/`redact_free_text`）已经做过协议泄漏与
                # 凭据净化，这里的目录校验只再保留协议泄漏这一道，不能再用为
                # 固定模板设计的自然语言词表（「还需/权限不足」等）拦截模型的
                # 日常措辞。
                card = self._catalog.card("query.result", result=body, contains_model_text=True)
            else:
                # Trace #469 S-1 TOP-9：此前这里也会先拼一行"已完成 · N 秒"，
                # 再换行拼 `query.empty` 卡的固定正文"本次未取得可用结果。"——
                # 两行紧挨着读像自相矛盾（"已完成"暗示成功，紧接着又说"没有
                # 结果"）。改成 `query.empty` 卡自己的模板一次性把两件事说成
                # 一句连贯的话（见 content.toml 该卡片的 body 模板），不再由
                # 这里分两行拼接。
                card = self._catalog.card("query.empty", elapsed_seconds=max(0, elapsed_seconds))
        else:
            # `failure.text` 可能是我们自己的固定失败文案，也可能是 STOPPED
            # 终态携带的模型残余正文（`worker.stopped_result`，同样已经在
            # worker 侧出口净化过）——这里已经无法区分两者，统一按“可能含模型
            # 正文”处理；我们自己的固定文案已经在 content.toml 加载期校验过，
            # 不会因此漏检（Issue #322）。
            card = self._catalog.card(
                "query.failure", message=failure.text, contains_model_text=True
            )

        self._sequence += 1
        try:
            self._before_external()
            self._rate_limiter.record(topic=self._topic, now=self._monotonic())
            self._transport.update(card_id=self._card_id, sequence=self._sequence, card=card)
            self._notify_send("card_final", True)
        except DeliveryRejected:
            # 明确失败（白名单，独立审核 R-1）：终态正文还没确定送达，只能整体降级。
            self._notify_send("card_final", False)
            self._fallback_needed = True
            return
        except Exception:  # 结果不明：终态正文是否已经写进卡片对进程
            # 不可知——绝不能当"明确失败"整体降级为文本兜底，那正是本次复现的跨
            # 通道重复投递。原样抛给调用方，不降级、不清预留位、不重试。
            raise

        self._sequence += 1
        try:
            self._before_external()
            self._rate_limiter.record(topic=self._topic, now=self._monotonic())
            self._transport.close(card_id=self._card_id, sequence=self._sequence, card=card)
            self._notify_send("card_final", True)
        except Exception:  # 见上面的方法说明：不降级，避免重复投递
            # 独立审核 B-1/R-1 均不延伸到这里：关闭失败——无论是 DeliveryRejected
            # 还是任何其它异常——都不改变"更新已经成功、答案对用户可见"这个结论，
            # 本身就与异常是否属于结果不明无关，因此不单独拆出 ``except DeliveryRejected``。
            self._notify_send("card_final", False)

    def send_fallback(self, content: RenderedContent) -> str | None:
        """发一次同话题文本兜底；不需要时（未降级）返回 ``None`` 且不产生外部调用。

        调用方负责在这次调用之前完成"外发前预留位"的持久化（Issue #151 审核 P3-6、
        本类文件头注释）——文本发送没有飞书原生幂等键，这里的异常刻意不吞掉，交由
        调用方区分"同步捕获的明确失败"（``DeliveryRejected``，可以清预留位、下一轮
        重试）、"结果不明"（除 ``DeliveryRejected`` 以外的一切异常，独立审核 R-1
        白名单：转 ``uncertain``，不清预留位、不重试）与"进程崩溃"（预留位留在
        数据库里，下一轮必须识别为 ``uncertain`` 而不是自动重发）。
        """

        if not self._fallback_needed:
            return None
        try:
            self._before_external()
            message_id = self._fallback.send_text(
                chat_id=self._chat_id,
                thread_id=self._thread_id,
                reply_to_message_id=self._reply_to_message_id,
                text=content.text,
            )
        except DeliveryRejected:
            # 明确失败（白名单，独立审核 R-1）。
            self._notify_send("message_final", False)
            raise
        except Exception:
            # 结果不明：文本是否已经送达对进程不可知，不计入"发送失败"的告警统计
            # （那会把可能已经成功的一次发送计成失败）；原样抛给调用方，不清预留位、
            # 不重试。
            raise
        self._message_id = message_id
        self._notify_send("message_final", True)
        return message_id

    def _render_step_line(
        self,
        *,
        action: str,
        elapsed_seconds: int,
        query_count: int | None = None,
        query_step: str | None = None,
        stalled_seconds: int | None = None,
        completed: bool = False,
    ) -> str:
        """按语义化进度动作码选文案，渲染成累积列表里的一行（Issue #321 方向
        C；Issue #407 增粒度／方向 B；Issue #444 停滞明示；Trace #469 S-1
        TOP-9 历史行改完成时措辞）。

        ``completed``——``True`` 时选用"已..."的完成时措辞（
        :data:`_ACTION_DONE_TEXT_KEYS`/:data:`_QUERY_STEP_ACTION_TEXT_KEYS_DONE`），
        供 :meth:`CardStream._accumulated_status_card` 渲染"已经翻篇的历史
        步骤"；``False``（默认）时保留既有"正在..."现在时措辞，只用于列表里
        最后一行（当前正在发生的步骤）。``stalled_seconds`` 与 ``completed=True``
        不会同时出现——停滞是"当前步骤"才有意义的判断，调用方（
        ``_accumulated_status_card``）只对最后一行计算 ``stalled_seconds``。

        ``query_count`` 缺失（``None``）时 ``querying`` 退化为默认 ``processing``
        文案而不是抛错或渲染出一句缺占位符的残句——这只可能发生在
        ``decode_progress_action`` 解析到脏数据、或调用方传参不一致时，卡片
        渲染必须失败关闭到一个安全默认值，不能把内部状态不一致带给用户。

        ``query_step``——白名单式映射（Issue #407 出口安全红线）：只有落在
        :data:`_QUERY_STEP_ACTION_TEXT_KEYS` 这张"工具名→用户语文案键"的映射表
        里才会选到对应的更细文案；``None``、或调用方传入任何不在表中的字符串
        （已经在 ``encode_progress_action``/``decode_progress_action`` 两处
        过滤过，这里是第三层防御——直接用 ``dict.get`` 查不到就落回默认值，
        不做任何字符串拼接或动态键名，因此不存在"传入未知值就把它拼进渲染键"
        这条注入面）一律落回通用文案 ``worker.action.querying_metrics``。

        ``stalled_seconds``——非 ``None`` 时（``_accumulate_step`` 判定同一枚
        身份已经跨过 :data:`STALL_THRESHOLD_SECONDS` 仍未变化）改用
        ``worker.status_stalled`` 明示停滞的措辞，而不是常规的
        ``worker.status``；``action_text`` 本身不变——它已经带着"停滞发生在
        哪一步"这个位置信息（例如"正在第 2 次查询指标数据"），停滞措辞只是
        换了个更明确的后缀，不是丢掉位置信息重新说一遍。
        """

        if action == PROGRESS_ACTION_COMPLETED:
            action_text = self._catalog.text("worker.action.completed").text
        elif action == PROGRESS_ACTION_QUERYING and query_count is not None:
            if completed:
                text_key = _QUERY_STEP_ACTION_TEXT_KEYS_DONE.get(
                    query_step, _DEFAULT_QUERYING_DONE_TEXT_KEY
                )
            else:
                text_key = _QUERY_STEP_ACTION_TEXT_KEYS.get(query_step, "worker.action.querying_metrics")
            action_text = self._catalog.text(text_key, count=query_count).text
        elif action == PROGRESS_ACTION_COMPOSING:
            # 完成时措辞改走 _ACTION_DONE_TEXT_KEYS（B-4，Trace #469 修复包
            # B）——此前这里内联拼接 "..._done"，与上面查询类分支已经在用的
            # _QUERY_STEP_ACTION_TEXT_KEYS_DONE 查表姿态不一致，也让
            # _ACTION_DONE_TEXT_KEYS 这张表定义了却从未被引用。取值与内联字面量
            # 逐字节相同（"worker.action.composing_done"），渲染输出不变。
            key = (
                _ACTION_DONE_TEXT_KEYS[PROGRESS_ACTION_COMPOSING]
                if completed
                else "worker.action.composing"
            )
            action_text = self._catalog.text(key).text
        elif action == PROGRESS_ACTION_WORKING:
            key = (
                _ACTION_DONE_TEXT_KEYS[PROGRESS_ACTION_WORKING]
                if completed
                else "worker.action.working"
            )
            action_text = self._catalog.text(key).text
        else:
            key = (
                _ACTION_DONE_TEXT_KEYS[PROGRESS_ACTION_PROCESSING]
                if completed
                else "worker.action.processing"
            )
            action_text = self._catalog.text(key).text
        if stalled_seconds is not None:
            return self._catalog.text(
                "worker.status_stalled", action=action_text, stalled_seconds=stalled_seconds
            ).text
        return self._catalog.text(
            "worker.status", action=action_text, elapsed_seconds=elapsed_seconds
        ).text

    def _status_card_handoff_notice(self) -> RenderedCard:
        """G-CARD 10 分钟自动关闭提前收口的固定文案（Issue #407），复用
        ``query.status`` 卡片形状——不是常规的"{action} · {elapsed_seconds} 秒"
        状态句，直接展示一句完整、不含占位符的说明。
        """

        return self._catalog.card(
            "query.status", status=self._catalog.text("worker.card_handoff_notice").text
        )

    @property
    def _topic(self) -> str:
        return f"{self._chat_id}\x00{self._thread_id or ''}"

    def _before_external(self) -> None:
        if self._mark_external_side_effect is not None:
            marked = self._mark_external_side_effect()
            if marked is False:
                raise RuntimeError("任务已不再由当前 worker 持有")

    def _notify_send(self, operation: str, succeeded: bool) -> None:
        """把发送结果交给告警层；告警层故障不能改变用户任务的出站语义。"""

        if self._on_send_outcome is None:
            return
        try:
            self._on_send_outcome(operation, succeeded)
        except Exception:
            # 告警输入失败不能反向把已成功的用户交付改成失败，也不能中断文本回退。
            return
