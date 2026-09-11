"""给全部 PL/pgSQL 触发器函数固定 ``search_path``，并收回清理函数的匿名执行权。

Revision ID: 0096_plpgsql_search_path
Revises: 0095_contact_reachability

## 为什么

``public`` 下 21 个 PL/pgSQL 函数里有 19 个没有固定 ``search_path``（基线内嵌的
4 个、``0054`` 到 ``0095`` 逐条新增的 15 个，含 ``0095`` 刚加的两个入站触发器）。
触发器函数按**调用会话**的搜索路径解析未限定的名字：任何能改自己会话
``search_path`` 的连接，都能让 ``app_user_reject_delegated_subject`` 里的
``feishu_delegated_subject`` 解析到另一张同名表，双向身份防线就此失守；
更常见的形态是托管平台每天在巡检里把这 19 个函数列成一串同类警告，
真正的异常被淹没在里面。另一处是受限清理函数
``public.lingxi_retention_cleanup(timestamptz, integer)``：托管平台给应用创建者
设了默认权限，函数一建出来匿名与登录态两个平台角色就各持有一条直接 EXECUTE，
而九十天回收只应由 scheduler 发起。

## 改了什么

1. 19 个函数逐个 ``SET search_path = pg_catalog, pg_temp``。函数体只用 NEW / OLD
   与内置函数的 14 个用 ``ALTER FUNCTION``，定义原文一字不动；函数体里有未限定表
   引用的 5 个（共 7 处）用 ``CREATE OR REPLACE`` 重定义，**只**给表名加 ``public.``
   前缀并加上 ``SET search_path``，触发器时机、异常文案、属主、权限、SECURITY 属性
   全部沿用（``CREATE OR REPLACE`` 保留属主与 ACL，触发器绑定的是函数 OID）。
2. 收回清理函数对 ``anon`` / ``authenticated`` 的两条直接 EXECUTE。属主是无登录的
   ``lingxi_retention_owner``，撤权必须以属主身份执行；执行者拿不到 ``SET ROLE``
   时沿 ``0054`` 已有的做法临时给自己 ``WITH SET TRUE``、用完按授予方精确收回，
   前后核对成员关系没有多出任何一条。撤完回读有效权限必须为假，否则响亮失败。
3. 应用创建者 ``postgres`` 在 ``public`` 对 FUNCTIONS 的默认权限里，撤掉给
   ``anon`` / ``authenticated`` 的两项 EXECUTE，让下一次重建清理函数时不再自动
   长回来。``service_role`` 那一项保留。

## 没改什么

- 不把三个服务从 ``postgres`` 身份拆开，不动平台自己（``supabase_admin``）的默认
  权限，不批量撤任何角色在其他对象上的权限，不改任何成员关系，不消音日志。
- 撤默认权限**不取消**内建的 PUBLIC EXECUTE：以后新建的清理类函数仍要显式
  ``REVOKE … FROM PUBLIC`` 并回读实际权利，这条规则写在数据库设计里，由静态门禁守。
- 平台角色不存在的库（CI、本机容器）上，第 2、3 条静默跳过，第 1 条照常执行。

## 回滚

``downgrade()`` 把 14 个函数 ``RESET search_path``、5 个函数按原文重建（回到
``0095`` 时的定义），并在角色存在的库上把清理函数的两条直接 EXECUTE 以属主为授予方、
默认权限的两项以 ``postgres`` 为授予方补回。回滚不删数据，不依赖表内有没有行。
"""

from __future__ import annotations

from alembic import op

revision: str = "0096_plpgsql_search_path"
down_revision: str | None = "0095_contact_reachability"
branch_labels: str | None = None
depends_on: str | None = None


# 14 个只用 NEW / OLD 与内置函数的触发器函数：定义不动，只固定搜索路径。
_ALTER_ONLY_FUNCTIONS: tuple[str, ...] = (
    "feishu_org_sync_run_fix_expiry",
    "galaxy_import_batch_fix_expiry",
    "inbound_event_fix_expiry",
    "task_freeze_invariants",
    "queue_failure_notice_fix_expiry",
    "task_delivery_event_fix_expiry",
    "publish_outbox_fix_expiry",
    "mcp_access_token_immutable",
    "mcp_sync_check_fix_expiry",
    "onboarding_completion_notice_fix_expiry",
    "innertest_content_capture_fix_expiry",
    "task_document_delivery_request_fix_expiry",
    "outreach_message_freeze_anchors",
    "pending_action_fix_retention_expiry",
)

