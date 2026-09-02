"""两源合并的集中合并（纯函数）：银河 ∪ 本地授权 − 本地抑制。

[Issue #319](https://github.com/Moshuiwang/lingxi/issues/319) 的 S-P-3（Trace #328
E-P 批次）。上游已经分别把「银河这一侧解释出什么」（:mod:`lingxi.core.permission.
publish_row`、:mod:`lingxi.core.permission.metric_translation`）与「本地覆盖这一侧
解决出什么」（:mod:`lingxi.core.permission.local_override` 的
:func:`~lingxi.core.permission.local_override.resolve_local_overrides`，``suppress``
赢已经内化——``grants`` 字段本身就是本地净授权）分别答完；本模块只回答最后一步：
**这个用户最终真实拥有的权限，是把两个已经算好的来源按什么规则拼成一份**。

真实权限 ``= (银河 ∪ 本地授权) − 本地抑制``，在 ``{公司ID 或全公司通配
"*"：值字符串列表}`` 这个粒度上做集合运算——即 :func:`merge_permission_sources` 的
输出形状。

**存量沿用（legacy source）机制已退役**（PM 2026-08-30 裁定，Issue #441）：旧系统
权限多维表格的存量用户权限改走差集导入为管理员本地授权（`local_permission_
override`，方向 grant，原因「2.0 迁移导入」），不再有单独的「存量沿用」来源参与
合并；``core/permission/legacy_source.py`` 与本模块曾经的 ``legacy`` 参数已一并
删除，历史设计与「发布足迹有界化」取舍见 Git 历史（本模块 2026-08-30 之前的版本）
与 `Issue #328`/`#419`，不在当前文档复述。

## 挂点与输入类型：与设计初稿的差异（历史事实，如实保留；#346 已把两个调用点收敛
## 到同一种输入形状，见下方「2026-08-28 更正」）

设计初稿把 ``galaxy`` 参数的类型定为 :class:`~lingxi.core.permission.publish_row.
PermissionAggregate`，隐含假设是"两个调用点都消费翻译层产出的 ``{公司:[指标名]}``
映射"。核对 S-P-3 落地时（2026-08-27）的实现（读 ``core/identity/onboarding_
runner.py`` 与 ``apps/scheduler/permission_refresh.py`` 的当时代码）发现这个假设
只对**其中一个**调用点成立：

1. **只有 ``permission_refresh.py`` 的 ``_refresh_user`` 真正调用了翻译层**
   （:func:`~lingxi.core.permission.metric_translation.translate_company_functions`），
   产出 ``{公司: (指标名, …)}``。``core/identity/onboarding_runner.py`` 的
   ``AutoOnboardingRunner._publish`` 当时用的是
   :func:`~lingxi.core.permission.publish_row.build_publish_row`（**未翻译**，值列表
   是 :attr:`PermissionAggregate.functions` 的职能标签，从未调用
   ``translate_company_functions``）——这不是 S-P-3 引入的新事实，是当时生产代码
   已经如此（``V-权限-13`` 矩阵条目里"``OnboardingRunner`` 生产唯一实现是失败关闭桩"
   一句写于 2026-08-18，晚于它的是 2026-08-19 把真实 ``PostgresPermissionPublishStore``
   接上开通链的那次合并——矩阵那句注记因此已经过期，不能作为"开通侧也翻译过"的依据）。
   因此当时"银河翻译产出 ``{公司:[指标名]}``"并**不是**两个调用点共享的唯一位置。

   本模块当时的取舍：把 ``galaxy`` 参数的类型从 ``PermissionAggregate`` 放宽成通用的
   ``Mapping[str, Sequence[str]]``（公司ID 或通配键 ``"*"`` → 值字符串列表）——本函数
   对值字符串的**语义**完全不关心，只做集合代数，因此两种输入天然都能正确合并：
   ``permission_refresh`` 一侧传入翻译后的指标名映射，``onboarding`` 一侧当时传入从
   ``aggregate.companies``/``functions``/``all_companies`` 现算出的职能标签映射（在
   ``_publish`` 里现算，形状与 :func:`~lingxi.core.permission.publish_row.
   serialize_permissions` 内部构造 ``document`` 的逻辑一致，只是这里不经过
   ``json.dumps``）。**S-P-3 明确声明"修复开通侧'从未翻译'这件事本身不在本卡范围
   内"**——那是一次远超"四源聚合手术"的行为变更（会改变开通首次发布的实际内容），
   S-P-3 只负责让本地覆盖在开通侧"无论银河那一侧给的是职能标签还是指标名"都能正确
   生效：本地覆盖条目本身永远是具体指标名，把它们并入职能标签列表虽然让该列表出现
   "标签与指标名并存"的过渡态，但**local 那一份被并入/减去的每一个字符串仍然是精确
   的指标名**，问数 MCP 按公司键取值列表做逐字匹配，因此这一个用户在这一个公司下被
   本地覆盖显式点名的那个指标名，无论周围混着什么职能标签，都会被正确放行/拒绝——
   这正是本地覆盖机制当时要保证的最小承诺，不依赖开通侧翻译问题何时被修。

   **2026-08-28 更正（`Issue #346`，Trace #373 S-H1-5）**：开通侧"从未翻译"已被
   坐实为硬切（`#263`）前必修的缺陷——硬切后开通链是权威源，未翻译的职能标签消费方
   读不懂。``AutoOnboardingRunner._publish`` 现在在构造 ``galaxy`` 参数之前先调用
   与 ``permission_refresh._refresh_user`` 同一个 ``translate_company_functions``
   （同一条 fail-closed 语义：存在未覆盖的「公司+职能」组合时整条链拒绝发布、外部表
   零写入），因此**两个调用点现在传入的都是翻译后的指标名映射**，上面第 1 点描述的
   "职能标签 vs 指标名"差异是历史事实，不再是当前代码的实况。本模块的签名与实现
   不需要跟着改——它早已放宽成语义无关的 ``Mapping[str, Sequence[str]]``，本来就
   兼容这一种输入收敛；只有这段文档需要如实更正，避免继续把一个已经修复的缺陷描述
   成"设计取舍"。

2. **本地覆盖条目的 ``user_id`` 是内部 ``app_user.id``**（迁移 ``0072``，见
   :mod:`lingxi.adapters.postgres_local_permission` 的建表与
   ``tests/test_local_permission_postgres.py`` 的用例），不是银河账号标识。
   ``onboarding_runner.py`` 的聚合（``_match``，读银河快照）发生在建档
   （``_provision``）**之前**，这时候还没有 ``app_user.id``；真正拿到它、也真正要
   结算发布行的位置是 ``_publish``（建档、复核、令牌签发都已完成之后）。因此本卡
   在开通侧的实际接线点是 ``_publish``，不是设计初稿写的"聚合后"那一行——聚合发生
   的地方还没有本地覆盖查询所需要的键。

   **2026-08-29 更正（PM 裁定，Issue #419）**：上一段仍然成立——``_publish`` 依旧是
   "结算发布行、真正把合并结果写进外部表"的唯一位置——但开通侧现在多了**第二个**
   调用点：``_recheck_still_provisionable`` 之后、``_issue_token``/
   ``_create_environment`` 之前的 ``_reject_zero_galaxy_without_local_grant``。
   它在 ``aggregate.granted`` 为假时提前查一次**本地覆盖**（``galaxy={}``），只为
   回答"要不要继续往下走"，不结算发布行；`_publish` 随后仍然会用同一个函数再算
   一次并真正落决定。之所以多出这一次提前查询，是为了不为一个最终仍会被拒绝的
   零银河用户签发问数 MCP 令牌、创建带凭据的用户环境——理由与取舍见
   ``onboarding_runner.py`` 该方法自己的文档字符串，本模块不重复。

## 调用时机：``aggregate.granted`` 不再是本函数被调用的前提条件（PM 2026-08-29 裁定，Issue #419）

设计初稿（乃至 S-P-3 落地时）默认两个调用点只在"银河这一侧判定有效授权"时才会走到
本函数——``permission_refresh.py::_refresh_user`` 与 ``onboarding_runner.py::
AutoOnboardingRunner._publish`` 都曾经把 ``aggregate.granted`` 当作"要不要合并"的
前置闸门，零银河权限的用户结构上从不到达合并这一步，管理员对这类用户发起的本地
授权因此结构上不生效（`V-权限-15` 此前登记的已知限制）。产品负责人 2026-08-29
裁定「并集无条件成立，包括银河侧为零权限的用户」之后，两个调用点都新增了一条
"``aggregate.granted`` 为假时改传 ``galaxy={}`` 继续调用本函数"的分支——本函数
自身**一行代码都没有变**：它从 S-P-3 起就把 ``galaxy`` 参数放宽成语义无关的
``Mapping[str, Sequence[str]]``，``{}`` 只是这个类型的一个普通空值，走的仍然是
非通配分支的既有代数（`keys = set(galaxy_map) | set(local_grants)`
在 ``galaxy_map`` 为空时天然只剩本地授权的键）。这正是当初"签名放宽到
语义无关"这个取舍的红利：上游多出一种新的合法输入形状，下游纯函数不需要跟着改一
个字符。

## 通配角 v1 语义（编排者裁定，逐字执行；2026-08-30 起只覆盖「真全指标通配」一种
## 形态，见下「通配角 v2」——历史裁定原文如实保留，不删改，只在下一节更正范围）

目标用户聚合为 ``all_companies=True``（`V-权限-14` 银河「后台管理员」角色，见
``publish_row.ADMIN_FULL_ACCESS_FUNCTION`` ——2026-08-30 之前的文档误把这当成
``all_companies=True`` 的**唯一**成因，见下「通配角 v2」的更正）时，即 ``galaxy``
里出现 :data:`ALL_COMPANIES_KEY` 这个键：

- **本地 ``suppress`` 不生效**——通配已经覆盖全公司全指标，一条按具体公司登记的
  抑制既拦不住通配（抑制的是具体公司键，通配走的是 ``"*"`` 键，`lookup_metrics`
  按公司键精确匹配，一条 ``"1011"`` 的抑制条目对 ``"*"`` 键完全无感）。
- **本地 ``grant`` 同样跳过**——通配已经含全部指标，追加具体指标名是纯冗余。

两者都**跳过**而不是"尝试合并后发现没有变化"：如果把具体公司的授权/抑制条目并入
通配映射，会在结果里额外长出一个具体公司键，与原本只有 ``"*"`` 一个键的形状不同——
读侧 :func:`~lingxi.core.permission.publish_row.lookup_metrics` 对**存在的**具体公司
键不再回退通配，那个具体公司会失去通配本该覆盖的其余指标，方向是**少给权限**，且
只发生在"恰好也被本地覆盖点过名"的那个公司，是一个极难被发现的窄范围回归。因此
通配下本地源**整体不参与合并**，产出的 ``permissions`` 与银河原始映射逐字节相同，
只把"为什么被跳过"作为事实告诉调用方（:attr:`MergedPermissionSources.skipped_reasons`），
由调用方决定用什么审计事件名记下来（两个调用点各自的动作名不同，命名姿态相同，
见两处调用点模块文档）。

## 通配角 v2：区分「真全指标通配」与「有限指标通配」（Issue #440，2026-08-30 修复）

上一节「通配角 v1」的设计假设是"``all_companies=True`` 只有『银河后台管理员、
全公司 × 全指标』一种成因"——`V-权限-14` 的判据（``ADMIN_FULL_ACCESS_FUNCTION
in functions``）确实只覆盖这一种成因，但 :func:`~lingxi.core.permission.
publish_row.aggregate_permission` 让 ``all_companies`` 为真的条件是
``scope.all_countries or ADMIN_FULL_ACCESS_FUNCTION in functions`` ——**两个**
互相独立的成因都会让 ``galaxy`` 翻译出同一种形状（``{"*": (指标名, …)}``）。
第二个成因（银河角色本身是"全公司范围"，但持有的职能不是「后台管理员」）落地
后，``mapping["*"][职能]`` 翻译出的是**那个职能自己的、通常有限的**指标名清单
——`Issue #440` 2026-08-30 实测坐实：stage 4 名 ``all_companies=True`` 用户里
3 名是这种「有限指标 ``*``」形态，只有 1 名（用户 A）是真正的「全指标 ``*``」。

**这两种形态在 ``galaxy`` 这个纯字符串映射里逐字节不可分**：本函数一贯不关心
值字符串的语义（模块文档「挂点」一节），``{"*": ("日活","收入","成本")}`` 既可能
是後台管理员当前已知的全部指标，也可能是某个有限职能翻译出的三个指标——本函数
拿不到、也不该反过来去猜"这份清单是不是已经等于全部"（那需要一份指标全集才能
判断，本函数没有、也不该耦合这份全集）。因此**区分只能来自调用方**：调用方在
调 :func:`~lingxi.core.permission.publish_row.aggregate_permission` 时已经知道
``all_companies=True`` 究竟来自 ``scope.all_countries`` 还是
``ADMIN_FULL_ACCESS_FUNCTION in functions``（两者可能同时成立，此时按「真全指标
通配」处理更安全——持有后台管理员职能的人本来就该维持 v1 的整体跳过）——本函数
新增关键字参数 :paramref:`~merge_permission_sources.full_access_wildcard`，把
这个判断显式收进签名，**不猜测**。

**必填，无默认值（Trace #445 opus 审查坐实并修复，结构性防复发）**：本参数
落地之初带 ``True`` 默认值（保持「通配角 v1」原有行为逐字不变，供两个既有
调用点当时不必立即改动），这个默认值本身后来正是根因——`core/permission/
targeted_recompute.py`（Issue #438 载体，晚于本卡新增的第三个写发布行调用点）
接入本函数时因为默认值"看似安全"而没有显式判断该传哪个值，实际把它的通配
用户（无论真全指标还是有限指标）一律当成真全指标处理，静默复现了本卡本该
修复的同一个误判（`Issue #445` 坐实）。修复方式是把默认值整体删除、改成
必填关键字参数：调用方漏接不再表现为"行为退回 v1、悄悄吃掉一个已知缺陷"，
而是当场 ``TypeError`` 拒绝调用——把"忘记声明"从一个运行时才会被发现（甚至
不一定会被发现）的语义错误，收紧成开发期就会报错的结构错误。全仓库当前
三个生产调用点（`permission_refresh.py::_refresh_user`、`onboarding_runner.py::
AutoOnboardingRunner._publish`、`targeted_recompute.py::TargetedPermissionRecompute.
recompute_and_publish`）与两个零银河分支（`galaxy={}`，通配键结构上不存在，
参数取值不影响结果，但同样必须显式传参）均已同步改为显式传参，全部测试同步。

**``full_access_wildcard=False``（有限指标 ``*``）时的语义**——「在 ``"*"``
清单上并集/减集」，逐字执行：

- collect 全部本地 ``grant``/``suppress`` 条目的 **指标名**，**忽略**每条条目
  自己携带的 ``company_id``（`Issue #440` 验收断言"合并语义与行来源无关"—— 一条
  本地授权条目无论创建时记的是哪个具体公司、还是字面量 ``"*"`` 本身，对一个
  基线本来就横跨全公司的用户而言都是同一件事："这个用户应该额外看到/看不到这个
  指标"，不是"这个用户在这一个公司应该额外看到/看不到这个指标"——后者对一个
  ``all_companies=True`` 的用户没有意义，因为读侧从不会对他按具体公司区分）。
- ``merged = (galaxy["*"] ∪ 全部本地 grant 指标名) − 全部本地 suppress 指标名``，
  写回**同一个** ``"*"`` 键——**不产出任何具体公司键**，逐字保持"通配下结果只有
  一个 ``"*"`` 键"这个形状不变量，这正是 v1 一直在防的"窄化回归"继续被防住的
  原因：无论本地条目的 ``company_id`` 是什么，都不会有任何具体公司键出现在
  结果里，读侧 :func:`~lingxi.core.permission.publish_row.lookup_metrics` 对
  **任意**具体公司的查询依旧回退到这个（可能已经变化的）``"*"`` 值。
- ``merged`` 为空集合时**丢弃** ``"*"`` 键，不写空列表——与「空结果」一节对
  非通配分支的既有纪律同一姿态（该用户被本地抑制减到一个指标都不剩，与非通配
  分支某个公司被减到零指标，读侧语义相同：这一支查不到任何指标）。
- **``skipped_reasons`` 恒为空元组**——这一支从不"跳过"，本地源要么真的改变了
  结果、要么恰好没有贡献（后者与非通配分支"参与合并、恰好没有贡献"是同一件事，
  同样不登记跳过原因，见 :class:`NoLocalSourceIsIdentityTests` 一类既有先例）。
  **这是「理由码修正」的落点**：:data:`REASON_GRANT_REDUNDANT_WILDCARD` 与
  :data:`REASON_SUPPRESS_INAPPLICABLE_WILDCARD` 两个字面量在代码里**只**出现在
  ``full_access_wildcard=True`` 这一支——`Issue #440` 报告的缺陷之一正是"有限
  指标 ``*`` 用户的补授被打上 ``grant_redundant_wildcard``"，这个理由码现在
  结构上不可能再出现在有限指标这一支，不需要额外的"这次到底是不是真冗余"的
  运行时判断去防止误标（哪怕这次补授的指标碰巧已经在清单里、合并后确实没有
  变化，也不登记这个理由码——因为在有限指标形态下，"清单外的指标不能被补授"
  这个假设本身就是错的，登记"冗余"会重新暗示这个假设成立）。

## 本地 ``"*"`` 组：本地授权带 ``"*"`` 公司键（rc25 S-1，Issue #540）

存量用户首聊差集导入（``core/permission/legacy_diff.py``）会为旧行 ``{"*": …}`` 的用户
落一组 ``company_id="*"`` 的本地授权（公司维度保留 ``*``、指标维度显式）。银河侧**没有**
``"*"`` 键时（普通具体公司用户，或零银河），非通配分支的既有代数会把 ``"*"`` 当成一个
普通键与各具体公司键并列产出——读侧 :func:`~lingxi.core.permission.publish_row.
lookup_metrics` 对**存在的**具体公司键不再回退 ``"*"``，那个公司会失去 ``"*"`` 覆盖的
其余指标，方向是**少给**（与通配角 v1/v2 防住的是同一种窄化）。因此新增一支，
**精确形状（独立审核 P2-3 修正）**：``"*"`` 键＝本地 ``"*"`` 指标（减去 ``"*"`` 上的
抑制）；对银河有值、或本地有具体授权/抑制的每家公司，产出具体键＝``"*"`` ∪ 银河该公司值
∪ 本地该公司授权 − 该公司抑制，与 ``"*"`` 相同则省略。除该公司自己的抑制外，每个具体键
都 ⊇ ``"*"``（读侧回退制下不会无故出现比 ``"*"`` 更窄的键；抑制咬住时更窄是抑制的正确语义）；公司专有的指标只留在该公司键下——不像设计初稿那样把「其它
本地具体键值 ∪ 银河各公司值」并进 ``"*"``（那会把 40 号公司专有的指标发给全部公司，与合同
「自动处理不扩大权限」冲突）。某公司减到空不可表示（写侧不产出空列表、读侧缺键会回退
``"*"``），登记进 :attr:`MergedPermissionSources.unrepresentable_companies`，全部调用点一律
fail-closed（不发布、不撤权、审计理由码 ``suppression_on_all_scope_unrepresentable``）——已知
边界：要完全屏蔽「全部」组用户的某一家公司，先撤销该组。银河侧为有限 ``"*"``（v2）时本地
``"*"`` 指标本来就会并入清单，行为不变；真全指标通配（v1）整体跳过不变（已 ⊇）。

## 空结果：合并后某个公司的值集合被抑制到空时**丢弃这个键**，不写空列表

与 :func:`~lingxi.core.permission.publish_row.serialize_translated_permissions` 现有
纪律的边界配合：那个函数的既有测试
（``TranslatedSerializationTest.test_an_empty_metric_list_for_any_company_is_rejected``）
钉着"写侧不产出空列表"，本卡刻意不去碰这条纪律、不去改它的测试——因为**不需要**。
非通配分支里，``galaxy``（`#346` 之后两个调用点都传入
:func:`~lingxi.core.permission.metric_translation.translate_company_functions` 的
产出——翻译后的指标名映射）从不含 ``"*"`` 键（该函数的产出恒为"要么整份是
``{"*": …}``、要么整份是具体公司键"，两者不会在同一份里混），因此在非通配分支里，丢弃一个被抑制到空的公司键与保留它写成空列表，对读侧
:func:`~lingxi.core.permission.publish_row.lookup_metrics` 是**完全等价**的结果
（该公司键缺失时回退查 ``"*"``，而这一支里 ``"*"`` 根本不存在，回退查不到，两条路径
都收敛到空元组）。选择丢弃键而不是保留空列表，换来的是不用去放宽
``serialize_translated_permissions`` 已经写了详尽理由、且被测试钉住的既有约束——
一处新增行为不需要的改动越少，越不容易在无关路径上引入回归。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from lingxi.core.permission.local_override import ResolvedLocalOverrides, to_company_metric_map

#: 「全非」通配在合并输入/输出里的公司键。与 ``publish_row.ALL_COMPANIES_KEY``、
#: ``metric_translation.ALL_COMPANIES_KEY`` 是同一个字面量的第三份独立拷贝——两处
#: 既有先例（见 ``metric_translation.py`` 模块文档「为什么不从 publish_row import」）
#: 已经把"每个模块各留一份，不建立反向依赖"定为本仓库对这个字面量的既定姿态，本模块
#: 处在翻译层与序列化层之间，同一条理由继续成立：不让本模块反过来被上下游任何一层
#: 的常量位置约束住。一致性由 ``tests/test_permission_merge_sources.py`` 与另外两个
#: 模块各自的测试分别钉住同一个值。
ALL_COMPANIES_KEY = "*"

#: 通配角 v1：本地授权在通配下被跳过（冗余，通配已经覆盖全部指标）。
REASON_GRANT_REDUNDANT_WILDCARD = "grant_redundant_wildcard"

#: 通配角 v1：本地抑制在通配下被跳过（拦不住通配，抑制的是具体公司键）。
REASON_SUPPRESS_INAPPLICABLE_WILDCARD = "suppress_inapplicable_wildcard"

#: 读本地覆盖条目时数据库异常：调用方据此让该用户本轮跳过本地源，而不是让异常
#: 冒泡带走整轮/整次开通。两个调用点共用同一个原因码字面量，审计动作名各自不同
#: （``permission_refresh.local_override_skipped`` / ``onboarding.local_override_skipped``），
#: 见各自模块文档。
REASON_LOCAL_OVERRIDE_READ_FAILED = "local_override_read_failed"


@dataclass(frozen=True)
class MergedPermissionSources:
    """一次两源合并的结果：最终权限映射 + 为什么某些输入被跳过。

    :attr:`skipped_reasons` 只在**真全指标通配**（``full_access_wildcard=True``，
    默认值）生效时非空（本地授权/抑制因为通配而被整体跳过，见模块文档「通配角
    v1」）。**有限指标通配**（``full_access_wildcard=False``，模块文档「通配角
    v2」，`Issue #440`）下本地授权/抑制改为参与合并，恒不登记跳过原因——它是
    **事实**，不是审计动作——本模块是纯函数、不做任何 I/O，真正的
    ``audit.record(...)`` 调用留给两个调用点各自完成（与
    :meth:`~lingxi.core.permission.publish_row.PermissionAggregate.audit_facts` 同一
    姿态：纯函数只回答"发生了什么"，调用方决定"记成哪一条审计"）。
    """

    permissions: Mapping[str, tuple[str, ...]]
    skipped_reasons: tuple[str, ...]
    #: 本地「全部」组（rc25 S-1，模块文档「本地 ``"*"`` 组」一节）下，被本地抑制
    #: 减到**空**的具体公司键：读侧回退制没有"某公司零指标、其余公司按 ``"*"``"的
    #: 可表示形状（写侧不产出空列表），调用方必须 fail-closed（不发布、不撤权），
    #: 理由码 ``suppression_on_all_scope_unrepresentable``。非空时 ``permissions``
    #: 仍是"假如没有这些公司"的合成结果，调用方不得拿去发布。
    unrepresentable_companies: tuple[str, ...] = ()


def merge_permission_sources(
    *,
    galaxy: Mapping[str, Sequence[str]],
    local: ResolvedLocalOverrides | None,
    full_access_wildcard: bool,
) -> MergedPermissionSources:
    """真实权限 ``= (银河 ∪ 本地授权) − 本地抑制``。

    ``galaxy``：银河这一侧已经算好的 ``{公司ID 或 "*"：值字符串列表}``——`#346` 之后
    两个调用点都传入 :func:`~lingxi.core.permission.metric_translation.
    translate_company_functions` 翻译后的指标名映射（历史上开通侧曾经传入未翻译的
    职能标签映射，见模块文档「挂点」一节「2026-08-28 更正」）。本函数不关心值字符串
    的语义，只做集合代数，因此两种输入都能正确合并——这也是签名仍然保留通用
    ``Mapping[str, Sequence[str]]``、不收紧回 ``PermissionAggregate`` 的原因。

    ``local``：:func:`~lingxi.core.permission.local_override.resolve_local_overrides`
    的结果，``None`` 表示"这一轮本地源不参与"（store 未装配、或读取失败后调用方
    降级传入）——对结果**恒等**：产出与 ``galaxy`` 逐字节相同（键集合与值集合都
    只由 ``galaxy`` 决定）。

    ``full_access_wildcard``：**必填，无默认值**（模块文档「通配角 v2」的结构性
    防复发一节——默认值本身曾是一次真实漏接的根因）。``galaxy`` 出现
    :data:`ALL_COMPANIES_KEY` 键时，这个通配到底是「真全指标通配」（``True``，
    `V-权限-14` 银河后台管理员）还是「有限指标通配」（``False``，同样
    ``all_companies=True`` 但成因是 ``scope.all_countries``、职能有限，
    `Issue #440`）——两者在 ``galaxy`` 这个纯字符串映射里逐字节不可分，调用方
    必须显式声明，本函数不猜测。非通配（``galaxy`` 没有 :data:`ALL_COMPANIES_KEY`
    键）时这个参数取值不影响结果，但仍必须显式传入。

    **通配角 v1**（``full_access_wildcard=True``）：``local``
    整体不参与合并，产出与 ``galaxy`` 逐字节相同；
    :attr:`MergedPermissionSources.skipped_reasons` 按"``local`` 是否带着非空
    授权/抑制"分别登记
    :data:`REASON_GRANT_REDUNDANT_WILDCARD`/:data:`REASON_SUPPRESS_INAPPLICABLE_WILDCARD`。

    **通配角 v2**（``full_access_wildcard=False``）：``local`` 的全部 ``grant``/
    ``suppress`` 指标名（忽略每条条目自己的 ``company_id``）直接在
    ``galaxy[ALL_COMPANIES_KEY]`` 这一份清单上做并集/减集，写回同一个 ``"*"``
    键，**不产出任何具体公司键**（防窄化回归，见模块文档）；``skipped_reasons``
    恒为空元组——两个理由码字面量在这一支代码里不出现，`Issue #440` 修正的正是
    "有限指标 ``*`` 用户的补授被打上 ``grant_redundant_wildcard``" 这个误判。

    **非通配**：结果键集合 = ``galaxy`` 键 ∪ 本地授权命中的公司键。
    每个键的值 = ``(galaxy[key] ∪ 本地授权[key]) − 本地抑制[key]``
    （缺席一律按空集合处理），按字符串排序去重成 ``tuple``。**减到空集合的键会被
    丢弃**，不产出空列表——理由与"为什么不产出空列表不违反读侧语义"见模块文档
    「空结果」一节。
    """

    galaxy_map: dict[str, tuple[str, ...]] = {
        key: tuple(values) for key, values in galaxy.items()
    }

    if ALL_COMPANIES_KEY in galaxy_map:
        if full_access_wildcard:
            skipped: list[str] = []
            if local is not None and local.grants:
                skipped.append(REASON_GRANT_REDUNDANT_WILDCARD)
            if local is not None and local.suppressions:
                skipped.append(REASON_SUPPRESS_INAPPLICABLE_WILDCARD)
            return MergedPermissionSources(
                permissions=galaxy_map, skipped_reasons=tuple(skipped)
            )

        # 有限指标通配（通配角 v2，Issue #440）：全部本地 grant/suppress 指标名
        # （忽略各自的 company_id——「行来源无关」）直接在 "*" 这一份清单上做
        # 并集/减集，只写回 "*" 键，绝不产出具体公司键（防窄化回归）。
        baseline = set(galaxy_map[ALL_COMPANIES_KEY])
        grant_metrics = {metric for _, metric in local.grants} if local is not None else set()
        suppress_metrics = (
            {metric for _, metric in local.suppressions} if local is not None else set()
        )
        wildcard_values = (baseline | grant_metrics) - suppress_metrics

        wildcard_permissions: dict[str, tuple[str, ...]] = {}
        if wildcard_values:
            wildcard_permissions[ALL_COMPANIES_KEY] = tuple(sorted(wildcard_values))
        # wildcard_values 为空：丢弃 "*" 键，不写空列表（模块文档「空结果」一节）。
        return MergedPermissionSources(permissions=wildcard_permissions, skipped_reasons=())

    local_grants = to_company_metric_map(local.grants) if local is not None else {}
    local_suppressions = to_company_metric_map(local.suppressions) if local is not None else {}

    if ALL_COMPANIES_KEY in local_grants:
        # 本地「全部」组（rc25 S-1，Issue #540；类比 #440 防窄化，独立审核 P2-3 后改为
        # **精确形状**）：银河侧没有 "*" 键而本地授权带 "*" 公司键时——
        #   "*"   ＝ 本地 "*" 指标 − "*" 抑制（组本身）；
        #   具体键 ＝ "*" ∪ 银河该公司值 ∪ 本地该公司授权 − 该公司抑制，
        #           与 "*" 相同则省略（读侧回退到 "*" 等价）。
        # 除该公司自己的抑制外，每个具体键都 ⊇ "*"（不会无故出现比 "*" 更窄的键）；同时公司专有
        # 的指标只留在该公司键下，不被抹平到全部公司（不扩权——旧写法把「其它本地
        # 具体键值 ∪ 银河各公司值」并进 "*"，等于把 40 号公司专有的指标发给全部公司）。
        # 某公司减到**空**不可表示（写侧不产出空列表、读侧缺键会回退 "*"），登记进
        # `unrepresentable_companies` 交调用方 fail-closed。
        star = set(local_grants[ALL_COMPANIES_KEY]) - set(local_suppressions.get(ALL_COMPANIES_KEY, ()))
        if star:
            collapsed: dict[str, tuple[str, ...]] = {ALL_COMPANIES_KEY: tuple(sorted(star))}
            unrepresentable: list[str] = []
            companies = (
                set(galaxy_map)
                | {company for company in local_grants if company != ALL_COMPANIES_KEY}
                | {company for company in local_suppressions if company != ALL_COMPANIES_KEY}
            )
            for company in sorted(companies):
                values = star | set(galaxy_map.get(company, ())) | set(local_grants.get(company, ()))
                values -= set(local_suppressions.get(company, ()))
                if not values:
                    unrepresentable.append(company)
                elif values != star:
                    collapsed[company] = tuple(sorted(values))
            return MergedPermissionSources(
                permissions=collapsed,
                skipped_reasons=(),
                unrepresentable_companies=tuple(unrepresentable),
            )
        # 组被 "*" 抑制减到空：本地没有「全部」授权了，回到下面的非通配代数
        # （与非通配分支的空结果同一语义：丢键、不写空列表）。
        local_grants = {company: values for company, values in local_grants.items() if company != ALL_COMPANIES_KEY}

    keys = set(galaxy_map) | set(local_grants)
    merged: dict[str, tuple[str, ...]] = {}
    for key in keys:
        values = set(galaxy_map.get(key, ())) | set(local_grants.get(key, ()))
        values -= set(local_suppressions.get(key, ()))
        if values:
            merged[key] = tuple(sorted(values))
        # values 为空：丢弃这个键，不写空列表（模块文档「空结果」一节）。

    return MergedPermissionSources(permissions=merged, skipped_reasons=())


__all__ = [
    "ALL_COMPANIES_KEY",
    "REASON_GRANT_REDUNDANT_WILDCARD",
    "REASON_LOCAL_OVERRIDE_READ_FAILED",
    "REASON_SUPPRESS_INAPPLICABLE_WILDCARD",
    "MergedPermissionSources",
    "merge_permission_sources",
]
