"""每日权限重算职责（Issue [#156](https://github.com/Moshuiwang/lingxi/issues/156) 的 S-C-03a）。

一轮做的事，次序不可调换：

1. **顺序判据**：花名册持久快照的 ``captured_at`` 必须是**今天**（UTC 日界）。不是就
   整轮不跑，只留一条审计——**不触银河、不产生任何发布意图**；
2. 读当前有效批次的银河快照（:mod:`lingxi.adapters.postgres_galaxy_snapshot`）；
   没有有效批次同样整轮不跑，审计可分辨；
3. **翻译层整体可用性判据**：翻译映射为空时整轮同样不跑，**一条发布意图都不排，
   撤权也不例外**——见「翻译」一节，这是外部独立审查 2026-08-18 坐实的 P1 修复；
4. 遍历**已开通**用户（``provisioning_state='active'`` 且 ``account_state NOT IN
   ('deleting','deleted')``，过滤写在 SQL 里，与花名册审计同一口径），逐人：
   花名册身份 → 银河账号匹配 → 聚合当前有效权限 → 授权侧**翻译成指标名**（这一条
   组合未覆盖就跳过这个人，见「翻译」一节；撤权侧不需要翻译，写的是不含指标名的
   ``{}``）→ 结算发布行 → 记一次权限决定；
5. 记一条只含计数的职责报告审计。

## 为什么顺序判据是「花名册今天更新过」而不是一个开关

合同要求每日刷新**严格先刷新花名册、再刷新银河快照**（`V-权限-07`）。「先」如果只靠
职责在列表里的位置来保证，那么花名册那一轮失败、或者压根没注册时，权限重算照样会跑
——用的是几天前的花名册。那不是"顺序对了"，那是"顺序看起来对了"。因此这里把顺序变成
一条**数据判据**：只有当库里那份花名册快照是今天取的，才允许重算。

**这条判据在当前部署下仍然为假，但原因已经换了一层，这是刻意的失败关闭、不是缺陷。**
花名册读取所用的短期令牌供给已由 [#215](https://github.com/Moshuiwang/lingxi/issues/215)
在代码层接上（凭据轮换职责按需派生、进程内持有者转交，`build_loop` 默认就建一条），因此
只要花名册那三个环境变量配齐、日报职责注册并真的读到一轮，快照就会被换成今天那一份，
本职责的第一步同一轮内即可通过。挡住它的现在是**部署事实**：当前部署没配那三个变量，
且真实读取所需的专用主体凭据自 2026-08-09 起未落盘（Issue
[#52](https://github.com/Moshuiwang/lingxi/issues/52) 的 G-READ 判定），``roster_snapshot``
表因此仍然是空的，本职责每轮都会在第一步停下并留一条
``permission_refresh.skipped_roster_not_fresh``。**不提供任何旁路开关**：
一个"允许用旧花名册重算"的环境变量，会在第一次运维着急时被打开，然后再也不会被关上。

## 撤权：**保行、清空 ``permissions``**，且只对"我们发布过的人"发

产品负责人 2026-08-18 裁定 3（`V-权限-08` 的刷新侧，留痕见 Trace
[#203](https://github.com/Moshuiwang/lingxi/issues/203) 的决策评论）：权限从有变无时，
发布表那一行**留着**、``permissions`` 写成空对象、``status`` 不动、``token_cipher``
不碰。落点是 :func:`~lingxi.core.permission.publish_row.build_revocation_row`，它产出的
行只有六个字段，因此走的必然是更新路径。

本职责在它之上还有**两条边界**，都是刻意的：

1. **只有在发布链上留下过足迹的用户才发撤权更新**（判据
   :meth:`~lingxi.adapters.postgres_permission_publish.PostgresPermissionPublishStore.
   has_publish_footprint`：发布成功过，**或**当前还有 ``pending``/``publishing`` 的意图
   在途）。"在途也算"这一半是二级审查 N7 抓到的时间线要求的：昨天排的授权意图还堵在
   ``pending``、今天这个人被撤权时若跳过，等发布面消费积压时**已经被收回的范围**会被
   写进外部表并触发一条"范围已更新"通知；把它算进来之后，撤权决定推进版本，旧意图被
   认领时判 ``SUPERSEDED``（一次外部调用都不发），撤权意图自身在外部无行时以
   ``missing_token_cipher`` 失败关闭——两条路的方向都安全。

   **在发布链上一点足迹都没有的人照旧跳过**：为他新建一行空权限没有意义——问数 MCP 对
   查无此人本来就默认拒绝——而新建还需要一份令牌密文，等于替"存量用户令牌归属"越过产品负责人已定的方向
   （2026-08-18 裁定 6：硬切窗口统一重签）自作主张。既有 26 行那些旧系统写的人也落在这一路：硬切之前他们的撤权由
   旧系统负责（同一条裁定的时效边界）。审计原因分类
   :data:`SKIP_NO_PUBLISHED_ROW` 让这一路可分辨。
2. **只有聚合层明确判定"无可用权限"才算撤权**（``no_galaxy_roles`` /
   ``no_supported_function`` / ``no_company_scope``）。**匹配阶段的失败仍然只跳过**
   （``roster_not_found``、``key_conflict``、``roster_multiple_rows`` …）：那些说的是
   "我们认不出这个人是谁"，不是"银河说他没有权限"。据一次花名册歧义或数据陈旧去清空
   一个人的权限，方向与花名册那一侧的既定口径正好相反——那边对"花名册查无此人"的处置
   是**仅提示、不做任何自动处置**（``roster.note.removed``），由管理员在日报里判断。

幂等由 :meth:`~lingxi.adapters.postgres_permission_publish.
PostgresPermissionPublishStore.record_decision` 既有的内容比对承担：第二天仍然无权限时，
撤权行与上一条意图逐字段相同 → ``UNCHANGED``，不推进版本、不排新意图、不重复写外部表。
权限恢复时则是一次普通的六字段更新，**不重建行**。

**本职责仍然不通知**：通知的触发点在发布读回一致（撤权）或就绪探针成功（增权）之后，
属 :mod:`lingxi.apps.scheduler.permission_publish`。这里连一个发送端口都没有。

## 全抑制（本地覆盖压光全部权限）同样走撤权出口（红线-2，Trace #328 opus 审查）

四源合并（下文「本地权限覆盖合并」一节）之后，``merged.permissions`` 可能是空字典——
这个人银河这一侧原本是有效授权（否则更早的 ``aggregate.granted`` 判据已经把他分流进
上面的撤权分支，走不到翻译与合并这一步），但本地抑制把翻译出的全部指标都抑制掉了。
**这不是"无可用银河权限"**（``no_galaxy_roles``/``no_supported_function``/
``no_company_scope`` 三个原因码都不适用），是"银河给了、本地行政性地收回到零"——语义
上与撤权完全一致（这个人此刻确实没有任何可发布内容），因此复用同一套
:meth:`_revoke` 机制（保行清空 ``permissions``、只对发布链上留过足迹的人发、走
:data:`PERMISSION_REVOKE_REASON`），但传一个**可分辨**的原因码
（:data:`REASON_FULLY_SUPPRESSED`）。修复前的实际行为是：直接把空字典传给
``build_translated_publish_row`` → ``serialize_translated_permissions`` 对空输入抛
``ValueError``，这个异常不被任何分支捕获，冒泡到 ``run_once`` 的单用户异常兜底，
记成一条不可分辨的通用 ``permission_refresh.user_failed``——审计上完全看不出"这个人
是被本地抑制清空的"，与真正的技术故障混在同一个原因桶里。

## 已送达正文同步清（S-P-5，Trace #328）

授权、撤权两条路径调用 :meth:`~lingxi.adapters.postgres_permission_publish.
PostgresPermissionPublishStore.record_decision` 时都传 ``clear_delivered_content=True``：
权限确实变化（``decision.enqueued`` 为真，即这次不是 ``UNCHANGED``）时，该方法在**自己
的同一个数据库事务**里顺带清空这个用户全部会话已送达、随会话保留的投递正文，并排队
失效当前 Agent 会话——机制与理由见该方法文档，本职责这里只是**接入点**，不复制其锁序
或事务边界的任何细节。

**开关放在调用方而不是 ``record_decision`` 内部自行判断**：同一个方法也服务首次开通
（``core/identity/onboarding_runner.py``），那条路径的用户结构上不可能有历史会话，因此
不传这个开关——见 ``record_decision`` 文档「为什么是调用方显式传入的开关」一节。

清理**发生**时（即 ``decision.enqueued`` 为真，不管这次实际清出多少条事件）记一条
``permission_refresh.delivered_content_cleared`` 审计，``cleared`` 字段是清出的事件数
（可能是 0——一个刚被授权、还没有任何历史会话的人）；不含正文本身，与本职责其余审计
同一条纪律（模块文档顶部 :class:`PermissionRefreshReport` 的说明）。``UNCHANGED`` 分支
不记这条审计，因为清理压根没有发生。

**``trigger`` 字段**（``grant``/``revoke``，:data:`TRIGGER_GRANT`/:data:`TRIGGER_REVOKE`，
Trace #328 opus 审查）：这条审计的两个调用点分别在 :meth:`_refresh_user`（授权，含
红线-2 的全抑制撤权，见「全抑制」一节）与 :meth:`_revoke`（银河侧撤权），运维排查时
不必回头核对同一批次里的其他审计行才能分辨"这次清理是哪条路径触发的"。

## 令牌：只读既有，绝不签发

需要新建发布行的用户，其 ``token_cipher`` 只取该用户**已经登记在令牌表里**的那一份
（:meth:`lingxi.adapters.postgres_mcp_token.PostgresMcpTokenStore.token_cipher`，只取
密文、不解密）。取不到就传 ``None``，随后由发布执行器以 ``missing_token_cipher``
失败关闭——**这正是预期结果，不绕过**。

**本职责一次都不签发令牌。** 「存量用户的令牌归属」（那 26 行是旧系统 biai-agent 签发
的，我们不知其明文）已由产品负责人 2026-08-18 裁定 6 定下方向（留痕见
[#203](https://github.com/Moshuiwang/lingxi/issues/203) 的决策评论）：**在硬切窗口由
lingxi 统一重签、覆写发布行密文，并经 Epic D 的用户环境链重投**。那是一次**有编排、
有窗口、有重投**的迁移动作；在每日刷新里顺手给他们签一份，恰恰是同一件事最危险的做法
——密文写进外部表而新明文没有送到用户环境，那 26 个人下一次问数就会失败，且不可逆。
因此本职责照旧只读既有密文、一次都不签发。

## 翻译：公司 + 职能 → 指标名（Issue #227），两层判据、两个不同的关闭面

聚合层（:func:`~lingxi.core.permission.publish_row.aggregate_permission`）产出的
``functions`` 是**职能标签**，发布表要的是**指标名**。两者之间的翻译由
:func:`~lingxi.core.permission.metric_translation.translate_company_functions` 完成，
映射来自构造时注入的 :attr:`_metric_translation_map`（真实装配读随包发布的
``lingxi/config/company_function_metric_map.toml``，见
:mod:`lingxi.apps.scheduler` 的 ``_build_permission_refresh_duty``；``build_loop``
只加载这一份文件一次，首次开通编排的发布闸——``_build_onboarding_duty`` 的
``publish_allowed``——复用**同一个**已加载对象，不另读一次）。

**这条纪律分两层，关闭面不一样，外部独立审查 2026-08-18 坐实过一次漏洞（P1）之后
定稿如下：**

1. **整轮判据（``run_once`` 开头，遍历任何用户之前）**：映射**整体为空**时，
   :data:`SKIP_METRIC_TRANSLATION_UNAVAILABLE` 让**整轮**一条发布意图都不排——
   授权、撤权**一个都不放行**，连匹配、聚合都不会对任何人执行。这是刻意的最外层
   闸门，理由是**撤权从不调用翻译**（``_revoke`` 写的是不含指标名的
   ``{}``，语义上不需要翻译），如果只在授权那条路径挡，会出现"内容到位之前，
   权限只能被减、不能被恢复"的单向不对称——这正是外部审查抓到的漏洞形状：翻译闸
   摆在授权路径上，撤权从旁路绕过直接写正式表。判据不是"这一行要不要翻译"，是
   "翻译层这一轮可不可用"；映射非空之后，这层闸门自动打开，不需要额外开关。判据
   实现是 :func:`~lingxi.core.permission.metric_translation.metric_translation_available`
   ——**唯一**允许存在的一份，首次开通编排的发布闸调用的是同一个函数。
2. **逐用户判据**（``_refresh_user`` 内，只在整轮判据已经通过、即映射非空之后才可能
   走到）：某个「公司 + 职能」组合在非空映射里仍然查不到时，
   :data:`SKIP_METRIC_TRANSLATION_UNCOVERED` 只跳过**这一个授权用户**——不发布，
   也不撤权（这条与撤权无关：这个人还是走 ``aggregate.granted=True`` 分支，只是
   翻译不出来，从未进入 ``_revoke``）。计入
   :attr:`PermissionRefreshReport.incomplete`，与存档缺邮箱/姓名同一个桶——都是
   "我们这一侧还缺东西，不是这个人的权限变了"。

映射内容全部填齐之前，本职责因此表现为"整轮一条发布意图都不排、报告里连
``examined`` 都是 0，只有一条整轮跳过审计"——这条现象是预期的，不是回归；内容
（哪怕只填了一部分）到位后整轮判据自动打开，逐用户判据接管未覆盖组合的收窄。

同日至多一轮，靠 :attr:`~PermissionRefreshDuty.completed_on` 这个**进程内**的 UTC 日期
水位。跨重启不幂等（新进程水位为空，当天会再跑一轮），这与
:class:`~lingxi.apps.scheduler.RosterAuditDuty` 是同一条已知情接受的残留（迁移
``0063`` 的注释与产品负责人 2026-08-06 的 C2/R2 裁定）。差别是本职责的重跑**在产品上
真正幂等**：权限内容没变时 :meth:`~lingxi.adapters.postgres_permission_publish.
PostgresPermissionPublishStore.record_decision` 判 ``UNCHANGED``，不推进版本、不排新
意图，因此重跑既不会重复发布，也不会让消费方看到任何变化。

**水位在一轮走完之后置位，即使这一轮里有个别用户失败。** 单个用户的失败已经逐条留痕
并计入报告，而"有失败就整轮重来"会让一次持续的数据库故障变成每分钟重跑一遍全员——
那既救不了那个用户，又会把其余职责的时间预算吃掉。失败的用户等下一天那一轮，这正是
"每日刷新"的语义。收到停止信号而中断的那一轮**不置位**：它没走完。

## 本地权限覆盖合并（S-P-3，Issue #319）与存量权限沿用（S-P-2，Trace #328）

授权侧翻译成功（``company_metrics``）之后、结算发布行之前，`_refresh_user` 调用
:func:`~lingxi.core.permission.merge_sources.merge_permission_sources` 把该用户当前
生效的本地覆盖（:class:`_LocalOverrideReader`，装配层未接线时为 ``None``）与存量沿用
（正式权限发布表里该用户的既有行，:class:`~lingxi.core.permission.legacy_source.
LegacyPermissionTable`，装配层未接线时同样为 ``None``）并进去：真实权限 =
(银河翻译结果 ∪ 本地授权 ∪ 存量沿用) − 本地抑制。语义细节（通配角 v1、空结果丢键、
``legacy=None`` 恒等）见该模块文档，不在这里复述。存量源的读取与失败降级由
:func:`~lingxi.core.permission.legacy_source.resolve_legacy_source` 承担——读取/解析
失败只跳过这一个用户的存量源、响亮记 ``permission_refresh.legacy_source_skipped``
审计，不整轮/整人失败；装配层未接线时静默按"没有存量源"处理，与本地覆盖同一姿态。
本地覆盖与存量沿用都**只影响授权路径**，不影响 ``_revoke``——撤权写的是不含指标名
的 ``{}``，两者对它都没有作用面，与翻译层「与撤权无关」同一条边界。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Protocol

from lingxi.core.identity.roster_audit import ArchivedIdentity
from lingxi.core.identity.roster_snapshot import StoredSnapshotFacts
from lingxi.core.permission.account_match import MATCHED, match_galaxy_account
from lingxi.core.permission.local_override import (
    LocalPermissionOverrideEntry,
    ResolvedLocalOverrides,
    resolve_local_overrides,
)
from lingxi.core.permission.legacy_source import LegacyPermissionTable, resolve_legacy_source
from lingxi.core.permission.merge_sources import (
    ALL_COMPANIES_KEY,
    REASON_LOCAL_OVERRIDE_READ_FAILED,
    merge_permission_sources,
)
from lingxi.core.permission.metric_translation import (
    UncoveredPermissionCombination,
    metric_translation_available,
    translate_company_functions,
)
from lingxi.core.permission.publish_row import (
    aggregate_permission,
    build_revocation_row,
    build_translated_publish_row,
)

logger = logging.getLogger(__name__)

_UTC = timezone.utc

#: 写进发布意图 ``reason`` 列的原因码。它回答"这条意图是谁排的"，与首次开通那条
#: （``first_onboarding``）区分开，让运维能一眼看出某次外部写入来自每日刷新。
PERMISSION_REFRESH_REASON = "daily_permission_refresh"

#: 撤权更新那一条意图的 ``reason``。与授权刷新分开是为了让"这次外部写入是去清空权限的"
#: 在 outbox 里一眼可辨——两者的 payload 差别只有 ``permissions`` 一列的内容，
#: 靠肉眼比对 JSON 文本来分辨一次不可逆的外部写入，不是可接受的运维姿态。
PERMISSION_REVOKE_REASON = "daily_permission_revoke"

#: 银河这一侧原本有效授权，但本地抑制把翻译结果压光到空字典时的撤权原因码
#: （红线-2，Trace #328 opus 审查）。与匹配失败、聚合层 fail-closed 的三个原因码
#: （``no_galaxy_roles``/``no_supported_function``/``no_company_scope``）区分开，
#: 让审计能一眼看出"这个人是被本地抑制清空的"，不是"银河本来就没给他权限"。
REASON_FULLY_SUPPRESSED = "fully_suppressed"

#: ``permission_refresh.delivered_content_cleared`` 审计的 ``trigger`` 字段取值
#: （S-P-5 措辞如实化，Trace #328 opus 审查）：区分这次清理是授权路径（含全抑制
#: 撤权，见 :data:`REASON_FULLY_SUPPRESSED`）还是银河撤权路径触发的，供运维排查
#: 时不必回头核对 ``reason`` 列才能分辨。
TRIGGER_GRANT = "grant"
TRIGGER_REVOKE = "revoke"

# ---- 跳过原因码。全部是**固定字面量**，不含任何字段值 -----------------------
#: 花名册快照压根不存在。
SKIP_MISSING_SNAPSHOT = "missing_snapshot"
#: 花名册快照存在，但不是今天取的。
SKIP_STALE_SNAPSHOT = "stale_snapshot"
#: 没有当前有效的银河批次。
SKIP_NO_GALAXY_BATCH = "no_galaxy_batch"
#: 已开通用户的存档里没有人员 ID：匹配链的第一环就断了。
SKIP_MISSING_PERSONNEL_ID = "missing_personnel_id"
#: 已开通用户的存档缺邮箱或姓名：发布行的 ``record_key``/``name`` 两列没有来源。
SKIP_ARCHIVED_IDENTITY_INCOMPLETE = "archived_identity_incomplete"
#: 撤权用户在发布表里**没有我们发布过的行**：不为他新建空权限行（模块文档「撤权」一节）。
SKIP_NO_PUBLISHED_ROW = "no_published_row"
#: 「公司 + 职能 → 指标名」翻译层**整份映射一个条目都没有**（Issue #227）：产品负责人
#: 还没有开始填内容。当前部署下随包发布的配置文件正是这个状态，因此**每一个有效授权
#: 用户**都会落在这个原因码上——这是刻意的硬闸，见模块文档「翻译」一节。与
#: :data:`SKIP_METRIC_TRANSLATION_UNCOVERED` 分开登记，是为了让运维能从审计上分辨
#: 「整体还没开始填」与「已经在填、只是差几条」这两种截然不同的状态。
SKIP_METRIC_TRANSLATION_UNAVAILABLE = "metric_translation_unavailable"
#: 「公司 + 职能 → 指标名」翻译层**有内容，但这一次要用的组合没被覆盖**（Issue #227）：
#: 不发布、不撤权，只跳过。
SKIP_METRIC_TRANSLATION_UNCOVERED = "metric_translation_uncovered"

#: 逐用户结果的四个分类。``granted`` 之外的三类都**不产生任何发布意图**。
STAGE_MATCH = "match"
STAGE_AGGREGATE = "aggregate"
STAGE_IDENTITY = "identity"
STAGE_TRANSLATE = "translate"

#: 整轮跳过的原因码 → 审计动作名。外部独立审查 2026-08-18 坐实的 P1：翻译层整体
#: 不可用（映射为空）必须让**整轮**一条发布意图都不排，包括撤权——判据不是"这一行
#: 要不要翻译"，是"发布面这一轮开不开"。因此它和花名册/银河两组前置判据同属**整轮**
#: 跳过，不是逐用户判据，与 :data:`SKIP_METRIC_TRANSLATION_UNCOVERED`（映射非空但
#: 某个组合没覆盖到，仍是逐用户判据，见 :meth:`PermissionRefreshDuty._refresh_user`）
#: 是两回事，动作名也刻意不同，以便与"配了但未覆盖"在审计上区分开。
_ROUND_SKIP_ACTIONS: Mapping[str, str] = {
    SKIP_NO_GALAXY_BATCH: "permission_refresh.skipped_no_galaxy_batch",
    SKIP_MISSING_SNAPSHOT: "permission_refresh.skipped_roster_not_fresh",
    SKIP_STALE_SNAPSHOT: "permission_refresh.skipped_roster_not_fresh",
    SKIP_METRIC_TRANSLATION_UNAVAILABLE: "permission_refresh.skipped_metric_translation_unavailable",
}


def _utc_date(moment: datetime) -> date:
    """把一个时刻折成**它所在的 UTC 日期**。

    全模块只有这一处做「时刻 → 日期」的转换，理由是 ``date()`` 会直接取时钟自身时区的
    日期：一个 ``+08:00`` 的时钟在 ``00:30`` 给出的 ``date()`` 已经是新的一天，而按
    UTC 那还是前一天。日界不统一会让"今天已经跑过"与"快照是不是今天的"用两把不同的
    尺子，表现为某些时段整天不重算、或同一天重算两轮。日期一律 UTC（接口设计
    「二、通用约定」，与 :class:`~lingxi.apps.scheduler.RosterAuditDuty` 的日报日期同口径）。

    naive 时间**直接失败**而不是按本地时区解读：那种解读会在跨时区部署上静默算错，
    而算错的方向不可预测。
    """

    if not isinstance(moment, datetime):
        raise ValueError("权限重算的时间必须是时间戳")
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("权限重算的时间必须带时区：日界一律按 UTC 判定")
    return moment.astimezone(_UTC).date()


class _AuditSink(Protocol):
    """审计出口。

    与 :class:`lingxi.apps.scheduler.AuditSink` 是同一个结构化签名，在这里单独写一份
    只是为了避免与装配模块相互 import；两者互相满足，装配时传的就是同一个对象。
    """

    def record(self, action: str, /, **fields: object) -> None: ...


class _BaselineReader(Protocol):
    """已开通用户的读取口。实现是
    :class:`lingxi.adapters.postgres_roster_audit.PostgresRosterBaselineReader`——
    **复用**而不是照抄它的 SQL：``provisioning_state``/``account_state`` 这两条过滤
    是产品口径（`V-花名册-10`、`V-花名册-11`），第二份实现迟早会与它分叉，而分叉的
    方向可能是"给一个正在删除的用户重新发了权限"。
    """

    def load_active_baseline(self) -> Sequence[ArchivedIdentity]: ...


class _RosterSnapshotStore(Protocol):
    """花名册持久快照的读取口（:mod:`lingxi.adapters.postgres_roster_snapshot`）。

    先读 :meth:`load_facts` 再读 :meth:`load` 是刻意的：顺序判据在当前部署下每轮都
    不成立，而元信息只有一行——用整份快照（一千两百多行）去做一次每分钟都要做的
    新鲜度判断，代价与结论完全不成比例。
    """

    def load_facts(self) -> StoredSnapshotFacts | None: ...
    def load(self) -> Any: ...


class _GalaxySnapshotReader(Protocol):
    def load_current(self) -> Any: ...


class _TokenCipherReader(Protocol):
    """令牌**密文**的只读口。

    这里刻意只声明 :meth:`token_cipher` 一个方法：签发口
    （``issue_token``）不在这个协议里，因此"在每日刷新里顺手签一份令牌"这件事，
    在类型上就写不出来（模块文档「令牌：只读既有，绝不签发」）。
    """

    def token_cipher(self, user_id: str) -> str | None: ...


class _PublishHistory(Protocol):
    """「这个人在发布链上有没有留下过足迹」的只读口
    （:meth:`~lingxi.adapters.postgres_permission_publish.
    PostgresPermissionPublishStore.has_publish_footprint`）。

    单独声明成一个只有一个方法的协议，理由与 :class:`_TokenCipherReader` 相同：
    撤权那一路只需要回答"有没有"，把整个发布 outbox 的写侧摆在这里，等于让"顺手改一下
    那条意图"在类型上变得可写。装配时传进来的确实是同一个存储对象。
    """

    def has_publish_footprint(self, user_id: str) -> bool: ...


class _Decision(Protocol):
    enqueued: bool
    # 本次决定顺带清掉的已送达、随会话保留投递正文事件数（S-P-5，
    # Trace #328）。只有调用 ``record_decision(clear_delivered_content=True)`` 且
    # 真的走到 ``ENQUEUED`` 时才可能非零；本职责只如实把它写进审计计数，不自己
    # 判断"要不要清"——那条判定连同事务边界只有 ``record_decision`` 一处实现。
    cleared_events: int


class _DecisionStore(Protocol):
    """权限决定的落库口（:meth:`~lingxi.adapters.postgres_permission_publish.
    PostgresPermissionPublishStore.record_decision`）。

    **版本推进与幂等完全由它承担**：本职责不读、不写、不比较 ``permission_version``，
    也不自己判断"这次权限有没有变化"。那条判定连同它的锁与事务边界只有一处实现。
    """

    def record_decision(
        self,
        *,
        user_id: str,
        row: Any,
        reason: str,
        decided_at: datetime,
        clear_delivered_content: bool = False,
    ) -> _Decision: ...


class _LocalOverrideReader(Protocol):
    """本地权限覆盖的按用户读取口（S-P-3，Issue #319）。

    实现是 :meth:`~lingxi.adapters.postgres_local_permission.
    PostgresLocalPermissionOverrideStore.effective_entries` 经装配层适配（返回值从
    ``StoredLocalPermissionOverride`` 解出 ``.entry``——本协议只认纯类型
    :class:`~lingxi.core.permission.local_override.LocalPermissionOverrideEntry`，
    不认数据库分配的行标识，理由与 :class:`_PublishHistory` 只声明一个方法相同：
    本职责只需要"这个用户当前生效的覆盖条目有哪些"，不需要收回单条覆盖的能力）。

    ``None``（装配层未装配）与本方法读取失败在调用方眼里是**不同**的两件事：前者
    静默按"没有本地源"处理（部署事实，不告警）；后者响亮审计
    （:data:`~lingxi.core.permission.merge_sources.REASON_LOCAL_OVERRIDE_READ_FAILED`），
    但结果都是"这一轮/这个用户跳过本地源"——不整轮失败、不静默吞掉异常。
    """

    def effective_entries(self, *, user_id: str) -> Sequence[LocalPermissionOverrideEntry]: ...


@dataclass(frozen=True)
class PermissionRefreshReport:
    """一轮重算的结果。**只有计数与固定原因码，没有任何字段值。**

    :attr:`reasons` 的键全部来自 :mod:`lingxi.core.permission` 的固定原因码
    （``roster_not_found``、``no_supported_function`` 一类）与本模块顶部的常量，
    它们描述的是**为什么**，不是**是谁**——邮箱、姓名、工号、银河账号、公司编号与
    职能标签一个都不在这里（纪律同 `V-花名册-33`、`V-银河-13`）。
    """

    examined: int = 0
    enqueued: int = 0
    unchanged: int = 0
    # 本轮判定为"无可用权限"的人数（匹配失败也算在内：它是无权限的一种）。
    revoked: int = 0
    # 其中**真的排出了一条撤权更新意图**的人数（`V-权限-08` 的刷新侧）。它一定
    # ≤ :attr:`revoked`：匹配失败、从来没发布过、存档不全的那些人都只跳过。
    revoked_published: int = 0
    # 输入不完整而被跳过的人数：存档缺人员 ID / 缺邮箱或姓名。
    incomplete: int = 0
    # 处理过程中抛异常的人数。一个人的异常不影响其余人（模块文档）。
    failed: int = 0
    # 收到停止信号而中断：这一轮没走完，水位不置位。
    interrupted: bool = False
    reasons: Mapping[str, int] = field(default_factory=dict)

    @property
    def processed(self) -> int:
        """真正走完整条链并落了一次权限决定的人数。"""

        return self.enqueued + self.unchanged

    def audit_facts(self) -> dict[str, Any]:
        """可直接进审计与日志的事实。键与值都不含任何人员资料。"""

        facts: dict[str, Any] = {
            "examined": self.examined,
            "processed": self.processed,
            "enqueued": self.enqueued,
            "unchanged": self.unchanged,
            "revoked": self.revoked,
            "revoked_published": self.revoked_published,
            "incomplete": self.incomplete,
            "failed": self.failed,
        }
        if self.interrupted:
            facts["interrupted"] = True
        # 原因分类逐项展开成 `reason.<码>=<计数>`：审计行会被 grep 与比对，
        # 一个嵌套字典在结构化日志里只会变成一串引号。
        for reason, count in sorted(self.reasons.items()):
            facts[f"reason.{reason}"] = count
        return facts


@dataclass
class _Tally:
    """累加器。:class:`PermissionRefreshReport` 是冻结的（它会进审计），
    因此计数在这里累加，最后一次性结算成不可变的报告。"""

    examined: int = 0
    enqueued: int = 0
    unchanged: int = 0
    revoked: int = 0
    revoked_published: int = 0
    incomplete: int = 0
    failed: int = 0
    reasons: dict[str, int] = field(default_factory=dict)

    def count(self, reason: str) -> None:
        self.reasons[reason] = self.reasons.get(reason, 0) + 1

    def freeze(self, *, interrupted: bool = False) -> PermissionRefreshReport:
        return PermissionRefreshReport(
            examined=self.examined,
            enqueued=self.enqueued,
            unchanged=self.unchanged,
            revoked=self.revoked,
            revoked_published=self.revoked_published,
            incomplete=self.incomplete,
            failed=self.failed,
            interrupted=interrupted,
            reasons=dict(self.reasons),
        )


class PermissionRefreshDuty:
    """每日权限重算：花名册新鲜 → 银河当前批次 → 逐个已开通用户重算并排发布意图。

    语义与边界见模块文档。本类**只编排**：匹配、聚合、翻译、发布行结算四条规则分别在
    :mod:`lingxi.core.permission.account_match`、
    :mod:`lingxi.core.permission.metric_translation`、
    :mod:`lingxi.core.permission.publish_row` 里，版本推进与幂等在
    :mod:`lingxi.adapters.postgres_permission_publish`，这里一条都不复制。
    """

    name = "每日权限重算"

    def __init__(
        self,
        *,
        baseline_reader: _BaselineReader,
        roster_snapshot: _RosterSnapshotStore,
        galaxy: _GalaxySnapshotReader,
        decisions: _DecisionStore,
        publish_history: _PublishHistory,
        token_ciphers: _TokenCipherReader,
        role_function_map: Mapping[str, str],
        metric_translation_map: Mapping[str, Mapping[str, Sequence[str]]],
        audit: _AuditSink,
        clock: Callable[[], datetime] | None = None,
        stop: threading.Event | None = None,
        local_overrides: _LocalOverrideReader | None = None,
        legacy_source: LegacyPermissionTable | None = None,
    ) -> None:
        self._baseline_reader = baseline_reader
        self._roster_snapshot = roster_snapshot
        self._galaxy = galaxy
        self._decisions = decisions
        self._publish_history = publish_history
        self._token_ciphers = token_ciphers
        self._role_function_map = role_function_map
        self._metric_translation_map = metric_translation_map
        self._audit = audit
        # 本地权限覆盖读取口（S-P-3）：``None`` 表示装配层还没接这个 store——本轮/
        # 本用户的合并按"没有本地源"处理，产出与今天逐字节一致（模块文档「翻译」
        # 一节旁的「本地覆盖」小节 :func:`merge_permission_sources` 对 ``local=None``
        # 恒等的性质）。装配层的真实实现见 ``apps/scheduler/assembly.py``。
        self._local_overrides = local_overrides
        # 存量权限只读源（S-P-2，Trace #328）：``None`` 表示装配层还没接这个 store——
        # 本轮/本用户的合并按"没有存量源"处理，产出与今天逐字节一致（同
        # ``local_overrides=None`` 的既有姿态）。真实实现见 ``apps/scheduler/assembly.py``。
        self._legacy_source = legacy_source
        # 时钟注入：跨轮判重与"今天"的用例要能自己决定日期，不能靠等到明天。
        self._clock = clock or (lambda: datetime.now(_UTC))
        # 与同一进程内的其他职责共享停止标志：SIGTERM 一次让所有职责停止领取新工作。
        self._stop = threading.Event() if stop is None else stop
        self._completed_on: date | None = None
        # 跳过类审计的**当日去重水位**：当天已经记过哪些原因。顺序判据在当前部署下每轮
        # 都不成立，而调度周期是一分钟——不去重的话，一天会刷出一千四百多条内容完全相同
        # 的审计，真正的信号会被埋掉。
        #
        # 存的是**原因集合**而不是"最后一个原因"：同一天里原因会来回变（花名册快照到了
        # 又被换成旧的、银河批次过期又重导），只记最后一个的话 A→B→A 会把 A 记两次，
        # 去重就在最需要它的那条路径上失效了。
        self._skip_audited: tuple[date, set[str]] | None = None

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    @property
    def completed_on(self) -> date | None:
        """已完成重算的那一天。``None`` 表示本进程实例今天还没跑完过。"""

        return self._completed_on

    def request_stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------
    # 一轮
    # ------------------------------------------------------------------

    def run_once(self) -> PermissionRefreshReport | None:
        """跑一轮。返回 ``None`` 表示本轮没有重算（停止中、今天已跑完，或前置不成立）。"""

        if self._stop.is_set():
            # 已经在停止中：一轮都不开，一条发布意图都不排。
            return None
        now = self._clock()
        today = _utc_date(now)
        if self._completed_on == today:
            return None

        facts = self._roster_snapshot.load_facts()
        if facts is None:
            self._audit_skip(today, SKIP_MISSING_SNAPSHOT)
            return None
        if _utc_date(facts.captured_at) != today:
            self._audit_skip(
                today, SKIP_STALE_SNAPSHOT, snapshot_date=_utc_date(facts.captured_at).isoformat()
            )
            return None

        snapshot = self._roster_snapshot.load()
        if snapshot is None:
            # 元信息与整份快照分两条语句读，中间可能有一次并发替换（花名册审计职责
            # 就在同一进程里）。这里不是防御式编程：读到"元信息说有、整份却没有"时，
            # 唯一安全的动作是本轮不跑，下一轮那份新快照会自己把日期判据带过来。
            self._audit_skip(today, SKIP_MISSING_SNAPSHOT)
            return None
        if _utc_date(snapshot.facts.captured_at) != today:
            self._audit_skip(
                today, SKIP_STALE_SNAPSHOT, snapshot_date=_utc_date(snapshot.facts.captured_at).isoformat()
            )
            return None

        # 顺序判据成立之后才碰银河：花名册不新鲜的那一轮**一次银河读取都不发起**。
        galaxy = self._galaxy.load_current()
        if galaxy is None:
            self._audit_skip(today, SKIP_NO_GALAXY_BATCH)
            return None

        if not metric_translation_available(self._metric_translation_map):
            # 外部独立审查 2026-08-18 坐实的 P1：翻译层映射整体为空时，**整轮**
            # 一条发布意图都不排——撤权也不例外。``_revoke`` 从不调用翻译（它写的
            # 是不含指标名的 ``{}``），因此把这条判据放在逐用户层面挡不住撤权；
            # 唯一挡得住的位置是**遍历开始之前**：判据是"翻译层这一轮可不可用"，
            # 不是"这一行要不要翻译"。模块文档「翻译」一节有完整理由——映射为空时
            # 若只挡授权、放行撤权，权限在内容到位之前只能单向减少、不能恢复，
            # 这是最危险的那种不对称。
            #
            # ``metric_translation_available`` 是唯一允许存在的判据实现（见其
            # docstring）：首次开通编排（``apps.scheduler.assembly`` 的
            # ``publish_allowed``，Issue #227 开通侧整合）对同一个已加载对象调用
            # 同一个函数，两个独立写入点因此不会漂移出两套看起来等价的检查。
            self._audit_skip(today, SKIP_METRIC_TRANSLATION_UNAVAILABLE)
            return None

        baseline = self._baseline_reader.load_active_baseline()
        tally = _Tally()
        interrupted = False
        for identity in baseline:
            if self._stop.is_set():
                # 停止信号落在遍历中间：不再为后面的人排新的发布意图。已经落库的那些
                # 决定各自是一个完整事务，不存在半态；水位不置位，因此下一次启动会把
                # 这一轮重跑一遍——重跑对已经处理过的人是 ``UNCHANGED``，不产生第二条意图。
                interrupted = True
                break
            # 计数在**领取时**递增，不在遍历前按基线行数一次性写死：被停止信号挡在外面
            # 的那些人从来没有被看过一眼，把他们算进"已检查"会让中断轮的报告读起来像是
            # "全都查过了、只是什么都没做"。
            tally.examined += 1
            try:
                self._refresh_user(identity, snapshot.rows, galaxy, now, tally)
            except Exception as error:  # noqa: BLE001 - 一个用户的失败不得带走整轮
                # 只记异常类型：异常正文可能带上被处理对象的内容（邮箱、姓名）。
                tally.failed += 1
                tally.count(f"failed_{type(error).__name__}")
                self._audit.record(
                    "permission_refresh.user_failed",
                    user=identity.app_user_id,
                    error=type(error).__name__,
                )
                logger.error(
                    "单个用户的权限重算失败，其余用户继续 user=%s error=%s",
                    identity.app_user_id,
                    type(error).__name__,
                )

        report = tally.freeze(interrupted=interrupted)
        if interrupted:
            self._audit.record(
                "permission_refresh.interrupted",
                report_date=today.isoformat(),
                **galaxy.audit_facts(),
                **report.audit_facts(),
            )
            logger.info("停止信号在权限重算期间到达，本轮未走完，水位不置位")
            return report

        self._audit.record(
            "permission_refresh.completed",
            report_date=today.isoformat(),
            **galaxy.audit_facts(),
            **report.audit_facts(),
        )
        self._completed_on = today
        # 摘要只有计数（`V-花名册-33` 的同一条纪律：日志流向排障、CI 输出与工单）。
        logger.info(
            "每日权限重算完成 已开通用户=%s 新发布意图=%s 无变化=%s 无可用权限=%s "
            "其中已排撤权=%s 输入不完整=%s 失败=%s",
            report.examined,
            report.enqueued,
            report.unchanged,
            report.revoked,
            report.revoked_published,
            report.incomplete,
            report.failed,
        )
        return report

    # ------------------------------------------------------------------
    # 单个用户
    # ------------------------------------------------------------------

    def _refresh_user(
        self,
        identity: ArchivedIdentity,
        roster_rows: Sequence[Any],
        galaxy: Any,
        now: datetime,
        tally: _Tally,
    ) -> None:
        """重算一个已开通用户。任何"不发布"的出口都在这里显式返回，不落到默认分支。"""

        if not identity.personnel_id:
            # 建档合同要求人员 ID 必填，但存档里真的没有时，匹配层会直接抛错。
            # 在这里归类成"输入不完整"，而不是让它冒充一次技术故障。
            self._skip(tally, identity, STAGE_IDENTITY, SKIP_MISSING_PERSONNEL_ID, revoked=False)
            return

        match = match_galaxy_account(identity.personnel_id, roster_rows, galaxy.user_rows)
        if match.state != MATCHED or not match.galaxy_user_id:
            # 匹配不上就是"没有可用的银河权限"（`V-开通-02/03/06/09` 的统一出口）。
            # 原因码由匹配层给出且可分辨（``roster_not_found``、``key_conflict`` …），
            # 但用户侧的产品语义是同一个，本职责在这里只跳过并计数。
            self._skip(tally, identity, STAGE_MATCH, match.reason, revoked=True)
            return

        aggregate = aggregate_permission(
            galaxy_user_id=match.galaxy_user_id,
            user_role_rows=galaxy.role_rows(match.galaxy_user_id),
            datacountry_rows=galaxy.datacountry_rows(match.galaxy_user_id),
            country_rows=galaxy.country_rows,
            role_function_map=self._role_function_map,
        )
        if not aggregate.granted:
            # 撤权侧：**保行清空**，且只对"我们发布过的人"发（模块文档「撤权」一节）。
            self._revoke(tally, identity, aggregate.reason, now)
            return

        if not identity.email or not identity.display_name:
            # 发布行的 ``record_key``/``email``/``name`` 三列都来自存档身份。缺了就
            # 没有"这一行是谁的"的答案——先归类，而不是让 ``build_translated_publish_row``
            # 抛错之后被当成一次技术故障。
            self._skip(tally, identity, STAGE_IDENTITY, SKIP_ARCHIVED_IDENTITY_INCOMPLETE, revoked=False)
            return

        # 翻译「公司 + 职能」→ 指标名（Issue #227）。未覆盖就跳过——不发布、不撤权，
        # 详见模块文档「翻译」一节。放在令牌读取之前：既然本轮不会发布，没有必要为
        # 一个注定要跳过的人去查令牌表。
        try:
            company_metrics = translate_company_functions(
                companies=aggregate.companies,
                functions=aggregate.functions,
                all_companies=aggregate.all_companies,
                mapping=self._metric_translation_map,
            )
        except UncoveredPermissionCombination as error:
            # `run_once` 的整轮判据已经确保走到这里时映射非空，因此
            # `error.mapping_is_empty` 实践中恒为 False；这个分支仍然按它的真实值
            # 分类而不是硬编码，是为了不让这条逐用户判据的正确性依赖"调用方一定会先
            # 做整轮判据"这条外部不变量——`translate_company_functions` 是纯函数，
            # 直接调用它时映射完全可能是空的（模块文档「翻译」一节两层判据分工）。
            reason = (
                SKIP_METRIC_TRANSLATION_UNAVAILABLE
                if error.mapping_is_empty
                else SKIP_METRIC_TRANSLATION_UNCOVERED
            )
            self._skip(tally, identity, STAGE_TRANSLATE, reason, revoked=False)
            return

        # 四源合并（S-P-3 本地覆盖 #319 + S-P-2 存量沿用 #328）：真实权限 =
        # (银河 ∪ 本地授权 ∪ 存量沿用) − 本地抑制。挂在「翻译完成之后、结算发布行
        # 之前」——`company_metrics` 就是银河那一侧已经翻译好的 `{公司: (指标名, …)}`。
        # 见 `core/permission/merge_sources.py` 模块文档；存量源的失败降级见
        # `core/permission/legacy_source.py` 模块文档。
        local = self._resolve_local_overrides(identity.app_user_id)
        legacy = self._resolve_legacy_source(identity.app_user_id, identity.email, company_metrics)
        merged = merge_permission_sources(galaxy=company_metrics, local=local, legacy=legacy)
        for reason in merged.skipped_reasons:
            # 通配角 v1：本地源在 `all_companies=True` 下整体不参与合并，见
            # `merge_permission_sources` 模块文档「通配角」一节。
            self._audit.record(
                "permission_refresh.local_override_skipped",
                user=identity.app_user_id,
                reason=reason,
            )

        if not merged.permissions:
            # 红线-2（Trace #328 opus 审查）：银河这一侧原本是有效授权
            # （company_metrics 非空，翻译已经成功），但本地抑制把合并结果压光到
            # 空字典——这个人此刻没有任何可发布内容，语义上等同于撤权。走
            # `_revoke` 同一套机制（保行清空、只对发布链上留过足迹的人发），但带一个
            # 可分辨的原因码，不落到 `build_translated_publish_row` 对空输入的
            # `ValueError` → 通用 `user_failed`（模块文档「全抑制」一节）。
            self._revoke(tally, identity, REASON_FULLY_SUPPRESSED, now)
            return

        # 只取**已有**密文，取不到就是 None（发布层随后以 ``missing_token_cipher``
        # 失败关闭）。这里没有、也不允许有任何签发路径。
        token_cipher = self._token_ciphers.token_cipher(identity.app_user_id)
        row = build_translated_publish_row(
            company_metrics=merged.permissions,
            email=identity.email,
            display_name=identity.display_name,
            decided_at=now,
            token_cipher=token_cipher,
        )
        decision = self._decisions.record_decision(
            user_id=identity.app_user_id,
            row=row,
            reason=PERMISSION_REFRESH_REASON,
            decided_at=now,
            # 权限确实变化时，在 record_decision 自己的同一个事务里顺带清空该用户
            # 已送达、随会话保留的投递正文（S-P-5，Trace #328）。
            clear_delivered_content=True,
        )
        if decision.enqueued:
            tally.enqueued += 1
            self._audit.record(
                "permission_refresh.delivered_content_cleared",
                user=identity.app_user_id,
                cleared=decision.cleared_events,
                trigger=TRIGGER_GRANT,
            )
        else:
            # ``UNCHANGED``：权限内容与上一条仍然有效的意图逐字段相同。不推进版本、
            # 不排新意图、不清理——判定在 ``record_decision`` 里，本职责只如实计数。
            tally.unchanged += 1

    def _resolve_local_overrides(self, user_id: str) -> ResolvedLocalOverrides | None:
        """读该用户当前生效的本地覆盖条目并解决成 ``ResolvedLocalOverrides``。

        两种情形都返回 ``None``（对 :func:`merge_permission_sources` 恒等），但审计
        姿态不同：

        - **装配层没有接这个 store**（``self._local_overrides is None``）：部署事实，
          不告警——与「store 缺席=行为一致」的既有装配纪律同一姿态。
        - **读取失败**（数据库异常）：该用户本轮跳过本地源，**响亮**记一条
          ``permission_refresh.local_override_skipped``（``reason=local_override_read_failed``），
          异常本身不冒泡——一个用户的本地覆盖读取失败不得带走这个人当轮的银河权限
          发布，更不能带走整轮（`_refresh_user` 外层的 ``run_once`` 也兜底捕获单用户
          异常，这里提前捕获是为了把"翻译失败"与"本地覆盖读取失败"两种原因分开
          审计，而不是让两者都落进同一个笼统的 ``permission_refresh.user_failed``）。
        """

        if self._local_overrides is None:
            return None
        try:
            entries = tuple(self._local_overrides.effective_entries(user_id=user_id))
        except Exception as error:  # noqa: BLE001 - 本地源读取失败只降级，不整轮/整人失败
            self._audit.record(
                "permission_refresh.local_override_skipped",
                user=user_id,
                reason=REASON_LOCAL_OVERRIDE_READ_FAILED,
            )
            logger.error(
                "本地权限覆盖读取失败，本轮该用户跳过本地源 user=%s error=%s",
                user_id,
                type(error).__name__,
            )
            return None
        return resolve_local_overrides(user_id=user_id, entries=entries)

    def _resolve_legacy_source(
        self, user_id: str, email: str, company_metrics: Mapping[str, Sequence[str]]
    ) -> dict[str, tuple[str, ...]] | None:
        """按红线-1 的有界条件（Trace #328 opus 审查）决定这个用户本轮要不要读存量源。

        两条判据都会让本方法直接返回 ``None``、**不发起任何 ``find_rows`` 调用**
        （省一次读放大）：

        1. **通配用户**（``company_metrics`` 出现 :data:`~lingxi.core.permission.
           merge_sources.ALL_COMPANIES_KEY` 键）：:func:`~lingxi.core.permission.
           merge_sources.merge_permission_sources` 的通配分支本就完全不读 ``legacy``
           参数（该模块文档「通配角」一节），读来的存量权限必然被丢弃——读了也是白读。
        2. **已经在发布链上留下过足迹**（:meth:`_PublishHistory.has_publish_footprint`
           为真：发布成功过，或当前还有 ``pending``/``publishing`` 的意图在途）：正式
           权限发布表此刻很可能已经是 Lingxi 自己写的内容，不再是"旧系统遗留、我们
           从未碰过"的存量——参与合并会把自己昨天的发布内容原样并回来，形成自反馈环
           （详见 ``core/permission/legacy_source.py`` 模块文档「有界条件」一节）。

        两条判据都不成立（未通配、且从未有过发布足迹）时才真正调用
        :func:`~lingxi.core.permission.legacy_source.resolve_legacy_source`，读取/解析
        失败的降级与审计姿态见该函数文档，本方法不重复。
        """

        if ALL_COMPANIES_KEY in company_metrics:
            return None
        if self._publish_history.has_publish_footprint(user_id):
            return None
        return resolve_legacy_source(
            email=email,
            table=self._legacy_source,
            audit=self._audit,
            action="permission_refresh.legacy_source_skipped",
            user=user_id,
        )

    def _revoke(
        self,
        tally: _Tally,
        identity: ArchivedIdentity,
        reason: str,
        now: datetime,
    ) -> None:
        """银河侧明确判定这个人现在没有可用权限：该清空的清空，该跳过的跳过。

        三条出口按次序判，**每一条都显式返回**，不落到默认分支：

        1. **存档缺邮箱或姓名** → 跳过。撤权行的 ``record_key``/``email``/``name``
           三列同样来自存档身份，缺了就没有"这一行是谁的"的答案。这一步在查库之前，
           因为它不需要查库。
        2. **在发布链上一点足迹都没有**（既没发布成功过、也没有在途意图）→ 跳过
           （:data:`SKIP_NO_PUBLISHED_ROW`）。理由见模块文档「撤权」一节：不为一个
           没有发布行的人新建一行空权限。
        3. 否则结算撤权行并落一次权限决定。是否真的排出新意图由
           ``record_decision`` 的内容比对决定——第二天仍然无权限时判 ``UNCHANGED``，
           因此撤权**不会每天重发一次**。
        """

        tally.revoked += 1
        tally.count(reason)
        if not identity.email or not identity.display_name:
            tally.count(SKIP_ARCHIVED_IDENTITY_INCOMPLETE)
            self._audit.record(
                "permission_refresh.user_skipped",
                user=identity.app_user_id,
                stage=STAGE_IDENTITY,
                reason=SKIP_ARCHIVED_IDENTITY_INCOMPLETE,
            )
            return
        if not self._publish_history.has_publish_footprint(identity.app_user_id):
            tally.count(SKIP_NO_PUBLISHED_ROW)
            self._audit.record(
                "permission_refresh.user_skipped",
                user=identity.app_user_id,
                stage=STAGE_AGGREGATE,
                reason=reason,
                revocation=SKIP_NO_PUBLISHED_ROW,
            )
            return

        row = build_revocation_row(
            email=identity.email,
            display_name=identity.display_name,
            decided_at=now,
        )
        decision = self._decisions.record_decision(
            user_id=identity.app_user_id,
            row=row,
            reason=PERMISSION_REVOKE_REASON,
            decided_at=now,
            # 权限确实变化（真的排出撤权意图）时，在 record_decision 自己的同一个
            # 事务里顺带清空该用户已送达、随会话保留的投递正文（S-P-5，Trace #328），
            # 与授权侧同一个开关、同一条理由。
            clear_delivered_content=True,
        )
        if decision.enqueued:
            tally.enqueued += 1
            tally.revoked_published += 1
            self._audit.record(
                "permission_refresh.delivered_content_cleared",
                user=identity.app_user_id,
                cleared=decision.cleared_events,
                trigger=TRIGGER_REVOKE,
            )
        else:
            # 上一条意图已经是同一份空权限：不推进版本、不排新意图、不清理。
            tally.unchanged += 1
        self._audit.record(
            "permission_refresh.user_revoked",
            user=identity.app_user_id,
            reason=reason,
            enqueued=decision.enqueued,
        )

    def _skip(
        self,
        tally: _Tally,
        identity: ArchivedIdentity,
        stage: str,
        reason: str,
        *,
        revoked: bool,
    ) -> None:
        """记一次"这个人本轮不发布"，并计数。

        审计字段只有**内部用户标识、阶段与原因码**：``app_user.id`` 是内部 ULID，
        离开数据库就映射不到人；邮箱、姓名、工号、银河账号、公司编号与职能标签
        一个都不写（`V-花名册-33` 的同一条纪律）。
        """

        if revoked:
            tally.revoked += 1
        else:
            tally.incomplete += 1
        tally.count(reason)
        self._audit.record(
            "permission_refresh.user_skipped",
            user=identity.app_user_id,
            stage=stage,
            reason=reason,
        )

    # ------------------------------------------------------------------
    # 跳过整轮
    # ------------------------------------------------------------------

    def _audit_skip(self, today: date, reason: str, **facts: object) -> None:
        """整轮跳过时留痕，**同一天同一原因只留一条**（构造函数里的水位注释）。

        去重只影响审计条数，不影响判据本身：下一轮照样重新判一次，前置一旦成立
        就立刻开跑。**同一天里出现过的每一种原因都会被记到**，包括来回切换后又回到
        先前那一种（A→B→A 只留 A、B 各一条，不会因为"最后一次记的不是 A"而把 A 记两次）。
        """

        day, reasons = (
            self._skip_audited if self._skip_audited is not None and self._skip_audited[0] == today
            else (today, set())
        )
        if reason in reasons:
            return
        reasons.add(reason)
        self._skip_audited = (day, reasons)
        action = _ROUND_SKIP_ACTIONS.get(reason, "permission_refresh.skipped_roster_not_fresh")
        self._audit.record(action, report_date=today.isoformat(), reason=reason, **facts)
        logger.warning("每日权限重算本轮不执行 reason=%s", reason)


__all__ = [
    "PERMISSION_REFRESH_REASON",
    "PERMISSION_REVOKE_REASON",
    "PermissionRefreshDuty",
    "PermissionRefreshReport",
    "REASON_FULLY_SUPPRESSED",
    "SKIP_METRIC_TRANSLATION_UNAVAILABLE",
    "SKIP_METRIC_TRANSLATION_UNCOVERED",
    "SKIP_NO_PUBLISHED_ROW",
    "TRIGGER_GRANT",
    "TRIGGER_REVOKE",
]
