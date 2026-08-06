"""expires_at 九十天保留清理机制：触发器、受限清理函数与数据库角色

Revision ID: 0054_retention_cleanup
Revises: 20260806_baseline
Create Date: 2026-08-06

Issue #54 / 开发计划 S9。在这条 revision 之前，`expires_at` 只是两张表上的一个列：
`feishu_org_sync_run` 有不可后移触发器（基线内的 007 部分）但没有任何东西回收它；
`galaxy_import_batch` 连触发器都没有，调用方可以传任意值、也可以随后把它改到更晚。
九十天上限因此只写在文档里（[数据库设计原则 4 与第九节](../../../docs/技术设计/数据库设计.md)），
数据库不执行它。

本 revision 交付三件事：

1. `galaxy_import_batch` 补上与 007 同型的不可后移触发器，并回填历史偏离行；
2. 受限清理函数 `public.lingxi_retention_cleanup(timestamptz, integer)`，按到期时间
   小批量删除两张父表的到期行，子行由既有 8 条 `ON DELETE CASCADE` 边带走；
3. 数据库设计第十节已定义、但全仓库此前零定义的四个角色，以及本切片相关的最小授权。

**DDL 直接内联在本文件里，不再引用 `migrations/*.sql`。** 编号 SQL 已随 #53 冻结：
基线 `20260806_baseline` 逐字节覆盖 `006`–`012`，而 `scripts/ci/check_alembic_revisions.py`
要求顶层每个编号 SQL 都被某条 revision 的 `CHAIN` 覆盖——新增编号文件不会进入基线，
两条血统会就此分叉，门禁直接红。所以基线之后的 revision 是 DDL 的唯一载体。
本 revision 没有 `CHAIN`（它不是任何磁盘文件的副本），逐字节比对因此不适用于它。

**豁免只由 `expires_at <= p_now` 表达。** 不存在「排除最近完成批次」之类的无条件
保护：九十天是合同硬上限（数据库设计 :570/:574），`galaxy_*` 不在「当前状态」例外
名单里（:596）。下游 `current_batch_id()` 本来就按「过期即不算当前」实现
（`src/lingxi/adapters/galaxy_import.py:95`），因此删掉过期批次不改变任何下游可
观察结果——它此前就已经返回 None 了。组织快照侧对称的后果是可观察状态由 STALE
变 UNAVAILABLE，两者都不建档、不产生候选，属知情接受（Issue #54 编排者裁定 ②④）。

清理条件与「当前有效」判定严格互补：`expires_at > now()` 与 `expires_at <= p_now`
在同一时刻不可能同时成立，所以「当前批次被 CASCADE 清理」在现语义下不可能发生。
断言 V-保留-04 固化这条互补性，防的是未来有人去掉 `current_batch_id()` 的过期过滤。

顺带登记一处**已被本 revision 取代的旧注释**：`migrations/010_create_galaxy_import_batch.sql`
里写着「任何按 expires_at 的清理流程必须排除当前有效批次」，那是 #17 时期的登记，
与上面的裁定 ② 相反。该文件已随 #53 冻结、且被基线逐字节内嵌，改它必须连基线副本
一起改，风险大于一句过期注释的收益，因此**有意不动**——以本 revision 与
`docs/技术设计/验证与门禁.md` 的 `V-保留-20` 为准。

`downgrade()` 移除本次新增的函数与触发器并收回全部授权，但**不删除四个角色**
（集群级共享对象），也**不还原历史行回填**（原值已不可知）——两条边界见
`migrations/README.md` 与下方 `_DOWNGRADE_SQL` 的注释。
"""

from __future__ import annotations

from alembic import op

revision: str = "0054_retention_cleanup"
down_revision: str | None = "20260806_baseline"
branch_labels: str | None = None
depends_on: str | None = None


