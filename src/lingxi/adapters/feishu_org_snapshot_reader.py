"""组织快照的读取编排：递归遍历两条身份路径，组装可提交的 :class:`SnapshotBatch`。

Issue #250：`feishu_org_*` 四张快照表当前全空，且产品侧没有任何东西会写它们——
`adapters/postgres_identity.py` 的 ``PostgresOrgSnapshotStore`` 与
``adapters/feishu_directory.py`` 的 ``FeishuDirectoryClient`` 都已就绪，缺的是把
"两条身份路径各自递归读一遍、拼成一轮完整快照"这件事做出来的那一层。

**两条路径缺一不可**（``core/identity/org_snapshot.TenantScope`` 的字段名已经说明
这一点：``app_member_keys`` / ``user_member_keys``）：

- **应用身份路径**（``directory/v1/collaboration_tenants`` + ``share_entities``）——
  只用来交出每个租户的成员键集合，用于与用户路径交叉校验；
- **专用授权用户身份路径**（``trust_party/v1/collaboration_tenants`` +
  ``visible_organization``）——交出完整的部门与成员标准化投影（``SnapshotMember`` /
  ``SnapshotDepartment``），这是首次开通链身份定位真正要用的那一份。

两条路径独立看到的成员集合必须相等，是 2026-07-28 实况问题（8 个关联租户中 2 个
返回 0 条成员，Issue #16）的正式校验，落在
``core/identity/org_snapshot.verify_batch`` 的 ``PATH_SETS_DIFFER`` /
``EMPTY_TENANT``。**本模块不做这项校验**，也不做"完整性不通过怎么办"的判断——那是
``core/`` 的职责，也是 ``PostgresOrgSnapshotStore.commit_batch`` 已经在调用的既有
逻辑（不通过就不提交半轮快照）。本模块只负责**如实**组装两条路径各自读到的内容，
包括看漏、看少的那些情况——校验层要靠"如实"才能真正发挥作用；这里但凡替调用方
猜一个"应该是对的"默认值，校验层就失去了意义。

**任何一次分页 / 递归调用失败都会原样向上抛出**，不吞、不重试、不返回半截结果：
调用方（``apps/scheduler/org_snapshot_sync.py``）据此不调用
``commit_batch``，上一份完成批次原样保留（"空源 / 半页 / 超时不得替换基线"）。

不入库、不日志：本模块只处理组织资料（部门名、成员标识、姓名），不接触任何令牌或
凭据；令牌只作为调用参数原样转给 :class:`FeishuDirectoryClient`。
"""

from __future__ import annotations

from typing import Any, Mapping

from lingxi.adapters.feishu_directory import FeishuDirectoryClient, department_identifier
from lingxi.core.identity.org_snapshot import SnapshotBatch, SnapshotDepartment, SnapshotMember, TenantScope

# 单个租户部门树的安全遍历上界。真实规模约 25 个部门/租户（Issue #250 编排者
# 2026-08-19 实测，8 个租户合计约 710 名成员），这里留出远超真实规模的余量，
# 只为防止 page_token / 部门树本身异常（例如互相成环）导致的死循环——撞上它说明
# 响应形状有问题，不是"这个租户部门特别多"。
MAX_DEPARTMENTS_PER_TENANT = 2000

# 飞书共享范围递归遍历的根部门标识。应用身份路径必须显式传它作为起点，接口会
# 连带返回一条 ``open_department_id`` 等于它自己的伪根记录，遍历时必须排除，
# 否则会把"递归入口本身"误当成一个真实子部门重新入队（Issue #250 编排者
# 2026-08-19 实测）。
ROOT_DEPARTMENT_ID = "0"


class OrgSnapshotReadError(RuntimeError):
    """读取阶段的形状问题（与 :class:`FeishuDirectoryError` 并列，不改写它）。"""


def _tenant_key(record: Mapping[str, Any]) -> str | None:
    for field in ("tenant_key", "target_tenant_key"):
        value = record.get(field)
        if isinstance(value, str) and value:
            return value
    return None


def _text(value: Any) -> str | None:
    """取字符串字段；也接受 ``{"default_value": "..."}`` 形态（应用身份路径的成员
    姓名按编排者 2026-08-19 实测是这个结构，用户身份路径的既有实现已验证是纯字符串——
    两条路径分别按各自已确认的形状解析，这里的双形态兼容只服务应用路径可能出现的
    结构化姓名，不改变用户路径的既有行为）。"""

    if isinstance(value, str):
        text = value.strip()
        return text or None
    if isinstance(value, Mapping):
        candidate = value.get("default_value")
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return None


def _walk_app_scope(client: FeishuDirectoryClient, *, token: str, tenant_key: str) -> frozenset[str]:
    """应用身份路径：递归遍历共享范围，只交出成员键集合（与用户路径交叉校验用）。

    键取 ``open_user_id``——参照已受控验证的 ``scripts/sync_feishu_org_snapshot.py``
    对同一字段的处理（该脚本把 ``open_user_id`` 与用户路径的 ``open_id`` 视为同一个
    标识类型），因此这里产出的集合与 :func:`_walk_user_scope` 产出的 ``open_id``
    集合在同一个租户下应当逐值相等。
    """

    member_keys: set[str] = set()
    visited: set[str] = set()
    queue: list[str] = [ROOT_DEPARTMENT_ID]
    while queue:
        if len(visited) > MAX_DEPARTMENTS_PER_TENANT:
            raise OrgSnapshotReadError("app_scope_department_limit_exceeded")
        department_id = queue.pop(0)
        if department_id in visited:
            continue
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
            if child_id not in visited:
                queue.append(child_id)
        for entity in members:
            key = entity.get("open_user_id")
            if not isinstance(key, str) or not key:
                raise OrgSnapshotReadError("app_scope_member_id_missing")
            member_keys.add(key)
    return frozenset(member_keys)


