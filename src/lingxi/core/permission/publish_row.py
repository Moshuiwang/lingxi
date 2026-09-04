"""按用户聚合当前有效权限，并结算成**当前权限多维表格的目标行**（纯函数）。

上游（银河账号匹配）已经回答"这个人是谁、匹配到哪个银河账号"；本模块回答接下来两件事：
聚合（把角色与国家授权解释成「公司范围 + 支持职能」，fail-closed）与结算成发布表那一行的
全部字段文本——序列化格式在这里定稿，全仓库只有这一份实现。**判定语义不在这里**：角色
映射见 :mod:`lingxi.core.permission.role_function`，公司范围解释见
:mod:`lingxi.core.permission.galaxy_scope`，银河账号匹配见
:mod:`lingxi.core.permission.account_match`——本模块只调用，不复制它们的规则。

正式表 ``user_company_permissions`` 的 7 个字段全部是单行文本，没有版本字段、没有结构化
类型；每个字段的写入约定就近写在各自的常量与函数 docstring 里，不在此重复。**写侧目前
放进值列表的还不是指标名，是职能标签**：翻译层载体已交付
（:mod:`lingxi.core.permission.metric_translation`），映射内容为空前一律 fail-closed，
真实发布仍是待完成项，见 :func:`serialize_translated_permissions`。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from lingxi.core.permission.account_match import normalize_email
from lingxi.core.permission.galaxy_scope import (
    country_keys_for_user,
    resolve_company_scope,
    role_names_for_user,
)
from lingxi.core.permission.role_function import resolve_role_functions

_UTC = UTC

#: 「全非」通配在 ``permissions`` 里的键。既有行用它表示「所有公司」，因此我们不把
#: 通配展开成几十个重复条目——展开规则是我们这一侧的解释，消费方已有自己的约定。
ALL_COMPANIES_KEY = "*"

#: 触发「角色即全公司」特例的 Lingxi 职能标签（银河"后台管理员"角色映射得到）。持有
#: 它时 ``all_companies`` 强制为真且覆盖未来新增公司，不依赖该用户当次快照解析出的
#: 具体范围——见 :func:`aggregate_permission` 里唯一引用它的特例分支。
ADMIN_FULL_ACCESS_FUNCTION = "后台管理员"

#: 发布行的 ``status`` 值，取值域由消费方（问数 MCP）定义。**撤权行同样写此值**：
#: 这一列没有第二个被消费方认可的取值，撤权由 ``permissions`` 为空表达，不由
#: ``status`` 表达。
STATUS_APPROVED = "approved"

#: 撤权行的 ``permissions`` 文本：空对象，必须与 :func:`serialize_permissions` 同一套
#: 序列化约定（否则"没有任何权限"会有两种字节形态）。由
#: :func:`serialize_revoked_permissions` 产出，这里只留一份可核对的期望值。
REVOKED_PERMISSIONS_TEXT = "{}"

#: **更新既有行**时写入的字段，**顺序即比对与审计的输出顺序**。``token_cipher`` 不在
#: 其中：更新是部分更新，没列出的列保持原值，既有行的令牌因此既不被清空也不被覆盖
#: （见 :data:`TOKEN_CIPHER_FIELD`）。它不是遗漏，改动它需要产品负责人裁定。
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

#: 参与内容摘要的字段与顺序：更新集去掉时间戳，顺序即 :data:`PUBLISHED_FIELD_NAMES`
#: 的顺序。**这个顺序是摘要算法的一部分**，迁移 ``0085`` 的 SQL 回填按同一顺序拼串，
#: 改动它会让存量摘要与新算出来的摘要对不上。
DIGEST_FIELD_NAMES: tuple[str, ...] = tuple(
    name for name in PUBLISHED_FIELD_NAMES if name not in _VOLATILE_FIELD_NAMES
)


def content_digest(fields: Mapping[str, Any]) -> str:
    """整行内容（去掉 ``updated_at``）的 SHA-256 十六进制摘要。

    摘要的理由是它能活过 ``publish_outbox.payload`` 的九十天擦除：擦除后 payload 变成
    空对象，"这一版权限和上一版是否相同"就无法再靠比对 payload 判断，只能靠这份单向
    摘要（回答不了具体内容，只能回答"和另一份是否一样"）。

    拼串按 :data:`DIGEST_FIELD_NAMES` 顺序写成 ``名字=值``、用换行符连接（迁移 ``0085``
    的 SQL 回填逐字节复刻同一形态，改动顺序会让存量摘要与新算出来的对不上）；缺键按
    空串参与，不静默跳过。
    """
    canonical = "\n".join(f"{name}={_digest_text(fields.get(name))}" for name in DIGEST_FIELD_NAMES)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def permissions_digest(fields: Mapping[str, Any]) -> str:
    """``permissions`` 单字段的 SHA-256 十六进制摘要。

    与 :func:`content_digest` 分开，是因为两者回答的是不同的问题（见
    ``adapters/postgres_permission_publish._permissions_changed`` 文档）：整行摘要
    回答"要不要排一条新的发布意图"，这一个回答"这个人**实际可用权限**变了吗"——
    只有后者能决定要不要清空用户记忆与已送达正文。改名不该清记忆。
    """
    return hashlib.sha256(_digest_text(fields.get("permissions")).encode("utf-8")).hexdigest()


def _digest_text(value: Any) -> str:
    """摘要用的取值归一：``None`` 与缺键都算空串，其余按 ``str`` 取字面量。

    payload 从 JSONB 回来时数字会变成 Python 数字，而发布行永远是文本——不归一会让
    同一份内容在"刚写进去"与"从库里读回来"两个时刻算出不同的摘要。
    """
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


# fail-closed 的三个内部原因。用户侧一律是同一个「无可用银河权限」出口，这里的区分
# 只供审计与排障。
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
        """校验 fail-closed 与授权两种状态各自的字段形状不变量。"""
        if not self.granted and (self.companies or self.functions):
            raise ValueError("fail-closed 的聚合结果不得携带任何公司或职能范围")
        if self.granted and (not self.companies or not self.functions):
            raise ValueError("有效权限必须同时有公司范围与受支持职能")

    def audit_facts(self) -> dict[str, Any]:
        """可直接进审计与日志的事实：只有计数、原因码与**机器编号**，不含人员资料。

        ``companies`` 在 ``all_companies=True`` 时输出 ``None``，不输出实际条数：
        「角色即全公司」特例下 ``companies`` 只是"这次快照恰好解释出了哪些公司"，
        与实际覆盖范围（全公司，含未来新增公司）无关，继续输出这个数字会让审计行
        读出自相矛盾的话（"companies: 1, all_companies: true"）。
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


