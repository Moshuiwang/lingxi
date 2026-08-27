# 迁移目录约定

> 2026-08-06 随 [#53](https://github.com/Moshuiwang/lingxi/issues/53) 改写：Alembic 成为生产表结构的权威来源。
> 上一版（2026-08-05 随 #16 正式化）以顶层编号 SQL 为唯一来源。

## 谁说了算

**权威来源是 Alembic 的 revision 链**（`migrations/alembic/versions/`）。新的表结构变更
一律写成新的 revision，**顶层编号 SQL 已冻结，不再新增**。

| 当前事实 | 值 |
| --- | --- |
| 基线 revision（链首） | `20260806_baseline` |
| head revision | `0073_pending_action_perm_types` |
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

## `0060_gateway_delivery_dispatch`（Gateway 投递消费记账）

Issue #152。在 `task` 上新增四列（`delivery_consumed_sequence`、`delivery_message_id`、
`dispatch_reserved_kind`、`delivery_expired_notice_sent_at`），供 Gateway 消费循环记录
「消费到哪、绑定了哪个可回读标识、外发前预留位、是否已提示过期」；与 0057 已建但一直
未启用的 `card_id`/`card_seq`/`fallback_text` 同表。理由详见 revision 文件头部注释。

纯新增列，前滚兼容；`downgrade()` 直接 `DROP COLUMN`，不存在需要回填的历史值。

## `0061_agent_session_cleanup`（Agent 会话 JSONL 物理清理队列）

Issue #153。新表 `agent_session_cleanup`：三个会话边界触发点（`/new`、空闲两小时
到点、停用/权限变化感知）各自往这里排队"哪个 `agent_session_id` 不会再被 resume
了"，真正的物理文件删除延后到常驻 Worker 的周期性收口认领执行——触发点所在的
Gateway/scheduler 进程都没有挂载用户环境目录。`agent_session_id` 唯一索引防止
并发触发（例如 `/new` 与空闲到点扫描撞在同一时刻）产生重复待办；`queued_at` 上的
局部索引（`WHERE done_at IS NULL`）让待处理队列的领取查询不随历史已完成行增长
变慢。理由详见 revision 文件头部注释与[数据库设计「问数结果投递事件与会话保留
Outbox」](../docs/技术设计/数据库设计.md#问数结果投递事件与会话保留-outbox)。

整张表本 revision 新增，前滚兼容；`downgrade()` 直接 `DROP TABLE`，不存在需要
回填的历史值。

## `0062_onboarding_dispatch_ledger`（未开通首聊的交接账本）

Issue #65 轻审 P2-2。在 `inbound_event` 上新增一列 `onboarding_dispatched_at`
（`NULL` = 已认领但尚未确认交给开通编排），并对"待交接"的行建局部索引。#65 的接线
把认领放在事务里、把触发开通编排放在提交之后，提交与触发之间的崩溃或停机会留下一条
谁都不会再处理的孤儿行——重投被幂等挡下，编排永远不会被调用。这一列让这种行可判定，
供 `core/conversation/onboarding_recovery.py` 的对账扫描重新交接一次。理由与回填口径
详见 revision 文件头部注释。

纯新增列 + 局部索引，前滚兼容；`downgrade()` 直接删除两者。回填只写进本列，随列一起
消失，不存在"原值已不可知"的问题。

## `0063_roster_snapshot`（花名册持久快照载体）

Issue #52 的 S-B-02。两张新表 `roster_snapshot`（元信息：读取时间、行数、页数、源头
自报总数与完整性计数）与 `roster_snapshot_row`（行，按 `row_index` 保读取次序）。
产品负责人 2026-08-08 的 D2 裁定推翻了此前的「零新表」定案：每日花名册比对需要一份
跨进程重启与常规发布都还在的持久快照，源头返回空 / 失败 / 超时 / 半轮一律保留上一份。

三条约束刻意写进数据库而不是留给调用方自觉：`singleton` 列只能为 `TRUE` 且唯一
（任何时刻**最多一份**快照）、`row_count > 0`（零行快照会清空比对基线）、行表按
`ON DELETE CASCADE` 挂在元信息上（删元信息即整份消失，替换因此能在同一个事务里
做完）。行表**不加人员 ID 唯一约束**：同一人员 ID 多行是花名册实测常态。理由与
保留期留白详见 revision 文件头部注释。

两张表本 revision 新增，前滚兼容；`downgrade()` 按依赖反序 `DROP TABLE`，不存在
需要回填的历史值。

## `0064_permission_publish_outbox`（权限发布意图 outbox）

Issue #156 的 S-C-01。一张新表 `publish_outbox`：权限决定（`app_user.permission_version`
推进）与「要往当前权限多维表格发布什么」的内容快照在**同一个事务**里落库，投递异步进行。
回滚之后库里既没有新版本，也没有孤立的发布意图（`V-权限-01`）。

四条约束刻意写进数据库而不是留给调用方自觉：`UNIQUE (user_id, permission_version)`
（同一用户同一权限版本只有一条意图）、`published_at` 与 `status='published'` 互为充要
（一条 `failed` 却带着发布时间的行会被下游读成发布成功）、触发器把
`content_expires_at` 固定为 `created_at + 2160 hours` 并禁止改写
`created_at`/`user_id`/`permission_version` 三个锚点、`user_id` 上的 `ON DELETE CASCADE`
（账号删除即带走含邮箱与姓名的内容快照）。九十天到期后的内容擦除由
`adapters/postgres_permission_publish.py` 的 `redact_expired_payloads` 落实，未进
`0054` 的受限清理函数——理由与四处偏离数据库设计蓝本的说明详见 revision 文件头部注释。

本表本 revision 新增，前滚兼容；`downgrade()` 删表并删触发器函数，不存在需要回填的
历史值。

## `0065_mcp_token_and_sync_check`（MCP 访问令牌与就绪确认记录）

Issue #156 的 S-C-02。两张新表 + 一列：`mcp_access_token`（Lingxi 为建档用户签发的问数
MCP 访问令牌）、`mcp_sync_check`（发布之后每一次「当前用户 MCP 是否就绪」的判定），
以及 `publish_outbox.created_record_id`（**这条意图自己建过的那一行**）。

最后这一列与 0064 既有的 `external_record_id` 是两件事：后者是**审计**（上一次尝试操作
了哪一行，任何尝试都会写），前者是**出身**（只有 `create_row` 明确返回了 ID 时才写）。
混用会让既有 26 行在一次更新读回不明之后，重试时被判成永久冲突——那是对 S-C-01
「更新可重试收敛」语义的回归。0064 已合入不动，因此该列在本 revision 里 `ALTER TABLE` 追加。

五条约束刻意写进数据库而不是留给调用方自觉：

1. `mcp_access_token` 的**主键即 `user_id`**（"同一个人两条令牌"在结构上不可表达），
   且**没有任何明文列或指纹列**；
2. `mcp_access_token.token_cipher` 的 **CHECK 钉住我方签发格式的精确 envelope**
   （`^[A-Za-z0-9+/]{86}==$`：明文恒 43 字符 → 密文恒 64 字节 → base64 恒 88 字符）。
   它挡得住"把原样令牌明文写进这一列"，**即使绕过全部应用层、直接执行 SQL 也会被拒**；
   但它**不证明内容真的经过加密**——一段恰好 88 字符的合规 base64 文本仍能写进来，
   内容正确性由解密路径负责（解不开即失败关闭）。措辞刻意保守；

   与签发格式耦合：换令牌长度或分组模式时这条 CHECK 必须同步改。另一个已知代价是
   它与下面的禁改触发器合在一起，会让一个"形状合规但解不开"的值把那一行**砖化**
   （只能删行重签）——取舍理由见 revision 文件头部；
3. `mcp_access_token` 的 **BEFORE UPDATE 触发器让密文改不掉**（`user_id` /
   `token_cipher` / `issued_at` 一经写入不可改；签发走 `ON CONFLICT DO NOTHING`，
   不触发它）。覆盖会让库里的密文与已经发布出去的那一份分叉，而新值送不到消费方；
4. `mcp_sync_check.result` 带 CHECK 的五路取值域，加上**一条完整 CHECK 表达五路各自的
   精确形状**（就绪必须看见 > 0 条且无错误码；等待中必须有错误码且观察值只能缺省或为零；
   技术失败与两类非探针终态必须有错误码且不得携带观察值）。只挡"就绪没观察值"是不够的
   ——`waiting` 带着 `metric_count=5` 或 `technical_failure` 带着观察值，读起来都像
   "探针跑通了看见了指标"，而它们恰恰是"没就绪"；
5. `UNIQUE (user_id, permission_version, attempt_no)` 加上由数据库取号、并以事务级
   advisory lock 串行化的 `attempt_no`（进程重启后不会重号覆盖既有尝试，并发记账也不会
   有一方撞 `UNIQUE` 中止而丢掉一次已经真的发出去的探针记录）。

与数据库设计蓝本的四处差异（`id` 用 ULID、删掉三个 `observed_*` 列、`result` 五态、
`detail` 换成 `error_code`）与"为什么当前不提供令牌轮换"的取舍详见 revision 文件头部注释。

两张表本 revision 新增，前滚兼容；`downgrade()` 按依赖反序删表并删触发器函数，不存在
需要回填的历史值。

## `0066_onboarding_notice_outbox`（迟到就绪恢复的「开通完成」通知 outbox）

`V-开通-18` 外部独立审查 F1 修复。一张新表 `onboarding_completion_notice`：迟到就绪
恢复职责（首次开通十五分钟同步超时之后仍然把用户捞回来确认）把 `app_user.
provisioning_state` 推进到 `active` 与向本表排一条待发「开通完成」通知放进**同一个
数据库事务**，堵住"状态已经推进、但通知发送失败/进程崩溃后永久收不到"这条路——
「active 但永远不告知」与「永远不 active」对用户是同一种失败。

形状照 `0064` 的 `publish_outbox`，但更简单：只有 `pending` → `delivered` 两态，
**没有第三态**——早期版本给"收件人暂时查不到"配过一个 `skipped` 终态，外部独立审查
第三轮坐实这原路复活了 F1 要堵的洞（接口分辨不出"暂时"与"永久"，提前判死会让一个已经
`active` 的人永远等不到通知）：收件人暂时不可用现在仍然留在 `pending`、按既有退避
重试，真正的账号删除由 `user_id` 上的 `ON DELETE CASCADE` 处理。**没有持久化的
`publishing` 中间锁定态**，但认领与发送仍是两次独立调用，中间**确实存在**崩溃窗口
——只是没有专门的数据库状态标记它：崩溃发生在发送前，下一次到期原样重认领，不丢不发；
崩溃发生在**发送成功之后、确认送达之前**，会用同一个 `dedupe_key` 重发一次，这条"至少
一次投递"的残余窗口如实登记在 `apps/scheduler/late_readiness_recovery.py` 的模块文档。

三条约束刻意写进数据库而不是留给调用方自觉：

1. `dedupe_key` 上的 `UNIQUE` 约束——同一用户同一版权限只应该有一条待发通知，适配器
   插入时 `ON CONFLICT (dedupe_key) DO NOTHING`，因此"推进 active"这一步无论被调用
   多少次，通知只会被排出一次；
2. `delivered_at` 与 `status='delivered'` 互为充要（与 `0064` 的 `published_at`/
   `status='published'` 同一条纪律：一条 `pending` 却带着送达时间的行会被下游读成
   已经送达）；
3. 触发器把 `content_expires_at` 固定为 `created_at + 2160 hours`，并禁止改写
   `created_at`/`user_id`/`permission_version`/`dedupe_key` 四个锚点——与 `0064`/
   `0065` 同型。到期整行删除（本表没有可识别内容列可擦）只覆盖已送达
   （`delivered`）的行；**`pending` 的行永远不会被到期删除**，即使触发器已经给它
   算出了 `content_expires_at`：删掉一条还在等待送达的通知，等于让一个已经写成
   `active` 的用户永远收不到那句话，这正是本表要堵住的洞。

本表本 revision 新增，前滚兼容；`downgrade()` 删表并删触发器函数，不存在需要回填的
历史值。

## `0067_admin_registry`（服务端管理员角色登记表）

[Issue #95](https://github.com/Moshuiwang/lingxi/issues/95) S-M-01（2026-08-24 范围
重定）。一张新表 `admin_registry`：飞书身份（`feishu_open_id`）+ 三类角色授予状态
（`permission_admin_granted`/`ops_admin_granted`/`super_admin_granted` 三个布尔列，
不拆成逐角色行——决策记录"三类角色合并授予"）+ 条目状态（`entry_status`，
`active`/`revoked`）+ 时间戳（`granted_at`/`revoked_at`/`created_at`）。

**不是**数据库设计早先为管理 MCP 预留的 `admin_identity`/`admin_role` 两张表——那两个
名字继续标记"未建"，留给管理 MCP 真正立项时按那时的需要设计；本表只承接私聊管理
命令面（方案 A）当前真正要用的最小字段。详见 revision 文件头部注释。

判定语义：默认拒绝（没有一条 `entry_status='active'` 命中即非管理员）、每次请求
实时读表（消费方代码不得引入缓存）、唯一活跃身份由部分唯一索引
（`admin_registry_active_identity_idx`，只约束 `entry_status='active'` 的行）强制。

本 revision 不提供任何写路径的触发器或应用代码——唯一的写入口是一次性种子命令
`python -m lingxi.apps.admin_bootstrap`（幂等 `INSERT ... ON CONFLICT DO NOTHING`），
不落任何真实 open_id 进仓库；登记表的授予/撤销写动作（本人确认卡 + 审计）留给
S-M-02（[#96](https://github.com/Moshuiwang/lingxi/issues/96)）。

表本 revision 新增，前滚兼容；`downgrade()` 直接删表，不存在需要回填的历史值。

## `0072_local_permission_override`（本地权限覆盖表）

[Issue #319](https://github.com/Moshuiwang/lingxi/issues/319) S-P-1a（产品负责人
2026-08-26 裁定，推翻 [2026-08-24 决策记录](../docs/决策记录/2026-08-24-管理员职责集与银河外权限动作边界.md)
第 4 条「本地开通/扩权：不做」）。一张新表 `local_permission_override`：管理员经
确认卡对个别用户补授（`grant`）或收窄（`suppress`）的公司×指标级权限，供未来
每日权限重算/开通链聚合与银河翻译结果取并集再减集（真实权限 `= (银河翻译 ∪
本地授权) − 本地抑制`）。

**一张表双极性**（编排者裁定，覆盖 #319 字面把「本地授权」「本地抑制」写成并列
两个机制的读法）：`direction IN ('grant', 'suppress')` 同表，理由是两者共享完全
相同的形状且需要合并判定「同一 user×公司×指标 同时有一条 grant 与一条
suppress 时 suppress 赢」，拆两张表会让这类判定变成一次跨表 UNION。

三条约束刻意写进数据库而不是留给调用方自觉：

1. `pending_action_id NOT NULL REFERENCES pending_action(id)`——结构性堵死"没有
   确认卡就能写入本地权限"这条路径（#319「可观察完成标准」第二条），任何一次
   `INSERT` 如果不先有一条真实存在的 `pending_action` 行可引用，在数据库层面
   就会失败；
2. `entry_status`（`active`/`revoked`）与 `admin_registry`（迁移 `0067`）同一
   惯例，收回是同一行状态翻转（软删除，历史留痕），配套 CHECK 让
   `revoked_at`/`revoked_pending_action_id` 与 `entry_status='revoked'` 互为
   充要；
3. `local_permission_override_active_unique_idx`（`user_id, direction,
   company_id, metric_name` 上 `WHERE entry_status = 'active'` 的部分唯一
   索引）防止重复发起同一笔授权/抑制堆出多条冗余生效行，但不同 `direction`
   允许共存——那正是「suppress 赢」判定需要的输入。

**没有任何有效期/到期/复核列**：产品负责人在 #319 明确裁定本地覆盖「不设有效期、
不设定期复核」，与 `pending_action` 的十分钟确认窗口是两回事，不适用。

本卡（S-P-1a）只交付表结构 + 纯函数语义（`core/permission/local_override.py`）
+ 读写适配器（`adapters/postgres_local_permission.py`）；命令面（管理员如何发起
一笔授权/收回，S-P-1b）与聚合点接线（银河翻译结果与本地覆盖取并集，S-P-3）均
不在本 revision 范围。当前 `pending_action.action_type` 的 CHECK（迁移 `0068`）
尚未加入本地权限专属取值，S-P-1b 落地时需要一次新增迁移补上。

表本 revision 新增，前滚兼容；`downgrade()` 直接删表，不存在需要回填的历史值
（本 revision 未在任何环境应用过）。

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

**2026-08-23 #146 清退：`001`/`002`/`003`/`005` 已全部删除，目录已随 #146 清退批（2026-08-23）整体移除（001/002/003/005 连同其唯一消费者一并清退；007/008 冻结文件头部「001/005 仍留在仓库里」的表述自此失准——冻结文件逐字节不可改，以本行登记为准）。** 它们此前
服务于 Bot-Test 受控验证与既有测试（[代码框架第五节](../docs/技术设计/代码框架.md)），
其中 `001`/`003`/`005` 已分别被正式迁移 `008`/`006`/`007` 取代，`002` 所属的浏览器
OAuth 路径已被 2026-07-28 决策排除；它们此前**不属于生产链**、不参与上面的任何检查，
也不在 alembic 的 `script_location` 扫描范围内。

处置依据：真实 `OnboardingRunner` 已于 2026-08-21 通过 L4a，#67/本文件此前登记的清退
解除条件成立，产品负责人授权按调用关系逐项核对后清退：

- `001_create_app_user.sql`（原供 `tests/test_identity_postgres.sh` 使用）：该测试脚本
  自身头部已注明「本脚本断言的旧 `app_user` 形状只对 001 成立，随测试资产清退一并退休」，
  001 相关断言已由 `tests/test_identity_postgres_records.py` 对正式表（006-008）等价覆盖；
  冻结迁移 `008_create_app_user.sql` 头部注释本就把废弃时点显式委托给「验收人在 PR
  阶段决定」。测试脚本随之整体删除，`verify_repository.sh` 里的调用一并移除。
- `002_create_onboarding_progress.sql`、`003_create_feishu_user_refresh_token.sql`
  （原供 `tests/test_identity_postgres.sh`、`tests/test_refresh_token_postgres.py`、
  `adapters/refresh_tokens.py`、`adapters/postgres_onboarding.py` 使用）：随「飞书私聊卡片
  → 首次开通」Bot-Test 资产簇整体清退，调用关系核对确认没有任何生产入口消费者。
  `tests/test_refresh_token_postgres.py` 整体删除。
- `005_create_feishu_org_snapshot.sql`（原供 `scripts/sync_feishu_org_snapshot.py` 使用）：
  该脚本已被 [#250](https://github.com/Moshuiwang/lingxi/issues/250) 的 `OrgSnapshotSyncDuty`
  取代（生产侧现在每 UTC 日写真实 `feishu_org_*` 快照表），脚本随之删除；冻结迁移
  `007_create_feishu_org_snapshot.sql` 头部注释同样把废弃时点委托给「验收人在 PR 阶段
  决定」，脚本删除后 005 已无任何消费者。

历史沿革（保留供追溯）：2026-08-06 曾按「保留最小集」口径登记本轮清退清单为空
（[#55 盘点](https://github.com/Moshuiwang/lingxi/issues/55#issuecomment-5201705742)）；
2026-08-09 #67 阶段 B 交付正式重授权入口后，`002`、`003` 因仍是当时唯一跑通的开通验证
入口而继续保留，废弃时点顺延到 E4 真实 onboarding runner 通过 L4a 之后——即本次触发
清退的条件。
