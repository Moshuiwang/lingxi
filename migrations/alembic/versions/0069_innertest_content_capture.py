"""内测轮内容级采集表：用户问题/模型回答原文、工具调用详情，凭据形状已过滤。

Revision ID: 0069_innertest_content_capture
Revises: 0068_pending_action
Create Date: 2026-08-25

Issue #251（内测轮开工前置三项·第二项）/ #304（批次 3）。产品负责人 2026-08-24
裁定：内容级采集**全量开启**——用户问题、模型回答、工具调用详情，原文与结果正文
不设限（决策留痕见 #304 issuecomment-5391271841）；结构约束不变：默认关闭、保留
上限仍是数据库设计第九节的九十天一般上限、凭据类一律排除、仅受控查询、进库不
进群，正式环境「日志不含业务正文」纪律不变。

## 与 chat_message/audit_event（数据库设计第八节，仍"未建"）的关系

本表**不是**那两张表的提前落地。``chat_message``/``audit_event`` 是面向"公司内部
审计在九十天内可复核完整聊天记录"这条**长期、默认开启、不脱敏**的合同能力设计
的；本表是"内测轮观察—修复循环"这条**临时、默认关闭、只在 stage、凭据类必须
过滤**的工程能力，两者的开关姿态、脱敏姿态与生命周期都相反，混进同一张表会让
未来 ``chat_message`` 真正落地时不得不先处理一批语义不一致的历史行。表名刻意带
``innertest_`` 前缀，与 ``core/identity/innertest_roster_gate.py`` 同一命名边界。

## 写入点与开关（结构性保证：正式环境即使配了也不得生效）

``src/lingxi/apps/worker/config.py`` 的 ``LINGXI_INNERTEST_CONTENT_CAPTURE``
（须精确为 ``"1"``）与第二确认变量
``LINGXI_INNERTEST_CONTENT_CAPTURE_ENVIRONMENT_CONFIRM``（须精确等于该文件登记的
字面量 ``CONTENT_CAPTURE_ENVIRONMENT_CONFIRM_VALUE``）**同时满足**才生效；两个
变量默认都不生效（``WorkerConfig.innertest_content_capture_enabled`` 默认
``False``）。写入发生在 ``apps/worker/service.py`` 的 ``_process_task`` 收口处，
经 ``adapters/postgres_content_capture.py`` 的 ``PostgresContentCaptureWriter``；
失败降级为结构化审计日志，不影响任务主流程。

选型理由（研究结论：``deploy/compose.stage.yaml`` 与 ``compose.prod.yaml`` 结构
完全相同，唯一差异是各服务从哪个不入库的宿主机本地 env 文件读值，代码里当前不
存在任何"这是 stage 还是生产"的运行期判据）：单一布尔开关一旦被整份部署文件误
续用（例如把 stage 的 worker-queue env 文件复制进生产对应文件）就会随之带过去，
因此要求同时命中一个不像会被顺手抄对的精确字面量，把"部署配置漂移"与"确有其人
在 stage 显式选择开启"的门槛拉开一截；``scripts/ci/check_deploy_contract.py`` 的
``check_content_capture_prod_guard`` 断言该字面量与两个变量名都不出现在任何入库
的 compose 编排文件里，``deploy/.env.example``/``deploy/验收前部署配置清单.md``
同步登记"生产环境禁止配置"。**已知残余风险（如实登记）**：这仍然是环境变量层面
的约定，不是无法被人为绕开的技术保证——如果运维把两个变量的值一起复制进生产的
未入库 env 文件，代码层面无法区分"这是从 stage 抄过来的"；这是当前 stage/生产
共用完全相同镜像与编排结构下，仓库能提供的最强机械保证，最后一道防线仍是部署
操作纪律。完整判定语义见 ``WorkerConfig._innertest_content_capture`` 的模块内
文档。

## 凭据形状过滤

写入前对 ``question_content``/``answer_content`` 与 ``tool_calls`` 里的每个字符串
叶子过 ``core/execution/audit.py`` 的 ``redact_free_text_with_count``（复用执行层
既有的凭据形状检测——赋值语句、``bearer``/``basic`` 认证头、含数字或超长的裸令牌
串），命中即替换为占位符并计数进 ``*_redaction_count`` 列。已知局限与
``V-审计-03`` 相同：纯字母且短于 32 字符的裸秘密不会被过滤，``*_redaction_count``
是"命中并替换了几处"的可观测计数，不是"零命中即无凭据"的证明。

## 保留：90 天结构性上限 + 轮次结束即清的运维入口

与 ``0059``/``0064`` 同型：``BEFORE INSERT OR UPDATE`` 触发器把 ``expires_at``
固定为 ``created_at + 2160 小时``（90 天，数据库设计第九节的一般内容上限），
调用方传入值被忽略；这是**结构性最长上限**，不是"内测轮期间就保留这么久"。

内测轮的实际保留期望远短于 90 天——「轮次结束即清」。当前**没有**自动判定"轮次
何时结束"的机制（那是产品/运营判断，不是可由程序推导的时间点），因此没有配一个
scheduler 定时清理职责；到期靠 90 天触发器兜底，轮次结束时由运维显式执行下面这条
受控 SQL（与 ``migrations/README.md``「运维紧急删除路径」同一姿态，登记在这里而
不是新建一个特权清理函数——本表当前未纳入 ``lingxi_retention_owner`` 的限权清理
面，运行时进程尚未以任何限权角色连库，是与 ``0059``/``0064`` 相同的已知留白，
见两者各自文件头）：

.. code-block:: sql

    -- 轮次结束即清：删除某个内测轮时间窗口内的全部采集行。
    -- <round_start>/<round_end> 按实际内测轮起止时间替换（UTC）。
    DELETE FROM innertest_content_capture
     WHERE created_at >= '<round_start>' AND created_at < '<round_end>';

    -- 或者：清空全部现存采集行（下一轮开始前的整体清空）。
    TRUNCATE innertest_content_capture;

两条语句都不需要特殊角色——当前 ``lingxi_app`` 对本表有普通读写权限（同
``task_delivery_event``/``publish_outbox`` 的既有姿态，未来升级到限权清理函数是
可预见的后续加固项，不在本 Story 完成标准内）。执行前建议先
``SELECT count(*) FROM innertest_content_capture WHERE ...`` 核对范围；执行后无需
额外收尾——``expires_at`` 触发器与本次删除是两条独立、互不依赖的路径。

## 索引

``innertest_content_capture_task_idx``：按 ``task_id`` 查询（测试与运维核对的唯一
读路径，见 ``adapters/postgres_content_capture.py`` 的 ``read_recent_for_task``；
结构约束「仅受控查询」——本 Story 不建面向使用者的查询界面）。
``innertest_content_capture_expiry_idx``：90 天到期清理扫描键，形状同
``publish_outbox_content_expiry_idx``（不带谓词，已清理的行也要能被"还有多少未
到期"之类的核对读到）。

``downgrade()`` 真实可执行：表与触发器函数都是本 revision 新建的，按依赖反序整体
删除，不存在需要还原的历史行。
"""

