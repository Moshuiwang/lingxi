-- #54 S9：九十天保留的执行机制——不可后移的 expires_at 触发器与受限清理函数。
--
-- 在这条迁移之前，`expires_at` 只是两张表上的一个列：`feishu_org_sync_run` 有
-- 不可后移触发器（007:57-71）但没有任何东西回收它；`galaxy_import_batch` 连触发器
-- 都没有，调用方可以传任意值、也可以随后把它改到更晚。九十天上限因此只写在文档里
-- （[数据库设计原则 4 与第九节](../docs/技术设计/数据库设计.md)），数据库不执行它。
--
-- 本迁移交付三件事：
--   1. `galaxy_import_batch` 补上与 007 同型的不可后移触发器，并回填历史行；
--   2. 受限清理函数 `public.lingxi_retention_cleanup(timestamptz, integer)`，
--      按到期时间小批量删除两张父表的到期行，子行由既有 8 条 ON DELETE CASCADE 边带走；
--   3. 数据库设计第十节已定义、但全仓库此前零定义的四个角色，以及本切片相关的最小授权。
--
-- **豁免只由 `expires_at <= p_now` 表达。** 不存在「排除最近完成批次」之类的无条件
-- 保护：九十天是合同硬上限（数据库设计 :570/:574），`galaxy_*` 不在「当前状态」例外
-- 名单里（:596）。下游 `current_batch_id()` 本来就按「过期即不算当前」实现
-- （`src/lingxi/adapters/galaxy_import.py:95`），因此删掉过期批次不改变任何下游可
-- 观察结果——它此前就已经返回 None 了。组织快照侧对称的后果是可观察状态由 STALE
-- 变 UNAVAILABLE，两者都不建档、不产生候选，属知情接受（Issue #54 编排者裁定 ②④）。
--
-- 清理条件与「当前有效」判定严格互补：`expires_at > now()` 与 `expires_at <= p_now`
-- 在同一时刻不可能同时成立，所以「当前批次被 CASCADE 清理」在现语义下不可能发生。
-- 断言 V-保留-04 固化这条互补性，防的是未来有人去掉 `current_batch_id()` 的过期过滤。


-- ---------------------------------------------------------------------------
-- 一、数据库角色
-- ---------------------------------------------------------------------------
-- 数据库设计第十节 :610-613 定义了四个角色，但全仓库此前没有任何一处创建它们，
-- 于是「lingxi_app 不能删除内容表」「只有 scheduler 能调用清理函数」这类限权断言
-- 根本无从验证。这里按幂等方式建立它们——建的是**限权**基建，不是扩权。
--
-- 全部创建为 NOLOGIN 且不设口令：本切片不接线运行时连接身份（进程仍用现有 DSN），
-- 角色只用于承载权限边界与 `SET SESSION AUTHORIZATION` 的受控验证。授予 LOGIN 与
-- 口令属于部署接线，归 #62 / Ops，不在本迁移内发生。
--
-- **downgrade 不删除这四个角色**：角色是集群级共享对象，可能已被同集群的其他数据库
-- 或运维授权引用，删除是不可逆的越界操作。见 migrations/downgrades/013 的说明。
--
-- Supabase 托管实例是否允许 CREATE ROLE 尚未核实，登记为 stage（L4a）演练验证项。
DO $roles$
DECLARE
    role_name text;
BEGIN
    FOREACH role_name IN ARRAY ARRAY[
        'lingxi_app',
        'lingxi_scheduler',
        'lingxi_retention_owner',
        'lingxi_migrate'
    ] LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = role_name) THEN
            EXECUTE format('CREATE ROLE %I NOLOGIN', role_name);
        END IF;
    END LOOP;
END
$roles$;

-- 各角色的职责（对应数据库设计 :610-613）：
--   lingxi_app             业务表最小读写；对承载可识别内容的表没有 DELETE，也不能执行清理函数。
--   lingxi_scheduler       定时职责；可执行受限清理函数，但不能直接 DELETE，也不能 SET ROLE 到函数属主。
--   lingxi_retention_owner 无登录；持有清理函数与目标内容表的 SELECT / DELETE，无 INSERT / UPDATE / TRUNCATE / DDL。
--   lingxi_migrate         仅迁移时使用的 DDL 角色；不用于运行时连接。
--
-- 这些说明**故意只写成 SQL 注释，不写成 COMMENT ON ROLE**：`COMMENT ON ROLE` 要求
-- 执行者是超级用户，或对该角色持有 ADMIN OPTION。生产托管方（Supabase）的 postgres
-- 不是超级用户，而角色若由 Ops 预先建好，迁移执行者也拿不到 ADMIN OPTION——那样这四条
-- 纯文档语句会让整条迁移失败。文档价值不值这个失败面。

