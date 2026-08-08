# 迁移目录约定

> 2026-08-06 随 [#53](https://github.com/Moshuiwang/lingxi/issues/53) 改写：Alembic 成为生产表结构的权威来源。
> 上一版（2026-08-05 随 #16 正式化）以顶层编号 SQL 为唯一来源。

## 谁说了算

**权威来源是 Alembic 的 revision 链**（`migrations/alembic/versions/`）。新的表结构变更
一律写成新的 revision，**顶层编号 SQL 已冻结，不再新增**。

| 当前事实 | 值 |
| --- | --- |
| 基线 revision（链首） | `20260806_baseline` |
| head revision | `0058_worker_execution` |
| 配置文件 | 仓库根目录 `alembic.ini` |
| revision 目录 | `migrations/alembic/versions/` |
| 连接串环境变量 | `LINGXI_MIGRATION_DSN`（缺失即失败，无默认值） |
| 依赖声明 | `pyproject.toml` 的 `migrate` extra（`alembic>=1.19,<1.20`、`psycopg[binary]>=3.2,<4`） |

> 上表的两个 revision id 由 `scripts/ci/check_alembic_revisions.py` 与实际链核对；
> 新增 revision 却忘了更新这里，门禁会红。

`LINGXI_MIGRATION_DSN` 与业务进程的 `LINGXI_POSTGRES_DSN` **是两个变量**，且迁移不会
回落到后者。迁移需要 DDL 权限、业务进程不需要，两者本就该是不同的数据库角色；
门禁有一条否定断言专门验证「不设迁移变量时明确失败、不顺手用业务 DSN」。

日常执行：

```bash
export LINGXI_MIGRATION_DSN='postgresql://<用户>@<主机>:<端口>/<库>'
python -m alembic upgrade head
```

URL 的 scheme 写 `postgresql://` 或 `postgresql+psycopg://` 都可以，
`migrations/alembic/env.py` 会补齐成后者。**本仓库只有 psycopg3**，其余驱动一律拒绝——
裸 `postgresql://` 交给 SQLAlchemy，它会去找自己默认的 psycopg2 驱动，于是报
「没有 psycopg2 模块」，与真实原因（scheme 没写驱动）差得很远。

> `psycopg2` 这个名字在仓库里只以**说明和否定用例**的形式出现：本节、
> `migrations/alembic/env.py` 的模块说明，以及 `tests/test_alembic_revision_check.py`
> 里那条「非 psycopg3 驱动必须被拒绝」的用例。**它不是依赖**——`pyproject.toml`
> 与 `src/` 里都没有它。留着这些说明是因为下一个写连接串的人大概率会踩同一个坑。

## 基线 revision 是编号 SQL 的逐字节副本

`20260806_baseline` 把 `006`–`012` 六个文件按编号顺序**逐字节**嵌入并执行，
不是等价重写。原因是 PostgreSQL 的匿名约束名、匿名索引名、被 NAMEDATALEN 截断的
名字都由声明顺序决定：全链有 12 个匿名主键、8 个匿名外键、带序号后缀的匿名 CHECK。
改写成 `op.create_table(...)` 会重新生成这些名字，于是「旧库走编号 SQL、新库走 alembic」
两条血统拿到**结构相同但名字不同**的库，之后任何一条按名字 `DROP CONSTRAINT` 的迁移
都会一边成功一边失败——而这类分歧在建库时不报任何错。

`scripts/ci/check_migration_chain.sh` 每次门禁都实际建两个库并要求 `pg_dump` 归一化后
逐字节相等，所以副本与原文一旦分叉就是红灯。

**基线刻意不幂等**：它不带 `IF NOT EXISTS`，在已有对象的库上一定失败。这是有意的，
见下一节。

## 旧库接管（已经用编号 SQL 建成的库）

已经存在的库**不能直接** `alembic upgrade`——基线会撞上已有对象并失败。正确路径是
先把库补齐到编号 SQL 的末尾（`012`），再告诉 alembic「这个库已经在基线上了」：

```bash
# 0. psql 用的是 libpq，**不认** SQLAlchemy 的 `+psycopg` 驱动后缀；而上面说过
#    LINGXI_MIGRATION_DSN 两种写法都允许。这一行把它转成 libpq 形式，
#    使下面的命令对两种写法都逐字可执行。
export LINGXI_LIBPQ_DSN="${LINGXI_MIGRATION_DSN/postgresql+psycopg:/postgresql:}"

# 1. 先探测这个库已经跑到哪一步。每列为 NULL 表示对应编号**尚未执行**。
psql "$LINGXI_LIBPQ_DSN" -c "
  SELECT to_regclass('public.feishu_delegated_subject') AS \"006\",
         to_regclass('public.feishu_org_sync_run')      AS \"007\",
         to_regclass('public.app_user')                 AS \"008\",
         to_regclass('public.galaxy_import_batch')      AS \"010\",
         to_regclass('public.galaxy_user')              AS \"011\",
         to_regclass('public.galaxy_country')           AS \"012\";"

# 2. 把上一步显示为 NULL 的编号**按顺序**补齐。已经执行过的那几条可以跳过；
#    真跑了也没关系——编号 SQL 没有 IF NOT EXISTS，会立刻响亮失败。
#    **漏跑才是危险方向**：它不会报错，只会让你在第 3 步 stamp 出一个
#    「缺表却自称已在基线」的库。所以别凭印象跳过，按第 1 步的结果跳。
#
#    `--single-transaction` 不能省：psql 默认**逐语句提交**，ON_ERROR_STOP 只是
#    停下后续语句，已提交的那部分留在库里。那样一次中途失败会留下半个文件的对象，
#    而重试会卡在文件开头的第一条 CREATE TABLE（「已存在」），进退两难。
#    加上它，每个文件要么整份生效、要么完全没发生。
#    （已核对：006–012 里没有任何不能在事务块中执行的语句——无
#     CREATE INDEX CONCURRENTLY、VACUUM、CREATE DATABASE、ALTER SYSTEM。
#     将来新增编号 SQL 已被冻结，所以这个前提不会因为新文件而失效。）
psql "$LINGXI_LIBPQ_DSN" -v ON_ERROR_STOP=1 --single-transaction -f migrations/006_create_feishu_delegated_credential.sql
psql "$LINGXI_LIBPQ_DSN" -v ON_ERROR_STOP=1 --single-transaction -f migrations/007_create_feishu_org_snapshot.sql
psql "$LINGXI_LIBPQ_DSN" -v ON_ERROR_STOP=1 --single-transaction -f migrations/008_create_app_user.sql
psql "$LINGXI_LIBPQ_DSN" -v ON_ERROR_STOP=1 --single-transaction -f migrations/010_create_galaxy_import_batch.sql
psql "$LINGXI_LIBPQ_DSN" -v ON_ERROR_STOP=1 --single-transaction -f migrations/011_create_galaxy_user_and_role.sql
psql "$LINGXI_LIBPQ_DSN" -v ON_ERROR_STOP=1 --single-transaction -f migrations/012_create_galaxy_country_scope.sql

# 3. 重跑第 1 步确认六列都不是 NULL，然后只写版本号，不执行任何 DDL。
python -m alembic stamp 20260806_baseline

# 4. 之后全走 alembic。
python -m alembic upgrade head

# 5. **终验（不可跳过）**：见下一节。第 1 步的探测只看六张「首表」，
#    一个被中途打断过的编号文件可能建出了首表却缺兄弟表——那种库的探测结果与
#    完好的库一模一样，会被 stamp 成一个假基线。终验比对整个 schema，是唯一
#    能判定接管是否真的完成的步骤。
```

> 编号顺序是 `006 → 007 → 008 → 010 → 011 → 012`（`009` 与 `004` 从未使用），
> 不可乱序：`008` 的触发器要读 `006` 建的表，文件内有硬依赖断言。

第 3 步的 `stamp` 是整个接管流程的关键：它写下「本库已相当于基线」，不碰任何表。
跳过它直接 `upgrade` 会失败（这是好事）；而如果有人为了让它「跑得通」给基线加上
`IF NOT EXISTS`，失败会变成**沉默的成功**——alembic 记下「基线做过了」，实际什么都没做，
之后每一条迁移都建立在假的起点上。门禁有一条否定断言守着这一点。

### 第 5 步：终验（接管未经终验不算完成）

第 1 步的探测只查六张**首表**，它回答不了「这个编号文件是不是整份跑完了」。
一个曾被 `Ctrl-C`、连接中断或磁盘写满打断过的库，可能建出了 `galaxy_import_batch`
却缺 `galaxy_user`——探测六列全非空，与完好的库看起来一模一样，然后被 `stamp` 成
一个**假基线**。之后所有迁移都建立在这个假起点上，而第一次报错可能在几个月后。

唯一能判定接管真的完成的办法，是拿整个 schema 去比对一个已知正确的库：

```bash
# 5.1 建一个全新的对照库，用同一套 alembic 命令把它升到 head。
#     `lingxi_takeover_check` 是一次性的，验完就删。
psql "${LINGXI_LIBPQ_DSN%/*}/postgres" -c "CREATE DATABASE lingxi_takeover_check;"
LINGXI_MIGRATION_DSN="${LINGXI_MIGRATION_DSN%/*}/lingxi_takeover_check" \
  python -m alembic upgrade head

# 5.2 归一化导出两边的 schema。
#     --exclude-table 之外还要滤掉 pg_dump 每次现生成的随机 \restrict/\unrestrict 行
#     与含版本号的 `-- Dumped` 行，否则同一个库连续两次导出都不相等。
dump() {
  pg_dump --schema-only --no-owner --no-privileges --schema=public \
          --exclude-table=public.alembic_version "$1" |
    grep -v -E '^(-- Dumped |\\restrict |\\unrestrict )'
}
dump "$LINGXI_LIBPQ_DSN" > /tmp/taken_over.sql
dump "${LINGXI_LIBPQ_DSN%/*}/lingxi_takeover_check" > /tmp/reference.sql

# 5.3 必须零差异。
diff -u /tmp/reference.sql /tmp/taken_over.sql && echo "接管完成：schema 与全新库一致"

# 5.4 验完删掉对照库。
psql "${LINGXI_LIBPQ_DSN%/*}/postgres" -c "DROP DATABASE lingxi_takeover_check;"
```

**差异非零时的处置**：

- **不要继续使用这个库**，也不要「再补跑一遍缺的文件」——此刻 `alembic_version` 已经
  写着基线，再补跑会得到一个「版本号说在基线、结构却是手工拼出来」的库，比现在更难判定。
- 把 `diff` 输出原样报告给产品负责人（它只含结构，不含业务数据，可以直接贴）。
- 若这是一个可以重建的环境（`biai-stage`、测试库），最干净的处置是**新建一个空库、
  直接 `alembic upgrade head`**，再按数据导入流程迁数据。
- 若是生产库，停在这里等决定；不要在未经判定的结构上继续跑迁移。

第 1 步的探测保留为**快速预检**（它能便宜地告诉你要补跑哪几个文件），
但**判定接管是否完成一律以第 5 步为准**。

## 可回滚的边界

**基线 `20260806_baseline` 不支持 `downgrade`**，它显式 `raise` 并以非零退出。
理由不是实现麻烦，而是基线的 downgrade 等于删掉整个生产库的全部业务表，没有真实场景；
留一个能跑的实现只会让某次手滑的 `alembic downgrade base` 真的执行成功。
需要一个干净的空库时直接新建数据库。

**基线之后的每一条 revision 都必须可真往返**（`downgrade -1` 之后再 `upgrade` 能回到
同样的结构）。这是 [#53](https://github.com/Moshuiwang/lingxi/issues/53) TO PM 里「可前滚可回滚」承诺的确切口径：
承诺覆盖基线之后的全部变更，**不覆盖基线本身**。`scripts/ci/check_alembic_revisions.py`
拒绝空的 `downgrade()`——要么真正逆转，要么显式 `raise`，不允许「成功地什么都不做」。

## `0057_gateway_tables`（会话、任务队列与入站事件）

Issue #57 / S4 前半。建 `conversation`、`inbound_event`、`task` 三张表，DDL 全部内联
在 revision 里。

本切片早期先写过一个顶层编号文件 `migrations/013_*.sql`，缝链时按上面的冻结规则
**删除**：新增编号文件不会进入基线的逐字节副本，`check_alembic_revisions.py` 会因
「顶层编号 SQL 未被任何 revision 的 `CHAIN` 覆盖」而变红。基线之后的 revision 是 DDL
的唯一载体，本 revision 没有 `CHAIN`。

`downgrade()` 真实可执行且被逐 revision 真往返覆盖：三张表都是本 revision 新建的，
不存在需要还原的历史行，删除顺序与建表相反，两个函数显式删除。

## `0054_retention_cleanup` 的三条越界边界（保留清理）

1. **四个数据库角色**（`lingxi_app` / `lingxi_scheduler` / `lingxi_retention_owner` /
   `lingxi_migrate`）随本 revision 幂等创建，但 **`downgrade` 不删除它们**：角色是
   集群级共享对象，同集群的其他数据库或运维授权可能已经引用，一次数据库级回滚去删
   集群级对象影响面超出本迁移且不可逆。角色本身不持有内容，留着不构成数据风险；
   清退由 Ops 显式执行。四个角色一律 `NOLOGIN` 且无口令，运行时进程仍用现有连接身份；
   授予登录能力属部署接线。`downgrade` 的全部 `REVOKE` 都先查角色是否存在——回滚可能
   跑在一个"角色已被 Ops 另行清退"的集群上，那时对不存在的角色 `REVOKE` 会直接报错，
   把一次本该成功的回滚变成失败。

2. **历史行回填不可逆**：把 `galaxy_import_batch` 中 `expires_at` 偏离
   `started_at + 2160 小时` 的行校正回来，原值不再可知，`downgrade` 不还原。
   既有代码路径写出的行本来就精确满足该不变式（`started_at` 与 `expires_at` 两个
   DEFAULT 取同一个事务时间戳），所以实际影响面为零行。

3. **非超级用户执行者已实测通过。** 生产托管方（Supabase）的 `postgres` 有 `CREATEROLE`
   但没有 `SUPERUSER`，而 `ALTER FUNCTION ... OWNER TO`、`COMMENT ON ROLE` 这些语句的
   权限检查在超级用户下被**整体跳过**——本地容器与 CI 恰好是超级用户，所以这类问题
   不会在门禁里暴露。本 revision 已按非超级用户路径实测修正：不用 `COMMENT ON ROLE`、
   属主移交排在授权之后（先移交会让后续 `GRANT EXECUTE` 静默无效而退出码仍是 0）、
   移交前临时补齐 `SET` 成员资格与 schema `CREATE` 并在移交后立即收回。
   **若托管方禁止 `CREATE ROLE`、四个角色改由 Ops 预建**，则必须同时把
   `lingxi_retention_owner` 的 **ADMIN OPTION** 授予迁移执行者，否则本 revision 的
   `GRANT lingxi_retention_owner TO lingxi_migrate` 会 `permission denied`。
   这个失败形态是好的——响亮报错、整条 revision 原子回滚、不留半应用状态（内审已实证）——
   但它发生在部署当场，所以要写进运维契约而不是留给现场排查：

   ```sql
   -- Ops 预建角色时，除了建角色本身，还要给迁移执行者 ADMIN OPTION
   GRANT lingxi_retention_owner TO <迁移执行角色> WITH ADMIN OPTION;
   ```

   仍未核实的是该实例上 `CREATE ROLE` 是否被托管策略额外限制，以及是否允许
   `SET SESSION AUTHORIZATION`（真库角色用例依赖它）——两项均登记为 stage（L4a）演练验证项。

## 运维紧急删除到期数据的路径

保留清理的删除防线是**双条件**的：只有 `expires_at` 已到期、**且**执行身份是
`lingxi_retention_owner` 时，两张父表的行才删得掉。正常回收走受限清理函数
（它是 `SECURITY DEFINER`，以属主身份执行），因此不受影响。

运维确需绕开定时职责直接删除**已到期**数据时，用属主身份执行：

```sql
-- 需要超级用户或对 lingxi_retention_owner 有 SET 权限
SET SESSION AUTHORIZATION lingxi_retention_owner;
DELETE FROM public.galaxy_import_batch WHERE expires_at <= now() AND id = '<批次 id>';
RESET SESSION AUTHORIZATION;
```

**未到期**的数据任何身份都删不掉，属主也不行。那属于用户删除编排（合同的
「当前运行环境删除」），不是保留清理，走各自的编排流程；确有一次性需要时，
只能由 DBA 显式 `ALTER TABLE ... DISABLE TRIGGER` 并在操作后立即恢复，
该动作应当留审计记录。

## 过渡期：编号 SQL 还留着

顶层 `migrations/*.sql`（`006` `007` `008` `010` `011` `012`）**冻结但保留**，两个用途：

1. 旧库接管的第 1 步要用它们补齐到 `012`；
2. 门禁拿它们建出的库作为等价性比对的基准（上面那条持续断言）。

**不再新增编号 SQL。** 新的结构变更一律是新的 revision。

`tests/test_galaxy_import_postgres.py` 本批**不重指向**，仍按编号 SQL 建测试库
（2026-08-06 裁定⑩）：它的有效性由门禁那条持续等价断言背书——两条链建出的库逐字节
相同，按哪一条建测试库都是同一个结构。

**退休条件**：以下两条同时满足时删除顶层编号 SQL，并把 `test_galaxy_import_postgres.py`
改为按 alembic 建库：

- 所有在用的数据库（含 `biai-stage` 与生产）都已完成上面的接管流程，即 `alembic_version` 里有值；
- 届时等价性断言失去比对对象，由「两条链对比」降级为「alembic 单链前滚」。

退休时机由产品负责人在对应 Issue 决定，不由实施代理自行判断。

## 迁移脚本怎么进入部署制品

**已由镜像构建把 `migrations/` 与 `alembic.ini` 一并 `COPY` 进制品**（2026-08-06 裁定⑥，
[#62](https://github.com/Moshuiwang/lingxi/issues/62) 落地）。迁移作业不在生产机现场构建、
不从仓库拉取，与业务进程用同一个镜像 tag，因此「镜像 tag 即冻结版本」对迁移同样成立。

落地形态（`Dockerfile` 的 `migrate` 目标，编排见 `deploy/README.md`）：

- `alembic.ini` → `/opt/lingxi/alembic.ini`，`migrations/` → `/opt/lingxi/migrations/`；
  `migrations/testing/` 由 `.dockerignore` 排除，不进制品（它不属于生产链）。
- 入口是 `ENTRYPOINT ["python", "-m", "alembic", "-c", "/opt/lingxi/alembic.ini"]`，
  **`-c` 写绝对路径**而不是靠工作目录兜底：`python -m alembic` 不带 `-c` 时要求 CWD 含
  `alembic.ini`，调用方一旦换了工作目录就会找不到配置或找到别的配置。门禁用
  `docker run -w / <migrate 镜像>` 实测这一点（断言 M2-62-22）。
- 每次 CI 核对镜像里的 `alembic.ini` 与全部 revision 与提交**逐字节一致**，
  见 `scripts/ci/verify_image_contract.sh`。
- 迁移作业**不得配 restart 策略**：迁移失败必须停下来让人看见，自动重启只会把一次失败
  变成反复撞墙并掩盖原因。

依赖侧对应：`pyproject.toml` 的 `migrate` extra 直接声明两项——`alembic` 与
`psycopg[binary]`。**驱动必须自己声明**：alembic 不依赖任何数据库驱动，驱动由 URL 的
scheme 决定，少了它干净环境跑 `upgrade` 会报 `No module named 'psycopg'`。
传递闭包实测为五项：SQLAlchemy、Mako、MarkupSafe、greenlet、typing-extensions
（`psycopg-binary` 不在此列，它来自 `psycopg[binary]` 的直接声明）。
`CI / extras (migrate)` 那条矩阵腿在干净虚拟环境里证明它单独装得上、且驱动真的能导入。

## 测试资产（`migrations/testing/*.sql`）

`001` / `002` / `003` / `005` 服务于 Bot-Test 受控验证与既有测试
（[代码框架第五节](../docs/技术设计/代码框架.md)），其中 `001`/`003`/`005` 已分别被
`008`/`006`/`007` 取代，`002` 所属的浏览器 OAuth 路径已被 2026-07-28 决策排除。
它们**不属于生产链**、不参与上面的任何检查，也不在 alembic 的 `script_location` 扫描
范围内（alembic 只读 `migrations/alembic/versions/` 下的 `.py`），仅供
`tests/test_identity_postgres.sh`、`tests/test_refresh_token_postgres.py` 与
`scripts/sync_feishu_org_snapshot.py` 使用。

2026-08-06 登记（[#55 盘点](https://github.com/Moshuiwang/lingxi/issues/55#issuecomment-5201705742)）：按「保留最小集」口径，本轮清退清单为空。
`002` 服务的**员工**浏览器 OAuth 路径确已排除，但 `onboarding_progress` 同时是
「四达文档会议助手」重授权链的一环——卡片 `card_nonce` 即 OAuth `state`，回调换码前
必须先占用该行（`src/lingxi/adapters/oauth_bridge.py:542`），因此现在删除会移除正式
凭据代码「轮换失败 → 人工重新授权」的唯一落地手段。废弃时点改为：随
[#67](https://github.com/Moshuiwang/lingxi/issues/67) 的正式重授权入口交付后执行。
