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

**同一个人只允许一行**由发布执行器守（见 :mod:`lingxi.core.permission.publish` 的
``CONFLICT`` 分支）：写入前按 ``record_key`` **或** ``email`` 查找，命中一行且
``record_key`` 一致才更新；命中的行 ``record_key`` 口径不同、或命中多行，一律失败关闭，
既不更新也不新建。业务侧既有 26 行用的是什么 ``record_key`` 口径**尚未回源核对**，因此
首次真实发布时它们会全部走 CONFLICT——这是刻意的：让一个未知的外部约定在受控窗口里响亮
暴露，比静默写出重复行安全。

### ``permissions``：一行紧凑 JSON

```text
{"all_companies":false,"companies":["1001","1002"],"functions":["OTT","运营"],"v":1}
```

- ``json.dumps`` + ``sort_keys=True`` + 无空格分隔符：同一份权限**永远序列化成同一串
  字节**。发布是「写入后逐字段读回比对」，不确定的序列化会让一次没有任何变化的重发被
  判成不一致；``ensure_ascii=False`` 让职能标签在表里对人可读。
- ``companies`` 是 ``sys_country.boss_company_id``（产品负责人 2026-08-05 决策 3：
  向问数 MCP 申请权限用它），去重后按字符串排序；``name_cn`` 是**展示用**字段，不进
  发布行——发布表是给 MCP 读的机器契约，不是给人看的展示层。
- ``functions`` 是配置文件（``lingxi/config/galaxy_role_function_map.toml``）映射出的
  Lingxi 职能标签，去重排序；未映射角色不产生职能（`V-银河-13` 的口径）。
- ``all_companies`` 是「全非」通配的**事实标记**，与已展开的 ``companies`` 同时给出，
  不用它代替列表：消费方无需知道我们的展开规则，但需要知道这是通配授权。
- ``v`` 是格式版本。业务侧既有 26 行的 ``permissions`` 实际取值格式**尚未回源核对**
  （G-BIT 只取了 schema，没有回显业务数据值）；对齐时只改本模块的
  :func:`serialize_permissions` 与它的用例，不改其它任何地方。

### ``token_cipher``：**永不写入**