-- lingxi_migrate 建函数后要把它移交给 lingxi_retention_owner，因此必须是该角色的成员。
-- 这条成员关系只给迁移角色，lingxi_app / lingxi_scheduler 都不给（断言 V-保留-13）。
GRANT lingxi_retention_owner TO lingxi_migrate;

-- 反向也写死：业务角色若因为任何原因拿到了这条成员关系，重新应用本迁移必须收回它。
-- 限权迁移应当**强制**自己的边界，而不是假设没有别人越权授过。
--
-- 这条不是防御性洁癖。角色成员关系是集群级对象，**不随表一起重建**：表的 ACL 在
-- 重建表时自然清空，成员关系不会。#54 的变异测试把成员资格授给 lingxi_app 之后，
-- 重跑整条迁移链并没有收回它，于是「应用角色不能删除内容表」「不能取得属主角色」
-- 这一组限权断言在之后的整轮测试里**全部静默失效**——授权面被放宽了，而没有任何
-- 东西报错。有了下面这行，同样的越权授权会在下一次迁移时被自动纠正。
REVOKE lingxi_retention_owner FROM lingxi_app, lingxi_scheduler;

-- 建库脚本重建 schema 后 PUBLIC 的 USAGE 可能不在；显式授予，不依赖默认 ACL
-- （Supabase 等托管实例的 public schema ACL 与自建集群不一致）。
GRANT USAGE ON SCHEMA public TO lingxi_app, lingxi_scheduler, lingxi_retention_owner;


-- ---------------------------------------------------------------------------
-- 二、galaxy_import_batch 的不可后移 expires_at
-- ---------------------------------------------------------------------------
-- 历史行回填。010 的 `DEFAULT now() + interval '2160 hours'` 与 `started_at DEFAULT now()`
-- 在同一条 INSERT 里取的是同一个事务时间戳，所以既有代码写出的行本来就精确满足
-- `expires_at = started_at + 2160h`（断言 V-保留-08 对此取证）。会被这条 UPDATE 改到的
-- 只有「调用方显式传了别的 expires_at 或 started_at」的行。
--
-- 回填在建触发器**之前**执行，是一次普通的数据校正：downgrade 不还原它（原值已不可知），
-- 这是本迁移唯一不可逆的部分，见 migrations/downgrades/013 与 PR 说明。
UPDATE galaxy_import_batch
   SET expires_at = started_at + INTERVAL '2160 hours'
 WHERE expires_at <> started_at + INTERVAL '2160 hours';

-- 与 007:57-71 同型：到期时间由来源时间推导，调用方传什么都会被覆盖；
-- 来源时间本身不允许改（改了就等于换一条推导基线，可以无限后移到期时间）。
CREATE OR REPLACE FUNCTION galaxy_import_batch_fix_expiry() RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.expires_at := NEW.started_at + INTERVAL '2160 hours';
    IF TG_OP = 'UPDATE' AND NEW.started_at <> OLD.started_at THEN
        RAISE EXCEPTION '不允许修改银河导入批次的来源时间';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS galaxy_import_batch_expiry ON galaxy_import_batch;
CREATE TRIGGER galaxy_import_batch_expiry
    BEFORE INSERT OR UPDATE ON galaxy_import_batch
    FOR EACH ROW EXECUTE FUNCTION galaxy_import_batch_fix_expiry();


-- ---------------------------------------------------------------------------
-- 三、受限清理函数
-- ---------------------------------------------------------------------------
-- 设计约束逐条来自数据库设计 :538 与 :600：
--   * SECURITY DEFINER，属主是无登录的 lingxi_retention_owner；
--   * `SET search_path = pg_catalog, pg_temp`，函数体内所有目标对象带 schema 全限定名，
--     调用方伪造搜索路径改不了它删哪张表；
--   * 拒绝晚于当前时间的 p_now；实际条件是 `expires_at <= LEAST(p_now, clock_timestamp())`；
--   * 每次调用有批量上限；
--   * 返回表名、删除数与实际时间范围供 scheduler 写运行日志——**只有这四类值**，
--     不返回任何行内容，因此摘要里不可能出现人员数据（断言 V-保留-14）。
--
-- 两张父表在同一次调用里各处理一批：漏掉一张的实现会被 V-保留-02 抓住。
-- 子表不出现在这里——8 条 ON DELETE CASCADE 边（007:75/86/99、011:12/28/40、012:15/35）
-- 负责带走子行，V-保留-06 的 pg_constraint 断言锁住「恰好 8 条且全是 CASCADE」。
DROP FUNCTION IF EXISTS public.lingxi_retention_cleanup(timestamptz, integer);
CREATE FUNCTION public.lingxi_retention_cleanup(
    p_now   timestamptz,
    p_limit integer
)
RETURNS TABLE (
    target_table      text,
    deleted_rows      bigint,
    oldest_expires_at timestamptz,
    newest_expires_at timestamptz
)
LANGUAGE plpgsql
SECURITY DEFINER
-- 必须以 pg_temp 结尾。search_path 里没有显式写 pg_temp 时，PostgreSQL 会把它
-- **隐式排在最前面**来解析关系名与类型名——于是调用方只要 `CREATE TEMP TABLE
-- timestamptz (...)`，就能让下面 DECLARE 里那两个未限定的类型名指向自己的临时对象，
-- 进而让赋值走到自己的类型转换函数上，在 SECURITY DEFINER 的属主身份里执行任意代码。
-- 实测（PostgreSQL 16）：不写 pg_temp 时该攻击可让调用方代码以 lingxi_retention_owner
-- 身份运行；写在最后则 pg_catalog 先命中，攻击失效。表名已经全限定挡不住这一条——
-- 能被劫持的不是"删哪张表"，是"在属主身份里跑什么"。
SET search_path = pg_catalog, pg_temp
AS $cleanup$
DECLARE
    -- 单次调用每张表最多删除的父行数上限。清理是常驻定时职责，宁可多跑几轮，
    -- 也不要一次拿住几万行的锁。
    c_max_batch CONSTANT integer := 1000;
    v_cutoff timestamptz;
    v_limit  integer;