def _resolve_supported_functions(
    role_names: Sequence[str], role_function_map: Mapping[str, str]
) -> tuple[tuple[str, ...], int, int]:
    """把角色名解释成受支持的 Lingxi 职能标签，返回 ``(职能, 角色数, 未映射角色数)``。

    去重用 ``dict.fromkeys``（保留首次出现次序）再显式排序，不用 ``sorted(set(...))``：
    集合迭代次序随 ``PYTHONHASHSEED`` 变化，写成显式排序让"排序"是一个能被单独看见、
    也能被单独改坏的动作——发布靠的正是恒等序列化。
    """
    resolved_roles = resolve_role_functions(role_names, role_function_map)
    functions = tuple(
        sorted(dict.fromkeys(item.function for item in resolved_roles if item.function is not None))
    )
    unmapped = sum(1 for item in resolved_roles if not item.mapped)
    return functions, len(role_names), unmapped


def _resolve_company_ids(
    account: str,
    datacountry_rows: Iterable[Mapping[str, Any]],
    country_rows: Sequence[Mapping[str, Any]],
) -> tuple[Any, tuple[str, ...], int]:
    """把国家授权解释成带公司编号的公司集合，返回 ``(范围, 公司 ID, 缺公司编号计数)``。

    「解释到了国家但该行没有 ``boss_company_id``」只计数、不否决整个用户——没有公司
    编号就没法向 MCP 申请，写进发布行只会得到消费方看不懂的空值，方向是少给权限。
    """
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
    return scope, company_ids, missing_company_id