_PIN_SEARCH_PATH_SQL = r"""
ALTER FUNCTION public.feishu_org_sync_run_fix_expiry() SET search_path = pg_catalog, pg_temp;
ALTER FUNCTION public.galaxy_import_batch_fix_expiry() SET search_path = pg_catalog, pg_temp;
ALTER FUNCTION public.inbound_event_fix_expiry() SET search_path = pg_catalog, pg_temp;
ALTER FUNCTION public.task_freeze_invariants() SET search_path = pg_catalog, pg_temp;
ALTER FUNCTION public.queue_failure_notice_fix_expiry() SET search_path = pg_catalog, pg_temp;
ALTER FUNCTION public.task_delivery_event_fix_expiry() SET search_path = pg_catalog, pg_temp;
ALTER FUNCTION public.publish_outbox_fix_expiry() SET search_path = pg_catalog, pg_temp;
ALTER FUNCTION public.mcp_access_token_immutable() SET search_path = pg_catalog, pg_temp;
ALTER FUNCTION public.mcp_sync_check_fix_expiry() SET search_path = pg_catalog, pg_temp;
ALTER FUNCTION public.onboarding_completion_notice_fix_expiry() SET search_path = pg_catalog, pg_temp;
ALTER FUNCTION public.innertest_content_capture_fix_expiry() SET search_path = pg_catalog, pg_temp;
ALTER FUNCTION public.task_document_delivery_request_fix_expiry() SET search_path = pg_catalog, pg_temp;
ALTER FUNCTION public.outreach_message_freeze_anchors() SET search_path = pg_catalog, pg_temp;
ALTER FUNCTION public.pending_action_fix_retention_expiry() SET search_path = pg_catalog, pg_temp;
"""

_RESET_SEARCH_PATH_SQL = r"""
ALTER FUNCTION public.feishu_org_sync_run_fix_expiry() RESET search_path;
ALTER FUNCTION public.galaxy_import_batch_fix_expiry() RESET search_path;
ALTER FUNCTION public.inbound_event_fix_expiry() RESET search_path;
ALTER FUNCTION public.task_freeze_invariants() RESET search_path;
ALTER FUNCTION public.queue_failure_notice_fix_expiry() RESET search_path;
ALTER FUNCTION public.task_delivery_event_fix_expiry() RESET search_path;
ALTER FUNCTION public.publish_outbox_fix_expiry() RESET search_path;
ALTER FUNCTION public.mcp_access_token_immutable() RESET search_path;
ALTER FUNCTION public.mcp_sync_check_fix_expiry() RESET search_path;
ALTER FUNCTION public.onboarding_completion_notice_fix_expiry() RESET search_path;
ALTER FUNCTION public.innertest_content_capture_fix_expiry() RESET search_path;
ALTER FUNCTION public.task_document_delivery_request_fix_expiry() RESET search_path;
ALTER FUNCTION public.outreach_message_freeze_anchors() RESET search_path;
ALTER FUNCTION public.pending_action_fix_retention_expiry() RESET search_path;
"""