_UPGRADE_SQL = r"""
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
-- 或运维授权引用，删除是不可逆的越界操作。见下方 `_DOWNGRADE_SQL` 的说明。
--
-- Supabase 托管实例是否允许 CREATE ROLE 尚未核实，登记为 stage（L4a）演练验证项。
DO $roles$
DECLARE
    role_name   text;
    v_is_super  boolean;
    v_can_login boolean;
BEGIN
    FOREACH role_name IN ARRAY ARRAY[
        'lingxi_app',
        'lingxi_scheduler',
        'lingxi_retention_owner',
        'lingxi_migrate'
    ] LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = role_name) THEN
            EXECUTE format('CREATE ROLE %I NOLOGIN', role_name);
            CONTINUE;
        END IF;

        -- 角色已经存在：**必须校验**，不能默认它就是我们要的那个。
        -- `IF NOT EXISTS` 一跳过，一个由别人预建的同名角色就会静默继承本迁移赋予
        -- 的全部含义——其中 lingxi_retention_owner 会直接成为 SECURITY DEFINER
        -- 清理函数的属主。那时"无登录专用角色"这句话是假的，而没有任何东西会报错。
        SELECT rolsuper, rolcanlogin INTO v_is_super, v_can_login
          FROM pg_catalog.pg_roles WHERE rolname = role_name;

        -- 超级用户身份下，本迁移建立的每一条限权边界都不成立（它绕过全部权限检查）。
        -- 这个不尝试"收紧"：把别人的超级用户降权是越界操作，响亮失败才是正确行为。
        IF v_is_super THEN
            RAISE EXCEPTION '角色 % 已存在且是超级用户，本迁移的限权边界在它身上全部无效', role_name
                USING HINT = '请先由 DBA 确认该角色的用途，再决定改名或降权；迁移不擅自修改超级用户';
        END IF;

        -- 登录能力：只有 lingxi_retention_owner 强制收紧。它是清理函数的属主，
        -- 一个可登录的属主等于把"只能经受限函数删除"变成"可以直接连上来删"。
        -- 另外三个角色的登录能力属于部署接线（#62 会给它们口令与 LOGIN），
        -- 迁移在这里强行 NOLOGIN 会把运行时接线反复打回，故只校验不改。
        IF role_name = 'lingxi_retention_owner' AND v_can_login THEN
            EXECUTE format('ALTER ROLE %I NOLOGIN', role_name);
        END IF;
    END LOOP;

    -- 属主角色不得是任何角色的成员：它一旦继承了别的权限，SECURITY DEFINER 的
    -- 执行身份就不再是"只有两张父表 SELECT/DELETE"的那个最小面。
    -- 先做能自动纠正的那一类：业务角色被直接授予了属主角色。这条自愈在下面的
    -- 传递性校验**之前**执行——顺序反了的话，一次可以自动纠正的直接越权授权会被
    -- 校验当成致命错误挡下来，迁移永远跑不到自愈那一行（本轮实测踩到）。
    IF EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'lingxi_app') THEN
        EXECUTE 'REVOKE lingxi_retention_owner FROM lingxi_app';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'lingxi_scheduler') THEN
        EXECUTE 'REVOKE lingxi_retention_owner FROM lingxi_scheduler';
    END IF;

    -- 属主角色不得继承任何其他角色的权限，**也不得经由中间角色间接继承**。
    -- 直接查 pg_auth_members 只看得见一层：`owner → bridge → app` 这样的链上，
    -- owner 的直接成员行只有 bridge，看起来"没有问题"，而它实际已经拿到了 app 的
    -- 全部权限；针对 app 的直接 REVOKE 也撤不掉这条链（codex 二轮 P1-2）。
    -- `pg_has_role` 是按传递闭包判定的，正是这里要的语义。
    FOREACH role_name IN ARRAY ARRAY['lingxi_app', 'lingxi_scheduler', 'lingxi_migrate'] LOOP
        IF pg_catalog.pg_has_role('lingxi_retention_owner', role_name, 'USAGE')
           OR pg_catalog.pg_has_role('lingxi_retention_owner', role_name, 'MEMBER') THEN
            RAISE EXCEPTION 'lingxi_retention_owner 可经成员关系取得 % 的权限（可能是间接链）', role_name
                USING HINT = '受限清理函数的执行身份必须保持最小权限面；'
                             '请检查 pg_auth_members 上以 lingxi_retention_owner 为成员的整条链';
        END IF;
    END LOOP;

    -- 反向也查一次：任何业务角色都不得（直接或间接）取得属主角色。
    FOREACH role_name IN ARRAY ARRAY['lingxi_app', 'lingxi_scheduler'] LOOP
        IF pg_catalog.pg_has_role(role_name, 'lingxi_retention_owner', 'SET')
           OR pg_catalog.pg_has_role(role_name, 'lingxi_retention_owner', 'USAGE') THEN
            RAISE EXCEPTION '% 可经成员关系取得 lingxi_retention_owner（可能是间接链）', role_name
                USING HINT = '业务角色取得属主角色即可绕过受限清理函数直接删除内容';
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

-- 「业务角色不得持有属主角色」这条边界的强制执行已经移进上面的 DO 块：
-- 自愈（直接授予→收回）必须排在传递性校验之前，否则一次可以自动纠正的直接越权
-- 会被校验当成致命错误先挡下来。这里不再重复。
--
-- 为什么需要自愈：角色成员关系是集群级对象，**不随表一起重建**——表的 ACL 在重建表
-- 时自然清空，成员关系不会。#54 的变异测试把成员资格授给 lingxi_app 之后，重跑整条
-- 迁移链并没有收回它，于是「应用角色不能删除内容表」「不能取得属主角色」这一组限权
-- 断言在之后的整轮测试里全部静默失效——授权面被放宽了，而没有任何东西报错。

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
-- 这是本迁移唯一不可逆的部分，见下方 `_DOWNGRADE_SQL` 的说明。
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
-- 二之二、未到期内容的删除防线
-- ---------------------------------------------------------------------------
-- 数据库设计 :535-538 的原话是「权限收紧防误操作，触发器防误授权」——两道是并列的，
-- 不是二选一。此前只做了前半道：`lingxi_app` 没有 DELETE。但那只在授权正确时成立，
-- 而「授权被改错」正是这道防线要挡的场景。codex 外审实测：临时给 lingxi_app 授上
-- DELETE 之后，它可以删掉**未到期**的批次，九十天窗口内的内容凭空消失，
-- 没有任何东西拦。
--
-- 规则按到期时间判定，不按角色判定：
--   * `expires_at > clock_timestamp()`（未到期）→ 一律拒绝，谁来都拒绝；
--   * 已到期 → 放行。受限清理函数只选到期行，因此完全不受影响；
--     CASCADE 删的是子表行，子表没有这个触发器，也不受影响。
--
-- 按到期时间而不是按 `current_user = 'lingxi_retention_owner'` 判定，是因为后者会
-- 把防线变成「只信任某个角色名」——角色名可以被预建、被冒名（这正是 P1-3 要挡的），
-- 而到期时间是行自己的事实。两者叠加时，真正起作用的仍然是到期时间那一条。
--
-- **TRUNCATE 不经过行级触发器**，这道防线管不到它。TRUNCATE 由权限控制：
-- 本迁移不向任何 lingxi_* 角色授予它，断言 V-保留-21 直接核对这一点。
CREATE OR REPLACE FUNCTION public.lingxi_reject_premature_delete() RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp
AS $guard$
BEGIN
    -- **双条件**：到期 **且** 由属主角色执行，才放行（编排者裁定）。
    -- 数据库设计 :514-538 的原话是「只有属主在到期后可删」，两个条件是并列的。
    --
    -- 四象限：
    --   属主  × 已到期 → 放行（受限清理函数走的就是这一格）
    --   属主  × 未到期 → 拒绝（属主自己也不能提前删）
    --   非属主 × 已到期 → 拒绝（误授 lingxi_app DELETE 后，连过期行也删不掉）
    --   非属主 × 未到期 → 拒绝
    --
    -- 我此前只做了到期这一维，理由是「角色名可被预建冒名」。那个顾虑现在已经被
    -- 本迁移第一节的角色规范化消解掉了：属主角色的 LOGIN、SUPERUSER 与成员图都在
    -- 迁移时被强制校验，冒名者过不了那一关。于是「校验执行角色」重新变得可信，
    -- 而它挡住的正是单条件挡不住的那一格（非属主 × 已到期）。
    IF OLD.expires_at > clock_timestamp() THEN
        RAISE EXCEPTION '拒绝删除未到期的 %.% 行：到期时间 % 尚未到达',
            TG_TABLE_SCHEMA, TG_TABLE_NAME, OLD.expires_at
            USING HINT = '九十天窗口内的可识别内容只能等到期后由受限清理函数回收；'
                         '需要提前删除属于用户删除编排，不是保留清理。';
    END IF;
    IF current_user <> 'lingxi_retention_owner' THEN
        RAISE EXCEPTION '拒绝由 % 直接删除 %.% 的到期行', current_user, TG_TABLE_SCHEMA, TG_TABLE_NAME
            USING HINT = '到期内容只能经受限清理函数回收（它以 lingxi_retention_owner 身份执行）；'
                         '运维确需直接删除时，见 migrations/README.md 的紧急删除路径。';
    END IF;
    RETURN OLD;
END;
$guard$;

DROP TRIGGER IF EXISTS galaxy_import_batch_no_premature_delete ON galaxy_import_batch;
CREATE TRIGGER galaxy_import_batch_no_premature_delete
    BEFORE DELETE ON galaxy_import_batch
    FOR EACH ROW EXECUTE FUNCTION public.lingxi_reject_premature_delete();

DROP TRIGGER IF EXISTS feishu_org_sync_run_no_premature_delete ON feishu_org_sync_run;
CREATE TRIGGER feishu_org_sync_run_no_premature_delete
    BEFORE DELETE ON feishu_org_sync_run
    FOR EACH ROW EXECUTE FUNCTION public.lingxi_reject_premature_delete();


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
    newest_expires_at timestamptz,
    -- 这一轮该表是否因为拿不到锁而整批让路。**必须与"真的没有到期行"区分开**：
    -- 两者的 deleted_rows 都是 0，但前者是"没做成"、后者是"没有可做的"。
    -- 不区分的话，一张长期被占的表会在运行日志里表现为一切正常，而内容一直没被回收
    -- ——保留违规最不该有的形态就是它悄无声息（codex 二轮 P1-3）。
    blocked           boolean
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
-- 受控等待上限。清理与凭据轮换跑在 scheduler 的同一个线程里串行执行，这里一旦
-- 无限等待就是整个进程停摆：SIGTERM 只能设一个事件，解不开一条正在等 transactionid
-- 的锁，进程会挂到关闭宽限期结束被强杀（codex 外审在 PG16 复现）。
--
-- 为什么不是 `FOR UPDATE SKIP LOCKED`：任何行锁子句（FOR UPDATE / FOR NO KEY UPDATE /
-- FOR SHARE / FOR KEY SHARE）都要求调用者对该表持有 UPDATE 权限，而属主角色按
-- 数据库设计 :613 只持有 SELECT / DELETE。四种子句在该权限面下实测全部
-- `permission denied`。要用 SKIP LOCKED 就得给属主 UPDATE——那是放宽已经写定的限权
-- 边界，属于产品决定，不在本切片内做。改用超时是同一目标下不动权限面的做法。
SET lock_timeout = '2s'
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

    -- 逐表捕获锁超时。不捕获的话，一张表上的一把锁会让整次调用抛异常，
    -- 另一张表这一轮也跟着不清理；捕获之后这张表如实报 0，另一张照常进行。
    -- plpgsql 的 EXCEPTION 块是一个子事务：超时那一刻这张表已做的删除整体回滚，
    -- 不会留下半批。
    BEGIN
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
           max(removed.gone_at),
           false
      FROM removed;
    EXCEPTION WHEN lock_not_available THEN
        RETURN QUERY SELECT 'galaxy_import_batch'::text, 0::bigint,
                            NULL::timestamptz, NULL::timestamptz, true;
    END;

    -- 逐表捕获锁超时。不捕获的话，一张表上的一把锁会让整次调用抛异常，
    -- 另一张表这一轮也跟着不清理；捕获之后这张表如实报 0，另一张照常进行。
    -- plpgsql 的 EXCEPTION 块是一个子事务：超时那一刻这张表已做的删除整体回滚，
    -- 不会留下半批。
    BEGIN
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
           max(removed.gone_at),
           false
      FROM removed;
    EXCEPTION WHEN lock_not_available THEN
        RETURN QUERY SELECT 'feishu_org_sync_run'::text, 0::bigint,
                            NULL::timestamptz, NULL::timestamptz, true;
    END;
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

-- 业务角色：只授断言实际需要的 SELECT，不做投机性授权。
--
-- 这里原先给了 10 张表的 INSERT / UPDATE。但 `src/` 里对其中 8 张子表的 UPDATE
-- 语句是**零条**，本切片也不接线运行时连接身份——那些授权没有任何调用方，
-- 属于「先授出去再说」。授权面是本迁移声称建立的边界，预支边界等于没有边界。
--
-- 两张父表的 SELECT 保留：V-保留-13 断言的是「读得到、删不掉」，把 SELECT 一并
-- 拿掉，那条对比就退化成「什么都没授所以当然删不掉」，测不到 DELETE 的缺席。
-- 业务写授权随运行时接线切片按**实际调用方**逐张补，不在这里预支。
GRANT SELECT ON public.galaxy_import_batch, public.feishu_org_sync_run TO lingxi_app;

-- scheduler 对两张父表**一条表权限都不需要**。它唯一要做的事是调用清理函数，
-- 而那个函数是 SECURITY DEFINER：真正读写表的是属主身份，不是调用方。
-- 运行摘要也来自函数返回值，不需要 scheduler 自己去 SELECT 核对。
-- 先前授出的 SELECT 没有任何调用方，属于投机性授权，这里显式收回——
-- 授权面是本迁移声称建立的边界，预支边界等于没有边界。
REVOKE ALL ON public.galaxy_import_batch, public.feishu_org_sync_run FROM lingxi_scheduler;

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
    v_can_set boolean := pg_catalog.pg_has_role(current_user, 'lingxi_retention_owner', 'SET');
BEGIN
    IF NOT v_can_set THEN
        EXECUTE format('GRANT lingxi_retention_owner TO %I WITH SET TRUE', current_user);
    END IF;
    EXECUTE 'GRANT CREATE ON SCHEMA public TO lingxi_retention_owner';

    EXECUTE 'ALTER FUNCTION public.lingxi_retention_cleanup(timestamptz, integer) OWNER TO lingxi_retention_owner';

    EXECUTE 'REVOKE CREATE ON SCHEMA public FROM lingxi_retention_owner';

    -- 精确撤销**本次这一条授予**，用 `GRANTED BY current_user` 限定授予方。
    -- 成员关系按 (角色, 成员, 授予方) 三元组存放：本次是以 current_user 身份授出的，
    -- 只撤这一条，CREATEROLE 建角色时那条由别的授予方留下的自动授予原样不动。
    --
    -- 走过的三个错误答案都记在这里，它们只在非超级用户下才暴露：
    --   1. `GRANT ... WITH SET FALSE` —— INHERIT 缺省取被授予角色的 rolinherit，
    --      "还原"反而把 inherit=f 变成 inherit=t；
    --   2. 逐项 `WITH ADMIN %s, INHERIT %s, SET %s` —— `format('%s', boolean)` 渲染成
    --      `t`/`f`，语法错误；换 TRUE/FALSE 后又撞上
    --      `ADMIN option cannot be granted back to your own grantor`；
    --   3. `REVOKE SET OPTION FOR ...` —— 它撤的是「所有授予方」那一条上的 SET 选项，
    --      并不能把本次多出来的整条授予去掉，INHERIT 残留因此仍在（验收二次抓到；
    --      当时我还写了一句"已修"的注释，而捕获的三元组从头到尾没有被读过——
    --      没有用例覆盖成员关系状态，两轮都没自查出来）。
    IF NOT v_can_set AND current_user <> 'lingxi_migrate' THEN
        EXECUTE format('REVOKE lingxi_retention_owner FROM %I GRANTED BY %I', current_user, current_user);
    END IF;
END
$handover$;
"""


