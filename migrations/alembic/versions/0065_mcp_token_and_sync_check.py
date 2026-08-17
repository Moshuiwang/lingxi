"""MCP 访问令牌与就绪确认记录：token 只以密文落库，每次探针尝试独立成行。

Revision ID: 0065_mcp_token_and_sync_check
Revises: 0064_permission_publish_outbox
Create Date: 2026-08-17

[Issue #156](https://github.com/Moshuiwang/lingxi/issues/156) 的 S-C-02（Epic C 第二个
Story）。两张新表 + 一列：

- ``mcp_access_token``：Lingxi 为建档用户签发的问数 MCP 访问令牌，**只存密文**；
- ``mcp_sync_check``：发布之后「当前用户 MCP 是否就绪」的每一次判定，逐次成行；
- ``publish_outbox.created_record_id``：**这条意图自己建过的那一行**。它与既有的
  ``external_record_id``（审计语义：上一次尝试操作了哪一行）是两件事，理由见下面
  ``ALTER TABLE`` 上方的注释——混用会让既有 26 行在一次更新失败之后被永久判成冲突。

## ``mcp_access_token``：明文写不进来，密文写进来就改不掉

产品合同「凭据不进代码、日志、数据库、用户环境」在这张表上的落点是**两条数据库约束**，
不是应用层的自觉：

1. **``token_cipher`` 的 CHECK 钉住我方签发格式的精确 envelope**：
   ``^[A-Za-z0-9+/]{86}==$``（明文恒 43 字符 → 密文恒 64 字节 → base64 恒 88 字符）。
   因此"把原样令牌明文写进这一列"**即使绕过全部应用层代码、直接执行 SQL 也会被拒**。
   **它不证明内容真的经过加密**——一段恰好 88 字符的合规 base64 文本仍能写进来；
   内容正确性由解密路径负责（解不开即失败关闭）。措辞刻意保守，不宣称超出它能力的事。
2. **BEFORE UPDATE 触发器让密文改不掉**：``user_id`` / ``token_cipher`` / ``issued_at``
   一经写入不可改。签发走 ``INSERT ... ON CONFLICT DO NOTHING``，不触发它。

这两条补的是同一个洞：只写"表里没有明文列"是不够的——``token_cipher`` 本身是一个可写的
裸 ``TEXT``，谁都可以往里写明文或覆盖既有密文。

**两条约束合在一起有一个已知代价**：一个通过了 CHECK 但内容不对的值（合规 base64、
却解不开）写进去之后，触发器会让它**改不掉**，那一行就砖化了，只能删行重签。这是刻意的
取舍——允许覆盖会让"已经发布出去的令牌被悄悄换掉"重新成为可能，而后者的失败形态
（用户某天忽然没有权限、且没人知道为什么）比前者（响亮的解密失败 + 删行重签）更坏。

令牌明文由 ``secrets.token_urlsafe(32)`` 生成，经 AES-256-CBC 加密成 ``token_cipher``
后才落库；明文只在签发那一瞬间与后续按需解密（就绪探针、将来的用户环境写入）时存在于
内存。加密协议逐字执行 biai-agent 的 ``docs/mcp/mcp-encryption-spec.md`` v1
（已签署的交接协议），实现在 ``src/lingxi/adapters/mcp_token_cipher.py``。

**刻意不加明文指纹 / 哈希列**：一个 ``token_hash`` 列看上去无害，实际会让「拿一份
候选明文来碰」变成一次本地比对；而这张表的消费方（问数 MCP）走的是密文解密后等值匹配，
根本不需要指纹。多一列就是多一条泄露面，且没有任何读取方。

**主键即 ``user_id``（一对一）**：一个用户在同一时刻只能有一个有效令牌。用独立 ULID 主键
加一个唯一索引也能达成同样效果，但那会让"同一个人两条令牌"在**结构上**变得可表达，
只靠约束去挡；而这里连表达都表达不出来。

**没有轮换列，也没有轮换路径**（应用层同样不提供）：Lingxi 只在**新建**发布行时写入
``token_cipher``，更新既有行时**不清空也不覆盖**它（`V-权限-11`）。因此轮换一个已经发布
出去的令牌，在当前发布语义下**无法送达消费方**——库里换了新密文，外部表里还是旧的，
用户侧表现为"某天开始问数忽然没有权限"。要支持轮换，必须先由产品负责人决定 Lingxi 是否
可以覆盖既有行的 ``token_cipher``；在那之前提供一个"能改库但送不出去"的轮换接口，
比没有轮换更危险。签发因此是**幂等**的：已存在即返回既有那一份，绝不覆盖。

## ``mcp_sync_check``：与数据库设计蓝本的四处差异

蓝本见数据库设计[「四、开通与发布」](../../../docs/技术设计/数据库设计.md#四开通与发布)。
本 revision 是权威，设计文档已同步：

1. **``id`` 是 ULID ``TEXT`` 而不是 ``BIGSERIAL``**：与 ``0057``–``0064`` 建的每一张表
   一致（接口设计「二、通用约定」：内部主键一律 ULID）。
2. **删掉 ``observed_permission_version`` / ``observed_companies`` /
   ``observed_functions`` 三列**：蓝本假设 MCP 能回读权限版本与生效范围，判定是
   「回读比对一致后再发一次最小查询」。**当前权限多维表格没有版本字段**（2026-08-17
   全表回源核对，G-155 终判），回读比对因此不可行；就绪的唯一依据改为**探针法**——
   用该用户的明文令牌对问数 MCP 真实执行一次 ``list_metrics``。没有可比对的观察值，
   就不留三个永远为 ``NULL`` 的列：空列会让将来读表的人以为"曾经比对过"。
3. **``result`` 的取值域是五路分流**（``ready`` / ``no_permission`` / ``waiting`` /
   ``timed_out`` / ``technical_failure``），而不是蓝本的三态。三态把「明确无权限」
   （数据库侧就没有可发布权限，压根没有可等的东西）与「技术失败」（网络、协议、
   解密、数据库异常）都挤进 ``error``，于是运维分不清"这个人本来就没权限"和
   "我们的探针根本没跑起来"。这一列**加 CHECK**：取值域是产品判定的一部分，五路互斥
   是合同要求（与 ``publish_outbox.last_outcome`` 刻意不加 CHECK 的取舍不同——那一列
   是诊断分类，这一列是结论）。
4. **``detail`` 自由文本换成 ``error_code``**：自由文本是凭据与人员资料最常见的泄露
   路径（一次 ``str(exception)`` 就够）。改成错误码之后，这张表里**没有任何可识别内容**
   ——只有内部 ULID、权限版本、次序、时间、结论与错误码。

## ``attempt_no`` 由数据库算，不由调用方传

写入语句取 ``COALESCE(MAX(attempt_no), 0) + 1``（同一 ``user_id`` + ``permission_version``
范围内）。调用方传次序会在**进程重启**后重号：恢复出来的确认会从 1 开始，与已经落库的
第一次冲突。``UNIQUE (user_id, permission_version, attempt_no)`` 因此不是装饰——它挡的是
两个确认流程同时对同一个人同一版权限记账，撞上即失败关闭，而不是安静地互相插队。

## 保留

这张表没有可识别内容列，但 ``user_id`` 仍指向具体的人，因此仍按数据库设计第九节走
九十天上限：触发器把 ``content_expires_at`` 固定为 ``started_at + 2160 hours``；到期
删行由 ``adapters/postgres_mcp_token.py`` 的 ``purge_expired_checks`` 落实（**删整行**
而不是擦某一列——没有列可擦）。行本身还有一条已经存在的删除路径：``user_id`` 上的
``ON DELETE CASCADE``。``mcp_access_token`` 同样 CASCADE：账号删除即带走令牌密文。

与 ``0059``/``0061``/``0063``/``0064`` 同型，**不授任何表权限**：四个数据库角色的表级
授权当前只在 ``0054`` 里针对它自己那两张表出现，运行时进程也尚未以这些角色连库。

``downgrade()`` 真实可执行：两张表与触发器函数都是本 revision 新建的，按依赖反序整体
删除，不存在需要还原的历史行。
"""

