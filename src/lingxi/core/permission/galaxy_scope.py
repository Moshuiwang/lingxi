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

    连接键是 `country_key`；`sys_country` 中约五分之一的行没有 `country_key`，
    这些行无法从授权表到达，出现时记入 `unresolved_country_keys` 而不是静默丢弃。
    """

    by_country_key: dict[str, CountryScope] = {}
    for row in country_rows:
        country_key = _text(row.get("country_key"))
        if country_key is None:
            continue
        by_country_key.setdefault(
            country_key,
            CountryScope(
                country_key=country_key,
                name=_text(row.get("name")),
                name_cn=_text(row.get("name_cn")),
                boss_company_id=_text(row.get("boss_company_id")),
            ),
        )

    explicit = _unique(_required_text(key) for key in country_keys)
    all_countries = SENTINEL_COUNTRY_KEY in explicit

    if all_countries:
        resolved = tuple(scope for key, scope in by_country_key.items() if key != SENTINEL_COUNTRY_KEY)
    else:
        resolved = tuple(by_country_key[key] for key in explicit if key in by_country_key)

    # 连不上的键无论是否持有通配都要留痕：它是导出与解释之间的不一致信号。
    unresolved = tuple(
        key for key in explicit if key != SENTINEL_COUNTRY_KEY and key not in by_country_key
    )

    return CompanyScope(
        all_countries=all_countries,
        explicit_country_keys=explicit,
        countries=resolved,
        unresolved_country_keys=unresolved,
    )
