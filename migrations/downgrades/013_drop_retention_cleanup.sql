-- #54 S9：013_create_retention_cleanup.sql 的回滚。
--
-- 本目录**不属于生产前滚链**：`migrations/*.sql` 的 glob 只取顶层文件，
-- 回滚脚本放在子目录里，免得门禁的空库整链前滚把刚建好的对象又删掉。
--
-- 两件事**不会**被这里还原，都是有意的：
--
-- 1. **四个数据库角色不删除。** 角色是集群级共享对象——同一个 PostgreSQL 集群里
--    可能还有别的数据库、别的授权引用它们。一次数据库级回滚去删集群级对象，
--    影响面超出本迁移，而且不可逆。角色本身不持有任何内容，留着不构成数据风险；
--    真要清退由 Ops 显式执行。
-- 2. **历史行的 expires_at 回填不还原。** 原值在回填时已被覆盖，无从恢复。
--    这是本迁移唯一不可逆的部分。实际影响面为零行的理由见 013 第二节的注释。
--
-- 会被还原的是本迁移新建的函数、触发器与全部授权。

-- 先删函数，再收回成员关系：以 lingxi_migrate 身份执行时，删除属主为
-- lingxi_retention_owner 的函数正需要那条成员关系。
DROP FUNCTION IF EXISTS public.lingxi_retention_cleanup(timestamptz, integer);

DROP TRIGGER IF EXISTS galaxy_import_batch_expiry ON galaxy_import_batch;
DROP FUNCTION IF EXISTS galaxy_import_batch_fix_expiry();

REVOKE SELECT, DELETE ON public.galaxy_import_batch  FROM lingxi_retention_owner;
REVOKE SELECT, DELETE ON public.feishu_org_sync_run  FROM lingxi_retention_owner;

REVOKE SELECT, INSERT, UPDATE ON
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
FROM lingxi_app;

REVOKE SELECT ON public.galaxy_import_batch, public.feishu_org_sync_run FROM lingxi_scheduler;

REVOKE USAGE ON SCHEMA public FROM lingxi_app, lingxi_scheduler, lingxi_retention_owner;

REVOKE lingxi_retention_owner FROM lingxi_migrate;
