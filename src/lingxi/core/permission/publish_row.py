"""按用户聚合当前有效权限，并结算成**当前权限多维表格的目标行**（纯函数）。

[Issue #156](https://github.com/Moshuiwang/lingxi/issues/156) 的 S-C-01 前半段。上游
（[#105](https://github.com/Moshuiwang/lingxi/issues/105) 冻结的判定层）已经回答了
「这个人是谁、匹配到哪个银河账号」；本模块回答接下来的两件事：

1. **聚合**：把该银河账号的角色与国家授权解释成「公司范围 + Lingxi 支持职能」，
   空、不支持、解释不出范围一律 **fail-closed**（`V-权限-08` 的纯逻辑面）。
2. **目标行**：把聚合结果结算成发布表那一行的**全部字段文本**，序列化格式在这里定稿，
   全仓库只有这一份实现。

**判定语义不在这里，也不得在这里被改写**：角色映射在 :mod:`lingxi.core.permission.
role_function`，公司范围解释在 :mod:`lingxi.core.permission.galaxy_scope`，银河账号
匹配在 :mod:`lingxi.core.permission.account_match`。本模块只调用它们，不复制它们的规则。

## 发布表的通道事实（G-BIT 2026-08-17 回源实测，Trace #203）

正式表 ``user_company_permissions`` 的 **7 个字段全部是单行文本**：``record_key`` /
``email`` / ``name`` / ``token_cipher`` / ``updated_at`` / ``status`` / ``permissions``。
平台不提供任何结构化字段，因此「一行权限长什么样」完全由写入方约定——下面这套约定就是
本 Story 的定稿，改动它等于改变外部契约。

### ``record_key``：规范化邮箱

发布表的消费方（问数 MCP）要按人取权限，而表里唯一能同时被双方识别到人的列是
``email``。因此 ``record_key`` 取**规范化邮箱**（去首尾空白 + 转小写，与
:func:`lingxi.core.permission.account_match.normalize_email` 同一口径）。

**为什么不用 Lingxi 内部的 ``app_user.id``**：那个 ULID 从没离开过我们的数据库，而这张
表在我们第一次写入之前就已经有 26 行业务侧写入的记录。用内部 ULID 作键，等于第一次发布
就为这 26 个人各建出**第二行**权限——同一个人两行权限正是「不得重复扩大权限」要挡的形状。

**这条口径已经回源核对成立**：2026-08-17 编排者对正式表做过一次全表只读核对，26/26 行的
``record_key`` 都等于该行 ``email`` 的规范化值。因此 upsert 键与业务侧既有行同口径，
正常情况下命中即更新，不会为既有的人新建第二行。

**同一个人只允许一行**仍由发布执行器守（见 :mod:`lingxi.core.permission.publish` 的
``CONFLICT`` 分支）：写入前按 ``record_key`` **或** ``email`` 查找，命中一行且
``record_key`` 一致才更新；命中的行 ``record_key`` 口径不同、或命中多行，一律失败关闭，
既不更新也不新建。这道防线不因为口径已核对而放松——核对是某一时刻的观察，而这条守则
要挡的是**将来**业务侧换键口径、或同一个人被写出两行的情形。

### ``permissions``：``{公司ID: [指标名, …]}``

```text
{"1011":["日活","收入"],"1012":["日活","收入"]}     ← 显式公司范围
{"*":["日活","收入"]}                               ← 所有公司
{"1011":["*"]}                                      ← 该公司下所有指标（读侧形状，写侧不产出）
{"1011":[]}                                         ← 该公司下无任何指标
```

**形状依据**：2026-08-17 编排者对正式表的全表只读回源核对（26/26 行一致）。
**语义依据**：产品负责人 2026-08-17 对 [#155](https://github.com/Moshuiwang/lingxi/issues/155)
三问的答复（留痕见该 Issue 评论）。这张表是**现行问数 MCP 正在消费的权限源**，发布必须
写消费方看得懂的形状与取值，不能自定。

三条权威语义：

1. **值列表里的字符串是「指标名」**，不是职能标签、不是范围描述。
2. **``"*"`` 看位置**：出现在**键**上表示「所有公司」；出现在**值列表内**表示
   「该公司下所有指标」。空列表 ``[]`` 表示「该公司下无任何指标」，与缺键不同。
3. **大小写与全半角敏感**：MCP 逐字匹配。``"OTT"`` 与 ``"ott"``、半角 ``"OTT"`` 与
   全角 ``"ＯＴＴ"`` 都是**不同的指标**。

### 因此这条链路上的指标名字符串**零归一**

聚合层与序列化层对指标名**不做任何大小写折叠、全半角转换或裁剪**，原样透传，只在
:func:`serialize_permissions` 里禁掉 ``None`` 与非字符串（那是本侧缺陷，不是数据）。
排序与去重不算变形——它们不改变任何一个字符。守卫用例见
``tests/test_permission_publish_row.py`` 的 ``MetricNameSensitivityTest``：一旦有人在
这条链路上加一次"顺手归一"，那组用例立刻变红。

**这条纪律不适用于 ``record_key`` / ``email``**：那两列是**键口径**，规范化（去空白 +
转小写）由 26/26 行回源核对证实，保留不动。公司 ID 键同样只做去空白——它是编号，不是
逐字匹配的指标名。

### 写侧目前放进值列表的**还不是指标名**（真实发布的阻塞项，载体已交付、内容待填）

Lingxi 当前的权限模型产出的是**职能标签**（角色名映射配置
``lingxi/config/galaxy_role_function_map.toml`` 的值，见
:mod:`lingxi.core.permission.role_function`），而合同要求这里放**指标名**。中间缺的
**「公司 + 职能 → 指标名列表」翻译层**已由 [#227](https://github.com/Moshuiwang/lingxi/issues/227)
交付**载体**（存放形态 + 加载器 + 聚合层接线 + 校验，见
:mod:`lingxi.core.permission.metric_translation`、
:mod:`lingxi.adapters.company_function_metric_map_file`、随包发布的
``lingxi/config/company_function_metric_map.toml``，接线点在
:mod:`lingxi.apps.scheduler.permission_refresh`）；:func:`aggregate_permission` 的
接口本身保持现状不变——它产出的仍然是职能标签，翻译发生在它与序列化之间的下一层。

**映射内容**（哪个公司的哪个职能对应哪些指标名）仍只有产品负责人能给，#227 刻意不
实现它：随包发布的配置文件当前是空的，翻译函数
（:func:`~lingxi.core.permission.metric_translation.translate_company_functions`）
因此对任何一次调用都 fail-closed（既不猜测，也不回落成职能标签本身，也不静默丢弃
只翻译一部分）。**这只是授权路径的收窄，本身挡不住撤权**——撤权写的是不含指标名的
``{}``，从不调用这个函数。外部独立审查 2026-08-18 坐实过一次由此产生的漏洞（P1）：
映射为空时若只挡授权、放行撤权，会出现「内容到位之前权限只能被减、不能被恢复」的
单向不对称。真正挡住撤权的是调用方（:mod:`lingxi.apps.scheduler.permission_refresh`）
在 ``run_once`` 遍历任何用户**之前**加的整轮判据——映射为空时整轮（含撤权）一条
发布意图都不排，详见该模块文档「翻译」一节与 ``docs/技术设计/验收矩阵.md`` 的
``V-权限-13``。

后果必须说清楚：**内容到位之前，翻译层不会产出任何可发布内容，效果等同于旧的阻塞项
（值列表不是 MCP 能用的指标名）**，只是失败发生的位置从「隐式：没人接令牌」换成了
「显式：翻译层 fail-closed」。真实发布本就留 L4a 受控窗口，映射内容补齐并核对之前
不得真实写入——这条阻塞项不因为载体已交付而解除，仍然登记在此。

**本函数（``serialize_permissions``）与它的既有用例本身不因翻译层而改变**：它的
形状是"一份职能列表适用于用户持有的全部公司"，翻译之后是"每个公司一份可能不同的
指标名列表"，两者结构不同，因此翻译层走的是姊妹函数
:func:`serialize_translated_permissions`，不是把这个函数改成翻译层的入口——这一点
与本文档更早版本"翻译层补上时只改这一个函数"的预告不同，是 #227 落地时的实际选择，
理由见 :func:`serialize_translated_permissions` 与
:mod:`lingxi.core.permission.metric_translation` 的模块文档。

### 其余定稿

- **键**是 ``sys_country.boss_company_id``（产品负责人 2026-08-05 决策 3：向问数 MCP
  申请权限用它）；持有「全非」通配时只写一个 ``"*"`` 键，不展开成几十个重复条目——
  展开规则是我们这一侧的解释，消费方已经有它自己的通配约定。``name_cn`` 是**展示用**
  字段，不进发布行：发布表是给 MCP 读的机器契约，不是给人看的展示层。
- 银河的授权模型里职能是**用户级**的（角色不按国家区分），因此每个公司键下是同一份
  列表；按公司细分需要银河提供按国家的角色授权，而它没有。
- 没有版本字段。因此本实现**不写 ``v``**：往消费方的契约里塞一个它不认识的键，
  最好的结果是被忽略，最坏的结果是解析失败。
- ``json.dumps`` + ``sort_keys=True`` + 无空格分隔符 + 上游已排序的键与值：同一份权限
  **永远序列化成同一串字节**。发布是「写入后逐字段读回比对」，不确定的序列化会让一次
  没有任何变化的重发被判成不一致；``ensure_ascii=False`` 让中文指标名原样落表——这一条
  同时也是「全半角敏感」的前提，ASCII 转义会把原字符藏进 ``\\uXXXX`` 形式的六字符序列。

### 消费方节奏

MCP **每 15 分钟读一次**这张表（产品负责人答复）。因此「发布完成」到「MCP 看见」之间
天然有一个最长约十五分钟的窗口——这与 [#156](https://github.com/Moshuiwang/lingxi/issues/156)
就绪轮询的十五分钟上限是同一个数量级，属 S-C-02 的设计输入，不在本模块。

### ``token_cipher``：**新建行必须带，更新行一律不碰**

这条在 2026-08-17 由产品负责人裁定，取代了 S-C-01 的「永不写入」（依据：Trace #203 的
G-155 终判评论与同日的三项裁定；`V-权限-11` 的措辞已同步改写）。改变的原因是一条产品
事实：**没有 ``token_cipher`` 的行对问数 MCP 毫无意义**——MCP 逐行解密该列，与请求
``Bearer`` 明文等值匹配，解密失败即不放行。一行没有令牌的权限，写出去等于没写。

因此这一列有两套语义，**按动作分**，不能合并：

- **新建行必须携带 Lingxi 自己签发的 ``token_cipher``**。签发在
  :mod:`lingxi.adapters.postgres_mcp_token`（明文 ``secrets.token_urlsafe(32)``，
  只持久化 AES-256-CBC 密文），加密协议见 :mod:`lingxi.adapters.mcp_token_cipher`。
  缺它就**不新建**：发布执行器以 ``invalid`` + ``missing_token_cipher`` 失败关闭
  （见 :mod:`lingxi.core.permission.publish`）。
- **更新既有行时既有密文既不被清空也不被覆盖**。:data:`PUBLISHED_FIELD_NAMES`（=更新集）
  里刻意没有它，而飞书多维表格的记录更新是**部分更新**：没出现在字段集里的列保持原值。
  理由不是洁癖：正式表既有的 26 行是旧系统 biai-agent 签发的令牌，**Lingxi 不知道它们
  的明文**，用我们的值覆盖过去等于让那 26 个人在下一次刷新时静默失去问数能力。
- **但那一列为空时要补上**。「不覆盖」保护的是**既有令牌**，空值没有什么可保护的；而一行
  没有密文的权限对 MCP 毫无意义。这条是"新建成功但平台漏写了那一列"和"新建结果不明"
  重试时的收敛出口——少了它，重试会走六字段更新、读回一致、静默收敛成「发布完成」，
  而那一行对该用户永远无效（探针会烧满十五分钟再转运维，分类还指向"权限同步超时"）。
  判定与三条读回防线在 :func:`lingxi.core.permission.publish.publish_claim`。

**明文与主密钥仍然一步都不进 outbox、日志与告警。** 进 payload 的是密文，而
:func:`is_cipher_shaped` 让"把明文当密文写出去"在 ``core``（拿不到主密钥的地方）就被
拦住：``token_urlsafe`` 的明文是 43 个字符的 URL 安全 base64，过不了标准 base64 校验。

**已知后果，不掩盖**：既有 26 行的用户沿用旧系统的令牌，而 Lingxi 的就绪探针用的是
**我们**签发的那一份，因此对这些用户探针不会成功。发布执行器已经把动作记在
``PublishAttempt.action``（``create`` / ``update``），上游据此决定要不要对该用户发起
就绪确认——本 Story 不替上游做这个决定，也不为此新增第六种发布结果。

### "密文被改写不得判发布完成"这条保证的**边界**

实际保证的是：**同一条发布意图内**，我们建过的那一行如果密文变成了别的值，必被识别为
不一致（判据是 ``publish_outbox.created_record_id``，见 :func:`lingxi.core.permission.
publish.publish_claim`）。以及：**密文缺失（含纯空白）时任何路径都不判发布完成**。

**识别不到的两条路径，编排者 2026-08-17 裁定不修**：新权限版本产生的新意图，对"上一版
曾建过的行"出身为 ``None``；创建结果不明、没拿到记录标识时（行其实建成了）出身也为
``None``。理由是改写者与旧系统的合法密文在那两条路径上**不可区分**；猜错方向会把一行
合法的旧记录打成永久失败。这两条路径由**就绪探针**兜底——带着错误密文的行永远探不成功，
以十五分钟超时转运维暴露，不产生用户可见的假成功。

**存量用户令牌归属已由产品负责人 2026-08-18 裁定 6 定下方向**（留痕见
[#203](https://github.com/Moshuiwang/lingxi/issues/203) 的决策评论）：既有 26 行那些旧
系统 biai-agent 签发、我们不知其明文的令牌，将在**硬切窗口**由 lingxi 统一重签、覆写
发布行密文，并经 Epic D 的用户环境链重投。**这不改变上面那条取舍**：重签是一次性的迁移
动作，硬切之前那些旧密文仍然合法、仍然与"被改写"不可区分，因此本模块照旧不猜。硬切之后
"我们不知道明文的合法密文"这一类整体消失，那两条路径届时可以重新评估。

### ``status``：``approved``

既有 26 行的 ``status`` **全部是** ``approved``（同一次回源核对）。此前本模块自拟的
``active`` 会在同一列里造出第二种取值，而这一列的取值域由消费方定义、不由我们定义。

### 撤权行：**保行、清空 ``permissions``**（产品负责人 2026-08-18 裁定 3）

权限从有变无时，`V-权限-08` 的刷新侧语义是：**那一行留着，``permissions`` 写成空对象**
（:func:`serialize_revoked_permissions`，出口是 :func:`build_revocation_row`）。不删行、
不改 ``status``、不碰 ``token_cipher``、不重建行——权限恢复时是一次普通的六字段更新。

为什么空对象足以表达"没有权限"：消费方的权限判定是**回退制**（:func:`lookup_metrics`）
——具体公司键查不到就回退 ``"*"``，两个都没有就是空元组，任何指标都不放行。因此
「查无任何键」在 MCP 那一侧就是拒绝，不需要我们再造一个 ``status`` 取值去说同一件事。

三条不做的事，各有理由：

- **不删行**：删除是不可逆的外部副作用，而那一行还带着一份我们（对既有 26 行而言）
  不知道明文的 ``token_cipher``；删掉之后权限恢复就必须重建行、重新给令牌。
- **不改 ``status``**：取值域由消费方定义，自拟取值等于赌它对未知值的处理方式。
- **不碰 ``token_cipher``**：撤权行只有六个字段，走的必然是更新路径；那一列因此保持原值
  （:data:`PUBLISHED_FIELD_NAMES` 里本来就没有它）。

**时效边界**：这条语义在**硬切之后**才由 Lingxi 负责；硬切之前正式表仍有旧系统在写，
撤权由旧系统承担（同一条裁定）。

### ``updated_at``：**权限决定的时刻**，不是发布尝试的时刻

既有行是 ISO 风格时间串（含 ``-`` 与 ``:``），与 :func:`format_updated_at` 产出的
``2026-08-17T03:00:00Z`` 同族。

取值取的是**权限决定**的时刻：它随发布意图一起冻结在 outbox 的 payload 里，重试写入的
永远是同一串文本。取发布时刻会让每次重试都改变待写内容，于是「读回与预期一致」永远只能
证明最后一次写入自洽，证明不了写进去的是当初决定要发布的那一版。
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from lingxi.core.permission.account_match import normalize_email
from lingxi.core.permission.galaxy_scope import (
    country_keys_for_user,
    resolve_company_scope,
    role_names_for_user,
)
from lingxi.core.permission.role_function import resolve_role_functions

_UTC = timezone.utc

#: 「全非」通配在 ``permissions`` 里的键。既有行用它表示「所有公司」，因此我们不把
#: 通配展开成几十个重复条目——展开规则是我们这一侧的解释，消费方已有自己的约定。
ALL_COMPANIES_KEY = "*"

#: 触发「角色即全公司」特例的 Lingxi 职能标签（银河「后台管理员」角色，role_id 513，
#: 经 ``galaxy_role_function_map.toml`` 的「"后台管理员" = "后台管理员"」映射得到）。
#: 产品负责人 2026-08-27 裁定（Trace #328 裁定 #1，取代 [#320](https://github.com/
#: Moshuiwang/lingxi/issues/320) 落地的 A 口径）：持有这个职能就是「全公司 × 全指标」，
#: **含未来新增公司**——不依赖任何一次银河快照里枚举到了哪些公司。见
#: :func:`aggregate_permission` 里唯一引用它的那处特例分支；其余各处一律引用这个
#: 常量，不重复写字面量。
ADMIN_FULL_ACCESS_FUNCTION = "后台管理员"

#: 发布行的 ``status`` 值。取 ``approved`` 是因为既有 26 行全是它（2026-08-17 全表回源
#: 核对）；这一列的取值域由消费方定义，不由我们定义。
#:
#: **撤权行同样写 ``approved``**（产品负责人 2026-08-18 裁定 3，见模块文档「撤权行」）：
#: 这一列没有第二个被消费方认可的取值，自拟一个 ``revoked`` 只会让 MCP 读到一个它不认识
#: 的字符串，而它对这一列的处理方式未经验证。撤权由 ``permissions`` 为空表达，不由
#: ``status`` 表达。
STATUS_APPROVED = "approved"

#: **撤权行**的 ``permissions`` 文本：一个空对象。
#:
#: 它必须与 :func:`serialize_permissions` 出自同一套序列化约定（``json.dumps`` +
#: ``sort_keys`` + 无空格分隔符），否则同一份「没有任何权限」会有两种字节形态，而
#: 「写入后逐字段读回比对」与「权限有没有变化」两处判定都是按**字符串**做的。
#: 由 :func:`serialize_revoked_permissions` 产出，这里只留一份可被指着看的期望值。
REVOKED_PERMISSIONS_TEXT = "{}"

#: **更新既有行**时写入的字段，**顺序即比对与审计的输出顺序**。``token_cipher`` 不在
#: 其中：更新是部分更新，没列出的列保持原值，那 26 行旧系统签发的令牌因此既不被清空
#: 也不被覆盖（模块文档「``token_cipher``」一节）。它不是遗漏，改动它需要产品负责人裁定。
PUBLISHED_FIELD_NAMES: tuple[str, ...] = (
    "record_key",
    "email",
    "name",
    "permissions",
    "status",
    "updated_at",
)

#: 令牌密文所在的列名。只有这一处字面量，别处一律引用它。
TOKEN_CIPHER_FIELD = "token_cipher"

#: **新建行**时写入的字段：更新集 + 令牌密文。没有它的新行对问数 MCP 毫无意义
#: （MCP 逐行解密后与请求 Bearer 明文等值匹配），因此这里是必填而不是可选。
CREATED_FIELD_NAMES: tuple[str, ...] = PUBLISHED_FIELD_NAMES + (TOKEN_CIPHER_FIELD,)

#: 判断「权限有没有变化」时**不看**的字段。只有时间戳一个：把它算进去，每天一轮的
#: 权限刷新会天天判成「变了」，天天重发一次内容完全相同的权限。
_VOLATILE_FIELD_NAMES: frozenset[str] = frozenset({"updated_at"})

# fail-closed 的三个内部原因。用户侧一律是同一个「无可用银河权限」出口
# （Issue #17 已确认的产品规则），这里的区分只供审计与排障。
REASON_GRANTED = "granted"
REASON_NO_ROLES = "no_galaxy_roles"
REASON_NO_SUPPORTED_FUNCTION = "no_supported_function"
REASON_NO_COMPANY_SCOPE = "no_company_scope"


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


@dataclass(frozen=True)
class PermissionAggregate:
    """一个用户的当前有效权限，以及为什么是这样。

    ``granted`` 为假时 :attr:`companies` / :attr:`functions` **一定为空**（由
    ``__post_init__`` 强制）：一个「没有权限但带着范围」的对象只要被谁读一半，
    就会变成一次越权发布。
    """

    granted: bool
    reason: str
    companies: tuple[str, ...] = ()
    functions: tuple[str, ...] = ()
    all_companies: bool = False
    # 下面四个只是**计数与键**，用于审计和排障，不含任何人员资料值。
    role_count: int = 0
    unmapped_role_count: int = 0
    unresolved_country_keys: tuple[str, ...] = ()
    countries_without_company_id: int = 0

    def __post_init__(self) -> None:
        if not self.granted and (self.companies or self.functions):
            raise ValueError("fail-closed 的聚合结果不得携带任何公司或职能范围")
        if self.granted and (not self.companies or not self.functions):
            raise ValueError("有效权限必须同时有公司范围与受支持职能")

    def audit_facts(self) -> dict[str, Any]:
        """可直接进审计与日志的事实：只有计数、原因码与**机器编号**。

        公司编号（``boss_company_id``）与职能标签不是人员资料，可以留痕；邮箱、姓名、
        工号一个都不在这里（纪律同 ``adapters/feishu_roster_bitable.audit_facts``）。

        **``companies`` 在 ``all_companies=True`` 时输出 ``None``，不输出
        ``len(self.companies)``**（Trace #328 opus 审查 P2）：「角色即全公司」特例
        （本模块「角色即全公司」特例一节，产品负责人 2026-08-27 裁定，Trace #328
        裁定 #1）让 ``all_companies`` 可以在 ``companies`` 只有一两个具体公司时仍然
        为真——``companies`` 此刻只是"这次快照恰好解释出了哪些公司"，与实际覆盖
        范围（全公司，含未来新增公司）毫无关系。继续输出这个数字会在审计行里读出
        一句自相矛盾的话（"companies: 1, all_companies: true"，像是"覆盖 1 家公司
        却又说是全公司"），银河「全非」通配展开成的大列表同样如此——那也只是"这次
        快照当时能看到多少国家"，不是真正的覆盖范围上界。``all_companies`` 为真时
        这个数字对任何读者都没有意义，因此显式置空，不写一个会被误读成"范围只有
        这么大"的数字。
        """

        return {
            "granted": self.granted,
            "reason": self.reason,
            "companies": None if self.all_companies else len(self.companies),
            "functions": list(self.functions),
            "all_companies": self.all_companies,
            "roles": self.role_count,
            "unmapped_roles": self.unmapped_role_count,
            "unresolved_country_keys": len(self.unresolved_country_keys),
            "countries_without_company_id": self.countries_without_company_id,
        }


def _denied(reason: str, **facts: Any) -> PermissionAggregate:
    return PermissionAggregate(granted=False, reason=reason, **facts)


def aggregate_permission(
    *,
    galaxy_user_id: str,
    user_role_rows: Iterable[Mapping[str, Any]],
    datacountry_rows: Iterable[Mapping[str, Any]],
    country_rows: Sequence[Mapping[str, Any]],
    role_function_map: Mapping[str, str],
) -> PermissionAggregate:
    """把一个银河账号的授权聚合成当前有效权限。

    输入是**已经落库的银河快照行**（``galaxy_user_role`` / ``galaxy_user_datacountry``
    / ``galaxy_country``），调用方按当前有效批次取出后传进来；本模块不读库、不选批次
    ——「哪一批是当前有效的」是导入层的职责（`V-银河-06`）。

    ``role_menu`` 不参与：Lingxi 的职能标签只认角色名映射配置（Issue #17 已确认的产品
    规则），菜单授权项是银河自己的授权面，猜测它与 Lingxi 职能的对应关系正是配置化要
    避免的事。

    **本函数产出的 :attr:`PermissionAggregate.functions` 是职能标签，不是发布表要的
    指标名**。两者之间的「公司 + 职能 → 指标名列表」翻译层尚未实现（输入来源待探索），
    本 Story 刻意保持本接口现状；影响与阻塞范围见模块文档。**这里不做任何字符串归一**
    ——职能标签将来会经翻译层变成逐字匹配的指标名，任何提前的大小写折叠或全半角转换都会
    在那一步变成静默的错范围。

    三个 fail-closed 分支（用户侧统一「无可用银河权限」，内部原因各自可分辨）：

    - ``no_galaxy_roles``：该账号一个角色都没有；
    - ``no_supported_function``：有角色但没有一个被配置映射到 Lingxi 职能——
      「用户只有未映射角色时按未授权」（Issue #17）；
    - ``no_company_scope``：解释不出任何带 ``boss_company_id`` 的公司。

    **两处「静默收窄」是刻意的，方向都安全**（会少给权限，不会多给）：

    1. 授权表引用了快照里不存在的 ``country_key``（``unresolved_country_keys``）：
       那一个国家不进范围，但不否决整个用户。这类键是导出与解释之间的数据陈旧，
       为它整体拒绝会让一批在职员工因为一条过期外键失去服务；而它造成的偏差方向是
       **少一个国家**，不会多给。哨兵（``全非``）损坏那一种**不走这条路**——
       :func:`resolve_company_scope` 已经把它按失败关闭处理，通配不展开。
    2. 解释到了国家但该行没有 ``boss_company_id``（``countries_without_company_id``）：
       没有公司编号就没法向 MCP 申请，写进发布行只会得到一个消费方看不懂的空值。

    两处收窄都只产出**计数**留痕；收窄到空集时照常 fail-closed。

    **「角色即全公司」特例（产品负责人 2026-08-27 裁定，Trace #328 裁定 #1，B 口径，
    取代 [#320](https://github.com/Moshuiwang/lingxi/issues/320) 落地的 A 口径）**：
    持有 :data:`ADMIN_FULL_ACCESS_FUNCTION` 这个职能时，``all_companies`` 强制为真，
    **不看这个用户的 datacountry 行解析出的银河侧范围**——不论银河那一侧授予的是
    「全非」通配还是具体公司枚举。A 口径（给 44 个已知公司键各写一条「后台管理员」
    映射）已被证明不足：具体公司枚举的持有人，聚合结果只覆盖枚举到的那些公司，银河
    将来新增公司时还得回来手改 ``company_function_metric_map.toml``（见
    ``tests/test_admin_role_full_access.py`` 的
    ``AdminRoleWithoutWildcardCompanyScopeStillTranslatesTest``）。B 口径把判据从
    「这个人枚举到了哪些公司」搬到「这个人有没有这个职能」，因此**天然覆盖未来新增
    公司**——不需要新公司出现时再补一条配置，通配键 :data:`ALL_COMPANIES_KEY` 从一
    开始就不按公司数量枚举。特例只改 ``all_companies``，不改 :attr:`PermissionAggregate.
    companies`——后者仍然是这次快照解释出的具体公司列表，满足 ``__post_init__`` 「有效
    权限必须同时有公司范围」的不变量；序列化与翻译层（:func:`serialize_permissions`、
    :func:`~lingxi.core.permission.metric_translation.translate_company_functions`）
    在 ``all_companies=True`` 时本来就只查通配键、完全不看 ``companies`` 字段，因此这里
    不发明新形状，只是让已有的通配分支多一条触发路径。
    """

    account = _text(galaxy_user_id)
    if not account:
        raise ValueError("银河账号标识不能为空")

    role_names = role_names_for_user(account, user_role_rows)
    if not role_names:
        return _denied(REASON_NO_ROLES)

    resolved_roles = resolve_role_functions(role_names, role_function_map)
    # 去重用 `dict.fromkeys`（保留首次出现次序）再显式排序，而不是 `sorted(set(...))`：
    # 集合的迭代次序随 `PYTHONHASHSEED` 变化，一旦哪天有人把外层的 `sorted` 拿掉，
    # 输出会变成"每次进程重启都不一样"，而发布靠的正是**恒等序列化**。这样写让排序是
    # 一个能被单独看见、也能被单独改坏的动作。
    functions = tuple(
        sorted(dict.fromkeys(item.function for item in resolved_roles if item.function is not None))
    )
    unmapped = sum(1 for item in resolved_roles if not item.mapped)
    if not functions:
        return _denied(
            REASON_NO_SUPPORTED_FUNCTION,
            role_count=len(role_names),
            unmapped_role_count=unmapped,
        )

    country_keys = country_keys_for_user(account, datacountry_rows)
    scope = resolve_company_scope(country_keys, country_rows)
    company_ids = tuple(
        sorted(
            dict.fromkeys(
                _text(item.boss_company_id)
                for item in scope.countries
                if _text(item.boss_company_id)
            )
        )
    )
    missing_company_id = sum(1 for item in scope.countries if not _text(item.boss_company_id))
    if not company_ids:
        return _denied(
            REASON_NO_COMPANY_SCOPE,
            role_count=len(role_names),
            unmapped_role_count=unmapped,
            unresolved_country_keys=scope.unresolved_country_keys,
            countries_without_company_id=missing_company_id,
        )

    # 「角色即全公司」特例（产品负责人 2026-08-27 裁定，Trace #328 裁定 #1，B 口径）：
    # 持有 ADMIN_FULL_ACCESS_FUNCTION 时强制 all_companies=True，不依赖 scope 从
    # datacountry 行解析出的范围——银河那一侧给的可能是「全非」通配，也可能是具体
    # 公司枚举，两种都不改这条特例的结论。company_ids 仍然是这次快照解释出的具体
    # 公司列表（不清空、不改写），只是序列化与翻译层在 all_companies=True 时本来就
    # 只查 ALL_COMPANIES_KEY、不看 companies 字段——因此这里天然满足「含未来新增
    # 公司」：新公司出现时不需要再回来改任何配置或代码。完整推理见本函数上方文档
    # 「角色即全公司」特例一节。
    all_companies = scope.all_countries or ADMIN_FULL_ACCESS_FUNCTION in functions

    return PermissionAggregate(
        granted=True,
        reason=REASON_GRANTED,
        companies=company_ids,
        functions=functions,
        all_companies=all_companies,
        role_count=len(role_names),
        unmapped_role_count=unmapped,
        unresolved_country_keys=scope.unresolved_country_keys,
        countries_without_company_id=missing_company_id,
    )


def serialize_permissions(aggregate: PermissionAggregate) -> str:
    """把有效权限序列化成 ``permissions`` 单元格的**唯一**文本形态。

    形状是 ``{公司ID: [指标名, …]}``，持有「全非」通配时只写一个
    :data:`ALL_COMPANIES_KEY` 键。形状依据是 2026-08-17 编排者对正式表的全表回源核对
    （26/26 行），语义依据是产品负责人同日对 #155 的答复——都不是我们自拟的约定，这张表
    是现行问数 MCP 正在消费的权限源，详见模块文档。

    **当前放进值列表的还是职能标签，不是指标名**：本函数只认
    :attr:`PermissionAggregate.functions`，从不查翻译映射——「公司 + 职能 → 指标名
    列表」的翻译载体已由 [#227](https://github.com/Moshuiwang/lingxi/issues/227) 交付
    （:mod:`lingxi.core.permission.metric_translation`），但走的是姊妹函数
    :func:`serialize_translated_permissions`，**不经过本函数**：本函数的输出形状与
    既有用例因此**一个字节都没变**。调用方（每日权限重算职责）已经切换成翻译后的
    路径；本函数继续保留只是因为它是"聚合结果 → 发布行"这条链在职能标签层面唯一有
    完整测试覆盖的实现，删掉它对当前生产调用点没有任何好处。模块文档「写侧目前放进
    值列表的还不是指标名」一节是完整阻塞项的正文（内容——即真实映射——仍待产品负责人
    填入配置文件，载体本身不产出可用于真实发布的取值）。

    **值列表里的字符串原样透传，一个字符都不动**（产品负责人 2026-08-17 答复：MCP 逐字
    匹配，大小写与全半角敏感）。这里只挡 ``None`` 与非字符串——那是本侧缺陷，不是数据。
    去重与排序不算变形：它们不改变任何一个字符。

    恒等序列化（「写入后逐字段读回按字符串比对」的前提）靠两层，**都必须在**：

    - :func:`aggregate_permission` 已经把公司与值各自排过序。值是 JSON 的**列表元素**，
      ``json.dumps`` 不会替它排序，只有这一层能保证它恒定；
    - ``sort_keys=True`` + ``separators=(",", ":")``。它对公司键是第二道防线（上游已经
      排过），刻意保留：这份文档将来若改成从别处取键，不会因为少了一次排序就变成
      「每次进程重启序列化结果都不一样」。
    """

    if not aggregate.granted:
        # 没有权限就不该有发布行；调用方走 fail-closed 出口，不发布空权限。
        raise ValueError("无可用权限的聚合结果不得序列化成发布内容")
    values = [_verbatim(item) for item in aggregate.functions]
    if aggregate.all_companies:
        document: dict[str, list[str]] = {ALL_COMPANIES_KEY: values}
    else:
        # 银河的授权模型里职能是**用户级**的（角色不按国家区分），因此每个公司键下是
        # 同一份列表。这不是偷懒展开：按公司细分需要银河提供按国家的角色授权，
        # 而它没有——凭空细分等于伪造一份我们并不知道的范围。
        document = {company: list(values) for company in aggregate.companies}
    return json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def serialize_translated_permissions(company_metrics: Mapping[str, Sequence[str]]) -> str:
    """把翻译层（:mod:`lingxi.core.permission.metric_translation`）产出的
    「公司 → 指标名列表」序列化成 ``permissions`` 单元格文本。

    与 :func:`serialize_permissions` 出自**同一套序列化纪律**：值列表元素经
    :func:`_verbatim` 逐字符透传（不做任何大小写折叠、全半角转换或裁剪），
    ``sort_keys=True`` + ``separators=(",", ":")`` + ``ensure_ascii=False``。
    唯一的形状差异是输入——:func:`serialize_permissions` 假设**同一份职能列表
    适用于用户持有的全部公司**（银河的授权模型决定的），本函数接受**每个公司可能
    不同**的指标名列表（翻译层是「公司 + 职能」联合键，天然允许不同）。两个函数
    因此不能合并成一个：合并会让"这份 functions 对所有公司通用"这条假设在类型上
    变得可以被翻译层悄悄打破，而调用方（每日权限重算职责）不会注意到。

    调用方必须保证传入的每个公司的指标名列表**已经去重排序**
    （:func:`~lingxi.core.permission.metric_translation.translate_company_functions`
    的产出满足这一点）——本函数不重新排序值列表元素，理由与
    :func:`serialize_permissions` 相同：恒等序列化需要"值是 JSON 列表元素、
    ``json.dumps`` 不会替它排序"这层保证在**上游**成立，这里只是复用它，不是
    重新发明它。公司键本身仍由 ``sort_keys=True`` 兜底排序。

    **写侧不产出空列表**：与 :func:`serialize_permissions` 同一条纪律
    （``PermissionAggregate`` 的 ``__post_init__`` 在那一侧挡住），这里由本函数
    自己挡——空列表表示"该公司下无任何指标"，是读侧
    （:func:`lookup_metrics`）的合法形状，写侧翻译出一个空列表意味着某个职能在
    某公司下被翻译成了"没有任何指标"，那本身就该在翻译层被当作未覆盖处理
    （模块文档「未覆盖」一节），不该流到序列化层才发现。
    """

    if not company_metrics:
        raise ValueError("翻译后的权限内容不得为空：至少要有一个公司键")
    document: dict[str, list[str]] = {}
    for company, metrics in company_metrics.items():
        if not isinstance(company, str) or not company:
            raise ValueError("翻译后的公司键必须是非空字符串")
        values = [_verbatim(item) for item in metrics]
        if not values:
            raise ValueError(f"公司 {company} 的翻译结果不得是空列表：写侧不产出该形状")
        document[company] = values
    return json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def serialize_revoked_permissions() -> str:
    """撤权行的 ``permissions`` 单元格文本：**空对象**，恒为 :data:`REVOKED_PERMISSIONS_TEXT`。

    产品负责人 2026-08-18 裁定 3（留痕见 [#203](https://github.com/Moshuiwang/lingxi/issues/203)
    的决策评论）：权限从有变无时**保留那一行、把 ``permissions`` 清空**，不删行、不改
    ``status``、不碰 ``token_cipher``。消费方一侧这条语义是成立的——问数 MCP 的权限判定
    走**回退制**（:func:`lookup_metrics`）：具体公司键查不到就回退 ``"*"``，两个都没有就是
    空元组，于是任何指标都不放行。空对象因此正是"这个人现在没有任何可用范围"的表达。

    **为什么不复用** :func:`serialize_permissions`：那个函数对 ``not granted`` 的聚合
    结果**抛错**，而且这条规则必须留着——它挡的是"把一份 fail-closed 的聚合当成有效权限
    发出去"。撤权是一条**显式**的路径，因此给它一个显式的出口，而不是把既有的守卫改松。

    走 ``json.dumps`` 而不是直接返回字面量，是为了让"撤权行与授权行出自同一套序列化约定"
    这件事在代码里成立，而不是靠两处字面量碰巧相等。
    """

    return json.dumps({}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _verbatim(value: Any) -> str:
    """值列表元素的**唯一**出口：原样返回，只拒绝 ``None`` 与非字符串。

    刻意**不**调用 ``strip()`` / ``casefold()`` / ``unicodedata.normalize()``：MCP 逐字
    匹配，一次"顺手归一"就会把 ``ＯＴＴ`` 变成 ``OTT``、把 ``日活 `` 变成 ``日活``，
    而这两者在消费方眼里是不同的指标——错的方向是**静默给错范围**，不是报错。

    这个函数存在的意义就是"这里没有归一"这件事有一个**能被指着看、也能被改坏**的位置：
    ``tests/test_permission_publish_row.py`` 的 ``MetricNameSensitivityTest`` 钉着它。
    """

    if not isinstance(value, str) or not value:
        raise ValueError("发布内容的值列表元素必须是非空字符串")
    return value


def format_updated_at(moment: datetime) -> str:
    """``updated_at`` 单元格的文本：秒精度的 UTC ISO-8601（既有行同为 ISO 风格时间串）。

    秒精度而不是微秒：这一列是给人和消费方看的时间，多出来的六位数字不带信息，
    却让「同一次决定重试写入是否产生同一串文本」多一个出错面。
    """

    if moment.tzinfo is None or moment.utcoffset() is None:
        # 时间一律 UTC（接口设计「二、通用约定」）。naive 时间会让跨时区部署写出
        # 互相矛盾的时间戳，而这一列正是消费方判断「这份权限有多新」的依据。
        raise ValueError("权限决定时间必须带时区")
    return moment.astimezone(_UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class PublishRow:
    """发布表那一行的完整待写内容。**只有文本，没有对象**。

    构造出来的实例即可直接交给发布执行器：:attr:`fields` 的键集恒等于
    :data:`PUBLISHED_FIELD_NAMES`，值恒为非空字符串。

    :attr:`token_cipher` 是**唯一可以缺席**的字段，因为它只在新建行时才写：一个已经
    存在于发布表的人（例如既有 26 行）不需要我们提供令牌，也不允许我们覆盖他的。
    缺席时 :attr:`create_fields` 会拒绝，发布执行器据此对"要新建却没有令牌"失败关闭。
    """

    record_key: str
    email: str
    name: str
    permissions: str
    status: str
    updated_at: str
    token_cipher: str | None = None

    def __post_init__(self) -> None:
        for field_name in PUBLISHED_FIELD_NAMES:
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"发布行字段 {field_name} 必须是非空文本")
            if "\n" in value or "\r" in value:
                # 目标列全是**单行文本**：带换行的值在平台侧的行为未经验证，
                # 与其赌它，不如在构造时响亮失败。
                raise ValueError(f"发布行字段 {field_name} 不得包含换行")
        if self.token_cipher is not None and not is_cipher_shaped(self.token_cipher):
            # **不回显收到的值**：它是凭据材料。形状判据见 :func:`is_cipher_shaped`——
            # 它同时挡住"把明文当密文传进来"这个最危险的手误。
            raise ValueError("发布行的 token_cipher 形状不合法（不回显收到的值）")

    @property
    def fields(self) -> dict[str, str]:
        """**更新既有行**时的待写字段映射。``token_cipher`` 不在其中（模块文档）。"""

        return {name: getattr(self, name) for name in PUBLISHED_FIELD_NAMES}

    @property
    def create_fields(self) -> dict[str, str]:
        """**新建行**时的待写字段映射：更新集 + ``token_cipher``。

        没有令牌就没有可新建的行——一行没有 ``token_cipher`` 的权限，问数 MCP 解不出
        任何 Bearer 与它匹配，写出去等于没写。因此这里**抛错**而不是退回六字段：
        静默少写一列的结果是"发布成功了，但这个人永远问不了数"。

        **这道检查保护的边界**（外部独立审查 2026-08-18 提请澄清的 P3，产品负责人
        2026-08-17 裁定 3 已确认这是有意为之，非缺陷）：它只在**新建行**这一条路径上
        生效，**不保护更新既有行**——更新走的是 :attr:`fields`，那个属性的字段集里
        本来就没有 ``token_cipher``（见其文档），既有密文因此靠"没被列进更新集"而
        保持原值，不是靠这里的校验。两条路径分别用两种**不同的机制**保护同一列，
        不是同一个校验覆盖了全部写入——这是刻意的：更新既有行不需要、也不应该要求
        调用方持有该用户的令牌（那 26 行既有密文是旧系统签发的，Lingxi 从未持有过
        明文），要求它会让"只更新不新建"的路径无缘无故多一个不该有的前置条件。
        """

        if not self.token_cipher:
            raise ValueError("新建发布行必须携带 Lingxi 签发的 token_cipher")
        return {name: getattr(self, name) for name in CREATED_FIELD_NAMES}

    @property
    def snapshot_fields(self) -> dict[str, str]:
        """进 outbox ``payload`` 的内容快照：有令牌就是七字段，没有就是六字段。

        为什么快照里放**密文**：``payload`` 是"当初决定发布的那一版"，重试必须能原样
        重放。取不到令牌就临时去查一次，会让"这一版发布的是什么"取决于重试那一刻的
        库状态；而密文是**当前状态**类数据（迁移 ``0065``），不含明文也不含主密钥，
        与 outbox 里已有的邮箱、姓名相比不提高敏感级别。明文与主密钥一步都不进这里。
        """

        return self.create_fields if self.token_cipher else self.fields

    @property
    def content_fields(self) -> dict[str, str]:
        """参与「权限有没有变化」判断的字段：整行去掉时间戳。

        用它而不是整行比较，是因为 ``updated_at`` 每轮都不同——拿整行比，一次内容
        完全没变的每日刷新也会被判成变化，于是天天产生一条发布意图、天天写一次
        外部表格。
        """

        return {
            name: value
            for name, value in self.fields.items()
            if name not in _VOLATILE_FIELD_NAMES
        }

    @classmethod
    def from_fields(cls, fields: Mapping[str, Any]) -> "PublishRow":
        """从 outbox payload 还原成发布行。

        payload 是**当初决定发布的那一版内容快照**，回读时逐键取；缺键或多键都直接
        失败，不做补齐——补齐等于让一份残缺的快照冒充完整的发布意图。

        ``token_cipher`` 是唯一的可选键：一份不带它的快照仍然合法（那一版决定是"只更新
        既有行"），但它**不能补齐**——一份原本没有令牌的快照被补上一个令牌，等于把
        "只更新"悄悄变成"可以新建"。
        """

        missing = [name for name in PUBLISHED_FIELD_NAMES if name not in fields]
        if missing:
            raise ValueError(f"发布内容快照缺少字段：{','.join(missing)}")
        unexpected = [name for name in fields if name not in CREATED_FIELD_NAMES]
        if unexpected:
            raise ValueError(f"发布内容快照含未登记字段：{','.join(sorted(unexpected))}")
        restored = {name: fields[name] for name in PUBLISHED_FIELD_NAMES}
        if TOKEN_CIPHER_FIELD in fields:
            restored[TOKEN_CIPHER_FIELD] = fields[TOKEN_CIPHER_FIELD]
        return cls(**restored)


def build_publish_row(
    *,
    aggregate: PermissionAggregate,
    email: str,
    display_name: str,
    decided_at: datetime,
    token_cipher: str | None = None,
) -> PublishRow:
    """把聚合结果 + 身份资料结算成目标行。

    ``email`` 取花名册存档的**原值**（``app_user.email``），在这里统一做一次
    :func:`normalize_email`：``record_key`` 与 ``email`` 两列因此同源，同一个人不会
    因为大小写差异被写成两行。``display_name`` 取身份链的姓名（``app_user.display_name``），
    与建档合同同一口径（见 ``core/identity/provisioning.py``）。

    ``token_cipher`` 由调用方在**决定发布之前**先向
    :class:`lingxi.adapters.postgres_mcp_token.PostgresMcpTokenStore` 取（签发幂等，
    已存在即返回既有那一份）。它是**关键字可选**参数而不是必填：调用次序错了要在
    "新建"那一步失败关闭，而不是让整条每日刷新链路因为某个只需要更新的人拿不到令牌
    而停摆。参数默认为 ``None`` 也让 S-C-01 的既有调用点保持可编译——但那些调用点
    产出的行只能走更新路径。
    """

    if not aggregate.granted:
        raise ValueError("无可用权限的用户不得生成发布行")
    normalized = normalize_email(email)
    if not normalized:
        # 没有邮箱就没有 record_key，也就没有「这一行是谁的」这个问题的答案。
        # 匹配层允许纯工号匹配成功，但发布表按人取权限只有邮箱这一个共同键。
        raise ValueError("发布行必须有可用邮箱：它同时是 record_key 与 email 两列")
    name = _text(display_name)
    if not name:
        raise ValueError("发布行必须有姓名")
    return PublishRow(
        record_key=normalized,
        email=normalized,
        name=name,
        permissions=serialize_permissions(aggregate),
        status=STATUS_APPROVED,
        updated_at=format_updated_at(decided_at),
        token_cipher=token_cipher,
    )


def build_translated_publish_row(
    *,
    company_metrics: Mapping[str, Sequence[str]],
    email: str,
    display_name: str,
    decided_at: datetime,
    token_cipher: str | None = None,
) -> PublishRow:
    """与 :func:`build_publish_row` 同构，但 ``permissions`` 取翻译层
    （:mod:`lingxi.core.permission.metric_translation`）已经算好的
    「公司 → 指标名列表」，而不是从 ``PermissionAggregate.functions`` 现算职能标签。

    **调用方必须先翻译成功才能调用本函数**：翻译失败（存在未覆盖的「公司 + 职能」
    组合）要走 fail-closed 出口，不产出任何发布行——本函数不接受
    ``PermissionAggregate``，也不知道"翻译失败时该怎么办"，那个决定在调用方
    （每日权限重算职责）。这不是遗漏：把"翻译失败"和"翻译成功后结算行"揉进同一个
    函数，会让"失败要不要继续往下走"这件事变成一个可以被参数悄悄绕过的分支，而
    :func:`serialize_translated_permissions` 对空输入与空列表已经会响亮失败——
    本函数不重复那层校验，直接复用它。

    ``email``/``display_name``/``decided_at``/``token_cipher`` 与
    :func:`build_publish_row` 完全同源同口径，不再重复注释；两个函数各自独立校验
    邮箱与姓名（而不是抽出一个共享的私有辅助函数），是为了不让"翻译路径"与
    "职能标签直传路径"在这一层产生任何隐式耦合——两条路径将来的生命周期不同
    （职能标签路径没有生产调用方，纯粹是既有测试覆盖的保留；翻译路径是唯一在产的
    路径），耦合会让删除前者变成一次要同时读两条路径的手术。
    """

    normalized = normalize_email(email)
    if not normalized:
        # 与 build_publish_row 同一条：没有邮箱就没有 record_key，
        # 也就没有「这一行是谁的」这个问题的答案。
        raise ValueError("发布行必须有可用邮箱：它同时是 record_key 与 email 两列")
    name = _text(display_name)
    if not name:
        raise ValueError("发布行必须有姓名")
    return PublishRow(
        record_key=normalized,
        email=normalized,
        name=name,
        permissions=serialize_translated_permissions(company_metrics),
        status=STATUS_APPROVED,
        updated_at=format_updated_at(decided_at),
        token_cipher=token_cipher,
    )


def build_revocation_row(
    *, email: str, display_name: str, decided_at: datetime
) -> PublishRow:
    """结算一行**撤权行**：保行、清空 ``permissions``、其余按授权行同一套规则。

    产品负责人 2026-08-18 裁定 3（`V-权限-08` 的刷新侧）：权限从有变无时行**保留**、
    ``permissions`` 序列化为空对象、``status`` 维持 :data:`STATUS_APPROVED`、
    ``updated_at`` 照常更新，而 ``token_cipher`` **一个字都不动**。

    三条边界写在类型上，不靠调用方自觉：

    1. **没有 ``token_cipher`` 参数。** 撤权行因此永远只有六个字段
       （:attr:`PublishRow.snapshot_fields` 退化成 :attr:`PublishRow.fields`），
       走的必然是**更新**路径——更新是部分更新，没列出的列保持原值，那一列因此既不
       被清空也不被覆盖。想在撤权时顺手改写密文，在这个签名下写不出来。
    2. **因此撤权也不可能新建行。** 一份不带密文的快照走到"没有命中任何既有行"那一步时，
       :attr:`PublishRow.create_fields` 抛错，发布执行器以 ``invalid`` +
       ``missing_token_cipher`` 失败关闭（见 :func:`lingxi.core.permission.publish.
       publish_claim`）。这不是意外收敛，正是我们要的形状：**为一个从来没有过发布行的人
       新建一行空权限没有任何意义**——问数 MCP 对查无此人本来就默认拒绝，而新建还需要
       一份我们未必该给他的令牌。调用侧（每日刷新职责）另有一道判据："这个人有过成功
       发布的意图才发撤权更新"，本函数这一条是它的兜底。
    3. **``status`` 不变。** 取值域由消费方定义；把撤权表达成一个 MCP 不认识的
       ``status`` 值，等于赌它对未知取值的处理方式，而那件事未经验证。

    ``email`` / ``display_name`` 与 :func:`build_publish_row` 同源同口径（存档身份），
    因此同一个人在授权与撤权之间不会因为大小写差异被写成两行。
    """

    normalized = normalize_email(email)
    if not normalized:
        # 与 :func:`build_publish_row` 同一条：没有邮箱就没有 record_key，
        # 也就没有"这一行是谁的"这个问题的答案。
        raise ValueError("发布行必须有可用邮箱：它同时是 record_key 与 email 两列")
    name = _text(display_name)
    if not name:
        raise ValueError("发布行必须有姓名")
    return PublishRow(
        record_key=normalized,
        email=normalized,
        name=name,
        permissions=serialize_revoked_permissions(),
        status=STATUS_APPROVED,
        updated_at=format_updated_at(decided_at),
    )


def is_cipher_shaped(value: Any) -> bool:
    """这个值**在形状上**是不是一份 ``token_cipher``（不解密，也不需要主密钥）。

    存在的意义只有一个：让"把令牌明文当密文写出去"在 ``core`` 里就被拦住。``core`` 拿
    不到主密钥，也不 import ``adapters``（代码框架第二节），因此这里只能判形状——而形状
    已经够了：签发的明文是 ``secrets.token_urlsafe(32)``，43 个字符的 **URL 安全**
    base64（含 ``-``/``_``、长度不是 4 的倍数），过不了标准 base64 校验；一份真密文则是
    ``base64(16B IV ‖ 16B 整数倍密文)``，解出来至少 32 字节且对齐。

    判据与 :func:`lingxi.adapters.mcp_token_cipher.looks_like_cipher` **必须一致**，
    两处各写一份是三层 import 规则的直接后果，改动时一起改；一致性由
    ``tests/test_mcp_token_cipher.py`` 与 ``tests/test_permission_publish_row.py``
    各自钉住同一组样本。
    """

    if not isinstance(value, str) or not value or value.strip() != value:
        return False
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        return False
    return len(raw) >= 32 and (len(raw) - 16) % 16 == 0


def parse_permissions(text: Any) -> dict[str, tuple[str, ...]]:
    """把 ``permissions`` 单元格文本解析回 ``{公司ID: (指标名, …)}``。

    这是 :func:`serialize_permissions` 的**读侧**，放在同一个模块是因为这套格式全仓库
    只有一份定稿。形状不对（不是对象、值不是字符串列表）一律抛错，不做宽容修补：
    一份读不懂的权限文档被"尽力解析"成半份，方向是**给错范围**，不是报错。
    """

    if not isinstance(text, str) or not text.strip():
        raise ValueError("permissions 文本不能为空")
    try:
        document = json.loads(text)
    except ValueError as error:
        raise ValueError("permissions 不是合法 JSON") from error
    if not isinstance(document, Mapping):
        raise ValueError("permissions 必须是 {公司ID: [指标名, …]} 形状的对象")
    parsed: dict[str, tuple[str, ...]] = {}
    for key, values in document.items():
        if not isinstance(key, str) or not key:
            raise ValueError("permissions 的键必须是非空字符串")
        if not isinstance(values, (list, tuple)):
            raise ValueError("permissions 的值必须是列表")
        for item in values:
            if not isinstance(item, str):
                raise ValueError("permissions 的值列表元素必须是字符串")
        parsed[key] = tuple(values)
    return parsed


def lookup_metrics(document: Mapping[str, Sequence[str]], company_id: Any = None) -> tuple[str, ...]:
    """按**回退制**取某个公司下的指标列表。**不取并集。**

    产品负责人 2026-08-17 的权威设计（留痕见
    [#155](https://github.com/Moshuiwang/lingxi/issues/155)）：

    1. 先找 ``document[company_id]``；**这个键存在就到此为止**，哪怕它是空列表——
       空列表表示"该公司下无任何指标"，与缺键是两回事（模块文档「``permissions``」一节）。
    2. 该键不存在，才回退 ``document["*"]``（"所有公司"）。
    3. 两个都没有 → 空元组。

    **并集是错的，不得沿用**：旧系统 biai-agent 把 ``document[company_id]`` 与
    ``document["*"]`` 归一成并集，那会让一个"在 1011 公司只有日活"的人，因为通配键里有
    收入而在 1011 也看见收入——方向是**多给权限**。这里刻意把回退写成一个能被指着看、
    也能被改坏的分支，``tests/test_permission_publish_row.py`` 的
    ``PermissionFallbackTest`` 钉着它。

    ``company_id`` 为 ``None`` 时问的是另一个问题——"这个人到底有没有任何指标"——
    此时对全部键取并集是正确的：那不是范围判定，是存在性判定。
    """

    if company_id is None:
        return tuple(
            sorted({item for values in document.values() for item in values})
        )
    key = _text(company_id)
    if not key:
        raise ValueError("公司标识不能为空")
    if key in document:
        return tuple(document[key])
    if ALL_COMPANIES_KEY in document:
        return tuple(document[ALL_COMPANIES_KEY])
    return ()


def readback_text(value: Any) -> str:
    """把读回的单元格值归一成**可比较文本**。

    这是 G-BIT 2026-08-17 复验移交的实现约束：多维表格的单条记录读取接口会把 Number
    字段序列化成字符串（写入无失真、值一致）。当前目标表 7 列全是文本，这条约束对首版
    没有影响，但校验实现**不得按 Python 类型严格相等**——某一列将来被业务侧改成数字
    字段时，严格相等会让每一次读回都判成不一致，而值其实是对的。

    刻意不复用 ``adapters/feishu_roster_bitable.field_text``：``core/`` 不 import
    ``adapters/``（代码框架第二节第 1 条），而且两者的语义不同——那个函数是「从可能
    嵌套的单元格里挑一个非空文本出来」，这里要的是「这个值等价于哪一串字符」，因此
    对空列表、空对象一律给空串，不做任何深挖式的择优。
    """

    if value is None:
        return ""
    if isinstance(value, bool):
        # 布尔不是文本列的合法回读形态；归成空串会让不一致被静默吞掉，
        # 归成 "True"/"False" 又会与真的写了 "True" 的文本混淆。两害相权，
        # 取「一定不等于任何我们写入的值」的空串，让比对红出来。
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        return "".join(readback_text(item) for item in value)
    if isinstance(value, Mapping):
        for key in ("text", "value", "name"):
            if key in value:
                return readback_text(value[key])
        return ""
    return ""


def compare_readback(expected: Mapping[str, str], actual: Mapping[str, Any]) -> tuple[str, ...]:
    """逐字段比对读回结果，返回**不一致的字段名**（按 :data:`CREATED_FIELD_NAMES` 序）。

    返回字段名而不是字段值：不一致要能进日志、告警和工单，而值里有邮箱、姓名和令牌密文
    （纪律同 ``core/identity/roster_audit.PersonDiff``）。空元组表示逐字段一致。

    比对范围**由 ``expected`` 决定**：传 :attr:`PublishRow.fields`（更新）就比六个字段，
    传 :attr:`PublishRow.create_fields`（新建）就连 ``token_cipher`` 一起比。这正是
    "更新既有行时我们不碰那一列，因此也无权要求它等于我们的值"这条语义在比对层的落点。
    """

    return tuple(
        name
        for name in CREATED_FIELD_NAMES
        if name in expected and readback_text(actual.get(name)) != expected[name]
    )
