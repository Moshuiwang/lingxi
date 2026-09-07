"""三个权限合并入口在「本地覆盖读不出来」这件事上的横向对照（IN-01，Issue #646）。

真实权限 =（银河 ∪ 本地授权）− 本地抑制这条规则全仓库只有一份实现，但**读不出来
时怎么收敛**此前是三份各写各的：零银河那一支失败关闭，另外三处（定向重算、每日批
银河非空分支、首聊开通）把读失败折叠成"没有本地源"。合并对"没有本地源"恒等，于是
一次数据库抖动产出的是一份**少了本地补授**却与完整结果同形的权限决定。

本文件不重复各入口自己的用例，只做一件事：把三个入口摆在一起，对同一组输入形态逐组
比对。四组输入：

1. **同输入**——同一份可读、非空的本地补授：三条链都发布，且发布内容都真的带上了它；
2. **不可读**——三条链都**一个字节都不写**，且原因码是同一个可分辨的字面量；
3. **合法空集**——读得出来、就是空：三条链照常发布。这一组是"不可读"那一组的对照，
   证明判据是"读没读到"而不是"结果空不空"——修复要是把合法空集一起挡掉，这组会红；
4. **身份与通知差异**——三条链的收敛终态**不该**被这次收拢抹平：每日重算连一个发送
   端口都没有、定向重算把结论交回管理员动作、首聊开通必须通知用户本人，且通知的是
   本侧故障（``LX-ONBOARD-001``）而不是「无可用权限」。

夹具全部复用三个入口各自的既有假实现，本文件不新建第二套——两套夹具会让"三条链行为
一致"这个断言在夹具漂移时静默失真。
"""

from __future__ import annotations

import unittest
from dataclasses import dataclass

import test_onboarding_runner as onboarding
import test_permission_refresh_duty as refresh
import test_targeted_permission_recompute as targeted

from lingxi.core.identity.onboarding_terminal import KEY_INTERNAL_ERROR, KEY_NOT_AUTHORIZED
from lingxi.core.permission.merge_sources import REASON_LOCAL_OVERRIDE_READ_FAILED

#: 三个入口的本地补授夹具用的是同一个指标名，因此"这条本地补授有没有进到发布内容里"
#: 可以在三条链上用同一个判据表达，不需要把三套银河夹具对齐成一份。
LOCAL_METRIC = "本地指标"

GRANTED = "granted"
UNREADABLE = "unreadable"
READABLE_EMPTY = "readable_empty"


@dataclass(frozen=True)
class _EntryOutcome:
    """一条链跑完之后，归一化到三个入口都能表达的四件事。"""

    name: str
    #: 真的落进权限决定的内容串。空元组＝一条权限决定都没排。
    published: tuple[str, ...]
    #: 这条链自己那个可分辨的故障/跳过原因码；``None`` 表示这次没有失败。
    fault_reason: str | None
    #: 发给**用户本人**的终态文案键；``None`` 表示这条链结构上不通知用户。
    user_message_key: str | None
    #: 审计里登记的受影响用户标识——三条链各自的身份来源不同，这里取证它没被抹平。
    audited_user: str | None


def _source(module, user_id: str, shape: str):
    """按输入形态造一个本地覆盖读取口替身（用各入口自己的那份假实现）。"""
    if shape is UNREADABLE:
        return module.FakeLocalOverrides(fail_for={user_id})
    if shape is READABLE_EMPTY:
        return module.FakeLocalOverrides({user_id: ()})
    return module.FakeLocalOverrides({user_id: (module._override_entry(),)})


def _first_field(audit_fields, key: str) -> str | None:
    return audit_fields[0][key] if audit_fields else None


def _daily_refresh(shape: str) -> _EntryOutcome:
    duty, parts = refresh.build_duty(
        identities=(refresh.identity(),),
        local_overrides=_source(refresh, refresh.USER_ONE, shape),
    )
    duty.run_once()
    return _EntryOutcome(
        name="每日重算",
        published=tuple(call["row"].permissions for call in parts["decisions"].calls),
        fault_reason=_first_field(
            parts["audit"].fields_for("permission_refresh.user_skipped"), "reason"
        ),
        # 模块文档：这条职责一次都不签发令牌，也不通知任何人——连一个发送端口都没有。
        user_message_key=None,
        audited_user=_first_field(
            parts["audit"].fields_for("permission_refresh.local_override_skipped"), "user"
        ),
    )