# 5 个函数体里有未限定表引用的：整条 CREATE OR REPLACE 写成完整常量，静态门禁
# （scripts/ci/check_plpgsql_search_path.py）直接读同一条语句里的 SET 子句。
# 加固版与下面的原文版差别**只有** 7 处表名前缀 ``public.``，模块导入时逐函数核对。
_HARDENED_DEFINITIONS_SQL = r"""
CREATE OR REPLACE FUNCTION public.app_user_reject_delegated_subject() RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp
AS $function_body$
BEGIN
    -- 与凭据侧触发器争用同一 advisory 锁：两个 BEFORE 触发器各自的 EXISTS 在
    -- MVCC 下看不见对方未提交的行，并发提交会让双向防线同时失守（终轮 Codex）。
    PERFORM pg_advisory_xact_lock(4217003);
    IF NEW.feishu_open_id IS NOT NULL
       AND EXISTS (
           SELECT 1 FROM public.feishu_delegated_subject
            WHERE subject_open_id = NEW.feishu_open_id
       ) THEN
        RAISE EXCEPTION '专用授权账号不能被建成用户记录';
    END IF;
    RETURN NEW;
END;
$function_body$;

CREATE OR REPLACE FUNCTION public.credential_reject_app_user_subject() RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp
AS $function_body$
BEGIN
    PERFORM pg_advisory_xact_lock(4217003);
    IF EXISTS (
        SELECT 1 FROM public.app_user WHERE feishu_open_id = NEW.subject_open_id
    ) THEN
        RAISE EXCEPTION '该 open_id 已是员工用户记录，不能成为专用授权主体';
    END IF;
    RETURN NEW;
END;
$function_body$;

CREATE OR REPLACE FUNCTION public.feishu_org_sync_run_verify_children() RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp
AS $function_body$
DECLARE
    actual_tenants INTEGER;
    actual_members INTEGER;
BEGIN
    IF NEW.status <> 'complete' THEN
        RETURN NULL;
    END IF;
    SELECT count(*) INTO actual_tenants FROM public.feishu_org_tenant_snapshot WHERE sync_run_id = NEW.id;
    SELECT count(*) INTO actual_members FROM public.feishu_org_member_snapshot WHERE sync_run_id = NEW.id;
    IF actual_tenants <> NEW.tenant_count OR actual_members <> NEW.member_count THEN
        RAISE EXCEPTION '完成批次 % 的声明计数与实际子行不一致（租户 %/%、成员 %/%）',
            NEW.id, NEW.tenant_count, actual_tenants, NEW.member_count, actual_members;
    END IF;
    RETURN NULL;
END;
$function_body$;

CREATE OR REPLACE FUNCTION public.app_user_record_real_inbound() RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp
AS $function_body$
BEGIN
    IF NEW.user_open_id IS NULL THEN
        RETURN NEW;
    END IF;
    UPDATE public.app_user SET
        first_inbound_at = LEAST(COALESCE(first_inbound_at, NEW.received_at), NEW.received_at),
        last_inbound_at = GREATEST(COALESCE(last_inbound_at, NEW.received_at), NEW.received_at),
        outbound_unavailable_at = CASE
            WHEN outbound_unavailable_at IS NULL OR outbound_unavailable_at <= NEW.received_at
            THEN NULL ELSE outbound_unavailable_at END,
        outbound_unavailable_code = CASE
            WHEN outbound_unavailable_at IS NULL OR outbound_unavailable_at <= NEW.received_at
            THEN NULL ELSE outbound_unavailable_code END
    WHERE feishu_open_id = NEW.user_open_id
      AND (last_inbound_at IS NULL OR last_inbound_at <= NEW.received_at);
    RETURN NEW;
END;
$function_body$;

CREATE OR REPLACE FUNCTION public.app_user_adopt_prior_inbound() RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp
AS $function_body$
DECLARE
    earliest TIMESTAMPTZ;
    latest TIMESTAMPTZ;
BEGIN
    IF NEW.feishu_open_id IS NULL THEN
        RETURN NEW;
    END IF;
    SELECT MIN(received_at), MAX(received_at) INTO earliest, latest
      FROM public.inbound_event WHERE user_open_id = NEW.feishu_open_id;
    IF earliest IS NULL THEN
        RETURN NEW;
    END IF;
    UPDATE public.app_user SET
        first_inbound_at = LEAST(COALESCE(first_inbound_at, earliest), earliest),
        last_inbound_at = GREATEST(COALESCE(last_inbound_at, latest), latest)
    WHERE id = NEW.id;
    RETURN NEW;
END;
$function_body$;
"""