BEGIN
    IF p_now IS NULL OR p_limit IS NULL THEN
        RAISE EXCEPTION '保留清理拒绝空参数：p_now 与 p_limit 都必须给出';
    END IF;

    -- 拒绝未来时间：否则调用方只要「把现在说晚一点」就能删掉尚未到期的内容。
    IF p_now > clock_timestamp() THEN
        RAISE EXCEPTION '保留清理拒绝晚于当前时间的 p_now（晚了 %）', p_now - clock_timestamp();
    END IF;

    IF p_limit <= 0 THEN
        RAISE EXCEPTION '保留清理的批量必须是正整数，收到 %', p_limit;
    END IF;

    -- 夹取而不是拒绝：调用方传了个离谱的大数时，正确行为是照常删一小批，
    -- 而不是让这一轮清理整体失败、到期内容继续留在库里。
    v_limit := LEAST(p_limit, c_max_batch);

    -- 时间下限取两者较小值。上面的未来校验已经保证 p_now <= clock_timestamp()，
    -- 因此这个 LEAST 当前是冗余的第二道闸；保留它是为了让「有人放宽那道校验」
    -- 不会静默扩大删除范围。V-保留-11 用一条源文本断言钉住它不被摘掉。
    v_cutoff := LEAST(p_now, clock_timestamp());

    RETURN QUERY
    WITH doomed AS (
        SELECT b.id
          FROM public.galaxy_import_batch b
         WHERE b.expires_at <= v_cutoff
         ORDER BY b.expires_at, b.id
         LIMIT v_limit
    ), removed AS (
        DELETE FROM public.galaxy_import_batch t
         USING doomed d
         WHERE t.id = d.id
        RETURNING t.expires_at AS gone_at
    )
    SELECT 'galaxy_import_batch'::text,
           count(*)::bigint,
           min(removed.gone_at),
           max(removed.gone_at)
      FROM removed;

    RETURN QUERY
    WITH doomed AS (
        SELECT r.id
          FROM public.feishu_org_sync_run r
         WHERE r.expires_at <= v_cutoff
         ORDER BY r.expires_at, r.id
         LIMIT v_limit
    ), removed AS (
        DELETE FROM public.feishu_org_sync_run t
         USING doomed d
         WHERE t.id = d.id
        RETURNING t.expires_at AS gone_at
    )
    SELECT 'feishu_org_sync_run'::text,
           count(*)::bigint,
           min(removed.gone_at),
           max(removed.gone_at)
      FROM removed;
END;
$cleanup$;

COMMENT ON FUNCTION public.lingxi_retention_cleanup(timestamptz, integer) IS
    '按 expires_at <= LEAST(p_now, clock_timestamp()) 小批量回收两张父表的到期行；'
    '每张表每次调用最多 p_limit 行（上限 1000）。返回表名、删除数与实际时间范围，不返回行内容。';

-- 属主移交在本文件**最末尾**（第五节）。顺序是刻意的：先由建函数的角色把
-- REVOKE / GRANT 授权面设好，最后才改属主。反过来做在非超级用户上会静默失败，
-- 见第五节的说明。


-- ---------------------------------------------------------------------------
-- 四、授权
-- ---------------------------------------------------------------------------
-- 属主拿到的**只有**两张父表的 SELECT / DELETE。子行不需要单独授权：
-- 外键的级联动作由约束机制执行，不检查调用者对子表的权限（本迁移落地时在
-- PostgreSQL 16 上实测确认，断言 V-保留-06 在真库上重复这条取证）。
GRANT SELECT, DELETE ON public.galaxy_import_batch  TO lingxi_retention_owner;
GRANT SELECT, DELETE ON public.feishu_org_sync_run  TO lingxi_retention_owner;