from __future__ import annotations

from alembic import op

revision: str = "0065_mcp_token_and_sync_check"
down_revision: str | None = "0064_permission_publish_outbox"
branch_labels: str | None = None
depends_on: str | None = None


_UPGRADE_SQL = r"""
-- publish_outbox 增一列：**这条意图自己建过的那一行**（S-C-02 加，0064 已合入不动）。
--
-- 为什么不能复用既有的 external_record_id：那一列是**审计**语义——"上一次尝试操作的是
-- 哪一行"，任何尝试（含既有行更新失败）都会经 complete() 的 COALESCE 落进去。拿它当
-- "这一行是我们建的"来用会误伤既有 26 行：它们只要有一次更新读回 uncertain/mismatch，
-- 行 ID 就落进 external_record_id，重试时判据成立、而它们的旧密文当然不等于我方快照，
-- 于是被判成永久 mismatch——这是对 S-C-01「更新可重试收敛」的回归。
--
-- 本列的写入口径**只有一种事实**：``create_row`` 明确返回了记录标识（``action='create'``
-- 且拿到了 ID）。因此它非空 ⇔ 这一行确实是我们建出来的。创建结果不明（没拿到 ID）时
-- 保持 NULL：那种情况下我们无法把"自己建的"与"并发写入方建的"区分开，重试按普通路径
-- 收敛，就绪探针是最终的门。
ALTER TABLE publish_outbox ADD COLUMN created_record_id TEXT;

CREATE TABLE mcp_access_token (
    -- 主键即用户：一个人同一时刻只能有一个有效令牌，"两条令牌"在结构上不可表达。
    -- CASCADE：账号删除编排删掉 app_user 那一行时，令牌密文一并消失。
    user_id      TEXT        PRIMARY KEY REFERENCES app_user(id) ON DELETE CASCADE,
    -- base64(IV(16B) ‖ AES-256-CBC 密文)。**没有明文列，也没有指纹列**（文件头部）。
    --
    -- 下面这条 CHECK 钉的是**我方签发格式的精确 envelope**，不是泛化的"像不像密文"：
    --
    --   明文 `secrets.token_urlsafe(32)` 恒为 43 个字符
    --     → UTF-8 43 字节 → PKCS7 补到 48 字节 → AES-CBC 密文 48 字节
    --     → 16B IV ‖ 48B 密文 = 64 字节 → 标准 base64 恒为 88 字符，且恒以 `==` 结尾。
    --
    -- 因此合法值的形状唯一：`^[A-Za-z0-9+/]{86}==$`。
    --
    -- **它能证明什么、不能证明什么**（措辞刻意保守）：它挡得住"把原样令牌明文写进这一列"
    -- （明文 43 字符、URL 安全字母表，两条都不满足），也挡得住绝大多数手误与半截值；
    -- 它**不证明**内容真的经过加密——一段恰好 88 字符的合规 base64 文本仍能写进来。
    -- 内容的正确性由解密路径负责（解不开即失败关闭，见 adapters/postgres_mcp_token.py）。
    --
    -- **与签发格式耦合，改一处要改两处**：将来若换令牌长度或分组模式，这条 CHECK 必须
    -- 同步改，否则新签发会被自己的数据库拒绝。这是刻意的耦合——宁可在签发时响亮失败，
    -- 也不要让一列"什么都收"的凭据列静默积累坏值。
    token_cipher TEXT        NOT NULL CHECK (token_cipher ~ '^[A-Za-z0-9+/]{86}==$'),
    issued_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 令牌**只能新建，不能改**。签发路径走 INSERT ... ON CONFLICT DO NOTHING（不触发本
-- 触发器），因此正常路径不受影响；这道触发器挡的是绕过应用层的 UPDATE——一次
-- `UPDATE mcp_access_token SET token_cipher = ...` 会让库里的密文与已经发布到外部
-- 表格的那一份分叉，而更新既有发布行时我们不写那一列（V-权限-11），新值永远送不出去，
-- 用户侧表现为"某天开始问数忽然没有权限"。要轮换必须先由产品负责人裁定是否允许覆盖
-- 既有发布行的 token_cipher，那时连同这道触发器一起改。
CREATE OR REPLACE FUNCTION mcp_access_token_immutable() RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.user_id IS DISTINCT FROM OLD.user_id THEN
        RAISE EXCEPTION '不允许修改访问令牌所属的用户';
    END IF;
    IF NEW.token_cipher IS DISTINCT FROM OLD.token_cipher THEN
        RAISE EXCEPTION '不允许覆盖已签发的访问令牌密文';
    END IF;
    IF NEW.issued_at IS DISTINCT FROM OLD.issued_at THEN
        RAISE EXCEPTION '不允许修改访问令牌的签发时间';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER mcp_access_token_no_overwrite
    BEFORE UPDATE ON mcp_access_token
    FOR EACH ROW EXECUTE FUNCTION mcp_access_token_immutable();

CREATE TABLE mcp_sync_check (
    id                 TEXT        PRIMARY KEY,            -- ULID, syn_*
    user_id            TEXT        NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    -- 就绪结论**绑定到这一版权限**：一次成功只对当次绑定的版本有效。0 是 app_user 的
    -- 初始值（还没有过任何权限决定），因此这里恒为正。
    permission_version BIGINT      NOT NULL CHECK (permission_version > 0),
    attempt_no         INT         NOT NULL CHECK (attempt_no > 0),
    started_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at        TIMESTAMPTZ,
    -- 五路互斥，取值域是结论的一部分，因此写进数据库（文件头部差异 3）。
    result             TEXT        NOT NULL
        CHECK (result IN ('ready','no_permission','waiting','timed_out','technical_failure')),
    -- 只有错误码，没有自由文本：自由文本是凭据与人员资料最常见的泄露路径。
    -- 空串与纯空白等同于"没写"，一律拒绝：下面的形状 CHECK 靠 `error_code IS NOT NULL`
    -- 判"未就绪必须说明原因"，而一个 '  ' 能满足 NOT NULL 却什么都没说明。
    -- 判据用 `~ '\S'`（至少有一个非空白字符）而不是 `BTRIM(...) <> ''`：BTRIM 默认只去
    -- 空格，一个制表符能原样通过——实测过。
    -- 长度上限 200：错误码是**码**，不是消息；放宽就会有人往里塞异常正文。
    error_code         TEXT        CHECK (error_code ~ '\S' AND length(error_code) <= 200),
    -- 探针看见的指标条数。就绪要求它 > 0——"明确空结果只证明这次查询没报错"
    -- （产品合同「问数 MCP」一节）。非探针分支（no_permission / timed_out）为 NULL。
    metric_count       INT         CHECK (metric_count >= 0),
    content_expires_at TIMESTAMPTZ NOT NULL,               -- 触发器固定为 started_at + 2160 小时

    -- 同一用户同一版权限的同一次序只允许一行：两个确认流程同时记账时失败关闭，
    -- 而不是安静地互相插队（文件头部「attempt_no 由数据库算」）。
    UNIQUE (user_id, permission_version, attempt_no),

    -- **五路结论各自的精确形状，一条 CHECK 全表达完。**
    -- 分成"只挡 ready 没观察值"和"只挡两类终态有观察值"两条是不够的（二级独立审查
    -- 实测）：那样 `waiting` 可以带着 metric_count=5 落库、`technical_failure` 可以
    -- 带着观察值落库，而这两种行读起来都像"探针跑通了、看见了指标"，恰恰是"没就绪"。
    --   ready             ：必须看见 > 0 条，且没有错误码（看见了就不该有错误）
    --   waiting           ：必须有错误码；观察值只能缺省（明确拒绝）或恰为 0（空结果）
    --   technical_failure ：必须有错误码，且**没有**观察值——探针没跑通，任何数字都是假的
    --   no_permission     ：必须有错误码，且没有观察值（这一路压根不发探针）
    --   timed_out         ：必须有错误码，且**不得保留可用观察值**。多数情况下它压根
    --                       没发探针（预算耗尽收口）；但 `success_after_deadline`
    --                       那一路是探针成功返回之后被降级的——观察值在那时一并丢弃，
    --                       因为它已经落在承诺窗口之外，留着会被读成"这一轮其实看见了"。
    -- 与 core 的 _require_attempt_shape 一一对应，两处必须同时改。
    CHECK (
        CASE result
            WHEN 'ready'             THEN metric_count IS NOT NULL AND metric_count > 0
                                          AND error_code IS NULL
            WHEN 'waiting'           THEN error_code IS NOT NULL
                                          AND (metric_count IS NULL OR metric_count = 0)
            WHEN 'technical_failure' THEN error_code IS NOT NULL AND metric_count IS NULL
            WHEN 'no_permission'     THEN error_code IS NOT NULL AND metric_count IS NULL
            WHEN 'timed_out'         THEN error_code IS NOT NULL AND metric_count IS NULL
            ELSE FALSE
        END
    )
);

-- 按 (用户, 权限版本) 取这一版的全部尝试：既是 attempt_no 的取号扫描键，
-- 也是"这一版到底等了几次、等了多久"的排查入口。
CREATE INDEX mcp_sync_check_binding_idx
    ON mcp_sync_check (user_id, permission_version, attempt_no);

-- 到期删行的扫描键。
CREATE INDEX mcp_sync_check_expiry_idx
    ON mcp_sync_check (content_expires_at);

-- 与 0057/0058/0059/0064 同型：到期时间由来源时间推导，调用方传什么都会被覆盖；
-- started_at / user_id / permission_version / attempt_no 一经写入不可改——它们是
-- "这条结论属于谁的哪一版权限的第几次"的锚点，改写任一项都等于伪造历史。
CREATE OR REPLACE FUNCTION mcp_sync_check_fix_expiry() RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.content_expires_at := NEW.started_at + INTERVAL '2160 hours';
    IF TG_OP = 'UPDATE' THEN
        IF NEW.started_at IS DISTINCT FROM OLD.started_at THEN
            RAISE EXCEPTION '不允许修改就绪确认的开始时间';
        END IF;
        IF NEW.user_id IS DISTINCT FROM OLD.user_id THEN
            RAISE EXCEPTION '不允许修改就绪确认所属的用户';
        END IF;
        IF NEW.permission_version IS DISTINCT FROM OLD.permission_version THEN
            RAISE EXCEPTION '不允许修改就绪确认绑定的权限版本';
        END IF;
        IF NEW.attempt_no IS DISTINCT FROM OLD.attempt_no THEN
            RAISE EXCEPTION '不允许修改就绪确认的尝试次序';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER mcp_sync_check_expiry
    BEFORE INSERT OR UPDATE ON mcp_sync_check
    FOR EACH ROW EXECUTE FUNCTION mcp_sync_check_fix_expiry();
"""

_DOWNGRADE_SQL = r"""
DROP TABLE IF EXISTS mcp_sync_check;
DROP FUNCTION IF EXISTS mcp_sync_check_fix_expiry();
DROP TABLE IF EXISTS mcp_access_token;
DROP FUNCTION IF EXISTS mcp_access_token_immutable();
ALTER TABLE publish_outbox DROP COLUMN IF EXISTS created_record_id;
"""


def _execute_verbatim(connection, sql: str) -> None:
    """与 0057–0064 同型：不走 ``op.execute()``，避免空参数集触发插值模式
    （本段 DDL 的 ``RAISE EXCEPTION`` 文案将来若加 ``%`` 占位符会被拒绝）。"""

    with connection.connection.cursor() as cursor:
        cursor.execute(sql)


def upgrade() -> None:
    _execute_verbatim(op.get_bind(), _UPGRADE_SQL)


def downgrade() -> None:
    _execute_verbatim(op.get_bind(), _DOWNGRADE_SQL)