def aggregate_permission(
    *,
    galaxy_user_id: str,
    user_role_rows: Iterable[Mapping[str, Any]],
    datacountry_rows: Iterable[Mapping[str, Any]],
    country_rows: Sequence[Mapping[str, Any]],
    role_function_map: Mapping[str, str],
) -> PermissionAggregate:
    """把一个银河账号的授权聚合成当前有效权限。

    输入是已经落库的银河快照行；"哪一批是当前有效"由导入层决定，本模块不读库、不选
    批次。三个 fail-closed 分支（用户侧统一「无可用银河权限」）：``no_galaxy_roles``
    （无角色）、``no_supported_function``（有角色但未映射到 Lingxi 职能）、
    ``no_company_scope``（解释不出任何带公司编号的公司）。产出的 ``functions`` 是
    职能标签，不是发布表要的指标名（见模块文档）；持有「后台管理员」职能时
    ``all_companies`` 强制为真且覆盖未来新增公司，见 :data:`ADMIN_FULL_ACCESS_FUNCTION`。
    """
    account = _text(galaxy_user_id)
    if not account:
        raise ValueError("银河账号标识不能为空")

    role_names = role_names_for_user(account, user_role_rows)
    if not role_names:
        return _denied(REASON_NO_ROLES)

    functions, role_count, unmapped = _resolve_supported_functions(role_names, role_function_map)
    if not functions:
        return _denied(
            REASON_NO_SUPPORTED_FUNCTION,
            role_count=role_count,
            unmapped_role_count=unmapped,
        )

    scope, company_ids, missing_company_id = _resolve_company_ids(
        account, datacountry_rows, country_rows
    )
    if not company_ids:
        return _denied(
            REASON_NO_COMPANY_SCOPE,
            role_count=role_count,
            unmapped_role_count=unmapped,
            unresolved_country_keys=scope.unresolved_country_keys,
            countries_without_company_id=missing_company_id,
        )

    # 「角色即全公司」特例：company_ids 不清空——序列化与翻译层在此时只查通配键。
    all_companies = scope.all_countries or ADMIN_FULL_ACCESS_FUNCTION in functions

    return PermissionAggregate(
        granted=True,
        reason=REASON_GRANTED,
        companies=company_ids,
        functions=functions,
        all_companies=all_companies,
        role_count=role_count,
        unmapped_role_count=unmapped,
        unresolved_country_keys=scope.unresolved_country_keys,
        countries_without_company_id=missing_company_id,
    )


