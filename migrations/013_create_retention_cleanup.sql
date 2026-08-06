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

COMMENT ON ROLE lingxi_app IS
    '业务表最小读写；对承载可识别内容的表没有 DELETE，也不能执行保留清理函数。';
COMMENT ON ROLE lingxi_scheduler IS
    '定时职责；可执行受限保留清理函数，但不能直接 DELETE，也不能 SET ROLE 到函数所有者。';
COMMENT ON ROLE lingxi_retention_owner IS
    '无登录；持有受限保留清理函数及目标内容表的 SELECT / DELETE，不持有 INSERT / UPDATE / TRUNCATE / DDL。';
COMMENT ON ROLE lingxi_migrate IS
    '仅迁移时使用的 DDL 角色；不用于运行时连接。';

-- lingxi_migrate 建函数后要把它移交给 lingxi_retention_owner，因此必须是该角色的成员。
-- 这条成员关系只给迁移角色，lingxi_app / lingxi_scheduler 都不给（断言 V-保留-13）。
GRANT lingxi_retention_owner TO lingxi_migrate;

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
--   * `SET search_path = pg_catalog`，函数体内所有目标对象带 schema 全限定名，
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
SET search_path = pg_catalog
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

ALTER FUNCTION public.lingxi_retention_cleanup(timestamptz, integer)
    OWNER TO lingxi_retention_owner;


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