from __future__ import annotations

from alembic import op

revision: str = "0069_innertest_content_capture"
down_revision: str | None = "0068_pending_action"
branch_labels: str | None = None
depends_on: str | None = None


_UPGRADE_SQL = r"""
CREATE TABLE innertest_content_capture (
    id                         TEXT        PRIMARY KEY,           -- ULID, icc_*
    -- CASCADE：任务本身被清理编排删除时（当前运行环境删除，见数据库设计第五节）
    -- 该任务的采集内容没有独立留存的产品理由，一并消失。
    task_id                    TEXT        NOT NULL REFERENCES task(id) ON DELETE CASCADE,
    worker_id                  TEXT        NOT NULL,
    -- 用户问题原文，凭据形状已过滤；按 2026-08-24 裁定不设截断上限。
    question_content           TEXT        NOT NULL DEFAULT '',
    question_redaction_count   INT         NOT NULL DEFAULT 0 CHECK (question_redaction_count >= 0),
    -- 模型回答原文（回合内消息流的最终正文，未经出口安全约束投影），
    -- 凭据形状已过滤；按裁定不设截断上限。
    answer_content              TEXT        NOT NULL DEFAULT '',
    answer_redaction_count      INT         NOT NULL DEFAULT 0 CHECK (answer_redaction_count >= 0),
    -- 工具调用详情：[{tool_use_id, tool_name, tool_input, result_summary,
    -- redaction_count}, ...]，形状见 core/innertest_content_capture.py 的
    -- ContentCaptureRecord.tool_calls_payload。result_summary.content 按 4000
    -- 字节截断（"结果摘要"字面含义），tool_input/tool_name 不截断。
    tool_calls                  JSONB       NOT NULL DEFAULT '[]'::jsonb,
    tool_calls_redaction_count  INT         NOT NULL DEFAULT 0 CHECK (tool_calls_redaction_count >= 0),
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at                  TIMESTAMPTZ NOT NULL              -- 触发器固定为 created_at + 2160 小时
);

-- 测试与运维核对的唯一读路径（结构约束「仅受控查询」，见文件头）。
CREATE INDEX innertest_content_capture_task_idx
    ON innertest_content_capture (task_id, created_at DESC);

-- 90 天到期清理扫描键，形状同 publish_outbox_content_expiry_idx：不带谓词，
-- 已清理（含轮次结束手工清空）的行也要能被"还有多少未到期"之类的核对读到。
CREATE INDEX innertest_content_capture_expiry_idx
    ON innertest_content_capture (expires_at);

-- 与 0057/0058/0059/0064 同型：到期时间由来源时间推导，调用方传什么都会被覆盖；
-- created_at / task_id 一经写入不可改——它们是"这条采集记录归属哪次回合、何时
-- 产生"的锚点，改写任一项都等于伪造历史。
CREATE OR REPLACE FUNCTION innertest_content_capture_fix_expiry() RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.expires_at := NEW.created_at + INTERVAL '2160 hours';
    IF TG_OP = 'UPDATE' THEN
        IF NEW.created_at IS DISTINCT FROM OLD.created_at THEN
            RAISE EXCEPTION '不允许修改采集记录的创建时间';
        END IF;
        IF NEW.task_id IS DISTINCT FROM OLD.task_id THEN
            RAISE EXCEPTION '不允许修改采集记录所属的任务';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER innertest_content_capture_expiry
    BEFORE INSERT OR UPDATE ON innertest_content_capture
    FOR EACH ROW EXECUTE FUNCTION innertest_content_capture_fix_expiry();
"""

_DOWNGRADE_SQL = r"""
DROP TABLE IF EXISTS innertest_content_capture;
DROP FUNCTION IF EXISTS innertest_content_capture_fix_expiry();
"""


def _execute_verbatim(connection, sql: str) -> None:
    """与 0057/0058/0059/0060/0061/0062/0063/0064 同型：不走 ``op.execute()``，避免
    空参数集触发插值模式（本段 DDL 的 ``RAISE EXCEPTION`` 文案若将来加 ``%``
    占位符会被拒绝）。"""

    with connection.connection.cursor() as cursor:
        cursor.execute(sql)


def upgrade() -> None:
    _execute_verbatim(op.get_bind(), _UPGRADE_SQL)


def downgrade() -> None:
    _execute_verbatim(op.get_bind(), _DOWNGRADE_SQL)
