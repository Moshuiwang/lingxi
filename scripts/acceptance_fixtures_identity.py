#!/usr/bin/env python3
"""确定性无权限身份夹具 + MCP 同步最小合法配置（Trace v12 机制③清单①②）。

背景：GitHub Issue #147（长期执行计划 v12）「受控触发夹具确定性合同」把
Epic D/E 联合验收要用到的受控失败旅程列为四类夹具，本模块负责其中不属于
``scripts/acceptance_fixtures.py``（环境变量注入开关）那一类的另外两项：

1. **确定性无权限身份夹具**——合成花名册 / 银河记录，覆盖零条、多条、双键
   冲突、资料不完整、无支持职能五种账号层确定性失败，且**与生产解析同口径**：
   本模块不重新实现判定逻辑，只构造输入，判定动作交给
   ``lingxi.core.permission.account_match.match_galaxy_account`` 与
   ``lingxi.core.permission.role_function``（连同随包发布的真实
   ``galaxy_role_function_map.toml``）。
2. **MCP 同步超时夹具——只用于窗口前的纯单测验证，不能注入真实 Stage 进程**
   （2026-08-18 编排者修复包 P2-11 更正；此前的措辞暗示它可以让验收现场跳过
   真实等待，这是错的）。合同节奏由
   ``lingxi.core.permission.mcp_readiness.ReadinessSchedule`` 承载，最小合法
   配置不是本模块另起一套数字，而是该模块文档已经写明、且
   ``tests/test_mcp_readiness_machine.py`` 已在用的
   ``ReadinessSchedule(interval_seconds=1, budget_seconds=1,
   probe_timeout_seconds=1)``——本模块只把这份配方钉成一个有名字、可 import
   的常量，供**窗口前**用注入的时钟/假探针跑纯单测（见配套契约测试），不需要
   在验收现场靠记忆重新拼参数。

   **已核实这份配方无法在真实 Stage 进程里生效**：`ReadinessSchedule` 由调用方
   在 Python 代码里构造，不读环境变量；已知唯一的运行期覆盖点是
   `apps/scheduler/config.py`（#237 拆分后的新位置）的 `LINGXI_QUERY_MCP_TIMEOUT_SECONDS`，且它**只**
   覆盖 `probe_timeout_seconds` 一项，`interval_seconds`（180 秒一次）与
   `budget_seconds`（900 秒预算）没有任何环境变量能改写，Epic D 的
   `OnboardingRunner`（若已在候选中真实实现）目前同样没有别的覆盖入口。因此**真实
   Stage 窗口里，MCP 同步确认要么按合同真实等最多十五分钟，要么本轮不覆盖这条
   分支**——不要在执行卡或窗口现场暗示可以用这份「最小合法配置」把真实等待压
   到两次探针，那只对本模块自己的纯单测成立。

夹具③（开通链内部故障注入开关）与④（发布读回不一致注入开关）不在本模块：
它们还没有生产实现（S-D-02 施工中），本模块不能替未落地的功能猜测环境变量名。
``scripts/acceptance_fixtures.py`` 的登记表按 ``*_INJECT`` / ``*_CANARY`` 命名
纪律做**静态漂移扫描**——S-D-02 落地后只要新夹具遵循同一纪律命名，配套契约
测试会自动要求补登记，不需要 S-E-01 提前猜接口。

本模块只 import 标准库与 ``lingxi.core`` 的纯函数层（不连数据库、不发网络
请求、不 import 任何适配器），可以在没有任何可选依赖的干净环境里直接运行。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

# ---------------------------------------------------------------------------
# 一、确定性无权限身份夹具：零条 / 多条 / 双键冲突 / 资料不完整 / 无支持职能
# ---------------------------------------------------------------------------

#: 花名册重复出现的合成人员 ID；真实花名册里同一「人员ID」可以有多行
#: （V-开通-09，718 行对 710 值的实测先例），本夹具复刻这个形状。
_PERSON_ID_PREFIX = "E2E-FIXTURE-PERSON-"


def _roster_row(
    personnel_id: str, *, employee_no: str = "", email: str = "", name: str = "验收夹具测试人员"
) -> dict[str, str]:
    return {
        "personnel_id": personnel_id,
        "employee_no": employee_no,
        "email": email,
        "name": name,
    }


def _galaxy_row(user_id: str, *, user_name: str = "", email: str = "", nick_name: str = "验收夹具测试人员") -> dict[str, str]:
    return {"user_id": user_id, "user_name": user_name, "email": email, "nick_name": nick_name}


@dataclass(frozen=True)
class NegativeIdentityFixture:
    """一条「确定性无权限」夹具：给定输入，交给生产判定函数后必须落在指定分支。"""

    name: str
    """人类可读名字，验收执行卡按这个名字引用。"""

    feishu_user_id: str
    roster_rows: tuple[Mapping[str, Any], ...]
    galaxy_rows: tuple[Mapping[str, Any], ...]
    expected_state: str
    """必须等于 ``account_match.NOT_FOUND`` 或 ``account_match.MATCHED``。"""

    expected_reason: str
    """``AccountMatch.reason`` 的精确值；本夹具的核心断言点。"""

    contract_note: str
    """一句话说明这条夹具对应产品合同/验收矩阵的哪一条，供人核对口径。"""


ZERO_HIT = NegativeIdentityFixture(
    name="零条：花名册查无对应记录",
    feishu_user_id=_PERSON_ID_PREFIX + "ZERO",
    roster_rows=(),
    galaxy_rows=(),
    expected_state="not_found",
    expected_reason="roster_not_found",
    contract_note="V-开通-03：工号与邮箱在权限记录中均查不到时给出申请指引终态。",
)

MULTIPLE_HIT = NegativeIdentityFixture(
    name="多条：同一人员 ID 花名册存在多行",
    feishu_user_id=_PERSON_ID_PREFIX + "MULTI",
    roster_rows=(
        _roster_row(_PERSON_ID_PREFIX + "MULTI", employee_no="80101", email="dup1@example-corp.invalid"),
        _roster_row(_PERSON_ID_PREFIX + "MULTI", employee_no="80101", email="dup1@example-corp.invalid"),
    ),
    galaxy_rows=(_galaxy_row("U-DUP", user_name="80101", email="dup1@example-corp.invalid"),),
    expected_state="not_found",
    expected_reason="roster_multiple_rows",
    contract_note=(
        "V-开通-09：同一「人员ID」存在多行时即使字段相同也不是唯一原始记录，"
        "不自动去重或取任意一行。"
    ),
)

KEY_CONFLICT = NegativeIdentityFixture(
    name="双键冲突：工号与邮箱各自唯一命中却指向不同银河账号",
    feishu_user_id=_PERSON_ID_PREFIX + "CONFLICT",
    roster_rows=(
        _roster_row(
            _PERSON_ID_PREFIX + "CONFLICT",
            employee_no="80102",
            email="conflict@example-corp.invalid",
        ),
    ),
    galaxy_rows=(
        _galaxy_row("U-BY-NO", user_name="80102", email="other-no-match@example-corp.invalid"),
        _galaxy_row("U-BY-EMAIL", user_name="99999", email="conflict@example-corp.invalid"),
    ),
    expected_state="not_found",
    expected_reason="key_conflict",
    contract_note="V-开通-02：匹配键命中多条记录或两键结果冲突时不自动选择任何一条。",
)

REQUIRED_FIELDS_MISSING = NegativeIdentityFixture(
    name="资料不完整：花名册命中但工号与邮箱均缺失",
    feishu_user_id=_PERSON_ID_PREFIX + "INCOMPLETE",
    roster_rows=(_roster_row(_PERSON_ID_PREFIX + "INCOMPLETE"),),
    galaxy_rows=(),
    expected_state="not_found",
    expected_reason="required_fields_missing",
    contract_note="V-开通-06 的必要资料缺失分支：工号与邮箱等必要资料均缺失时不建档。",
)

#: 「无支持职能」不是 account_match 的分支——账号匹配本身成功（matched），
#: 不支持职能发生在下一层角色映射。这里给出匹配成功后要喂给
#: ``role_function.resolve_role_functions`` 的角色名；两个角色名都摘自
#: ``galaxy_role_function_map.toml`` 正文注释里明确登记的「当前明确不支持」
#: 类别（APP 开头角色、「A海外本地员工营业厅」），不是本模块编造的猜测。
UNSUPPORTED_FUNCTION_ROLE_NAMES: tuple[str, ...] = ("APP", "A海外本地员工营业厅")

UNSUPPORTED_FUNCTION = NegativeIdentityFixture(
    name="无支持职能：账号唯一匹配成功，但全部角色未映射出任何 Lingxi 职能",
    feishu_user_id=_PERSON_ID_PREFIX + "NO-FUNCTION",
    roster_rows=(
        _roster_row(
            _PERSON_ID_PREFIX + "NO-FUNCTION",
            employee_no="80103",
            email="no-function@example-corp.invalid",
        ),
    ),
    galaxy_rows=(_galaxy_row("U-NO-FUNCTION", user_name="80103", email="no-function@example-corp.invalid"),),
    expected_state="matched",
    expected_reason="unique_employee_no_match",
    contract_note=(
        "账号匹配本身 matched；无支持职能的终态由角色映射层判定——"
        "见本模块 UNSUPPORTED_FUNCTION_ROLE_NAMES 与随包 galaxy_role_function_map.toml。"
    ),
)

NEGATIVE_IDENTITY_FIXTURES: tuple[NegativeIdentityFixture, ...] = (
    ZERO_HIT,
    MULTIPLE_HIT,
    KEY_CONFLICT,
    REQUIRED_FIELDS_MISSING,
    UNSUPPORTED_FUNCTION,
)


# ---------------------------------------------------------------------------
# 二、MCP 同步超时夹具：验收窗口用的「最小合法配置」节奏
# ---------------------------------------------------------------------------

#: 与 ``core.permission.mcp_readiness`` 模块文档「节奏与预算是受控可配置的
#: 合法值」一节记录的配方逐字段相同——本模块不重新决定这三个数字，只给它一个
#: 稳定、可 import 的名字，避免验收现场从文档散文里手抄参数。
MCP_READINESS_MINIMUM_LEGAL_SCHEDULE_KWARGS: Mapping[str, int] = {
    "interval_seconds": 1,
    "budget_seconds": 1,
    "probe_timeout_seconds": 1,
}


def negative_identity_fixture_by_name(name: str) -> NegativeIdentityFixture:
    for fixture in NEGATIVE_IDENTITY_FIXTURES:
        if fixture.name == name:
            return fixture
    raise KeyError(f"未登记的确定性无权限身份夹具：{name}")


def _cli_list() -> int:
    for fixture in NEGATIVE_IDENTITY_FIXTURES:
        print(f"{fixture.expected_reason}\t{fixture.name}")
    print(f"mcp-readiness-minimum-legal\t{dict(MCP_READINESS_MINIMUM_LEGAL_SCHEDULE_KWARGS)}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    del argv
    return _cli_list()


if __name__ == "__main__":
    raise SystemExit(main())