它是业务侧既有字段，看名字承载的是某种凭据。产品合同「凭据不进代码、日志、数据库、
用户环境」和 [#156](https://github.com/Moshuiwang/lingxi/issues/156) 范围第 8 条
（「凭据、token 和无关个人数据不入 outbox/日志」）在这里的落点是同一句话：Lingxi
**不产生、不读取、不写入** ``token_cipher``。:data:`PUBLISHED_FIELD_NAMES` 里刻意
没有它——更新既有行时它不在更新集里，因此不会被清空，也不会被我们的值覆盖。

### ``updated_at``：**权限决定的时刻**，不是发布尝试的时刻

它随发布意图一起冻结在 outbox 的 payload 里，重试写入的永远是同一串文本。取发布时刻会
让每次重试都改变待写内容，于是「读回与预期一致」永远只能证明最后一次写入自洽，证明不了
写进去的是当初决定要发布的那一版。
"""

from __future__ import annotations

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

#: ``permissions`` 文本的格式版本。改变字段含义时必须递增，消费方据此分辨。
PERMISSIONS_FORMAT_VERSION = 1

#: 发布行「有权限」时写入的 ``status`` 值。停用 / 收回的发布语义属 S-C-03/04，
#: 本 Story 只发布**有效**权限，因此这里只有一个取值，不预留一个没人写的枚举。
STATUS_ACTIVE = "active"

#: 本实现写入的字段，**顺序即比对与审计的输出顺序**。``token_cipher`` 不在其中，
#: 理由见模块文档；它不是遗漏，改动它需要同时改产品合同。
PUBLISHED_FIELD_NAMES: tuple[str, ...] = (
    "record_key",
    "email",
    "name",
    "permissions",
    "status",
    "updated_at",
)

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
        """

        return {
            "granted": self.granted,
            "reason": self.reason,
            "companies": len(self.companies),
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
    """

    account = _text(galaxy_user_id)
    if not account:
        raise ValueError("银河账号标识不能为空")

    role_names = role_names_for_user(account, user_role_rows)
    if not role_names:
        return _denied(REASON_NO_ROLES)

    resolved_roles = resolve_role_functions(role_names, role_function_map)
    functions = tuple(
        sorted({item.function for item in resolved_roles if item.function is not None})
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
        sorted({_text(item.boss_company_id) for item in scope.countries if _text(item.boss_company_id)})
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

    return PermissionAggregate(
        granted=True,
        reason=REASON_GRANTED,
        companies=company_ids,
        functions=functions,
        all_companies=scope.all_countries,
        role_count=len(role_names),
        unmapped_role_count=unmapped,
        unresolved_country_keys=scope.unresolved_country_keys,
        countries_without_company_id=missing_company_id,
    )


def serialize_permissions(aggregate: PermissionAggregate) -> str:
    """把有效权限序列化成 ``permissions`` 单元格的**唯一**文本形态。

    全仓库只有这一处实现，理由见模块文档：业务侧既有行的实际格式尚未回源核对，对齐
    时只改这一个函数与它的用例，不必去追第二处拼串的地方。

    ``sort_keys=True`` + ``separators=(",", ":")`` 让同一份权限恒等地序列化成同一串
    文本——「写入后逐字段读回按字符串比对」的前提。
    """

    if not aggregate.granted:
        # 没有权限就不该有发布行；调用方走 fail-closed 出口，不发布空权限。
        raise ValueError("无可用权限的聚合结果不得序列化成发布内容")
    document = {
        "v": PERMISSIONS_FORMAT_VERSION,
        "all_companies": aggregate.all_companies,
        "companies": list(aggregate.companies),
        "functions": list(aggregate.functions),
    }
    return json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def format_updated_at(moment: datetime) -> str:
    """``updated_at`` 单元格的文本：秒精度的 UTC ISO-8601。

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
    """

    record_key: str
    email: str
    name: str
    permissions: str
    status: str
    updated_at: str

    def __post_init__(self) -> None:
        for field_name in PUBLISHED_FIELD_NAMES:
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"发布行字段 {field_name} 必须是非空文本")
            if "\n" in value or "\r" in value:
                # 目标列全是**单行文本**：带换行的值在平台侧的行为未经验证，
                # 与其赌它，不如在构造时响亮失败。
                raise ValueError(f"发布行字段 {field_name} 不得包含换行")

    @property
    def fields(self) -> dict[str, str]:
        """待写字段映射。``token_cipher`` 不在其中（模块文档）。"""

        return {name: getattr(self, name) for name in PUBLISHED_FIELD_NAMES}

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
        """

        missing = [name for name in PUBLISHED_FIELD_NAMES if name not in fields]
        if missing:
            raise ValueError(f"发布内容快照缺少字段：{','.join(missing)}")
        unexpected = [name for name in fields if name not in PUBLISHED_FIELD_NAMES]
        if unexpected:
            raise ValueError(f"发布内容快照含未登记字段：{','.join(sorted(unexpected))}")
        return cls(**{name: fields[name] for name in PUBLISHED_FIELD_NAMES})


def build_publish_row(
    *,
    aggregate: PermissionAggregate,
    email: str,
    display_name: str,
    decided_at: datetime,
) -> PublishRow:
    """把聚合结果 + 身份资料结算成目标行。

    ``email`` 取花名册存档的**原值**（``app_user.email``），在这里统一做一次
    :func:`normalize_email`：``record_key`` 与 ``email`` 两列因此同源，同一个人不会
    因为大小写差异被写成两行。``display_name`` 取身份链的姓名（``app_user.display_name``），
    与建档合同同一口径（见 ``core/identity/provisioning.py``）。
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
        status=STATUS_ACTIVE,
        updated_at=format_updated_at(decided_at),
    )


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
    """逐字段比对读回结果，返回**不一致的字段名**（按 :data:`PUBLISHED_FIELD_NAMES` 序）。

    返回字段名而不是字段值：不一致要能进日志、告警和工单，而值里有邮箱和姓名
    （纪律同 ``core/identity/roster_audit.PersonDiff``）。空元组表示逐字段一致。
    """

    return tuple(
        name
        for name in PUBLISHED_FIELD_NAMES
        if name in expected and readback_text(actual.get(name)) != expected[name]
    )
