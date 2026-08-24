#!/usr/bin/env python3
"""归属核对门禁（Issue #238）：凡标注「产品合同明令」「合同要求」的断言必须能对上。

代码框架第三节曾写「凭据：不进代码、日志、数据库、用户环境（产品合同明令）」——
`docs/产品合同与外部边界.md` 正文从未提过"用户环境"这个范围，那其实是架构设计
自己的从紧要求被错记成合同条款。散文约束挡不住这类错误：只有把每一处"标注为
合同"的断言变成一条会核对、会变红的登记，才挡得住下一次同样的笔误。

**这道闸证明的是什么，不是什么**（2026-08-19 三路独立复查后更正措辞，Issue
#238）：它证明"这句归属已经被人显式核对过一次、登记进了下面的表、且登记的原文
在源文件里依然一字不差地存在（没有过期）"，因此**可以被下一个人复核**。它**不
证明**这句话在语义上真的成立——合同章节存在、原句字面未变，不代表原句的转述
准确反映了合同的意思（那需要人读两边的正文去判断，机器做不到）。同理，一处归属
第一次登记时"对上了"，只说明登记的那一刻核对过；合同正文后续修订后，登记表的
摘录仍会显示"在源文件里找到了"，但可能已经不再准确反映新版合同——这也是机械
核对无法覆盖的部分，只能靠合同正文修订时的人工复审兜底。

**为什么不能只靠正则找到"合同要求"就直接核对文字**：本仓库绝大多数归属断言是
**转述**而不是逐字引用（"银行家式复述" vs "逐字复制"），例如"合同要求两者不
一致时不得视为发布完成"转述的是合同原文"数据库记录与飞书多维表格发布结果不
一致…都不能视为 Lingxi 侧发布已经完成"。逐字子串匹配会把几乎所有转述都误判为
查无对应；而放宽成"语义相近就算"又没法用程序判定。因此本脚本采用**登记制**：

1. 本文件内置一份 ``GROUNDED_ATTRIBUTIONS`` 登记表，逐条记录"哪个文件的哪一行
   （用该行去除首尾空白后的**完整原文**精确定位，不是一段任意长度的摘录）对应
   产品合同的哪一节"——这是一次性的人工核对结果，2026-08-19 逐条核对产品合同
   正文写成，不是程序自动推导的。
2. 门禁做三件**机械**的事：(a) 登记表引用的合同章节必须真实存在于
   `docs/产品合同与外部边界.md`；(b) 登记表登记的那一行原文必须真的还能在它
   标注的源文件里找到（防止摘录过期还挂在表里）；(c) 仓库里每一处标注"产品
   合同明令"或"合同要求"这类归属短语的行，都必须与登记表里某一条**逐字相等**
   ——用完整行文本做精确匹配而不是"包含即算"，是 2026-08-19 三路独立复查实测
   坐实的两个绕过面倒逼的设计：①一个很短的摘录（如裸的"合同要求"四个字）用
   子串匹配会覆盖住这个文件里**此后新增的任何一行**，只要那一行里出现过这四个
   字；②往一行**已经登记过**的话后面继续追加全新的、从未核对过的归属声明，
   子串匹配同样会因为"旧摘录仍是新行的子串"而放行。改成整行逐字相等后，这一
   行只要有一个字符的变化就不再等于登记值，必须重新核对登记，两个绕过面同时
   关闭。
3. 少数几处归属经核对后仍有疑问（措辞源自具体 Issue 或 PR 的产品负责人 / 独立
   复核决定，而非 `产品合同与外部边界.md` 正文本身；见 ``REGISTERED_EXCEPTIONS``），
   按 AGENTS.md「宁可让门禁带一个明确登记的例外，也不要偷偷改合同」处理：不
   静默放行，登记为例外（强制携带来源 Issue/PR 号、裁定日期、裁定人，见
   ``RegisteredException``）并在门禁**每一次运行**的输出里可见地报出来（含
   判红的那一次——例外不能只在通过时才被看见），留给编排者与产品负责人裁定。

任何人往仓库里新加一句"产品合同明令 XXX"而不登记，门禁直接红；任何人把合同正文
的章节改名或删除导致登记表的引用失效，门禁也直接红；任何人往一行已登记的归属
后面追加新断言，或试图用一个过短的摘录覆盖住未来的新增行，门禁同样直接红——这
几条挡的都是"归属只在写下的那一刻被人读一遍，此后再没有人核对过"的腐烂路径。

**已知的一次真实腐烂**（2026-08-19，同批次三方合并演练触发，不是假设）：
`REGISTERED_EXCEPTIONS` 里"告警不可用时主流程行为有明确定义"那条最初登记在
`src/lingxi/apps/scheduler/__init__.py`；#237（拆分 scheduler 装配入口）把这段
文字连同它所在的类整体搬到了新文件 `src/lingxi/apps/scheduler/
alerting_assembly.py`，登记表若不跟着更新，门禁会在合并后立即判红（旧路径的
摘录消失 + 新路径出现一条未登记的归属）。这恰好证明了机制在真实生效——登记表
已按新路径更新，见下方对应条目。

扫描失败必须失败关闭：合同文档或任何一个被扫描文件读不出来，都直接判红。
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CONTRACT_DOCUMENT = REPOSITORY_ROOT / "docs" / "产品合同与外部边界.md"

# 归属触发词：本仓库里明确"把这句话的权威记成产品合同"的写法。M6（2026-08-19
# 三方复查最后一轮）指出：维护一份固定短语清单永远会漏——从两个短语扩到八个
# 之后，"产品合同禁止…""产品合同指出…""依据产品合同…"这类没进清单的写法
# 仍然逃逸。改成一条正则族：「(产品合同｜合同) + 可选"明确" + 归属动词」覆盖
# 后缀写法（"合同要求""合同明确排除"……），「按/依据/根据 + 合同」覆盖前缀
# 写法（"按合同""依据产品合同"……）。新出现的归属动词只要落在这两族里就会
# 被自动捞到，不必再逐个补短语。
# 不匹配裸的"合同"——本仓库大量使用"合同"表示模块自身的接口/服务合同
# （软件工程含义，如"OnboardingRunner.start 的服务合同"），那不是在对产品
# 合同文档做归属声明，不属于本门禁的核对范围。
TRIGGER_PATTERN = re.compile(
    r"(?:产品合同|合同)(?:明确)?(?:要求|规定|明令|禁止|约定|条款|指出|依据|排除)"
    r"|(?:按|依据|根据)(?:产品)?合同"
)

# 「合同条款」这个短语在本仓库还有另一个完全不同的含义：验收矩阵.md 与
# check_acceptance_matrix.py 用"合同条款覆盖清单"特指那份已有的、独立门禁
# （check_acceptance_matrix.py 的 cross_check）守着的机器可读映射表——那是在
# 描述一个已存在的治理机制的名字，不是在对某句具体规则做归属声明。不排除会
# 把这类自我描述误判成待核对的新断言，因此单独排除这几个固定短语。
META_EXCLUDE_PATTERN = re.compile(r"合同条款覆盖清单|合同条款无断言覆盖|产品合同条款\s*→")

# 只扫这四类正式文本文件——与仓库其余 check_*.py 扫描范围一致；.sql 是
# 2026-08-19 复查后新加的，migrations/ 下的顶层历史 SQL 与 alembic revision
# 里同样可能出现归属注释。
SCAN_SUFFIXES = (".py", ".md", ".sh", ".sql")
# 合同文档本身是权威源，不对自己做归属核对。
# 本脚本自身与它的单元测试是唯一例外：它们的源码里大量出现"合同要求"/"合同
# 明令"等字面串——一处是在讨论触发词本身（TRIGGER_PATTERN 的定义、登记表
# 数据、模块 docstring 里的举例），另一处是单元测试构造的字符串字面量夹具——
# 这些都不是在对产品合同文档做归属声明，扫描自己会把整份登记表和触发词定义
# 当成待核对的断言，那是检查工具在核对自己的实现细节，不是在核对产品事实。
EXCLUDED_PATHS = {
    CONTRACT_DOCUMENT,
    Path(__file__).resolve(),
    REPOSITORY_ROOT / "tests" / "test_contract_attribution_check.py",
}

# H1-H3 都算合法的章节标题：H1（合同文档标题本身）用于极少数**引用合同整体
# 而非某一节**的归属（例如"技术设计按合同的章节切分"这类组织性陈述），H2/H3
# 是正文各节。
HEADING = re.compile(r"^(#{1,3})\s+(.+?)\s*$")

# 摘录长度下限：防止用一个极短、容易在别处偶然重现的短语（例如裸的"合同
# 要求"四个字）去"覆盖"未来任何一行恰好包含它的新增内容。配合下方改成的
# 整行精确匹配，这条主要是防止有人直接拿触发词本身当登记内容。
MIN_EXCERPT_LENGTH = 8

# M5：例外的「来源」必须是可点击追溯的 Issue/PR 编号，不接受任意字符串。
EXCEPTION_SOURCE_PATTERN = re.compile(r"^(Issue|PR) #\d+$")


class AttributionCheckError(ValueError):
    """扫描或解析失败——必须失败关闭，不能当作「没有归属断言」悄悄通过。"""


@dataclass(frozen=True)
class GroundedAttribution:
    """一条已核对的归属：``file`` 里逐字等于 ``line`` 的那一行，对应合同 ``section`` 一节。"""

    file: str
    line: str
    section: str


@dataclass(frozen=True)
class RegisteredException:
    """一条已登记但未核对通过的归属：不静默放行，携带来源与裁定信息，可见地报出来。"""

    file: str
    line: str
    source: str
    decided_on: str
    decided_by: str
    reason: str


# ---------------------------------------------------------------------------
# 登记表：2026-08-19 对全仓库逐条核对产品合同正文（docs/产品合同与外部边界.md）
# 写成，见 PR #246 描述里的逐条对账结果。``line`` 必须逐字等于源文件里那一行
# 去除首尾空白后的内容——改动那一行（哪怕只加一个字）都必须回来同步这里，
# 这是刻意的（见模块 docstring 第 2 点）。新增一条归属声明时，先在这里核对它
# 对应合同哪一节、把整行原文和章节名登记进来，而不是先写代码再让门禁牵着走
# ——门禁的作用是挡住"忘了核对"，不是代替核对本身。
# ---------------------------------------------------------------------------

GROUNDED_ATTRIBUTIONS: tuple[GroundedAttribution, ...] = (
    GroundedAttribution(
        "docs/技术设计/代码框架.md",
        '- 测试资产中已被受控验证过的模式（如加密轮换 `refresh_token`、`open_id` 定位共享范围成员）是正式实现的参考输入，但正式代码按产品合同与技术设计重写，不直接把测试资产改名上线。',
        "产品合同与外部边界",
    ),
    GroundedAttribution(
        "docs/技术设计/验收矩阵.md",
        '**本组不覆盖什么。** 真实群通知与真实花名册读取属 L4a，两者都**未验证（证据等级 1）**：`FeishuGroupMessages` 与花名册 reader 的全部断言跑在注入的假传输层上，与 `adapters/feishu_directory.py` 同一姿态；真实读取所需的专用主体凭据自 2026-08-09 起未落盘（Issue #52 的 G-READ 判定，E1-A 授权码被烧事故的直接后果），真实面改依赖后续的 bootstrap 重授权切片。持久快照的**接线**已由 S-B-04 完成（`V-花名册-47/48`）：快照链挂进了每日日报职责，保旧告警事实接到 scheduler 的结构化警告日志，面向管理员的提醒走日报本身——`core/alerting.py` 的状态机只认心跳、任务滞留与发送连续失败三类信号，刻意没有为快照新鲜度新增第四类。**花名册读取所用的短期令牌供给已由 [#215](https://github.com/Moshuiwang/lingxi/issues/215) 接上**（方案 C 主接线，产品负责人 2026-08-18 裁定接受按需消费节奏，留痕见 [#203 决策评论](https://github.com/Moshuiwang/lingxi/issues/203#issuecomment-5321623142) 第 7 项）：凭据轮换职责**仍是一次性 `refresh_token` 的唯一消费者**，它按需消费一次并把派生的短期 `access_token` 放进**进程内**持有者（不落盘、不进日志与审计，重启即空），日报侧按新鲜度取用。消费频率随之从约 5.6 天一次变成按需（令牌寿命约 2 小时，正常一天约 12 次）。**[#276](https://github.com/Moshuiwang/lingxi/issues/276)（产品负责人 2026-08-21 裁定）解除了此前"每 UTC 日至多消费一次"的自设上界**，改为两次消费的最小间隔（默认 5 分钟）与每日消费次数上界（默认 100 次）双重保护；判据仍随凭据落盘、进程内不留第二份账本副本——`refresh_consumed_at`（最近一次消费时刻）与新增的 `refresh_consumed_count`（当日消费计数），均在文件锁内、置位消费标记之前判定。**唯一消费者与频率上界是两件不同的事**：2026-08-08 授权码被烧那次事故的形状是两个客户端抢占同一条通道，不是"换取太频繁"（详见 `core/identity/access_token_supply.py` 模块文档）。因此 `V-花名册-29` 的第四个前置现在只取决于配置：三个环境变量配齐即注册。**这一段全部是 L2 事实**：真实续期返回的短期令牌寿命、真实按需消费节奏与真实日报送达仍属 L4a、未验证。部门比对与账号状态落库由定案排除；账号复用换人的自动拦截由 [#34](https://github.com/Moshuiwang/lingxi/issues/34) 永久排除；群内处置由合同排除；`audit_event` 表属 S9，当前审计走结构化日志。',
        "不提供",
    ),
    GroundedAttribution(
        "src/lingxi/core/execution/input_safety.py",
        '核对更正，见 Issue #238）；但"不伪装成功"这个动机本身确有合同依据（结果',
        "交付规则",
    ),
    # docs/参考证据/MVP联合验收执行卡.md 的登记项已随该一次性执行卡退场删除
    # （2026-08-24 维护批；正文在 git 历史可追溯）。
    GroundedAttribution(
        "docs/参考证据/银河用户权限数据结构.md",
        "合同要求的「公司范围」与「职能范围」是两条互相独立的授权链，各自从 `user_id` 出发：",
        "统一用户记录与权限变化",
    ),
    GroundedAttribution(
        "docs/参考证据/银河用户权限数据结构.md",
        "该导出含全部内部人员的姓名、邮箱与逐人国家授权明细。**不得进入仓库、Issue、PR、日志或任何交付物**；只有脱敏后的结构与统计性质可以记录。导入 Lingxi 数据库后按合同的最小化保存与保留规则处理。",
        "统一用户记录与权限变化",
    ),
    GroundedAttribution(
        "docs/技术设计/README.md",
        "| 冲突时 | **以产品合同为准** | 按合同修正设计 |",
        "产品合同与外部边界",
    ),
    GroundedAttribution(
        # 2026-08-19 回合并 epic/d-first-onboarding 时，这一行在原有的 2026-08-19
        # 归属核对更正（#238，"不进用户环境"是架构自设要求而非合同明令）基础上，
        # 又拼回了 epic/d 的 2026-08-17 用户环境持有问数 MCP 令牌例外说明——登记表
        # 随行文同步更新（同一行文本变化就必须重新核对登记，见模块 docstring）。
        "docs/技术设计/代码框架.md",
        '- **凭据**：不进代码、日志、数据库。日志、数据库不存凭据明文是产品合同明令（[产品合同与外部边界](../产品合同与外部边界.md)「统一用户记录与权限变化」："飞书短期令牌、数据库认证材料、MCP 令牌明文及其他凭据不得进入用户表、日志、Issue、文档或用户交付物"）；不进代码、不进用户环境是架构设计自身的从紧要求，合同正文未规定这两处（2026-08-19 归属核对更正，[#238](https://github.com/Moshuiwang/lingxi/issues/238)）。**「不进用户环境」这条架构自设要求已于 2026-08-17 由产品负责人裁定收窄**：飞书机器人凭据、专用授权凭据与 MCP 加密主密钥仍然一律不进用户环境；**唯一的例外是逐用户的问数 MCP 令牌**，它按裁定明文落进该用户自己家目录的 `.mcp.json`（`440`），正文见[架构设计 6.9](架构设计.md)与[决策记录](../决策记录/2026-08-18-用户环境持有问数MCP令牌.md)。长期凭据放操作系统级密钥管理；测试只用固定假凭据探针。',
        "统一用户记录与权限变化",
    ),
    GroundedAttribution(
        "docs/技术设计/接口设计.md",
        "处理次序本身是合同要求，不能重排：",
        "问数与多轮对话",
    ),
    GroundedAttribution(
        "docs/技术设计/接口设计.md",
        "Lingxi 是问数 MCP 的**客户端**。合同规定 Lingxi 不复制其权限过滤、不验收其正确性，只做两件事：把它接给 Agent，以及在开通前确认同步。",
        "系统与外部依赖边界",
    ),
    GroundedAttribution(
        "docs/技术设计/接口设计.md",
        '合同明确排除，此处固化为"不存在的接口"而非"会拒绝的接口"：',
        "不提供",
    ),
    GroundedAttribution(
        "docs/技术设计/接口设计.md",
        "`authorized` 为 `true` 才代表交付完成。授权未确认时返回 `delivered: false` 与说明，由 Agent 按合同措辞告知用户核对，**不自动重发**。",
        "交付规则",
    ),
    GroundedAttribution(
        "docs/技术设计/接口设计.md",
        '**约定**：`enqueue_publish` 与 `audit.record` 在写路径上**必须接收调用方的事务对象**，由类型签名强制"审计与状态变更同事务"——这是合同要求，不能靠代码评审保证。',
        "管理员处理入口与安全确认",
    ),
    GroundedAttribution(
        "docs/技术设计/数据库设计.md",
        "3. **不存凭据。** 飞书短期令牌、数据库认证材料、MCP 令牌明文一律不入库（合同明令）。需要长期持有的外部凭据放操作系统级密钥管理，数据库里只存**是否已配置**这类布尔状态。",
        "统一用户记录与权限变化",
    ),
    GroundedAttribution(
        "docs/技术设计/数据库设计.md",
        '合同要求"建立统一的 `user` 用户表"。`user` 是 PostgreSQL 的保留字（`SELECT user` 返回当前数据库角色），裸用会导致语法错误，加引号则每一处查询都要写 `"user"`。',
        "统一用户记录与权限变化",
    ),
    GroundedAttribution(
        "docs/技术设计/数据库设计.md",
        '**`permission_version` 是乐观锁的锚点**：待确认操作在准备时记下它，确认时比对——合同要求"目标状态已经变化时一律不执行"。',
        "管理员处理入口与安全确认",
    ),
    GroundedAttribution(
        "docs/技术设计/数据库设计.md",
        '**收回用 `revoked_at` 而非物理删除**：合同要求"调岗时只收回明确失效的范围""恢复账号时不自动恢复曾被收回的权限"。后者需要知道"曾被收回"这件事，硬删除会丢失该信息。',
        "统一用户记录与权限变化",
    ),
    GroundedAttribution(
        "docs/技术设计/数据库设计.md",
        '**outbox 模式**：权限变更与发布意图在同一事务落库，投递异步进行。若直接在权限变更后调飞书 API，调用失败会留下"数据库已改、发布未做"的静默不一致，而合同要求两者不一致时不得视为发布完成（`V-权限-01`）。',
        "系统与外部依赖边界",
    ),
    GroundedAttribution(
        "docs/技术设计/数据库设计.md",
        "字段对应合同要求的审计内容：管理员身份、当时角色、所用管理入口、目标对象、动作类型、操作前状态、拟执行影响、确认或取消、操作后状态、结果与时间。",
        "管理员处理入口与安全确认",
    ),
    GroundedAttribution(
        "docs/技术设计/数据库设计.md",
        '`content` 存原始内容不脱敏——合同要求"公司内部审计可以在九十天内审计完整聊天记录、完整业务内容及其中的敏感内容"，脱敏会让这条不成立。脱敏只发生在 `audit_event.detail`。',
        "数据保留与删除",
    ),
    GroundedAttribution(
        "docs/技术设计/数据库设计.md",
        '**开通前发送的内容不入库**：合同要求"不执行、不保存、不回显"。gateway 在识别出未开通用户后，只写 `inbound_event`（记录收到过一个事件及处理方式）与审计，**不写 `chat_message`**。',
        "开通成功后",
    ),
    GroundedAttribution(
        "docs/技术设计/架构设计.md",
        "领域模块按合同的章节切分，模块之间只通过显式接口调用，不互相读对方的表：",
        "产品合同与外部边界",
    ),
    GroundedAttribution(
        "docs/技术设计/架构设计.md",
        '2. **一致性要求指向单库。** 合同要求"写操作的审计记录无法可靠保存时，用户状态不得改变"和"同一待确认操作最多成功执行一次"。任务状态、审计、业务状态在同一个事务里提交，天然满足；引入 broker 后就需要 outbox + 幂等消费者来重新达到同样的保证——为了避免这个复杂度而引入 broker，是本末倒置。',
        "管理员处理入口与安全确认",
    ),
    GroundedAttribution(
        "docs/技术设计/架构设计.md",
        "### 5.2 会话映射（合同要求逐条落地）",
        "问数与多轮对话",
    ),
    GroundedAttribution(
        "docs/技术设计/架构设计.md",
        '- JumpServer 登录进的是同一个家目录，因此产物、工作文件在两个入口下一致（合同要求"路径、归属和访问控制与飞书私聊完全一致"）；',
        "高级工作台",
    ),
    GroundedAttribution(
        "docs/技术设计/架构设计.md",
        "合同要求的审计事实由 SDK 回调与 Lingxi 自己的任务编排层共同产生：",
        "审核、审计与持续优化方向",
    ),
    GroundedAttribution(
        "docs/技术设计/架构设计.md",
        "审计写入失败的处理按合同分级：**读路径**（问数）审计失败记告警但不中断用户；**写路径**（管理动作、状态变更）审计失败则不改状态。",
        "管理员处理入口与安全确认",
    ),
    GroundedAttribution(
        "docs/技术设计/架构设计.md",
        '合同要求"高级工作台中产生的正式产物通过统一的交付 Skill 完成交付"，并且"用户取得产物的路径、归属和访问控制与飞书私聊完全一致"。',
        "高级工作台",
    ),
    GroundedAttribution(
        "docs/技术设计/架构设计.md",
        "6. **任何一次创建/更新/关闭失败** → 停止卡片路径，在同一私聊或同一话题用普通文本补发完整结果（合同要求），并记审计。",
        "交付样式",
    ),
    GroundedAttribution(
        "docs/技术设计/架构设计.md",
        '合同要求状态区展示"当前动作 + 低频耗时"、完成时展示"已完成 + 总耗时"、**不展示预计剩余时间、不展示内部工具调用与过程日志**——因此 `PreToolUse` 采集的工具名只进审计，不进卡片；卡片上的"当前动作"是业务语言的映射表（如 `mcp 查询` → `正在查询数据`）。',
        "交付样式",
    ),
    GroundedAttribution(
        "docs/技术设计/架构设计.md",
        '用 **outbox 模式**（业务写入与发布意图同事务落库，异步投递）而不是"改完权限直接调飞书 API"：后者在飞书调用失败时会留下数据库已改、发布未做的静默不一致，而合同明确要求两者不一致时不得视为发布完成。',
        "系统与外部依赖边界",
    ),
    GroundedAttribution(
        "docs/技术设计/架构设计.md",
        '- **脱敏**：合同要求"不在审计中保存凭据、完整令牌或无关个人信息"。审计写入统一走一个 `redact()` 出口，字段白名单制——新增字段默认不记录，必须显式加入白名单。',
        "管理员处理入口与安全确认",
    ),
    GroundedAttribution(
        "docs/技术设计/架构设计.md",
        '审批流程在飞书开通链路之外（合同规定），Lingxi 不实现审批，只在管理 MCP 里提供"标记某用户已获批高级工作台"的受控写操作。',
        "高级工作台",
    ),
    GroundedAttribution(
        "docs/技术设计/架构设计.md",
        "| [zarazhangrui/lark-coding-agent-bridge](https://github.com/zarazhangrui/lark-coding-agent-bridge) | ~2k★ | 与本产品飞书层最接近的公开实现：每个 chat / 话题 / 文档评论各自独立会话；单卡片实时更新；长连接接入；per-profile 凭据隔离 | 它把运行中收到的消息**排队到下一轮**；本产品合同明确要求运行中消息只提示、不排队、不自动生效 |",
        "问数与多轮对话",
    ),
    GroundedAttribution(
        "docs/技术设计/飞书组织快照与多维表格关联.md",
        "- 任何正式权限必须来自 Lingxi 数据库当前有效记录，并经过产品合同规定的 MCP 同步确认；",
        "开通流程",
    ),
    GroundedAttribution(
        "docs/技术设计/验收矩阵.md",
        '| V-队列-03 | 入队未成功时，用户**不收到任何表示已受理或已开始处理的回复**（已加的表情按合同只表示"已收到"），且事件不被标记为已成功处理 | L2（可注入 + 真库） | 已认领 |',
        "问数与多轮对话",
    ),
    GroundedAttribution(
        "migrations/010_create_galaxy_import_batch.sql",
        "-- 该导出含可识别人员数据，按合同的最长九十天保留：`expires_at` 交给受控清理",
        "数据保留与删除",
    ),
    GroundedAttribution(
        "migrations/alembic/versions/0064_permission_publish_outbox.py",
        "「数据库已改、发布未做」的静默不一致，而产品合同要求两者不一致时**不得视为发布完成**。",
        "系统与外部依赖边界",
    ),
    GroundedAttribution(
        "migrations/alembic/versions/0065_mcp_token_and_sync_check.py",
        "是合同要求（与 ``publish_outbox.last_outcome`` 刻意不加 CHECK 的取舍不同——那一列",
        "管理员处理入口与安全确认",
    ),
    GroundedAttribution(
        "migrations/alembic/versions/20260806_baseline_006_012.py",
        "-- 该导出含可识别人员数据，按合同的最长九十天保留：`expires_at` 交给受控清理",
        "数据保留与删除",
    ),
    GroundedAttribution(
        "scripts/acceptance_fixtures_identity.py",
        "Stage 窗口里，MCP 同步确认要么按合同真实等最多十五分钟，要么本轮不覆盖这条",
        "开通流程",
    ),
    GroundedAttribution(
        # 2026-08-19 #247 把 postgres_conversation.py 拆成包，这段文字随之
        # 搬到了 _transaction.py——登记表路径已同步更新（第二次真实腐烂被
        # 真实触发，同一批次第一次是 #237 搬走 alerting_assembly.py 那段）。
        "src/lingxi/adapters/postgres_conversation/_transaction.py",
        "清掉——合同规定忙碌期的 `/new` 只该得到提示。",
        "问数与多轮对话",
    ),
    GroundedAttribution(
        "src/lingxi/apps/gateway/log_redaction.py",
        "凭据不得进日志是产品合同明令（代码框架「三、横切约定」）；第三方 SDK 的这个",
        "统一用户记录与权限变化",
    ),
    GroundedAttribution(
        "src/lingxi/apps/scheduler/permission_refresh.py",
        "合同要求每日刷新**严格先刷新花名册、再刷新银河快照**（`V-权限-07`）。「先」如果只靠",
        "统一用户记录与权限变化",
    ),
    GroundedAttribution(
        "src/lingxi/apps/scheduler/permission_refresh.py",
        "# 建档合同要求人员 ID 必填，但存档里真的没有时，匹配层会直接抛错。",
        "开通流程",
    ),
    GroundedAttribution(
        "src/lingxi/apps/worker/report.py",
        '``obtained`` 就是产品合同明令禁止的"伪装成功"。原始的工具调用分类改名保留在',
        "交付规则",
    ),
    GroundedAttribution(
        "src/lingxi/core/conversation/pipeline.py",
        "**次序本身是合同要求，不能重排。** 这个模块的全部价值就是把那张次序表变成可判定的代码，",
        "问数与多轮对话",
    ),
    GroundedAttribution(
        "src/lingxi/core/conversation/pipeline.py",
        "一次，而 ``OnboardingRunner.start`` 按合同幂等；反过来，让一次已经拿到结论的",
        "系统与外部依赖边界",
    ),
    GroundedAttribution(
        "src/lingxi/core/execution/audit.py",
        "# 产品合同要求「不在审计中保存凭据、完整令牌」，这是绝对措辞，靠认键名做不到——",
        "管理员处理入口与安全确认",
    ),
    GroundedAttribution(
        "src/lingxi/core/execution/tool_policy.py",
        "# 2. 不要把内部工具名转述给用户——用户可见文案里不出现内部标识是产品合同要求；",
        "开通流程",
    ),
    GroundedAttribution(
        "src/lingxi/core/identity/credentials.py",
        "凭据不进代码、日志、数据库明文与用户环境。日志、数据库不存凭据明文是产品合同明令",
        "统一用户记录与权限变化",
    ),
    GroundedAttribution(
        "src/lingxi/core/identity/provisioning.py",
        "`OnboardingRunner.start` 的合同要求按 `event_id` / `open_id` 幂等，而",
        "系统与外部依赖边界",
    ),
    GroundedAttribution(
        "src/lingxi/core/permission/mcp_readiness.py",
        "产品合同要求「明确确认该用户应有的公司和职能权限已经同步且可以问数后，才宣告开通成功」。",
        "开通流程",
    ),
    GroundedAttribution(
        "src/lingxi/core/permission/mcp_readiness.py",
        "``now`` 必然略大于 ``started + 900``，于是合同要求的最后一次探针永远被跳过，",
        "开通流程",
    ),
    GroundedAttribution(
        "src/lingxi/core/permission/mcp_readiness.py",
        "``now`` 必然略大于 ``started + 900``，于是合同要求的最后一次探针**永远被跳过**，",
        "开通流程",
    ),
    GroundedAttribution(
        "src/lingxi/core/permission/mcp_readiness.py",
        "# 累计到第六次时 ``now`` 必然略过 ``started + 900``，合同要求的最后一次探针",
        "开通流程",
    ),
    GroundedAttribution(
        "tests/gateway_fakes.py",
        "设计要点：**所有调用都记进同一条 ``CallLog``**。接口设计 3.2 的处理次序是合同要求，",
        "问数与多轮对话",
    ),
    GroundedAttribution(
        "tests/test_acceptance_fixtures_identity_contract.py",
        '"""探针桩：每次都返回 0 条可见指标（明确空结果，按合同不算就绪）。"""',
        "开通流程",
    ),
    GroundedAttribution(
        "tests/test_galaxy_account_match.py",
        "# V-开通-09：工号缺失但邮箱可用时按合同走邮箱回退。",
        "开通流程",
    ),
    GroundedAttribution(
        "tests/test_gateway_pipeline.py",
        "本模块按合同原文与产品负责人 2026-08-17 定稿约束其触发面与文案。",
        "问数与多轮对话",
    ),
    GroundedAttribution(
        "tests/test_mcp_readiness_machine.py",
        "``started + 900``，于是合同要求的最后一次探针永远发不出去——十五分钟的窗口",
        "开通流程",
    ),
    GroundedAttribution(
        "tests/test_permission_publish_postgres.py",
        '进来——合同要求的"发布读回一致后立即探一次"就名存实亡了。',
        "开通流程",
    ),
    # 2026-08-19 回合并 epic/d-first-onboarding：以下 8 条随 Epic D / S-D-02 的
    # 首次开通编排代码与决策记录一起并入 main 树，此前只存在于 epic/d 分支，未经过
    # #238 落地时的那一轮登记核对。逐条对照 docs/产品合同与外部边界.md 核对如下。
    GroundedAttribution(
        "docs/决策记录/2026-08-18-首次开通编排住在scheduler.md",
        "- **gateway 只做两件事**：把未开通用户的首聊事件落进 `inbound_event`（标成 `auto_provisioning`），并**立刻**回一条合同要求的「已收到，正在核对」。它不再持有任何会产生外部副作用的开通实现。",
        "首次对话与自动准入",
    ),
    GroundedAttribution(
        "docs/决策记录/2026-08-18-首次开通编排住在scheduler.md",
        "- **接受首触延迟**：首次开通要等一个扫描周期才开始（stage 约 30 秒）。合同要求的第一条提示不受影响——它由 gateway 即时发出。",
        "首次对话与自动准入",
    ),
    GroundedAttribution(
        "src/lingxi/apps/gateway/__init__.py",
        "以及立刻回一条合同要求的「已收到，正在核对」。真正的编排由 scheduler 按",
        "首次对话与自动准入",
    ),
    GroundedAttribution(
        "src/lingxi/apps/gateway/onboarding.py",
        "2. 立刻回一条合同要求的「已收到，正在核对」。",
        "首次对话与自动准入",
    ),
    GroundedAttribution(
        "src/lingxi/apps/scheduler/onboarding.py",
        "代价是**首次开通要等一个扫描周期**才开始（产品负责人已知情并接受）。合同要求的第一条提示",
        "首次对话与自动准入",
    ),
    GroundedAttribution(
        "src/lingxi/core/identity/onboarding_runner.py",
        "# **合同要求的第二条固定提示**（`V-开通-11`）：权限已经排出去、进入同步等待时，",
        "首次对话与自动准入",
    ),
    GroundedAttribution(
        "src/lingxi/core/identity/onboarding_runner.py",
        '# - 也不能"先建档建环境、发布那步以后再补"：合同要求成功以发布 + 就绪确认',
        "开通流程",
    ),
    GroundedAttribution(
        "src/lingxi/core/identity/onboarding_runner.py",
        "# **只有到这里才写 active**：产品合同要求成功提示在环境创建、权限发布与当前",
        "开通流程",
    ),
)

# ---------------------------------------------------------------------------
# 已知例外：核对时发现措辞源自具体 Issue/PR 的裁定（有留痕），但
# `产品合同与外部边界.md` 正文本身没有对应文字。不静默放行、不擅自改写归属，
# 登记来源与裁定信息，门禁通过与失败时都可见地报出来，交给编排者判断是否需要
# 回写合同或改措辞。
# ---------------------------------------------------------------------------

REGISTERED_EXCEPTIONS: tuple[RegisteredException, ...] = (
    RegisteredException(
        "src/lingxi/apps/gateway/__init__.py",
        '只是"发送"这一步落到日志（Issue #153：合同要求"告警不可用时主流程行为有',
        "Issue #153",
        "2026-08-14",
        "产品负责人",
        "「告警不可用时主流程行为需要明确定义」出自 Issue #153 的产品负责人决定，"
        "产品合同与外部边界正文没有关于告警/监控行为的条款；与 "
        "apps/scheduler/alerting_assembly.py 同一处措辞（同一登记表下方的另一条）。",
    ),
    RegisteredException(
        "src/lingxi/apps/scheduler/alerting_assembly.py",
        '没有配置目标群不等于告警关闭（Issue #153：合同要求"告警不可用时主流程行为',
        "Issue #153",
        "2026-08-14",
        "产品负责人",
        "同上（gateway/__init__.py 的登记）：出自 Issue #153，合同正文未提及告警行为。"
        "2026-08-19 #237 把这段文字从 apps/scheduler/__init__.py 搬到本文件，"
        "登记表路径已同步更新——这是本门禁设计上要防的腐烂被真实触发的一次实例。",
    ),
    RegisteredException(
        "src/lingxi/core/identity/roster_snapshot.py",
        "**为什么门槛不能写成「rows 非空」**（`V-花名册-41`，PR #208 二级审查钉入的合同条款）：",
        "PR #208",
        "2026-08-17",
        "PR #208 二级独立复核",
        '"合同条款"在这里是转述二级审查的用词，指验收矩阵 V-花名册-41 这条被独立复核'
        "钉住的判据，不是指产品合同与外部边界正文；该文档没有关于花名册替换判据的具体规定。",
    ),
    RegisteredException(
        "src/lingxi/core/permission/publish_row.py",
        ":mod:`lingxi.core.permission.role_function`），而合同要求这里放**指标名**。中间缺的",
        "Issue #155",
        "2026-08-17",
        "产品负责人",
        "「发布表值列表放指标名」出自 Issue #155 产品负责人对三问的答复（留痕见该 "
        "Issue 评论），是与问数 MCP 消费方的既定数据格式约定，产品合同与外部边界 "
        "正文没有规定发布表的具体字段格式。",
    ),
    RegisteredException(
        "tests/test_roster_snapshot.py",
        "# **否定用例（PR #208 二级审查钉入的合同条款）**：INCOMPLETE 保留 rows 是有意",
        "PR #208",
        "2026-08-17",
        "PR #208 二级独立复核",
        "同上（roster_snapshot.py 的登记）。",
    ),
)


def tracked_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    paths = []
    for raw_path in result.stdout.split("\0"):
        if not raw_path:
            continue
        if not raw_path.endswith(SCAN_SUFFIXES):
            continue
        path = Path(raw_path)
        if path.parts and path.parts[0] == ".tmp":
            continue
        full_path = REPOSITORY_ROOT / path
        if full_path in EXCLUDED_PATHS:
            continue
        if full_path.is_file():
            paths.append(full_path)
    return paths


def contract_sections(text: str) -> set[str]:
    """解析合同文档的标题集合，跳过代码围栏与 HTML 注释块。

    M3（2026-08-19 外部复核实测坐实）：此前只跳过代码围栏，没跳过 HTML 注释
    ``<!-- ... -->``。编辑合同时把某一节临时注释掉是常见动作，若注释块里
    恰好留了一份同名标题（例如改名前的旧标题被注释保留做参考），门禁会把
    这份"已经不是合同正文"的注释当成真实章节，核对照样判绿。跳过位置与
    代码围栏同一层级——两者都是"看起来像标题但不是正文"的容器。
    """

    sections: set[str] = set()
    in_fence = False
    in_comment = False
    for raw_line in text.splitlines():
        line = raw_line
        if in_comment:
            if "-->" in line:
                in_comment = False
                line = line[line.index("-->") + len("-->") :]
            else:
                continue
        # 一行内可能有多段注释（含跨行注释的收尾）；逐个去掉自封闭的
        # `<!-- ... -->` 片段，剩余文本才拿去判断是否进入跨行注释状态。
        while "<!--" in line:
            start = line.index("<!--")
            end_marker = line.find("-->", start)
            if end_marker == -1:
                line = line[:start]
                in_comment = True
                break
            line = line[:start] + line[end_marker + len("-->") :]
        if in_comment:
            if not line.strip():
                continue
        if line.lstrip().startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = HEADING.match(line)
        if match:
            sections.add(match.group(2))
    return sections


def _display_path(path: Path) -> str:
    """相对仓库根显示；扫描根被指到仓库之外时（单元测试用临时文件会这么做），
    退化成绝对路径即可——出处只是给人看的诊断信息，不参与判定。
    """

    try:
        return path.relative_to(REPOSITORY_ROOT).as_posix()
    except ValueError:
        return str(path)


def find_triggered_lines(path: Path) -> list[tuple[int, str]]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise AttributionCheckError(f"无法读取 {_display_path(path)}：{error}") from error

    hits = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        # M1（2026-08-19 外部复核实测坐实）：此前是"整行只要出现过元排除短语
        # 就整行跳过"——一句话完全可以前半下断言、后半指向覆盖清单
        # （例如"产品合同要求凭据不得入库；详见合同条款覆盖清单。"），旧逻辑
        # 会把这整行当成元文本，真实断言随之静默消失。改成只**挖掉**元排除
        # 短语本身，再检查剩下的文本是否还含有真实触发词；只有整行**就是**
        # 元文本（挖掉后不剩其他触发词）才跳过。
        masked = META_EXCLUDE_PATTERN.sub("", stripped)
        if TRIGGER_PATTERN.search(masked):
            hits.append((line_number, stripped))
    return hits


def _read_file(relative: str, file_texts: dict[str, str], failures: list[str]) -> str | None:
    if relative in file_texts:
        return file_texts[relative]
    full_path = REPOSITORY_ROOT / relative
    try:
        text = full_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        failures.append(f"登记表引用的文件读不出来：{relative}（{error}）")
        file_texts[relative] = ""
        return None
    file_texts[relative] = text
    return text


def evaluate() -> tuple[list[str], list[str], str]:
    """返回 (阻断性失败列表, 例外债务说明列表, 汇总信息)。

    例外债务在**任何**运行结果下都单独返回，调用方必须在成功与失败两条路径
    上都打印它——例外不能只在门禁通过时才被看见（B2，2026-08-19 复查）。
    """

    try:
        contract_text = CONTRACT_DOCUMENT.read_text(encoding="utf-8")
    except OSError as error:
        raise AttributionCheckError(f"无法读取产品合同文档 {CONTRACT_DOCUMENT}：{error}") from error

    sections = contract_sections(contract_text)
    if not sections:
        raise AttributionCheckError("产品合同文档里一个标题都没解析到，无法核对归属")

    failures: list[str] = []

    for grounded in GROUNDED_ATTRIBUTIONS:
        if len(grounded.line) < MIN_EXCERPT_LENGTH:
            failures.append(
                f"登记表里 {grounded.file} 的登记行短于 {MIN_EXCERPT_LENGTH} 个字符，"
                "过短的登记容易被后续任意新增的同类短行意外撞上，请登记完整的行原文。"
            )
        if grounded.section not in sections:
            failures.append(
                f"登记表里 {grounded.file} 的归属指向章节「{grounded.section}」，"
                "但产品合同文档里找不到这个标题（改名了，还是删除了？）"
            )

    for exception in REGISTERED_EXCEPTIONS:
        if len(exception.line) < MIN_EXCERPT_LENGTH:
            failures.append(
                f"例外登记里 {exception.file} 的登记行短于 {MIN_EXCERPT_LENGTH} 个字符。"
            )
        if not (exception.source and exception.decided_on and exception.decided_by):
            failures.append(
                f"例外登记 {exception.file} 缺少来源 Issue/PR、裁定日期或裁定人三项之一——"
                "例外必须能被追溯，不能只写理由。"
            )
        # M5（2026-08-19 外部复核实测坐实）：此前只检查三个字段"非空字符串"，
        # `source="x"`、`decided_on="x"`、`decided_by="x"`、`reason=""` 就能
        # 通过——字段存在不等于内容可追溯。补上格式与内容校验。
        elif not EXCEPTION_SOURCE_PATTERN.match(exception.source):
            failures.append(
                f"例外登记 {exception.file} 的来源 {exception.source!r} 不是 "
                "「Issue #数字」或「PR #数字」这类可追溯的格式。"
            )
        if exception.decided_on:
            try:
                date.fromisoformat(exception.decided_on)
            except ValueError:
                failures.append(
                    f"例外登记 {exception.file} 的裁定日期 {exception.decided_on!r} "
                    "不是合法的 ISO 日期（YYYY-MM-DD，且必须是真实存在的日期）。"
                )
        if exception.reason is not None and not exception.reason.strip():
            failures.append(
                f"例外登记 {exception.file} 的 reason 是空字符串——"
                "例外必须写清楚为什么对不上合同正文，不能只有来源三项、没有理由本身。"
            )

    file_texts: dict[str, str] = {}

    for grounded in GROUNDED_ATTRIBUTIONS:
        text = _read_file(grounded.file, file_texts, failures)
        if text is not None:
            current_lines = {stripped for _, stripped in _iter_stripped_lines(text)}
            if grounded.line not in current_lines:
                failures.append(
                    f"登记表登记的行已经在源文件里找不到了（逐字比对）：{grounded.file} "
                    f"—— {grounded.line!r}。原句被改动或删除时，请同步更新登记表"
                    "（scripts/ci/check_contract_attribution.py）"
                )

    for exception in REGISTERED_EXCEPTIONS:
        text = _read_file(exception.file, file_texts, failures)
        if text is not None:
            current_lines = {stripped for _, stripped in _iter_stripped_lines(text)}
            if exception.line not in current_lines:
                failures.append(
                    f"例外登记的行已经在源文件里找不到了（逐字比对）：{exception.file} "
                    f"—— {exception.line!r}"
                )

    # 每一处「合同要求/合同明令」都必须与登记表或例外表里的某一条**逐字相等**，
    # 且按**出现次数**核对，不是"集合包含即算"（M4，2026-08-19 外部复核）：
    # 把一行已登记的原文复制到同文件另一处，旧的"是否在集合里"判定拿复制出来
    # 的那一份照样能匹配上——集合不区分"这条登记覆盖了几次出现"。改成配额制：
    # 每条登记只能兑现登记时核对过的**那一次**出现，同一段文本在文件里比登记
    # 次数多出来的那些新增出现，各自都要重新登记，不能蹭已核对过的旧配额。
    remaining_budget: dict[tuple[str, str], int] = {}
    for grounded in GROUNDED_ATTRIBUTIONS:
        key = (grounded.file, grounded.line)
        remaining_budget[key] = remaining_budget.get(key, 0) + 1
    for exception in REGISTERED_EXCEPTIONS:
        key = (exception.file, exception.line)
        remaining_budget[key] = remaining_budget.get(key, 0) + 1

    triggered_total = 0
    for path in tracked_files():
        relative = _display_path(path)
        for line_number, line in find_triggered_lines(path):
            triggered_total += 1
            key = (relative, line)
            if remaining_budget.get(key, 0) > 0:
                remaining_budget[key] -= 1
                continue
            failures.append(
                f"{relative}:{line_number}：出现「合同要求」类归属短语但未登记（或已经"
                f"超出登记表里这句原文的出现次数配额）——{line!r}。请先核对它是否真的"
                "对应产品合同正文，再登记进 scripts/ci/check_contract_attribution.py 的 "
                "GROUNDED_ATTRIBUTIONS（对上了）或 REGISTERED_EXCEPTIONS"
                "（对不上、且不能擅自改写归属时）。"
            )

    exception_notes = [
        f"- {exception.file}：{exception.line!r}\n"
        f"  来源：{exception.source}（{exception.decided_on}，{exception.decided_by}）—— {exception.reason}"
        for exception in REGISTERED_EXCEPTIONS
    ]
    summary = (
        f"归属核对：扫描到 {triggered_total} 处「合同要求」类归属短语，"
        f"{len(GROUNDED_ATTRIBUTIONS)} 条登记为已核对对应合同正文，"
        f"{len(REGISTERED_EXCEPTIONS)} 条登记为已知例外（未改写归属，待裁定）"
    )

    return failures, exception_notes, summary


def _iter_stripped_lines(text: str):
    for line_number, line in enumerate(text.splitlines(), start=1):
        yield line_number, line.strip()


def main(argv: list[str] | None = None) -> int:
    # 无任何可选参数——但仍要显式解析并拒绝未知参数（本仓上一批次真实栽过
    # `--e` 缩写命中另一个脚本里带写入副作用的选项的坑）；`allow_abbrev=False`
    # 关掉前缀缩写匹配。
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.parse_args(argv)

    try:
        failures, exception_notes, summary = evaluate()
    except AttributionCheckError as error:
        print(f"归属核对检查失败：{error}", file=sys.stderr)
        return 1

    if failures:
        print("归属核对检查失败：", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        if exception_notes:
            print("以下已登记例外仍然有效（与本次失败无关，一并报出）：", file=sys.stderr)
            for note in exception_notes:
                print(note, file=sys.stderr)
        return 1

    print(summary)
    if exception_notes:
        print("已知例外：")
        for note in exception_notes:
            print(note)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
