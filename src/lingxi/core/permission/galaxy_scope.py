"""银河权限快照的连接与解释：两条从 user_id 出发、互不约束的授权链。

```
galaxy_user.user_id ─┬─< galaxy_user_role >─ role_id ─< galaxy_role_menu    ← 职能范围
                     └─< galaxy_user_datacountry >─ country_key ─< galaxy_country  ← 公司范围
```

三个必须守住的取值陷阱：`user_role`/`sys_user_datacountry` 里的姓名列不是登录
账号，本模块只接受 `user_id`；公司范围的连接键是 `sys_country.country_key`，
不是主键 `id`；菜单一律以 `menu_id` 为准，`menu_name` 存在同名不同 id。

`全非`（`country_key=0` / `name=ALL` / `name_cn=全非`）解释为「所有国家所有公司」
的通配语义。展开只发生在解释层，落库保留原始行。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
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
        _required_text(row.get("role_id"))
        for row in user_role_rows
        if _required_text(row.get("user_id")) == key
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


def menu_ids_for_roles(
    role_ids: Iterable[str], role_menu_rows: Iterable[Mapping[str, Any]]
) -> tuple[str, ...]:
    """菜单权限的唯一判定键集合。"""
    return tuple(entry.menu_id for entry in menu_entries_for_roles(role_ids, role_menu_rows))


def role_names_for_user(
    user_id: str, user_role_rows: Iterable[Mapping[str, Any]]
) -> tuple[str, ...]:
    """该账号持有的角色名（自由文本）。

    角色名只用于职能标签映射（见 `role_function`），不用于连接，也不能反推公司范围。
    """
    key = _required_text(user_id)
    if not key:
        return ()
    return _unique(
        _required_text(row.get("role_name"))
        for row in user_role_rows
        if _required_text(row.get("user_id")) == key
    )


def country_keys_for_user(
    user_id: str, datacountry_rows: Iterable[Mapping[str, Any]]
) -> tuple[str, ...]:
    """按 `user_id` 取该账号被授权的国家键（源列 `USER_ID` / `DATACOUNTRY_ID`）。"""
    key = _required_text(user_id)
    if not key:
        return ()
    return _unique(
        _required_text(row.get("datacountry_id"))
        for row in datacountry_rows
        if _required_text(row.get("user_id")) == key
    )


def _index_countries(
    country_rows: Iterable[Mapping[str, Any]],
) -> tuple[dict[str, CountryScope], list[CountryScope]]:
    """按 `country_key` 建索引；没有 key 的行（约五分之一）单独收集，不丢弃。

    它们同样携带 `boss_company_id`：`全非` 展开时若静默丢弃，会让持有通配的
    账号系统性少拿这部分公司的权限。
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
    return by_country_key, unkeyed


def _validate_sentinel(country_rows: Iterable[Mapping[str, Any]]) -> bool:
    """哨兵行（`country_key=0`）本身是否形态正确（唯一、`name=ALL`、`name_cn=全非`）。

    授权表里出现 0 但哨兵行缺失或损坏，说明快照不可信，必须失败关闭（把 0 记为
    unresolved），绝不失败开放为全公司。
    """
    sentinel_rows = [
        row for row in country_rows if _text(row.get("country_key")) == SENTINEL_COUNTRY_KEY
    ]
    return (
        len(sentinel_rows) == 1
        and _text(sentinel_rows[0].get("name")) == "ALL"
        and _text(sentinel_rows[0].get("name_cn")) == "全非"
    )


def _resolve_countries(
    explicit: tuple[str, ...],
    by_country_key: Mapping[str, CountryScope],
    unkeyed: Sequence[CountryScope],
    all_countries: bool,
) -> tuple[CountryScope, ...]:
    if all_countries:
        return tuple(
            scope for key, scope in by_country_key.items() if key != SENTINEL_COUNTRY_KEY
        ) + tuple(unkeyed)
    # 哨兵不是国家：显式路径永不把 key=0 的行当作可用范围返回，即使哨兵本身
    # 损坏也不能让它顶着「全非」的名字混进结果。
    return tuple(
        by_country_key[key]
        for key in explicit
        if key != SENTINEL_COUNTRY_KEY and key in by_country_key
    )


def _unresolved_keys(
    explicit: tuple[str, ...], by_country_key: Mapping[str, CountryScope], sentinel_valid: bool
) -> tuple[str, ...]:
    # 连不上的键无论是否持有通配都要留痕：它是导出与解释之间的不一致信号。
    return tuple(
        key
        for key in explicit
        if (key != SENTINEL_COUNTRY_KEY and key not in by_country_key)
        or (key == SENTINEL_COUNTRY_KEY and not sentinel_valid)
    )


def resolve_company_scope(
    country_keys: Iterable[str], country_rows: Iterable[Mapping[str, Any]]
) -> CompanyScope:
    """把授权到的国家键解释成公司范围。

    连接键是 `country_key`，不是主键 `id`；通配（`全非`）只有在哨兵行本身验证
    通过时才展开，见 :func:`_validate_sentinel`。
    """
    country_rows = list(country_rows)
    by_country_key, unkeyed = _index_countries(country_rows)
    explicit = _unique(_required_text(key) for key in country_keys)
    sentinel_valid = _validate_sentinel(country_rows)
    all_countries = SENTINEL_COUNTRY_KEY in explicit and sentinel_valid

    return CompanyScope(
        all_countries=all_countries,
        explicit_country_keys=explicit,
        countries=_resolve_countries(explicit, by_country_key, unkeyed, all_countries),
        unresolved_country_keys=_unresolved_keys(explicit, by_country_key, sentinel_valid),
    )
