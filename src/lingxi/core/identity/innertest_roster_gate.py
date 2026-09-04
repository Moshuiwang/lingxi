"""内测名单闸的纯判定层。

Bot-Test 应用可见范围是全员可见，任何关联组织员工都能发起私聊，不只是内测名单
挑中的人。闸必须挡在 ``AutoOnboardingRunner._run`` 的最前面——早于组织快照读取、
在职状态实时回读与任何数据库写入——命中放行、未命中给一句干净的"内测未开放"且不
留任何业务状态。匹配键是 ``open_id``：私聊事件本身携带、不需要额外查询就能拿到的
唯一稳定身份信号；email/union_id 都要等组织快照查询跑完才拿得到，而组织快照查询
正是这道闸要挡在前面的东西，用它做匹配键等于闸自己先走了一遍要拦截的链路。

未配置时名单为空集合，判定对任何输入返回 ``False``（默认关闭＝全拒）。配置格式
非法时整份拒绝，不部分采纳、不静默丢弃单条坏值，调用方据此在进程启动期快速失败。
比对只做首尾空白裁剪与精确字符串相等，不做大小写归一化或模糊匹配。这道闸只挡
"还没开始走开通链"的人，不是持续生效的访问控制列表：移出名单不会切断已开通用户，
在途链不会中途复核名单；真正的切断工具是管理员停用动作。
"""

from __future__ import annotations

import re
from collections.abc import Callable

#: 飞书用户 open_id 的前缀，与 ``adapters/feishu_user_message.py`` 的
#: ``USER_OPEN_ID_PREFIX`` 同一个字面量。这里不从 adapters 反向 import——``core/``
#: 不得 import ``adapters/``（代码框架「二、三层之间的 import 规则」），因此各自维护
#: 一份同一个前缀常量，形状同 ``publish_row.ALL_COMPANIES_KEY`` 与
#: ``metric_translation.ALL_COMPANIES_KEY`` 的既有取舍。
_OPEN_ID_PREFIX = "ou_"

#: open_id 形状校验：只允许 ``ou_`` 后接 20 到 64 位英文字母或数字，宽松边界，
#: 不是飞书官方公布的精确长度承诺。分号、全角标点等粘连手误必须落在正则之外，
#: 让整份配置在启动期响亮失败，而不是被当成一个真实但永远匹配不到人的 open_id
#: 悄悄收进名单、让名单静默变小。
_OPEN_ID_PATTERN = re.compile(r"ou_[0-9A-Za-z]{20,64}")


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
            f"内测名单包含 {invalid_count} 条无法识别的条目（必须是 "
            f"{_OPEN_ID_PREFIX!r} 后接 20 到 64 位英文字母或数字的飞书用户 "
            "open_id，用英文逗号或换行分隔——不得包含分号、全角标点或其他分隔符，"
            "不回显取到的原始条目）"
        )


def _looks_like_open_id(token: str) -> bool:
    return _OPEN_ID_PATTERN.fullmatch(token) is not None


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


def build_innertest_roster_gate(roster: frozenset[str]) -> Callable[[str], bool]:
    """把已解析的内测名单集合包成 ``AutoOnboardingRunner`` 构造时要的判定口。

    纯粹的装配便利：把上面那个纯判定函数 :func:`is_open_id_innertest_allowed` 绑上
    一份具体的名单集合。判据、为什么匹配键是 open_id、为什么空集合＝全拒，全部写在
    本模块的文档字符串里，本函数不重复。
    """

    return lambda open_id: is_open_id_innertest_allowed(open_id, roster)