def _walk_user_scope(
    client: FeishuDirectoryClient, *, token: str, tenant_key: str
) -> tuple[tuple[SnapshotDepartment, ...], dict[str, SnapshotMember]]:
    """专用授权用户身份路径：递归遍历可见范围，交出完整的部门与成员标准化投影。"""

    departments: list[SnapshotDepartment] = []
    # open_id -> 已知直属部门名集合，最终折进 SnapshotMember.department_names。
    member_department_names: dict[str, set[str]] = {}
    members: dict[str, SnapshotMember] = {}
    department_names: dict[str, str] = {}
    # 同一个部门可能是多个父部门共同的子部门（共享的组织节点），因此会在两次不同
    # 层级的列表响应里各出现一次；`visited` 只防止**队列重复处理同一层级**，不足以
    # 防止 SnapshotDepartment 行本身重复——数据库对 `(sync_run_id, tenant_key,
    # department_key)` 有唯一约束，同一个 department_key 出现两行会让整轮提交在写库
    # 阶段失败。这里单独跟踪"已经产出过行的 department_key"，每个部门只落一行。
    seen_department_keys: set[str] = set()
    visited: set[tuple[str, str] | None] = set()
    queue: list[tuple[str, str] | None] = [None]

    while queue:
        if len(visited) > MAX_DEPARTMENTS_PER_TENANT:
            raise OrgSnapshotReadError("user_scope_department_limit_exceeded")
        current = queue.pop(0)
        if current in visited:
            continue
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
            identifier = department_identifier(entity)
            if identifier is None:
                raise OrgSnapshotReadError("user_scope_department_id_missing")
            key, _ = identifier
            name = _text(entity.get("name"))
            department_names[key] = name or key
            if key not in seen_department_keys:
                seen_department_keys.add(key)
                departments.append(SnapshotDepartment(tenant_key=tenant_key, department_key=key, name=name))
            if identifier not in visited:
                queue.append(identifier)

        for entity in entity_members:
            open_id = _text(entity.get("open_id"))
            user_id = _text(entity.get("user_id"))
            union_id = _text(entity.get("union_id"))
            display_name = _text(entity.get("name"))
            if not (open_id and user_id and union_id and display_name):
                # 不猜、不用姓名回退——三类标识 100% 可得已复验（Issue #16），一旦
                # 出现缺口就是需要人看一眼的异常，不是本模块该悄悄补全的情况。
                raise OrgSnapshotReadError("user_scope_member_identity_incomplete")
            members[open_id] = SnapshotMember(
                tenant_key=tenant_key,
                member_key=open_id,
                open_id=open_id,
                user_id=user_id,
                union_id=union_id,
                display_name=display_name,
            )
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


def read_org_snapshot(
    *, client: FeishuDirectoryClient, app_token: str, user_token: str
) -> SnapshotBatch:
    """遍历全部关联租户，组装一轮**未经批次完整性校验**的快照。

    调用方必须自己把结果交给
    ``PostgresOrgSnapshotStore.commit_batch``（内部调用
    ``core.identity.org_snapshot.require_complete_batch``）——不通过就不提交，
    这条纪律不在本函数内重复。

    **租户发现以应用身份路径为准**（``list_collaboration_tenants_as_app``）：与
    ``scripts/sync_feishu_org_snapshot.py`` 已受控验证过的次序一致——先枚举应用身份
    能看到的全部租户，再逐个核对用户身份路径是否也看得到。一个只对用户身份可见、
    对应用身份不可见的租户不会出现在结果里；这种情况在当前产品形态下没有出现过
    （应用与专用授权主体加入的是同一批关联组织），如果将来出现，`EMPTY_TENANT` /
    `PATH_SETS_DIFFER` 判据仍然会在应用身份那一侧因为看不到而整体拒绝——只是拒绝
    原因会记成"应用路径没这个租户"而不是一个专门的新分类，因为当前判据表里没有
    `TENANT_NOT_APP_VISIBLE` 这一类（`core/identity/org_snapshot.py` 未提供，
    本模块不新造判据）。
    """

    app_tenant_keys = {
        key
        for key in (_tenant_key(record) for record in client.list_collaboration_tenants_as_app(token=app_token))
        if key
    }
    user_tenant_keys = {
        key
        for key in (_tenant_key(record) for record in client.list_collaboration_tenants(token=user_token))
        if key
    }

    tenants: list[TenantScope] = []
    departments: list[SnapshotDepartment] = []
    members: list[SnapshotMember] = []

    for tenant_key in sorted(app_tenant_keys):
        app_member_keys = _walk_app_scope(client, token=app_token, tenant_key=tenant_key)
        visible = tenant_key in user_tenant_keys
        user_member_keys: frozenset[str] = frozenset()
        if visible:
            tenant_departments, tenant_members = _walk_user_scope(
                client, token=user_token, tenant_key=tenant_key
            )
            departments.extend(tenant_departments)
            members.extend(tenant_members.values())
            user_member_keys = frozenset(tenant_members)
        tenants.append(
            TenantScope(
                tenant_key=tenant_key,
                visible_to_user_identity=visible,
                app_member_keys=app_member_keys,
                user_member_keys=user_member_keys,
            )
        )

    return SnapshotBatch(tenants=tuple(tenants), departments=tuple(departments), members=tuple(members))
