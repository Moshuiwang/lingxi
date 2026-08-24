"""内测名单闸的纯判定层（Issue #302 S-N-01，载体 #251/#302）。

## 为什么需要这道闸，为什么必须挡在开通链最前端

编排者 2026-08-24 按飞书 API 回读核实：Bot-Test 应用的可见范围是**全员可见**
（``is_visible_to_all=1``）。这意味着**任何**关联组织员工都能在飞书里搜到 Bot-Test
并对它发起私聊——不只是内测名单挑中的那几个人。内测阶段只想放行一小撮存量用户真实
走开通链，其余任何人碰到 Bot-Test 都必须得到一句干净的「内测未开放」，且**不得**
留下任何业务状态（不建档、不发权限）。按最严实现：闸必须挡在
:meth:`~lingxi.core.identity.onboarding_runner.AutoOnboardingRunner._run` 的最前面，
早于组织快照读取、在职状态实时回读（那会消耗全系统独占的专用授权派生令牌）与任何
数据库写入。

## 匹配键为什么是 open_id，不是 email / union_id

开通链此刻唯一稳定持有、且**不需要任何额外查询**就能拿到的身份信号，是私聊事件本身
携带的应用侧发送者 ``open_id``（``AutoOnboardingRunner.start(*, open_id=...)`` 的入参，
对应产品合同「首次对话与自动准入」第 2 步"普通员工私聊事件提供应用侧的发送者
``open_id``"）。花名册工号/邮箱与组织快照成员都要等 ``_locate()`` 跑完组织快照查询
之后才拿得到——而组织快照查询本身正是这道闸要挡在前面的东西。用 email 或 union_id
做匹配键意味着闸必须先做一次身份解析才能判定，等于闸自己先走了一遍它要拦截的链路，
本末倒置。``open_id`` 还天然是"同一应用语义"下的标识（产品合同用语），与 Bot-Test
私聊事件本身的语义完全对齐，不存在跨应用换算的问题。

## 默认关闭＝全拒

未配置（环境变量为空/未设置）时名单为空集合，:func:`is_open_id_innertest_allowed`
对任何 ``open_id`` 都返回 ``False``——这不是一个单独的"开关"分支，只是"空集合不包含
任何元素"的自然结果。

配置格式非法（存在无法识别的条目）时 :func:`parse_innertest_roster` **整份**拒绝
（抛 :class:`InnerTestRosterConfigError`），不做部分采纳、不静默丢弃单条坏值——调用方
（``apps/scheduler/config.py`` 的 ``SchedulerConfig.from_env``）据此在**进程启动期**
快速失败，与本仓库对其余"配了但格式不对"环境变量（``LINGXI_ADMIN_GROUP_CHAT_ID``、
``LINGXI_MCP_TOKEN_ENCRYPT_KEY`` 等，见该文件 ``optional_identifier``/``raw_chat_id``
一节）的既有纪律一致：**错配不是未配**，静默降级会让人以为闸在正常工作。

## 比对语义

只做首尾空白裁剪、精确字符串相等，不做大小写归一化、不做前缀或模糊匹配——与
``core/identity/first_contact.py`` 的 ``locate_by_open_id`` 同一条纪律（``open_id``
前缀在 710 人实测中已知有碰撞；模糊匹配违反"不猜测身份"的产品合同硬要求）。
"""

from __future__ import annotations

#: 飞书用户 open_id 的前缀，与 ``adapters/feishu_user_message.py`` 的
#: ``USER_OPEN_ID_PREFIX`` 同一个字面量。这里不从 adapters 反向 import——``core/``
#: 不得 import ``adapters/``（代码框架「二、三层之间的 import 规则」），因此各自维护
#: 一份同一个前缀常量，形状同 ``publish_row.ALL_COMPANIES_KEY`` 与
#: ``metric_translation.ALL_COMPANIES_KEY`` 的既有取舍。
_OPEN_ID_PREFIX = "ou_"


class InnerTestRosterConfigError(ValueError):
    """内测名单配置格式非法：整份配置作废，不做部分采纳。

    fail-closed 的对象是"这份配置"本身，不是"这一条目"——宁可整体拒绝也不要猜测
    哪一条是笔误、哪一条是真值。``invalid_count`` 只报数量，调用方（配置装配层）
    据此快速失败；错误信息不回显任何取到的原始条目（与 open_id 同属身份标识，
    纪律同 ``apps/scheduler/config.py`` 里其他标识类环境变量的错误提示）。
    """

    def __init__(self, *, invalid_count: int) -> None:
        self.invalid_count = invalid_count
        super().__init__(
            f"内测名单包含 {invalid_count} 条无法识别的条目（必须是以 "
            f"{_OPEN_ID_PREFIX!r} 开头、不含空白字符的飞书用户 open_id，"
            "用英文逗号或换行分隔，不回显取到的原始条目）"
        )


def _looks_like_open_id(token: str) -> bool:
    return (
        token.startswith(_OPEN_ID_PREFIX)
        and len(token) > len(_OPEN_ID_PREFIX)
        and not any(character.isspace() for character in token)
    )


def parse_innertest_roster(raw: str | None) -> frozenset[str]:
    """把逗号/换行分隔的 open_id 名单解析成集合。

    - ``raw`` 为 ``None``，或裁剪、按分隔符拆分后一条非空条目都没有：**未配置**，
      返回空集合（闸对任何人返回 ``False``，见模块文档「默认关闭＝全拒」）。
    - 拆出的条目中**任意一条**不满足 :func:`_looks_like_open_id`：整份配置作废，
      抛出 :class:`InnerTestRosterConfigError`——不静默丢弃单条坏值，不部分采纳。
    - 全部合法：返回去重后的 open_id 集合（``frozenset``：天然去重，且类型上
      不能被误当成有序列表意外依赖顺序）。
    """

    if raw is None:
        return frozenset()
    tokens = [piece.strip() for piece in raw.replace("\n", ",").split(",")]
    non_blank = [token for token in tokens if token]
    if not non_blank:
        return frozenset()
    invalid_count = sum(1 for token in non_blank if not _looks_like_open_id(token))
    if invalid_count:
        raise InnerTestRosterConfigError(invalid_count=invalid_count)
    return frozenset(non_blank)


def is_open_id_innertest_allowed(open_id: object, roster: frozenset[str]) -> bool:
    """内测名单闸判定：纯粹的集合成员判断。

    只做首尾空白裁剪、精确相等比较，语义与 ``locate_by_open_id`` 一致（模块文档
    「比对语义」）。``roster`` 为空集合时对任何输入都返回 ``False``——这就是
    「默认关闭＝全拒」的全部实现，没有单独的开关分支需要另外测试。
    """

    if not isinstance(open_id, str):
        return False
    needle = open_id.strip()
    if not needle:
        return False
    return needle in roster
