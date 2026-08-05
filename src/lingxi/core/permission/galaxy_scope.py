"""银河权限快照的连接与解释：两条从 user_id 出发、互不约束的授权链。

```
galaxy_user.user_id ─┬─< galaxy_user_role >─ role_id ─< galaxy_role_menu    ← 职能范围
                     └─< galaxy_user_datacountry >─ country_key ─< galaxy_country  ← 公司范围
```

三个必须守住的取值陷阱（来源：docs/参考证据/银河用户权限数据结构.md）：

1. `user_role` / `sys_user_datacountry` 里的姓名列**不是登录账号**，实测值几乎全是
   中文姓名；按名称列连接会取到语义相反的值。本模块只接受 `user_id`。
2. 公司范围的连接键是 `sys_country.country_key`，**不是主键 `id`**；两者在真实
   导出中几乎完全对不上。
3. 菜单一律以 `menu_id` 为准：`menu_name` 存在同名不同 id。

`全非`（`country_key=0` / `name=ALL` / `name_cn=全非`）按产品负责人 2026-08-05
决策 1 解释为「所有国家所有公司」的通配语义。**展开只发生在解释层**，落库保留
原始行；向用户展示公司范围属于 S5，不在本切片。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

# `sys_country` 中的哨兵行：它不是一个国家，而是「全部」的标记。
SENTINEL_COUNTRY_KEY = "0"


@dataclass(frozen=True)
class MenuEntry:
    """一个菜单授权项。`menu_id` 是唯一判定依据，`menu_name` 只用于展示。"""

    menu_id: str
    menu_name: str | None


@dataclass(frozen=True)
class CountryScope:
    """一个可连接到的国家，以及它在下游业务系统里的公司编号。"""

    country_key: str
    name: str | None
    name_cn: str | None
    boss_company_id: str | None


@dataclass(frozen=True)
class CompanyScope:
    """公司范围的解释结果。

    `explicit_country_keys` 保留授权表原样（含哨兵），`countries` 是展开后的国家。
    `all_countries` 为真时表示持有 `全非` 通配。
    """

    all_countries: bool
    explicit_country_keys: tuple[str, ...]
    countries: tuple[CountryScope, ...]
    unresolved_country_keys: tuple[str, ...]


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _required_text(value: Any) -> str:
    text = _text(value)
    return text or ""


def _unique(values: Iterable[str]) -> tuple[str, ...]:
    seen: dict[str, None] = {}
    for value in values:
        if value:
            seen.setdefault(value, None)
    return tuple(seen)


def role_ids_for_user(user_id: str, user_role_rows: Iterable[Mapping[str, Any]]) -> tuple[str, ...]:
    """按 `user_id` 取该账号持有的角色，保持首次出现顺序。

    只读 `user_id` 列。`source_user_name` 存在于行里也不参与判定——列名误导是
    这张表最危险的地方。
    """

    key = _required_text(user_id)
    if not key:
        return ()
    return _unique(
        _required_text(row.get("role_id")) for row in user_role_rows if _required_text(row.get("user_id")) == key
    )


def menu_entries_for_roles(
    role_ids: Iterable[str], role_menu_rows: Iterable[Mapping[str, Any]]
) -> tuple[MenuEntry, ...]:
    """按 `role_id` 取菜单授权项，按 `menu_id` 去重（同名不同 id 各算一项）。"""

    wanted = {_required_text(role_id) for role_id in role_ids if _required_text(role_id)}
    entries: dict[str, MenuEntry] = {}
    for row in role_menu_rows:
        if _required_text(row.get("role_id")) not in wanted:
            continue
        menu_id = _required_text(row.get("menu_id"))
        if not menu_id:
            continue
        entries.setdefault(menu_id, MenuEntry(menu_id, _text(row.get("menu_name"))))
    return tuple(entries.values())


def menu_ids_for_roles(role_ids: Iterable[str], role_menu_rows: Iterable[Mapping[str, Any]]) -> tuple[str, ...]:
    """菜单权限的唯一判定键集合。"""

    return tuple(entry.menu_id for entry in menu_entries_for_roles(role_ids, role_menu_rows))


def role_names_for_user(user_id: str, user_role_rows: Iterable[Mapping[str, Any]]) -> tuple[str, ...]:
    """该账号持有的角色名（自由文本）。

    角色名只用于职能标签映射（见 `role_function`），不用于连接，也不能反推公司范围。
    """

    key = _required_text(user_id)
    if not key:
        return ()
    return _unique(
        _required_text(row.get("role_name")) for row in user_role_rows if _required_text(row.get("user_id")) == key
    )


def country_keys_for_user(user_id: str, datacountry_rows: Iterable[Mapping[str, Any]]) -> tuple[str, ...]:
    """按 `user_id` 取该账号被授权的国家键（源列 `USER_ID` / `DATACOUNTRY_ID`）。"""

    key = _required_text(user_id)
    if not key:
        return ()
    return _unique(
        _required_text(row.get("datacountry_id"))
        for row in datacountry_rows
        if _required_text(row.get("user_id")) == key
    )


def resolve_company_scope(
    country_keys: Iterable[str], country_rows: Iterable[Mapping[str, Any]]
) -> CompanyScope:
    """把授权到的国家键解释成公司范围。

    连接键是 `country_key`。`sys_country` 中约五分之一的行没有 `country_key`：
    按键连接时它们无法从授权表到达；但 `全非` 的语义是「所有国家所有公司」
    （2026-08-05 决策），通配展开必须把这些行也包含进来——它们同样携带
    `boss_company_id`，静默丢弃会让持有通配的近半数账号系统性少拿约五分之一
    公司的权限（独立复查发现）。
    """

    by_country_key: dict[str, CountryScope] = {}
    unkeyed: list[CountryScope] = []
    for row in country_rows:
        country_key = _text(row.get("country_key"))
        scope = CountryScope(
            country_key=country_key,
            name=_text(row.get("name")),
            name_cn=_text(row.get("name_cn")),
            boss_company_id=_text(row.get("boss_company_id")),
        )
        if not country_key:
            unkeyed.append(scope)
            continue
        by_country_key.setdefault(country_key, scope)

    explicit = _unique(_required_text(key) for key in country_keys)
    # 通配只有在哨兵行本身验证通过时才展开：授权表里出现 0、但快照里的哨兵行
    # 缺失或形态损坏（name/name_cn 不是 ALL/全非）说明快照不可信——此时必须
    # **失败关闭**（把 0 记为 unresolved），绝不失败开放为全公司（终轮 Codex）。
    sentinel_rows = [
        row for row in country_rows if _text(row.get("country_key")) == SENTINEL_COUNTRY_KEY
    ]
    sentinel_valid = (
        len(sentinel_rows) == 1
        and _text(sentinel_rows[0].get("name")) == "ALL"
        and _text(sentinel_rows[0].get("name_cn")) == "全非"
    )
    all_countries = SENTINEL_COUNTRY_KEY in explicit and sentinel_valid

    if all_countries:
        resolved = (
            tuple(scope for key, scope in by_country_key.items() if key != SENTINEL_COUNTRY_KEY)
            + tuple(unkeyed)
        )
    else:
        # 哨兵不是国家：显式路径永不把 key=0 的行当作可用范围返回——
        # 哨兵损坏时它会顶着「全非」的名字混进结果（终轮 Codex 的失败开放面）。
        resolved = tuple(
            by_country_key[key]
            for key in explicit
            if key != SENTINEL_COUNTRY_KEY and key in by_country_key
        )

    # 连不上的键无论是否持有通配都要留痕：它是导出与解释之间的不一致信号。
    unresolved = tuple(
        key
        for key in explicit
        if (key != SENTINEL_COUNTRY_KEY and key not in by_country_key)
        or (key == SENTINEL_COUNTRY_KEY and not sentinel_valid)
    )

    return CompanyScope(
        all_countries=all_countries,
        explicit_country_keys=explicit,
        countries=resolved,
        unresolved_country_keys=unresolved,
    )
