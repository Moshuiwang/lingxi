"""组织快照的读取编排：递归遍历两条身份路径，组装可提交的 :class:`SnapshotBatch`。

Issue #250：`feishu_org_*` 四张快照表当前全空，且产品侧没有任何东西会写它们——
`adapters/postgres_identity.py` 的 ``PostgresOrgSnapshotStore`` 与
``adapters/feishu_directory.py`` 的 ``FeishuDirectoryClient`` 都已就绪，缺的是把
"两条身份路径各自递归读一遍、拼成一轮完整快照"这件事做出来的那一层。

**两条路径缺一不可**（``core/identity/org_snapshot.TenantScope`` 的字段名已经说明
这一点：``app_member_keys`` / ``user_member_keys``）：

- **应用身份路径**（``directory/v1/collaboration_tenants`` + ``share_entities``）——
  只用来交出每个租户的成员键集合与部门键集合，用于与用户路径交叉校验；
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

**任何一次分页 / 递归调用失败都会原样向上抛出**，本模块自己不吞、不重试、不返回
半截结果：调用方（``apps/scheduler/org_snapshot_sync.py``）据此不调用
``commit_batch``，上一份完成批次原样保留（"空源 / 半页 / 超时不得替换基线"）。
**更下一层的 :class:`~lingxi.adapters.feishu_directory.FeishuDirectoryClient` 会对
飞书的频率限制业务错误码做一道窄而有界的重试**（Issue #271，节流不足以完全避免
撞限的余量证据后补充）——那发生在 :class:`FeishuDirectoryClient` 内部，重试耗尽
后同样是原样抛出 :class:`~lingxi.adapters.feishu_directory.FeishuDirectoryError`，
本模块看到的仍然只是"成功"或"最终失败"两种结果，不知道、也不需要知道下面重试过
几次；这里的"不重试"说的是本模块自己不做额外重试，不是整条调用链路里禁止重试。

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
    """读取阶段的形状问题（与 :class:`FeishuDirectoryError` 并列，不改写它）。

    独立审查 2026-08-20 必修 B：此前没有 ``__init__``，7 处 ``raise`` 全部塌成
    ``org_snapshot_sync.read_failed`` 审计里的 ``error=OrgSnapshotReadError``——
    正是 F1 要消灭的"只剩类名、没有诊断信息"那个形状，只是发生在上一层（本模块）
    而不是 :class:`~lingxi.adapters.feishu_directory.FeishuDirectoryError`。这里
    的 ``code`` 与后者同一个使用约定：一律是本模块自己写死的安全分类字符串（如
    ``"app_scope_department_id_missing"``），不含响应正文、令牌或 URL；
    ``org_snapshot_sync.py`` 已有的 ``getattr(error, "code", None)`` 鸭子类型不用
    改就能拿到它。
    """

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


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


def _walk_app_scope(
    client: FeishuDirectoryClient, *, token: str, tenant_key: str
) -> tuple[frozenset[str], frozenset[str]]:
    """应用身份路径：递归遍历共享范围，交出 ``(部门键集合, 成员键集合)``。

    成员键取 ``open_user_id``——参照已受控验证的 ``scripts/sync_feishu_org_snapshot.py``
    对同一字段的处理（该脚本把 ``open_user_id`` 与用户路径的 ``open_id`` 视为同一个
    标识类型），因此这里产出的成员集合与 :func:`_walk_user_scope` 产出的 ``open_id``
    集合在同一个租户下应当逐值相等。

    部门键同样取 ``open_department_id``，与用户路径 :func:`department_identifier`
    优先选用的 ID 类型一致，因此两条路径产出的部门集合在同一个租户下也应当逐值
    相等——这是 F3 的交叉校验输入：只交出成员集合会漏掉"用户路径没走到的空部门"
    这一类问题（那类部门不含任何成员，成员集合两边照样能对上）。
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
        current = queue.pop(0)
        if current in visited:
            continue
        # 差一修正（F7）：见 `_walk_app_scope` 同一处注释——检查放在"确定要新增一个
        # 已访问节点"之前，用 `>=`，否则恰好撞满上界、队列随后清空的那一轮永远等不到
        # 下一次循环入口去报错。
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
            existing = members.get(open_id)
            if existing is not None and (existing.user_id != user_id or existing.union_id != union_id):
                # 同一个 open_id 在不同页给出不同 user_id / union_id 是身份矛盾，不是
                # "后面这条更新"——`dict` 赋值会静默覆盖，让矛盾的那一条悄悄穿过去写进
                # 基线（Issue #250 编排者复查 F5）。交叉校验只比 open_id 时看不出这种
                # 矛盾，这里在合并前先比对稳定标识本身。
                raise OrgSnapshotReadError("user_scope_member_identity_conflict")
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


def _tenant_keys(records: list[dict[str, Any]], *, error_code: str) -> set[str]:
    """把租户列表逐条解析成租户键集合；任何一条取不到合法键立即抛错。

    此前用 ``if key`` 静默丢弃取不到键的记录（Issue #250 编排者复查 F2）：只要还有
    别的有效租户，批次就不会被判成"无租户"，于是一个格式异常的记录被悄悄当成
    "这个租户不存在"处理，提交的是一份缩小的基线，而不是一次应该被人看到的形状
    异常。这里改成不猜、不过滤——取不到键就直接抛，交给调用方按读取失败处理
    （不吞、不重试、不返回半截结果，与模块文档顶部的纪律一致）。
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

    调用方必须自己把结果交给
    ``PostgresOrgSnapshotStore.commit_batch``（内部调用
    ``core.identity.org_snapshot.require_complete_batch``）——不通过就不提交，
    这条纪律不在本函数内重复。

    **租户发现遍历两条身份路径各自看到的租户键的并集**（Issue #250 编排者复查
    F1）：此前只遍历应用身份路径看到的租户（``app_tenant_keys``），一个只在用户
    身份路径可见、应用身份路径因半页 / 权限暂时收窄而漏看的租户会被整个排除在
    结果之外——路径不一致判据根本没有机会检查它，因为它压根不会出现在
    ``SnapshotBatch.tenants`` 里，那个租户的全部成员会从新基线里静默消失。

    现在遍历 ``app_tenant_keys | user_tenant_keys``：只在相应身份能看到这个租户时
    才去读那条路径，看不到的一侧**显式置空集合**（不猜、不用另一侧的值回填）。
    这样两侧集合不对等时会被 ``core.identity.org_snapshot.verify_batch`` 的
    ``EMPTY_TENANT``（两侧都空）或 ``PATH_SETS_DIFFER``（一侧空、另一侧非空）
    挡住——``TenantScope`` 现有的两个字段已经足够表达"应用侧看不到"这件事，不需要
    新增判据。
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
        visible_to_app = tenant_key in app_tenant_keys
        visible_to_user = tenant_key in user_tenant_keys

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

        tenants.append(
            TenantScope(
                tenant_key=tenant_key,
                visible_to_user_identity=visible_to_user,
                app_member_keys=app_member_keys,
                user_member_keys=user_member_keys,
                app_department_keys=app_department_keys,
                user_department_keys=user_department_keys,
            )
        )

    return SnapshotBatch(tenants=tuple(tenants), departments=tuple(departments), members=tuple(members))
