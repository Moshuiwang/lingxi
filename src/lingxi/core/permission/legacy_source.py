"""存量权限沿用的只读读取（S-P-2 批二，Issue #319 / Trace #328 E-P）。

[`core/permission/merge_sources.py`](merge_sources.py) 的模块文档「``legacy`` 参数」
一节把签名定死、把数据源留白：``legacy: Mapping[str, Sequence[str]] | None``，本模块
补上这一份数据源——**只读**，从正式权限发布表（``adapters/feishu_permission_bitable.
BitablePermissionTable`` 已经实测过的 ``find_rows``/``read_row`` 两个方法，签名不改）
按用户取存量行的 ``permissions`` 字段文本，用 :mod:`lingxi.core.permission.publish_row`
既有的**读侧解析器** :func:`~lingxi.core.permission.publish_row.parse_permissions`
解析成 ``{公司ID: (指标名, …)}``，供 :func:`~lingxi.core.permission.merge_sources.
merge_permission_sources` 的 ``legacy`` 参数消费。

## 键口径：与发布路径同源

``record_key``/``email`` 两个查找键取**同一个规范化邮箱**——:func:`~lingxi.core.
permission.account_match.normalize_email` 归一后的值，与
:func:`~lingxi.core.permission.publish_row.build_publish_row`/
:func:`~lingxi.core.permission.publish_row.build_translated_publish_row` 结算发布行
时对两列写入的值完全同源（那两个函数都把 ``record_key``/``email`` 一并设成同一个
``normalized``，:func:`~lingxi.core.permission.publish.publish_claim` 的
``find_rows(record_key=row.record_key, email=row.email)`` 因此传的也是同一个值）。
本模块只暴露一个 ``email`` 入参，内部现算 ``record_key``——调用方不需要自己复述这条
归一，也不会有两处各自归一出现漂移。

## 失败语义：找不到行不是错误，读不懂/读不到才是

- **零行命中**：新用户从未在旧系统留下权限行，是预期情形——返回空映射（对
  ``merge_permission_sources`` 恒等，参与合并没有任何贡献）。
- **命中多行，或命中的行 ``record_key`` 与我们要查的口径不一致**：与
  :func:`~lingxi.core.permission.publish.publish_claim` 的 ``CONFLICT`` 分支同一姿态
  ——不知道该沿用哪一行，失败关闭，抛 :class:`LegacySourceError`
  （:data:`REASON_LEGACY_MULTIPLE_ROWS`/:data:`REASON_LEGACY_KEY_MISMATCH`）。
- **``permissions`` 列解析失败**（空文本、非法 JSON、形状不对——
  :func:`~lingxi.core.permission.publish_row.parse_permissions` 的既有判据，本模块
  不重新发明）：抛 :class:`LegacySourceError`\\ (:data:`REASON_LEGACY_UNPARSEABLE`)。
- **传输层异常**（``find_rows``/``read_row`` 抛出的
  :class:`~lingxi.core.permission.publish.PermissionTableError` 或任何未预期异常）：
  抛 :class:`LegacySourceError`\\ (:data:`REASON_LEGACY_READ_FAILED`)，原始异常经
  ``__cause__`` 保留，不吞。

四类失败**全部**只是"这一个用户本轮跳过存量源"——异常本身**不冒泡带走整轮/整人**：
:func:`resolve_legacy_source` 把「读取 + 降级 + 审计」这套姿态包成一份共用实现，供
两个调用点（``apps/scheduler/permission_refresh.py`` 的 ``_refresh_user``、
``core/identity/onboarding_runner.py`` 的 ``_publish``）各自传入自己的审计动作名
（``permission_refresh.legacy_source_skipped``/``onboarding.legacy_source_skipped``）
复用——姿态与 S-P-3 ``local_override_read_failed`` 同款分层：**装配未接线**
（``table=None``）静默按"没有存量源"处理，**读取/解析失败**响亮记审计
（``reason=`` 取错误码，``error=`` 取底层异常类名，**不含正文**——异常消息可能带上
Base 标识或记录内容片段）。

## 通配角：不需要在这里特殊处理

`513` 后台管理员（``all_companies=True``）持有人的存量沿用同样要被跳过（`merge_sources`
模块文档「通配角 v1」一节），但那条规则已经在
:func:`~lingxi.core.permission.merge_sources.merge_permission_sources` 的通配分支里
生效——该分支直接返回 ``galaxy_map``、完全不读 ``legacy`` 参数，因此本模块读到的存量
权限即使非空，也会在合并这一步被丢弃，不需要在读取侧重复判断一次。

## 有界条件：只在「Lingxi 从未为该用户成功发布过」时参与合并（红线-1，Trace #328 opus 审查）

**本模块读的表与发布通道写的是同一张表**（正式权限发布表
``adapters/feishu_permission_bitable.BitablePermissionTable``）。如果不加边界，会形成
一个自反馈环：Lingxi 今天发布收窄后的权限 → 写进这张表 → 明天这一轮重算把这一行当作
「存量沿用」读回来、并进合并结果 → 收窄被自己昨天写的内容原样抵消，指标降权/公司收窄
**永远不会真正生效**，且因为内容与上一条 ``pending``/``published`` 意图逐字段相同，
``record_decision`` 会判 ``UNCHANGED``，连一条新审计都不会有——从审计上完全看不出这个
回归正在发生（审查探针坐实）。

因此两个调用点（``apps/scheduler/permission_refresh.py`` 的 ``_refresh_user``、
``core/identity/onboarding_runner.py`` 的 ``_publish``）在调用
:func:`resolve_legacy_source` 之前，先查该用户在发布链上「有没有留下过足迹」
（:meth:`~lingxi.adapters.postgres_permission_publish.PostgresPermissionPublishStore.
has_publish_footprint`——与撤权侧复用的同一个只读口，见
``apps/scheduler/permission_refresh.py`` 模块文档「撤权」一节）：

- **有过足迹**（发布成功过，或当前还有 ``pending``/``publishing`` 的意图在途）：
  legacy 直接按 ``None`` 处理，**且不调用** :func:`read_legacy_permissions`（连
  ``find_rows`` 都不发起，省一次读放大）——这张表此刻很可能已经是 Lingxi 自己写的
  内容，不再是「旧系统遗留、我们从未碰过」的存量。
- **从未有过足迹**：legacy 照旧参与合并——这才是「存量沿用」要覆盖的真实场景：这个
  人的权限行是旧系统 biai-agent 写的，Lingxi 第一次为他结算发布内容时，要把这份旧
  权限并进来，不能让「切到 Lingxi」变成一次静默降权。

这条边界只影响**是否读取并参与合并**，不改变 :func:`merge_permission_sources` 本身
（`legacy=None` 恒等，模块文档「``legacy`` 参数」一节）——有界化发生在调用方，本模块
与合并层都不需要知道"为什么这次是 None"。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from lingxi.core.permission.account_match import normalize_email
from lingxi.core.permission.publish import ExistingPermissionRow
from lingxi.core.permission.publish_row import parse_permissions, readback_text

#: 命中多行：不知道该沿用哪一行（与 ``publish.py`` 的 ``CONFLICT`` 同一姿态）。
REASON_LEGACY_MULTIPLE_ROWS = "multiple_rows"
#: 命中一行，但它的 record_key 与我们要查的口径不一致：同上，失败关闭而不是猜。
REASON_LEGACY_KEY_MISMATCH = "record_key_mismatch"
#: 命中的行 permissions 列解析失败（空文本 / 非法 JSON / 形状不对）。
REASON_LEGACY_UNPARSEABLE = "unparseable"
#: 传输层调用失败（find_rows 抛出的异常，含 PermissionTableError 与其余未预期异常）。
REASON_LEGACY_READ_FAILED = "read_failed"


class LegacySourceError(RuntimeError):
    """读一个用户的存量权限行失败。

    ``code`` 是上面四个原因码之一，供调用方决定审计 ``reason``；``detail``（可选）是
    触发失败的底层异常的**类名**（``unparseable``/``read_failed`` 才有，
    ``multiple_rows``/``record_key_mismatch`` 是本模块自己的失败关闭判断、不源自某个
    被捕获的异常，因此为 ``None``）。消息与 ``detail`` 都不含任何字段值——只报类型，
    不报正文。
    """

    def __init__(self, code: str, *, detail: str | None = None) -> None:
        super().__init__(f"存量权限读取失败：{code}")
        self.code = code
        self.detail = detail


class LegacyPermissionTable(Protocol):
    """正式权限发布表的只读子集——只声明 ``find_rows`` 一个方法（读放大修复，Trace #328
    opus 审查 P2：``find_rows`` 的整表分页扫描已经把命中行的全部字段带回来了，见
    :meth:`~lingxi.adapters.feishu_permission_bitable.BitablePermissionTable.find_rows`
    的实现——逐条 ``ExistingPermissionRow.fields`` 就是那一行在列表接口里返回的完整
    字段映射，不需要再对同一行发一次 ``read_row`` 详情查询）。

    实现是 :class:`~lingxi.adapters.feishu_permission_bitable.BitablePermissionTable`
    （签名不改：装配层把同一个实例既喂给发布执行器，也喂给本协议，那个类仍然保留
    ``read_row``/``create_row``/``update_row`` 给发布执行器用，本协议只声明存量沿用
    路径真正用到的这一个方法）。故意不声明写方法：本模块只读，把它们排除在类型之外，
    让"存量沿用路径不会意外写这张表"这件事在类型上就说得清楚——与
    :class:`~lingxi.apps.scheduler.permission_refresh._TokenCipherReader` 只声明
    一个只读方法同一条理由。
    """

    def find_rows(self, *, record_key: str, email: str) -> Sequence[ExistingPermissionRow]: ...


class _AuditSink(Protocol):
    def record(self, action: str, /, **fields: object) -> None: ...


def read_legacy_permissions(
    *, email: str, table: LegacyPermissionTable
) -> dict[str, tuple[str, ...]]:
    """按用户当前邮箱取它在正式权限发布表里的存量行，解析出 ``{公司ID: (指标名, …)}``。

    失败语义见模块文档；本函数自身不做 I/O 之外的任何副作用、不记审计——姿态与
    :func:`~lingxi.core.permission.merge_sources.merge_permission_sources` 同一层次：
    这里只回答"这个用户存量行里写了什么"，记不记、怎么记留给
    :func:`resolve_legacy_source`。
    """

    normalized = normalize_email(email)
    try:
        matches = tuple(table.find_rows(record_key=normalized, email=normalized))
    except Exception as error:  # noqa: BLE001 - 传输层异常一律归类为读取失败
        raise LegacySourceError(REASON_LEGACY_READ_FAILED, detail=type(error).__name__) from error

    if not matches:
        return {}
    if len(matches) > 1:
        raise LegacySourceError(REASON_LEGACY_MULTIPLE_ROWS)
    if not matches[0].matches_key(normalized):
        raise LegacySourceError(REASON_LEGACY_KEY_MISMATCH)

    # 读放大修复（Trace #328 opus 审查 P2）：`find_rows` 的整表分页扫描已经把命中行
    # 的全部字段带回来了（见 `LegacyPermissionTable` 文档），直接用它，不再对同一行
    # 发第二次 `read_row` 详情查询——省一次外部表往返。
    text = readback_text(matches[0].fields.get("permissions"))
    try:
        return parse_permissions(text)
    except ValueError as error:
        raise LegacySourceError(REASON_LEGACY_UNPARSEABLE, detail=type(error).__name__) from error


def resolve_legacy_source(
    *,
    email: str,
    table: LegacyPermissionTable | None,
    audit: _AuditSink,
    action: str,
    user: str,
) -> dict[str, tuple[str, ...]] | None:
    """两个调用点共用的「读存量权限 + 失败降级 + 审计」姿态（S-P-2，Issue #319/#328）。

    ``table`` 为 ``None``（装配层未接线）时静默返回 ``None``——对
    :func:`~lingxi.core.permission.merge_sources.merge_permission_sources` 的
    ``legacy`` 参数恒等，行为与接线之前逐字节一致，不告警（部署事实，同
    ``local_overrides=None`` 的既有姿态）。

    读取/解析失败（:class:`LegacySourceError`）时**该用户本轮跳过存量源**，记一条
    ``action`` 审计（``reason=`` 取 ``.code``，``error=`` 取 ``.detail``，二者都不含
    任何字段正文），异常本身**不冒泡**——一次开通/一次刷新不因存量行读不懂而整链/
    整人失败。两个调用点（``apps/scheduler/permission_refresh.py`` 的
    ``_refresh_user``、``core/identity/onboarding_runner.py`` 的 ``_publish``）动作名
    各自不同（``permission_refresh.legacy_source_skipped``/
    ``onboarding.legacy_source_skipped``），姿态相同，因此只写一份实现，不在每个
    调用点各自 try/except 一次。
    """

    if table is None:
        return None
    try:
        return read_legacy_permissions(email=email, table=table)
    except LegacySourceError as error:
        fields: dict[str, object] = {"user": user, "reason": error.code}
        if error.detail is not None:
            fields["error"] = error.detail
        audit.record(action, **fields)
        return None


__all__ = [
    "LegacyPermissionTable",
    "LegacySourceError",
    "REASON_LEGACY_KEY_MISMATCH",
    "REASON_LEGACY_MULTIPLE_ROWS",
    "REASON_LEGACY_READ_FAILED",
    "REASON_LEGACY_UNPARSEABLE",
    "read_legacy_permissions",
    "resolve_legacy_source",
]