_DOWNGRADE_SQL = r"""
-- #54 S9：本 revision 的回滚 DDL。
--
-- 两件事**不会**被这里还原，都是有意的：
--
-- 1. **四个数据库角色不删除。** 角色是集群级共享对象——同一个 PostgreSQL 集群里
--    可能还有别的数据库、别的授权引用它们。一次数据库级回滚去删集群级对象，
--    影响面超出本迁移，而且不可逆。角色本身不持有任何内容，留着不构成数据风险；
--    真要清退由 Ops 显式执行。
-- 2. **历史行的 expires_at 回填不还原。** 原值在回填时已被覆盖，无从恢复。
--    这是本迁移唯一不可逆的部分。实际影响面为零行的理由见 upgrade 第二节的注释（历史行回填）。
--
-- 会被还原的是本迁移新建的函数、触发器与全部授权。
--
-- 全部 REVOKE 都先查角色是否存在。回滚可能跑在一个「角色由 Ops 另行清退过」的集群上，
-- 那时 `REVOKE ... FROM 不存在的角色` 会直接报错，把一次本该成功的回滚变成失败——
-- 而回滚失败的场合，通常正是最不能再多一个意外的场合。

DROP FUNCTION IF EXISTS public.lingxi_retention_cleanup(timestamptz, integer);

DROP TRIGGER IF EXISTS galaxy_import_batch_expiry ON galaxy_import_batch;
DROP FUNCTION IF EXISTS galaxy_import_batch_fix_expiry();

DROP TRIGGER IF EXISTS galaxy_import_batch_no_premature_delete ON galaxy_import_batch;
DROP TRIGGER IF EXISTS feishu_org_sync_run_no_premature_delete ON feishu_org_sync_run;
DROP FUNCTION IF EXISTS public.lingxi_reject_premature_delete();

DO $revoke$
DECLARE
    v_role text;
BEGIN
    IF EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'lingxi_retention_owner') THEN
        EXECUTE 'REVOKE SELECT, DELETE ON public.galaxy_import_batch FROM lingxi_retention_owner';
        EXECUTE 'REVOKE SELECT, DELETE ON public.feishu_org_sync_run FROM lingxi_retention_owner';
    END IF;

    IF EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'lingxi_app') THEN
        EXECUTE 'REVOKE SELECT ON public.galaxy_import_batch, public.feishu_org_sync_run FROM lingxi_app';
    END IF;

    -- scheduler 在 upgrade 里已经是 `REVOKE ALL`，一条表权限都没授过，
    -- 这里不再有对应的撤销动作（原先那三行是空操作，已删）。

    FOREACH v_role IN ARRAY ARRAY['lingxi_app', 'lingxi_scheduler', 'lingxi_retention_owner'] LOOP
        IF EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = v_role) THEN
            EXECUTE format('REVOKE USAGE ON SCHEMA public FROM %I', v_role);
        END IF;
    END LOOP;

    IF EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'lingxi_migrate')
       AND EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'lingxi_retention_owner') THEN
        EXECUTE 'REVOKE lingxi_retention_owner FROM lingxi_migrate';
    END IF;
END
$revoke$;
"""


def _execute_verbatim(connection, sql: str) -> None:
    """把整段 SQL 原样交给驱动执行。

    与基线 revision 用的是同一条路径，理由也相同：不能用 `op.execute()` 或
    `exec_driver_sql()`，两者最终都会把 SQL 连同一个（哪怕是空的）参数集交给
    psycopg，于是 psycopg 进入占位符插值模式。本段 DDL 里有 `DO` 块的
    `format('CREATE ROLE %I', ...)` 与 `RAISE` 的 `%` 占位符，插值模式下直接报
    `only '%s', '%b', '%t' are allowed as placeholders`。

    只传 SQL、不传参数时 psycopg 完全跳过插值，`%I` / `%` 原样送到服务端。
    这条路径已在 PostgreSQL 16 上以超级用户与非超级用户两种执行身份实跑验证。
    """

    with connection.connection.cursor() as cursor:
        cursor.execute(sql)


def upgrade() -> None:
    _execute_verbatim(op.get_bind(), _UPGRADE_SQL)


def downgrade() -> None:
    _execute_verbatim(op.get_bind(), _DOWNGRADE_SQL)