def serialize_permissions(aggregate: PermissionAggregate) -> str:
    """把有效权限序列化成 ``permissions`` 单元格的**唯一**文本形态。

    形状是 ``{公司ID: [指标名, …]}``，持有「全非」通配时只写一个
    :data:`ALL_COMPANIES_KEY` 键——这不是我们自拟的约定，这张表是现行问数 MCP 正在
    消费的权限源（详见模块文档）。**当前放进值列表的还是职能标签，不是指标名**：
    本函数只认 :attr:`PermissionAggregate.functions`，翻译后的路径走姊妹函数
    :func:`serialize_translated_permissions`，本函数的既有形状与用例因此不变。
    **值列表里的字符串原样透传，一个字符都不动**（MCP 逐字匹配，大小写与全半角
    敏感），这里只挡 ``None`` 与非字符串——那是本侧缺陷，不是数据。
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
    """把翻译层产出的「公司 → 指标名列表」序列化成 ``permissions`` 单元格文本。

    与 :func:`serialize_permissions` 同一套序列化纪律（逐字符透传、``sort_keys``、
    ``ensure_ascii=False``）；唯一的形状差异是输入——那个函数假设同一份职能列表适用
    于用户持有的全部公司，本函数接受**每个公司可能不同**的指标名列表，因此两者不能
    合并成一个。调用方须保证传入列表已去重排序，本函数不重新排序；**写侧不产出空
    列表**（空列表是读侧的合法形状，写侧出现空列表说明该「公司+职能」应在翻译层
    被当作未覆盖处理，不该流到这里才发现）。
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

    权限从有变无时**保留那一行、把 ``permissions`` 清空**，不删行、不改 ``status``、
    不碰 ``token_cipher``——消费方的权限判定走回退制（:func:`lookup_metrics`），空对象
    因此正是"这个人现在没有任何可用范围"的表达。不复用 :func:`serialize_permissions`：
    那个函数对 ``not granted`` 的聚合结果抛错，这条规则要继续挡住"把 fail-closed 的
    聚合当有效权限发出去"，撤权因此走一个显式的独立出口。
    """
    return json.dumps({}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _verbatim(value: Any) -> str:
    """值列表元素的**唯一**出口：原样返回，只拒绝 ``None`` 与非字符串。

    刻意**不**调用 ``strip()`` / ``casefold()`` / ``unicodedata.normalize()``：MCP 逐字
    匹配，一次"顺手归一"就会把 ``ＯＴＴ`` 变成 ``OTT``、把 ``日活 `` 变成 ``日活``，
    而这两者在消费方眼里是不同的指标——错的方向是**静默给错范围**，不是报错。
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
    存在于发布表的人不需要我们提供令牌，也不允许我们覆盖他的。缺席时
    :attr:`create_fields` 会拒绝，发布执行器据此对"要新建却没有令牌"失败关闭。
    """

    record_key: str
    email: str
    name: str
    permissions: str
    status: str
    updated_at: str
    token_cipher: str | None = None

    def __post_init__(self) -> None:
        """校验每个字段都是非空单行文本，且 token_cipher（若有）形状合法。"""
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

        没有令牌就没有可新建的行，因此这里**抛错**而不是退回六字段：静默少写一列的
        结果是"发布成功了，但这个人永远问不了数"。这道检查**只保护新建行**这一条
        路径，不保护更新既有行——更新走 :attr:`fields`，其字段集里本来就没有
        ``token_cipher``，既有密文靠"没被列进更新集"保持原值，是两种不同的机制，
        避免让"只更新不新建"的路径无缘无故要求调用方持有该用户的令牌。
        """
        if not self.token_cipher:
            raise ValueError("新建发布行必须携带 Lingxi 签发的 token_cipher")
        return {name: getattr(self, name) for name in CREATED_FIELD_NAMES}

    @property
    def snapshot_fields(self) -> dict[str, str]:
        """进 outbox ``payload`` 的内容快照：有令牌就是七字段，没有就是六字段。

        为什么快照里放**密文**：``payload`` 是"当初决定发布的那一版"，重试必须能原样
        重放。取不到令牌就临时去查一次，会让"这一版发布的是什么"取决于重试那一刻的
        库状态；而密文是当前状态类数据，不含明文也不含主密钥。
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
            name: value for name, value in self.fields.items() if name not in _VOLATILE_FIELD_NAMES
        }

    @classmethod
    def from_fields(cls, fields: Mapping[str, Any]) -> PublishRow:
        """从 outbox payload 还原成发布行。

        payload 是**当初决定发布的那一版内容快照**，回读时逐键取；缺键或多键都直接
        失败，不做补齐——补齐等于让一份残缺的快照冒充完整的发布意图。
        ``token_cipher`` 是唯一的可选键：一份不带它的快照仍然合法（那一版决定是"只
        更新既有行"），但它**不能补齐**——补上一个令牌等于把"只更新"悄悄变成"可以
        新建"。
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

    ``email`` 取花名册存档的原值，在这里统一做一次 :func:`normalize_email`：
    ``record_key`` 与 ``email`` 两列因此同源，同一个人不会因为大小写差异被写成两行。
    ``token_cipher`` 由调用方在**决定发布之前**先向
    :class:`lingxi.adapters.postgres_mcp_token.PostgresMcpTokenStore` 取（签发幂等）。
    它是关键字可选参数而不是必填：调用次序错了要在"新建"那一步失败关闭，而不是让
    整条每日刷新链路因为某个只需要更新的人拿不到令牌而停摆。
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
    """与 :func:`build_publish_row` 同构，但 ``permissions`` 取翻译层的结果。

    取的是已经算好的「公司 → 指标名列表」，而不是从 ``PermissionAggregate.functions``
    现算职能标签。**调用方必须先翻译成功才能调用本函数**：翻译失败要走 fail-closed 出口、不产出
    发布行，本函数不接受 ``PermissionAggregate``、也不知道"翻译失败时该怎么办"，那
    是调用方（每日权限重算职责）的决定。``email``/``display_name``/``decided_at``/
    ``token_cipher`` 与 :func:`build_publish_row` 完全同源同口径。
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


def build_revocation_row(*, email: str, display_name: str, decided_at: datetime) -> PublishRow:
    """结算一行**撤权行**：保行、清空 ``permissions``、其余按授权行同一套规则。

    三条边界写在类型上，不靠调用方自觉：没有 ``token_cipher`` 参数，因此撤权行永远
    只有六个字段，走的必然是更新路径，也因此不可能新建行（一份不带密文的快照命中不
    到既有行时，:attr:`PublishRow.create_fields` 会抛错失败关闭——为一个从没有过
    发布行的人新建一行空权限没有意义）；``status`` 不变，因为取值域由消费方定义。
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

    存在的意义只有一个：让"把令牌明文当密文写出去"在 ``core`` 里就被拦住——``core``
    拿不到主密钥、也不 import ``adapters``。签发的明文是 43 个字符的 URL 安全
    base64（含 ``-``/``_``，长度不是 4 的倍数），过不了标准 base64 校验；真密文是
    ``base64(16B IV ‖ 16B 整数倍密文)``，解出来至少 32 字节且对齐。判据与
    :func:`lingxi.adapters.mcp_token_cipher.looks_like_cipher` 必须一致，改动时一起改。
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