-- 业务角色：读写有，DELETE 没有。到期回收只有一条路径，就是下面那个函数。
GRANT SELECT, INSERT, UPDATE ON
    public.galaxy_import_batch,
    public.galaxy_user,
    public.galaxy_user_role,
    public.galaxy_role_menu,
    public.galaxy_country,
    public.galaxy_user_datacountry,
    public.feishu_org_sync_run,
    public.feishu_org_tenant_snapshot,
    public.feishu_org_department_snapshot,
    public.feishu_org_member_snapshot
TO lingxi_app;

-- scheduler 只需要看得见两张父表（写运行日志时核对），同样没有 DELETE。
GRANT SELECT ON public.galaxy_import_batch, public.feishu_org_sync_run TO lingxi_scheduler;

-- 先收回 PUBLIC 的默认 EXECUTE，再按精确签名只授给 scheduler。
REVOKE ALL ON FUNCTION public.lingxi_retention_cleanup(timestamptz, integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.lingxi_retention_cleanup(timestamptz, integer) TO lingxi_scheduler;


-- ---------------------------------------------------------------------------
-- 五、把函数移交给无登录的属主角色（必须最后做）
-- ---------------------------------------------------------------------------
-- `ALTER FUNCTION ... OWNER TO` 有两道权限检查，**两道在执行者是超级用户时都被整体
-- 跳过**——本地容器与 CI 的 postgres 恰好是超级用户，所以少做任何一步也一直是绿的。
-- 生产托管方（Supabase）的 postgres 没有 SUPERUSER，两道检查都会真的执行。实测：
--
--   1. 当前角色必须**能 SET ROLE 到新属主**。仅有 ADMIN OPTION 不够（PostgreSQL 16
--      把 ADMIN / SET / INHERIT 拆成三个独立选项），否则
--      `ERROR: must be able to SET ROLE "lingxi_retention_owner"`。
--   2. 新属主必须对目标 schema 有 **CREATE**；上面只授了 USAGE，否则
--      `ERROR: permission denied for schema public`。
--
-- 两者都只在移交这一刻需要，用完立即收回，不留常驻权限。
--
-- **移交必须排在授权之后。** PostgreSQL 16 里 CREATEROLE 角色建出来的角色是
-- `INHERIT FALSE` 的自动授予：先改属主的话，执行者不再继承属主权限，随后的
-- `GRANT EXECUTE ... TO lingxi_scheduler` 只会发一条
-- `WARNING: no privileges were granted` 然后**什么也不做**——scheduler 拿不到
-- EXECUTE，而迁移退出码是 0。这正是超级用户环境永远看不到的那一类失败。
-- 改属主时 PostgreSQL 会把 ACL 里旧属主授出的条目改记到新属主名下，因此先授后交不丢授权。
-- 判定必须看 **SET** 而不是 MEMBER：PostgreSQL 16 里 CREATEROLE 角色建出来的角色，
-- 自动授予是 `ADMIN TRUE, INHERIT FALSE, SET FALSE`。`pg_has_role(..., 'MEMBER')`
-- 对这种带 ADMIN 的成员关系返回真，于是"看起来已经是成员了"，而 `ALTER ... OWNER TO`
-- 要的恰恰是被关掉的那个 SET，照样报 `must be able to SET ROLE`。
DO $handover$
DECLARE
    v_is_member boolean := pg_catalog.pg_has_role(current_user, 'lingxi_retention_owner', 'MEMBER');
    v_can_set   boolean := pg_catalog.pg_has_role(current_user, 'lingxi_retention_owner', 'SET');
BEGIN
    IF NOT v_can_set THEN
        EXECUTE format('GRANT lingxi_retention_owner TO %I WITH SET TRUE', current_user);
    END IF;
    EXECUTE 'GRANT CREATE ON SCHEMA public TO lingxi_retention_owner';

    EXECUTE 'ALTER FUNCTION public.lingxi_retention_cleanup(timestamptz, integer) OWNER TO lingxi_retention_owner';

    EXECUTE 'REVOKE CREATE ON SCHEMA public FROM lingxi_retention_owner';
    -- 恢复到移交前的授予形态，不留多出来的能力。lingxi_migrate 的常驻成员关系
    -- （本文件第一节授出、且带 SET）不受影响：它进不了这两个分支。
    IF NOT v_can_set AND current_user <> 'lingxi_migrate' THEN
        IF v_is_member THEN
            EXECUTE format('GRANT lingxi_retention_owner TO %I WITH SET FALSE', current_user);
        ELSE
            EXECUTE format('REVOKE lingxi_retention_owner FROM %I', current_user);
        END IF;
    END IF;
END
$handover$;