def _targeted_recompute(shape: str) -> _EntryOutcome:
    recompute, parts = targeted.build_recompute(
        identities=(refresh.identity(),),
        published_users={refresh.USER_ONE},
        local_overrides=_source(refresh, refresh.USER_ONE, shape),
    )
    outcome = recompute.recompute_and_publish(user_id=refresh.USER_ONE)
    return _EntryOutcome(
        name="定向重算",
        published=tuple(call["row"].permissions for call in parts["decisions"].calls),
        fault_reason=outcome.reason,
        # 结论交回发起这次动作的管理员链路，本模块不冒充用户本人的通知。
        user_message_key=None,
        audited_user=_first_field(
            parts["audit"].fields_for("permission_targeted_recompute.local_override_skipped"),
            "user",
        ),
    )


def _first_onboarding(shape: str) -> _EntryOutcome:
    parts, _ = onboarding.run_once(local_overrides=_source(onboarding, onboarding.USER_ID, shape))
    facts = parts["audit"].facts("onboarding.result")
    return _EntryOutcome(
        name="首聊开通",
        published=tuple(row.permissions for row in parts["decisions"].rows),
        fault_reason=facts.get("failure_reason"),
        user_message_key=parts["notifier"].terminal()[1],
        audited_user=_first_field(
            [parts["audit"].facts("onboarding.local_override_skipped")]
            if "onboarding.local_override_skipped" in parts["audit"].actions()
            else [],
            "user",
        ),
    )


def _run_all(shape: str) -> tuple[_EntryOutcome, _EntryOutcome, _EntryOutcome]:
    return _daily_refresh(shape), _targeted_recompute(shape), _first_onboarding(shape)


class ReadFailureParityTests(unittest.TestCase):
    """四组输入形态在三个入口上的横向对照。"""

    def test_the_same_readable_local_grant_reaches_all_three_published_rows(self) -> None:
        """同输入：一份可读、非空的本地补授在三条链上都真的进了发布内容。"""

        for outcome in _run_all(GRANTED):
            with self.subTest(entry=outcome.name):
                self.assertEqual(len(outcome.published), 1, "应当发布恰好一条权限决定")
                self.assertIn(LOCAL_METRIC, outcome.published[0], "本地补授必须出现在发布内容里")
                self.assertIsNone(outcome.fault_reason, "可读输入不该产生任何故障出口")

    def test_an_unreadable_source_writes_nothing_anywhere(self) -> None:
        """不可读：三条链一个字节都不写，且原因码是同一个可分辨的字面量。

        这是本次修复的核心断言。修复前这一组里定向重算、每日批（银河非空）、首聊开通
        三条链都会照常写出一份少了本地补授的权限决定。
        """

        for outcome in _run_all(UNREADABLE):
            with self.subTest(entry=outcome.name):
                self.assertEqual(outcome.published, (), "读不出本地源时一条权限决定都不排")
                self.assertEqual(
                    outcome.fault_reason,
                    REASON_LOCAL_OVERRIDE_READ_FAILED,
                    "故障原因码必须可分辨，不得混进别的跳过原因",
                )

    def test_a_readable_empty_set_still_publishes_everywhere(self) -> None:
        """合法空集：读得出来、就是空 → 三条链照常发布。

        「不可读」那一组的对照。判据是"读没读到"，不是"结果空不空"；把合法空集一起
        挡掉会让绝大多数没有任何本地覆盖的普通用户彻底发不出权限。
        """

        for outcome in _run_all(READABLE_EMPTY):
            with self.subTest(entry=outcome.name):
                self.assertEqual(len(outcome.published), 1, "合法空集不影响发布")
                self.assertNotIn(LOCAL_METRIC, outcome.published[0])
                self.assertIsNone(outcome.fault_reason)

    def test_each_entry_point_keeps_its_own_identity_and_notification_promise(self) -> None:
        """身份与通知差异：收敛判据统一了，三条链各自的身份来源与通知承诺不得被抹平。"""

        daily, targeted_outcome, onboarding_outcome = _run_all(UNREADABLE)

        self.assertIsNone(daily.user_message_key, "每日重算没有发送端口，不通知任何人")
        self.assertIsNone(targeted_outcome.user_message_key, "定向重算把结论交回管理员动作")
        self.assertEqual(
            onboarding_outcome.user_message_key,
            KEY_INTERNAL_ERROR,
            "开通链必须通知用户本人，且说的是本侧故障",
        )
        self.assertNotEqual(
            onboarding_outcome.user_message_key,
            KEY_NOT_AUTHORIZED,
            "把一次读故障说成「没有权限」，会把已被特批的人引去银河申请无关权限",
        )

        self.assertEqual(
            (daily.audited_user, targeted_outcome.audited_user, onboarding_outcome.audited_user),
            (refresh.USER_ONE, refresh.USER_ONE, onboarding.USER_ID),
            "三条链各自的身份来源不同，审计必须按自己那份标识登记受影响的人",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
