"""组织快照的读取编排：递归遍历两条身份路径，组装可提交的 :class:`SnapshotBatch`。

**两条路径缺一不可**：应用身份路径只交出每个租户的成员键集合与部门键
集合，用于与用户路径交叉校验；专用授权用户身份路径交出完整的部门与成员
标准化投影，是首次开通链身份定位真正要用的那一份。两条路径独立看到的
成员集合必须相等，校验落在 ``core/identity/org_snapshot.verify_batch``——
**本模块不做这项校验**，只负责**如实**组装两条路径各自读到的内容：这里
但凡替调用方猜一个"应该是对的"默认值，校验层就失去了意义。

**任何一次分页/递归调用失败都会原样向上抛出**，本模块自己不吞、不重试、
不返回半截结果：调用方据此不调用 ``commit_batch``，上一份完成批次原样
保留。不入库、不日志：只处理组织资料，令牌只作为调用参数原样转给
:class:`FeishuDirectoryClient`。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from lingxi.adapters.feishu_directory import FeishuDirectoryClient, department_identifier
from lingxi.core.identity.org_snapshot import (
    SnapshotBatch,
    SnapshotDepartment,
    SnapshotMember,
    TenantScope,
)

# 单个租户部门树的安全遍历上界。留出远超真实规模的余量，只为防止
# page_token / 部门树本身异常（例如互相成环）导致的死循环——撞上它说明
# 响应形状有问题，不是"这个租户部门特别多"。
MAX_DEPARTMENTS_PER_TENANT = 2000

# 飞书共享范围递归遍历的根部门标识。应用身份路径必须显式传它作为起点，接口会
# 连带返回一条 ``open_department_id`` 等于它自己的伪根记录，遍历时必须排除，
# 否则会把"递归入口本身"误当成一个真实子部门重新入队。
ROOT_DEPARTMENT_ID = "0"


class OrgSnapshotReadError(RuntimeError):
    """读取阶段的形状问题（与 :class:`FeishuDirectoryError` 并列，不改写它）。

    ``code`` 与后者同一个使用约定：一律是本模块自己写死的安全分类字符串
    （如 ``"app_scope_department_id_missing"``），不含响应正文、令牌或 URL，
    供审计以 ``getattr(error, "code", None)`` 鸭子类型读取。
    """

    def __init__(self, code: str) -> None:
        """记录安全分类字符串；不接受任何可能含敏感信息的自由文本。"""
        super().__init__(code)
        self.code = code


def _tenant_key(record: Mapping[str, Any]) -> str | None:
    for field in ("tenant_key", "target_tenant_key"):
        value = record.get(field)
        if isinstance(value, str) and value:
            return value
    return None


def _text(value: Any) -> str | None:
    """取字符串字段；也接受 ``{"default_value": "..."}`` 形态。

    应用身份路径的成员姓名是这个结构，用户身份路径已验证是纯字符串——两条
    路径分别按各自已确认的形状解析，双形态兼容不改变用户路径的既有行为。
    """
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if isinstance(value, Mapping):
        candidate = value.get("default_value")
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return None


def _walk_app_scope(
    client: FeishuDirectoryClient, *, token: str, tenant_key: str
) -> tuple[frozenset[str], frozenset[str]]:
    """应用身份路径：递归遍历共享范围，交出 ``(部门键集合, 成员键集合)``。

    成员键取 ``open_user_id``，与用户路径 :func:`_walk_user_scope` 产出的
    ``open_id`` 视为同一标识类型；部门键取 ``open_department_id``，与
    :func:`department_identifier` 优先选用的 ID 类型一致——两条路径产出的
    集合在同一租户下应当逐值相等，这是交叉校验的输入。只交出成员集合会
    漏掉"用户路径没走到的空部门"，因此部门集合也要交出来。
    """
    department_keys: set[str] = set()
    member_keys: set[str] = set()
    visited: set[str] = set()
    queue: list[str] = [ROOT_DEPARTMENT_ID]
    while queue:
        department_id = queue.pop(0)
        if department_id in visited:
            continue
        # 差一修正（F7）：检查放在"确定要新增一个已访问节点"之前，边界是
        # "恰好撞满上界"也要报错，而不是"下一次入队才发现已经超了"——队列在
        # 撞满的那一刻可能已经清空，导致 > 上界的判据永远等不到下一轮循环。
        if len(visited) >= MAX_DEPARTMENTS_PER_TENANT:
            raise OrgSnapshotReadError("app_scope_department_limit_exceeded")
        visited.add(department_id)
        departments, members = client.list_share_entities(
            token=token, tenant_key=tenant_key, department_id=department_id
        )
        for entity in departments:
            child_id = entity.get("open_department_id")
            if not isinstance(child_id, str) or not child_id:
                raise OrgSnapshotReadError("app_scope_department_id_missing")
            if child_id == ROOT_DEPARTMENT_ID:
                # 伪根记录：递归入口本身被原样回显了一遍，不是真的子部门。
                continue
            department_keys.add(child_id)
            if child_id not in visited:
                queue.append(child_id)
        for entity in members:
            key = entity.get("open_user_id")
            if not isinstance(key, str) or not key:
                raise OrgSnapshotReadError("app_scope_member_id_missing")
            member_keys.add(key)
    return frozenset(department_keys), frozenset(member_keys)


def _record_department(
    entity: Mapping[str, Any],
    *,
    tenant_key: str,
    department_names: dict[str, str],
    seen_department_keys: set[str],
    departments: list[SnapshotDepartment],
) -> tuple[str, str]:
    """处理一条部门实体：登记显示名，首次出现时追加一行，返回其 identifier。

    显示名取 ``department_name``——``visible_organization`` 列表接口的
    ``name`` 字段恒为空。同一个部门可能是多个父部门共同的子部门，会在不同
    层级的响应里各出现一次；``seen_department_keys`` 单独跟踪"已经产出过行
    的 key"，每个部门只落一行（数据库对 department_key 有唯一约束）。
    """
    identifier = department_identifier(entity)
    if identifier is None:
        raise OrgSnapshotReadError("user_scope_department_id_missing")
    key, _ = identifier
    name = _text(entity.get("department_name"))
    department_names[key] = name or key
    if key not in seen_department_keys:
        seen_department_keys.add(key)
        departments.append(SnapshotDepartment(tenant_key=tenant_key, department_key=key, name=name))
    return identifier


def _record_member(
    entity: Mapping[str, Any], *, tenant_key: str, members: dict[str, SnapshotMember]
) -> str:
    """处理一条成员实体：四类标识须齐全且互不矛盾，登记后返回 ``open_id``。

    字段名是 ``open_user_id``/``user_id``/``union_user_id``/``user_name``
    （不是 ``open_id``/``union_id``/``name``，那三个在真实响应里恒为空）。
    不猜、不用姓名回退：缺一项就是需要人看一眼的异常，不是本模块该悄悄
    补全的情况。同一 ``open_id`` 在不同页给出不同 ``user_id``/``union_id``
    是身份矛盾，不是"后面这条更新"——先比对稳定标识本身再合并。
    """
    open_id = _text(entity.get("open_user_id"))
    user_id = _text(entity.get("user_id"))
    union_id = _text(entity.get("union_user_id"))
    display_name = _text(entity.get("user_name"))
    if not (open_id and user_id and union_id and display_name):
        raise OrgSnapshotReadError("user_scope_member_identity_incomplete")
    existing = members.get(open_id)
    if existing is not None and (existing.user_id != user_id or existing.union_id != union_id):
        raise OrgSnapshotReadError("user_scope_member_identity_conflict")
    members[open_id] = SnapshotMember(
        tenant_key=tenant_key,
        member_key=open_id,
        open_id=open_id,
        user_id=user_id,
        union_id=union_id,
        display_name=display_name,
    )
    return open_id


def _walk_user_scope(
    client: FeishuDirectoryClient, *, token: str, tenant_key: str
) -> tuple[tuple[SnapshotDepartment, ...], dict[str, SnapshotMember]]:
    """专用授权用户身份路径：递归遍历可见范围，交出完整的部门与成员标准化投影。"""
    departments: list[SnapshotDepartment] = []
    # open_id -> 已知直属部门名集合，最终折进 SnapshotMember.department_names。
    member_department_names: dict[str, set[str]] = {}
    members: dict[str, SnapshotMember] = {}
    department_names: dict[str, str] = {}
    seen_department_keys: set[str] = set()
    visited: set[tuple[str, str] | None] = set()
    queue: list[tuple[str, str] | None] = [None]

    while queue:
        current = queue.pop(0)
        if current in visited:
            continue
        # 差一修正：检查放在"确定要新增一个已访问节点"之前，用 `>=`，否则
        # 恰好撞满上界、队列随后清空的那一轮永远等不到下一次循环入口去报错。
        if len(visited) >= MAX_DEPARTMENTS_PER_TENANT:
            raise OrgSnapshotReadError("user_scope_department_limit_exceeded")
        visited.add(current)
        if current is None:
            entity_departments, entity_members = client.list_visible_organization(
                token=token, tenant_key=tenant_key
            )
            current_name: str | None = None
        else:
            department_id, department_id_type = current
            entity_departments, entity_members = client.list_visible_organization(
                token=token,
                tenant_key=tenant_key,
                department_id=department_id,
                department_id_type=department_id_type,
            )
            current_name = department_names.get(department_id)

        for entity in entity_departments:
            identifier = _record_department(
                entity,
                tenant_key=tenant_key,
                department_names=department_names,
                seen_department_keys=seen_department_keys,
                departments=departments,
            )
            if identifier not in visited:
                queue.append(identifier)

        for entity in entity_members:
            open_id = _record_member(entity, tenant_key=tenant_key, members=members)
            if current_name:
                member_department_names.setdefault(open_id, set()).add(current_name)

    finalized: dict[str, SnapshotMember] = {}
    for open_id, member in members.items():
        names = member_department_names.get(open_id)
        finalized[open_id] = member if not names else _with_department_names(member, names)
    return tuple(departments), finalized


def _with_department_names(member: SnapshotMember, names: set[str]) -> SnapshotMember:
    return SnapshotMember(
        tenant_key=member.tenant_key,
        member_key=member.member_key,
        open_id=member.open_id,
        user_id=member.user_id,
        union_id=member.union_id,
        display_name=member.display_name,
        display_name_locale=member.display_name_locale,
        department_names=tuple(sorted(names)),
    )


def _tenant_keys(records: list[dict[str, Any]], *, error_code: str) -> set[str]:
    """把租户列表逐条解析成租户键集合；任何一条取不到合法键立即抛错。

    不猜、不过滤：静默丢弃取不到键的记录会让批次提交一份缩小的基线，而
    不是一次应该被人看到的形状异常，与模块文档顶部的纪律一致。
    """
    keys: set[str] = set()
    for record in records:
        key = _tenant_key(record)
        if not key:
            raise OrgSnapshotReadError(error_code)
        keys.add(key)
    return keys


def read_org_snapshot(
    *, client: FeishuDirectoryClient, app_token: str, user_token: str
) -> SnapshotBatch:
    """遍历全部关联租户，组装一轮**未经批次完整性校验**的快照。

    调用方必须自己把结果交给 ``PostgresOrgSnapshotStore.commit_batch``——
    不通过就不提交。**租户发现遍历两条身份路径各自看到的租户键的并集**，
    不是只看应用身份路径；看不到的一侧显式置空集合，不猜、不用另一侧的值
    回填，两侧不对等时交给 ``verify_batch`` 挡住。
    """
    app_tenant_keys = _tenant_keys(
        client.list_collaboration_tenants_as_app(token=app_token),
        error_code="app_scope_tenant_key_missing",
    )
    user_tenant_keys = _tenant_keys(
        client.list_collaboration_tenants(token=user_token),
        error_code="user_scope_tenant_key_missing",
    )

    tenants: list[TenantScope] = []
    departments: list[SnapshotDepartment] = []
    members: list[SnapshotMember] = []

    for tenant_key in sorted(app_tenant_keys | user_tenant_keys):
        tenants.append(
            _read_tenant_scope(
                client,
                app_token=app_token,
                user_token=user_token,
                tenant_key=tenant_key,
                visible_to_app=tenant_key in app_tenant_keys,
                visible_to_user=tenant_key in user_tenant_keys,
                departments=departments,
                members=members,
            )
        )

    return SnapshotBatch(
        tenants=tuple(tenants), departments=tuple(departments), members=tuple(members)
    )


def _read_tenant_scope(
    client: FeishuDirectoryClient,
    *,
    app_token: str,
    user_token: str,
    tenant_key: str,
    visible_to_app: bool,
    visible_to_user: bool,
    departments: list[SnapshotDepartment],
    members: list[SnapshotMember],
) -> TenantScope:
    """读一个租户在两条身份路径下各自可见的部门/成员键集合，追加进共享列表。

    只在相应身份能看到这个租户时才去读那条路径；看不到的一侧保持空集合。
    """
    app_department_keys: frozenset[str] = frozenset()
    app_member_keys: frozenset[str] = frozenset()
    if visible_to_app:
        app_department_keys, app_member_keys = _walk_app_scope(
            client, token=app_token, tenant_key=tenant_key
        )

    user_department_keys: frozenset[str] = frozenset()
    user_member_keys: frozenset[str] = frozenset()
    if visible_to_user:
        tenant_departments, tenant_members = _walk_user_scope(
            client, token=user_token, tenant_key=tenant_key
        )
        departments.extend(tenant_departments)
        members.extend(tenant_members.values())
        user_department_keys = frozenset(d.department_key for d in tenant_departments)
        user_member_keys = frozenset(tenant_members)

    return TenantScope(
        tenant_key=tenant_key,
        visible_to_user_identity=visible_to_user,
        app_member_keys=app_member_keys,
        user_member_keys=user_member_keys,
        app_department_keys=app_department_keys,
        user_department_keys=user_department_keys,
    )