# 0095 时的原文（``pg_proc.prosrc`` 逐字节），**只由 downgrade 引用**：降级按它重建，
# 不带 SET 子句，proconfig 随之回到空。
_ORIGINAL_DEFINITIONS_SQL = r"""
CREATE OR REPLACE FUNCTION public.app_user_reject_delegated_subject() RETURNS TRIGGER
LANGUAGE plpgsql
AS $function_body$
BEGIN
    -- 与凭据侧触发器争用同一 advisory 锁：两个 BEFORE 触发器各自的 EXISTS 在
    -- MVCC 下看不见对方未提交的行，并发提交会让双向防线同时失守（终轮 Codex）。
    PERFORM pg_advisory_xact_lock(4217003);
    IF NEW.feishu_open_id IS NOT NULL
       AND EXISTS (
           SELECT 1 FROM feishu_delegated_subject
            WHERE subject_open_id = NEW.feishu_open_id
       ) THEN
        RAISE EXCEPTION '专用授权账号不能被建成用户记录';
    END IF;
    RETURN NEW;
END;
$function_body$;

CREATE OR REPLACE FUNCTION public.credential_reject_app_user_subject() RETURNS TRIGGER
LANGUAGE plpgsql
AS $function_body$
BEGIN
    PERFORM pg_advisory_xact_lock(4217003);
    IF EXISTS (
        SELECT 1 FROM app_user WHERE feishu_open_id = NEW.subject_open_id
    ) THEN
        RAISE EXCEPTION '该 open_id 已是员工用户记录，不能成为专用授权主体';
    END IF;
    RETURN NEW;
END;
$function_body$;

CREATE OR REPLACE FUNCTION public.feishu_org_sync_run_verify_children() RETURNS TRIGGER
LANGUAGE plpgsql
AS $function_body$
DECLARE
    actual_tenants INTEGER;
    actual_members INTEGER;
BEGIN
    IF NEW.status <> 'complete' THEN
        RETURN NULL;
    END IF;
    SELECT count(*) INTO actual_tenants FROM feishu_org_tenant_snapshot WHERE sync_run_id = NEW.id;
    SELECT count(*) INTO actual_members FROM feishu_org_member_snapshot WHERE sync_run_id = NEW.id;
    IF actual_tenants <> NEW.tenant_count OR actual_members <> NEW.member_count THEN
        RAISE EXCEPTION '完成批次 % 的声明计数与实际子行不一致（租户 %/%、成员 %/%）',
            NEW.id, NEW.tenant_count, actual_tenants, NEW.member_count, actual_members;
    END IF;
    RETURN NULL;
END;
$function_body$;

CREATE OR REPLACE FUNCTION public.app_user_record_real_inbound() RETURNS TRIGGER
LANGUAGE plpgsql
AS $function_body$
BEGIN
    IF NEW.user_open_id IS NULL THEN
        RETURN NEW;
    END IF;
    UPDATE app_user SET
        first_inbound_at = LEAST(COALESCE(first_inbound_at, NEW.received_at), NEW.received_at),
        last_inbound_at = GREATEST(COALESCE(last_inbound_at, NEW.received_at), NEW.received_at),
        outbound_unavailable_at = CASE
            WHEN outbound_unavailable_at IS NULL OR outbound_unavailable_at <= NEW.received_at
            THEN NULL ELSE outbound_unavailable_at END,
        outbound_unavailable_code = CASE
            WHEN outbound_unavailable_at IS NULL OR outbound_unavailable_at <= NEW.received_at
            THEN NULL ELSE outbound_unavailable_code END
    WHERE feishu_open_id = NEW.user_open_id
      AND (last_inbound_at IS NULL OR last_inbound_at <= NEW.received_at);
    RETURN NEW;
END;
$function_body$;

CREATE OR REPLACE FUNCTION public.app_user_adopt_prior_inbound() RETURNS TRIGGER
LANGUAGE plpgsql
AS $function_body$
DECLARE
    earliest TIMESTAMPTZ;
    latest TIMESTAMPTZ;
BEGIN
    IF NEW.feishu_open_id IS NULL THEN
        RETURN NEW;
    END IF;
    SELECT MIN(received_at), MAX(received_at) INTO earliest, latest
      FROM inbound_event WHERE user_open_id = NEW.feishu_open_id;
    IF earliest IS NULL THEN
        RETURN NEW;
    END IF;
    UPDATE app_user SET
        first_inbound_at = LEAST(COALESCE(first_inbound_at, earliest), earliest),
        last_inbound_at = GREATEST(COALESCE(last_inbound_at, latest), latest)
    WHERE id = NEW.id;
    RETURN NEW;
END;
$function_body$;
"""

# 每个重定义函数必须恰好改掉预期数量的引用，且除 ``public.`` 前缀外与原文逐字节相同；
# 对不上就在 import 阶段炸，不把一个没改全或改多了的函数体静默写进库。
_EXPECTED_QUALIFIED_REFERENCES: dict[str, int] = {
    "app_user_reject_delegated_subject": 1,
    "credential_reject_app_user_subject": 1,
    "feishu_org_sync_run_verify_children": 2,
    "app_user_record_real_inbound": 1,
    "app_user_adopt_prior_inbound": 2,
}