def lookup_metrics(
    document: Mapping[str, Sequence[str]], company_id: Any = None
) -> tuple[str, ...]:
    """按**回退制**取某个公司下的指标列表。**不取并集。**

    先找 ``document[company_id]``；这个键存在就到此为止，哪怕它是空列表——空列表
    表示"该公司下无任何指标"，与缺键是两回事。该键不存在才回退 ``document["*"]``
    （所有公司）；两个都没有则返回空元组。**并集是错的，不得沿用**：把两个键取并集
    会让一个"在某公司只有日活"的人，因为通配键里有收入而在该公司也看见收入，方向是
    多给权限。``company_id`` 为 ``None`` 时问的是另一个问题——"这个人到底有没有任何
    指标"，此时对全部键取并集是正确的：那不是范围判定，是存在性判定。
    """
    if company_id is None:
        return tuple(sorted({item for values in document.values() for item in values}))
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

    目标表 7 列全是文本，但读取接口可能把 Number 字段序列化成字符串（写入无失真、
    值一致），因此校验实现**不得按 Python 类型严格相等**。刻意不复用
    ``adapters/feishu_roster_bitable.field_text``：``core/`` 不 import ``adapters/``，
    而且语义不同——那个函数是"从可能嵌套的单元格里挑一个非空文本"，这里要的是"这个
    值等价于哪一串字符"，因此空列表、空对象一律给空串。
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

    返回字段名而不是字段值：不一致要能进日志、告警和工单，而值里有邮箱、姓名和令牌
    密文。比对范围**由 ``expected`` 决定**：传 :attr:`PublishRow.fields`（更新）就比
    六个字段，传 :attr:`PublishRow.create_fields`（新建）就连 ``token_cipher`` 一起比
    ——这正是"更新既有行时我们不碰那一列，因此也无权要求它等于我们的值"这条语义在
    比对层的落点。
    """
    return tuple(
        name
        for name in CREATED_FIELD_NAMES
        if name in expected and readback_text(actual.get(name)) != expected[name]
    )
