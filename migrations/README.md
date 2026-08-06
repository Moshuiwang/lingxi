# 迁移目录约定

> 2026-08-06 随 [#53](https://github.com/Moshuiwang/lingxi/issues/53) 改写：Alembic 成为生产表结构的权威来源。
> 上一版（2026-08-05 随 #16 正式化）以顶层编号 SQL 为唯一来源。

## 谁说了算

**权威来源是 Alembic 的 revision 链**（`migrations/alembic/versions/`）。新的表结构变更
一律写成新的 revision，**顶层编号 SQL 已冻结，不再新增**。

| 当前事实 | 值 |
| --- | --- |
| 基线 revision（链首） | `20260806_baseline` |
| head revision | `20260806_baseline` |
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
#    真跑了也没关系——编号 SQL 没有 IF NOT EXISTS，加上 ON_ERROR_STOP=1 会立刻
#    响亮失败，不会造成半成品。**漏跑才是危险方向**：它不会报错，只会让你在
#    第 3 步 stamp 出一个「缺表却自称已在基线」的库。所以别凭印象跳过，按第 1 步的结果跳。
psql "$LINGXI_LIBPQ_DSN" -v ON_ERROR_STOP=1 -f migrations/006_create_feishu_delegated_credential.sql
psql "$LINGXI_LIBPQ_DSN" -v ON_ERROR_STOP=1 -f migrations/007_create_feishu_org_snapshot.sql
psql "$LINGXI_LIBPQ_DSN" -v ON_ERROR_STOP=1 -f migrations/008_create_app_user.sql
psql "$LINGXI_LIBPQ_DSN" -v ON_ERROR_STOP=1 -f migrations/010_create_galaxy_import_batch.sql
psql "$LINGXI_LIBPQ_DSN" -v ON_ERROR_STOP=1 -f migrations/011_create_galaxy_user_and_role.sql
psql "$LINGXI_LIBPQ_DSN" -v ON_ERROR_STOP=1 -f migrations/012_create_galaxy_country_scope.sql

# 3. 重跑第 1 步确认六列都不是 NULL，然后只写版本号，不执行任何 DDL。
python -m alembic stamp 20260806_baseline

# 4. 之后全走 alembic。
python -m alembic upgrade head
```

> 编号顺序是 `006 → 007 → 008 → 010 → 011 → 012`（`009` 与 `004` 从未使用），
> 不可乱序：`008` 的触发器要读 `006` 建的表，文件内有硬依赖断言。

第 2 步的 `stamp` 是整个接管流程的关键：它写下「本库已相当于基线」，不碰任何表。
跳过它直接 `upgrade` 会失败（这是好事）；而如果有人为了让它「跑得通」给基线加上
`IF NOT EXISTS`，失败会变成**沉默的成功**——alembic 记下「基线做过了」，实际什么都没做，
之后每一条迁移都建立在假的起点上。门禁有一条否定断言守着这一点。

## 可回滚的边界

**基线 `20260806_baseline` 不支持 `downgrade`**，它显式 `raise` 并以非零退出。
理由不是实现麻烦，而是基线的 downgrade 等于删掉整个生产库的全部业务表，没有真实场景；
留一个能跑的实现只会让某次手滑的 `alembic downgrade base` 真的执行成功。
需要一个干净的空库时直接新建数据库。

**基线之后的每一条 revision 都必须可真往返**（`downgrade -1` 之后再 `upgrade` 能回到
同样的结构）。这是 [#53](https://github.com/Moshuiwang/lingxi/issues/53) TO PM 里「可前滚可回滚」承诺的确切口径：
承诺覆盖基线之后的全部变更，**不覆盖基线本身**。`scripts/ci/check_alembic_revisions.py`
拒绝空的 `downgrade()`——要么真正逆转，要么显式 `raise`，不允许「成功地什么都不做」。

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

**由镜像构建把 `migrations/` 与 `alembic.ini` 一并 `COPY` 进制品，[#62](https://github.com/Moshuiwang/lingxi/issues/62) 落地**
（2026-08-06 裁定⑥，已列为 #62 的分派输入）。迁移作业不在生产机现场构建、不从仓库
拉取，与业务进程用同一个镜像 tag，因此「镜像 tag 即冻结版本」对迁移同样成立。
在 #62 落地之前，本仓库**没有**可用的自动化部署路径——这是已登记的缺口，不是遗漏。

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