def _function_bodies(definitions_sql: str) -> dict[str, str]:
    """从一段 CREATE OR REPLACE 常量里取出 {函数名: 函数体}（美元引号标签固定）。"""

    bodies: dict[str, str] = {}
    for statement in definitions_sql.split("$function_body$;"):
        if "AS $function_body$" not in statement:
            continue
        head, body = statement.split("AS $function_body$", 1)
        name = head[head.rindex("public.") + len("public.") : head.rindex("() RETURNS TRIGGER")]
        bodies[name] = body
    return bodies


def _verify_definitions_only_add_schema_prefixes() -> None:
    original = _function_bodies(_ORIGINAL_DEFINITIONS_SQL)
    hardened = _function_bodies(_HARDENED_DEFINITIONS_SQL)
    if set(original) != set(_EXPECTED_QUALIFIED_REFERENCES) or set(hardened) != set(original):
        raise RuntimeError("重定义函数的名单与预期不一致")
    for name, expected in _EXPECTED_QUALIFIED_REFERENCES.items():
        added = hardened[name].count("public.") - original[name].count("public.")
        if added != expected:
            raise RuntimeError(f"{name} 预期限定 {expected} 处表引用，实际 {added} 处")
        if hardened[name].replace("public.", "") != original[name]:
            raise RuntimeError(f"{name} 加固后的函数体除 public. 前缀外与原文不一致")
    for name in _ALTER_ONLY_FUNCTIONS:
        pin = f"ALTER FUNCTION public.{name}() SET search_path = pg_catalog, pg_temp;"
        reset = f"ALTER FUNCTION public.{name}() RESET search_path;"
        if pin not in _PIN_SEARCH_PATH_SQL or reset not in _RESET_SEARCH_PATH_SQL:
            raise RuntimeError(f"{name} 没有同时出现在固定与还原两段语句里")
    if _HARDENED_DEFINITIONS_SQL.count("SET search_path = pg_catalog, pg_temp") != len(hardened):
        raise RuntimeError("加固版定义里 SET search_path 子句数量与函数数量不一致")


_verify_definitions_only_add_schema_prefixes()


# 清理函数两条直接 EXECUTE 的收回 / 补回共用的骨架。撤权与补回都必须以属主身份执行：
# 执行者不是属主、也拿不到 SET ROLE 时，临时给自己 ``WITH SET TRUE``、用完按授予方
# 精确收回（0054 的移交写法，缺陷史见那里），前后核对成员关系没有多出任何一条。
# ``%(verb)s`` / ``%(preposition)s`` 分别是 REVOKE/FROM 与 GRANT/TO。
_CLEANUP_ACL_TEMPLATE = r"""
DO $cleanup_acl$
DECLARE
    c_function CONSTANT text := 'public.lingxi_retention_cleanup(timestamptz, integer)';
    v_signature regprocedure := pg_catalog.to_regprocedure(c_function);
    v_executor  text := current_user;
    v_owner     text;
    v_role      text;
    v_targets   text[] := ARRAY[]::text[];
    v_members_before text;
    v_members_after  text;
    v_granted_set    boolean := false;
BEGIN
    IF v_signature IS NULL THEN
        RAISE EXCEPTION '找不到受限清理函数 %%，无法处理它对匿名角色的直接 EXECUTE', c_function;
    END IF;

    -- 只处理「角色存在」且「直接 ACL 里的状态与目标相反」的项：角色不存在的库
    -- （CI、本机容器）整段静默跳过，已经处理过的库重跑也是空操作。
    FOREACH v_role IN ARRAY ARRAY['anon', 'authenticated'] LOOP
        IF EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = v_role)
           AND (%(present_condition)s) = EXISTS (
               SELECT 1
                 FROM pg_catalog.pg_proc p, pg_catalog.aclexplode(p.proacl) a
                WHERE p.oid = v_signature
                  AND a.grantee = (SELECT oid FROM pg_catalog.pg_roles WHERE rolname = v_role)
                  AND a.privilege_type = 'EXECUTE') THEN
            v_targets := v_targets || v_role;
        END IF;
    END LOOP;
    IF pg_catalog.cardinality(v_targets) = 0 THEN
        RETURN;
    END IF;

    SELECT pg_catalog.pg_get_userbyid(p.proowner) INTO v_owner
      FROM pg_catalog.pg_proc p WHERE p.oid = v_signature;

    -- 成员关系快照：属主角色的全部成员行 + 执行者作为成员的全部行。
    -- 临时授予若有残留，一定落在这两类里。
    SELECT pg_catalog.string_agg(
               pg_catalog.format('%%s<-%%s/%%s:%%s%%s%%s', am.roleid::regrole, am.member::regrole,
                                 am.grantor::regrole, am.admin_option, am.inherit_option,
                                 am.set_option),
               ',' ORDER BY am.roleid, am.member, am.grantor)
      INTO v_members_before
      FROM pg_catalog.pg_auth_members am
     WHERE am.roleid = (SELECT oid FROM pg_catalog.pg_roles WHERE rolname = v_owner)
        OR am.member = (SELECT oid FROM pg_catalog.pg_roles WHERE rolname = v_executor);

    IF v_executor <> v_owner AND NOT pg_catalog.pg_has_role(v_executor, v_owner, 'SET') THEN
        EXECUTE pg_catalog.format('GRANT %%I TO %%I WITH SET TRUE', v_owner, v_executor);
        v_granted_set := true;
    END IF;
    IF v_executor <> v_owner THEN
        EXECUTE pg_catalog.format('SET ROLE %%I', v_owner);
    END IF;
    FOREACH v_role IN ARRAY v_targets LOOP
        EXECUTE pg_catalog.format('%(verb)s EXECUTE ON FUNCTION %%s %(preposition)s %%I',
                                  v_signature, v_role);
    END LOOP;
    IF v_executor <> v_owner THEN
        EXECUTE 'RESET ROLE';
    END IF;
    IF v_granted_set THEN
        EXECUTE pg_catalog.format('REVOKE %%I FROM %%I GRANTED BY %%I', v_owner, v_executor, v_executor);
    END IF;

    -- 回读实际权利，不信退出码：非属主执行的 REVOKE 会「警告后无效果」而退出码为 0。
    -- 处理完之后有效权限必须与处理前相反；仍与处理前相同就是没生效。
    FOREACH v_role IN ARRAY v_targets LOOP
        IF pg_catalog.has_function_privilege(v_role, v_signature, 'EXECUTE') = (%(present_condition)s) THEN
            RAISE EXCEPTION '角色 %% 对 %% 的有效 EXECUTE 仍是 %%，撤权 / 补回没有生效',
                v_role, c_function, (%(present_condition)s)
                USING HINT = '请核对执行身份能否以属主身份操作该函数';
        END IF;
    END LOOP;

    SELECT pg_catalog.string_agg(
               pg_catalog.format('%%s<-%%s/%%s:%%s%%s%%s', am.roleid::regrole, am.member::regrole,
                                 am.grantor::regrole, am.admin_option, am.inherit_option,
                                 am.set_option),
               ',' ORDER BY am.roleid, am.member, am.grantor)
      INTO v_members_after
      FROM pg_catalog.pg_auth_members am
     WHERE am.roleid = (SELECT oid FROM pg_catalog.pg_roles WHERE rolname = v_owner)
        OR am.member = (SELECT oid FROM pg_catalog.pg_roles WHERE rolname = v_executor);
    IF v_members_after IS DISTINCT FROM v_members_before THEN
        RAISE EXCEPTION '处理清理函数权限前后成员关系发生了变化：之前 [%%]，之后 [%%]',
            v_members_before, v_members_after
            USING HINT = '临时授予必须在同一段里精确收回；不得留下任何新的成员关系';
    END IF;
END
$cleanup_acl$;
"""

# 应用创建者在 public 对 FUNCTIONS 的默认权限里给两个平台角色的 EXECUTE。
# 只动 (postgres, public, FUNCTIONS) 这一行里的两项；service_role 与其他角色的
# 默认权限、表与序列的默认权限、平台自己的默认权限一律不碰。
_DEFAULT_ACL_TEMPLATE = r"""
DO $default_acl$
DECLARE
    c_creator CONSTANT text := 'postgres';
    v_role    text;
    v_targets text[] := ARRAY[]::text[];
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = c_creator) THEN
        RETURN;
    END IF;
    -- 只在创建者已有这一行默认权限的库上动手（托管平台的形态）；没有这一行的库
    -- （CI、本机容器）没有可撤的项，也不该在回滚时凭空造出一行。
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_default_acl d
         WHERE d.defaclrole = (SELECT oid FROM pg_catalog.pg_roles WHERE rolname = c_creator)
           AND d.defaclnamespace = 'public'::regnamespace
           AND d.defaclobjtype = 'f') THEN
        RETURN;
    END IF;
    FOREACH v_role IN ARRAY ARRAY['anon', 'authenticated'] LOOP
        IF EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = v_role)
           AND (%(present_condition)s) = EXISTS (
               SELECT 1
                 FROM pg_catalog.pg_default_acl d, pg_catalog.aclexplode(d.defaclacl) a
                WHERE d.defaclrole = (SELECT oid FROM pg_catalog.pg_roles WHERE rolname = c_creator)
                  AND d.defaclnamespace = 'public'::regnamespace
                  AND d.defaclobjtype = 'f'
                  AND a.grantee = (SELECT oid FROM pg_catalog.pg_roles WHERE rolname = v_role)
                  AND a.privilege_type = 'EXECUTE') THEN
            v_targets := v_targets || v_role;
        END IF;
    END LOOP;
    IF pg_catalog.cardinality(v_targets) = 0 THEN
        RETURN;
    END IF;
    -- ALTER DEFAULT PRIVILEGES FOR ROLE 要求执行者持有该角色的权限；拿不到就响亮
    -- 失败，不能让「匿名角色对以后新建的函数仍自动拿到 EXECUTE」静默留下。
    IF NOT pg_catalog.pg_has_role(current_user, c_creator, 'USAGE') THEN
        RAISE EXCEPTION '当前身份 %% 不能修改 %% 的默认权限，无法处理匿名角色的默认 EXECUTE',
            current_user, c_creator
            USING HINT = '迁移应以 LINGXI_MIGRATION_DSN 里的应用创建者身份执行';
    END IF;
    FOREACH v_role IN ARRAY v_targets LOOP
        EXECUTE pg_catalog.format(
            'ALTER DEFAULT PRIVILEGES FOR ROLE %%I IN SCHEMA public %(verb)s EXECUTE ON FUNCTIONS %(preposition)s %%I',
            c_creator, v_role);
    END LOOP;
END
$default_acl$;
"""


def _acl_sql(hardened: bool) -> str:
    """撤权（``hardened``）或补回两段 DO 块。

    ``present_condition`` 是「处理前该项应当存在」：撤权时只挑现在有的，补回时只挑
    现在没有的，两个方向都天然幂等。
    """

    fill = {
        "verb": "REVOKE" if hardened else "GRANT",
        "preposition": "FROM" if hardened else "TO",
        "present_condition": "true" if hardened else "false",
    }
    return (_CLEANUP_ACL_TEMPLATE % fill) + "\n" + (_DEFAULT_ACL_TEMPLATE % fill)


def _upgrade_sql() -> str:
    return (
        "-- 一、14 个只用 NEW / OLD 与内置函数的触发器函数：定义不动，只固定搜索路径。"
        + _PIN_SEARCH_PATH_SQL
        + "\n-- 二、5 个函数体里有未限定表引用的：重定义，只加 public. 前缀与搜索路径。"
        + _HARDENED_DEFINITIONS_SQL
        + "\n-- 三、清理函数的两条直接 EXECUTE 与创建者默认权限里的两项。\n"
        + _acl_sql(hardened=True)
    )


def _downgrade_sql() -> str:
    return (
        "-- 一、14 个函数：去掉本 revision 固定的搜索路径。"
        + _RESET_SEARCH_PATH_SQL
        + "\n-- 二、5 个函数：按 0095 时的原文重建（不带 SET 子句，proconfig 随之回到空）。"
        + _ORIGINAL_DEFINITIONS_SQL
        + "\n-- 三、在角色存在的库上把两条直接 EXECUTE 与默认权限的两项补回。\n"
        + _acl_sql(hardened=False)
    )


def _execute_verbatim(connection, sql: str) -> None:
    """与既有 revision 同型：不走 ``op.execute()``，避免空参数集触发插值模式。

    本段 DDL 里有 ``DO`` 块的 ``format('%I', …)`` 与 ``RAISE`` 的 ``%`` 占位符，
    psycopg 一旦进入插值模式就会拒绝它们。
    """

    with connection.connection.cursor() as cursor:
        cursor.execute(sql)


def upgrade() -> None:
    _execute_verbatim(op.get_bind(), _upgrade_sql())


def downgrade() -> None:
    _execute_verbatim(op.get_bind(), _downgrade_sql())
