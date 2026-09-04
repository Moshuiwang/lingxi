"""管理卡片族共用的 CardKit 2.0 拼装：按钮横排容器 + form 内交互组件 name 校验。

``management_card.py``（管理卡）与 ``notification.py``（确认卡/终态卡）各自
渲染不同的卡片，但两者的按钮横排与 form 内 name 校验是完全相同的拼装细节，
只写一份，避免两张卡各自维护一份容易漂移的 JSON 拼装代码。

## 为什么是 ``column_set`` 而不是别的容器

真实探针对抗验证过三种候选：``form`` 套 ``form`` 被拒；``column_set`` 嵌
``form`` 放行；form 内按钮缺 ``name`` 建卡请求本身不暴露缺陷，只在真实点击
那一刻触发拒绝，因此必须在装配阶段（:func:`assert_unique_named_form_elements`）
静态钉死。``column_set`` 是横排按钮的唯一正规容器，须显式声明 ``flex_mode``
——不声明时手机端会挤压变形；2/3 个按钮用 ``bisect``/``trisect``，其余用
``flow``（自动换行）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


def button_row(buttons: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """把一组按钮包进 ``column_set`` 横排容器，每个按钮各占一个 ``width=auto`` 的 ``column``。

    只有 1 个按钮时没有"横排"这回事，调用方不应该为单个按钮调用本函数（管理卡逐行
    「撤销」按钮就是这种情形，见 ``management_card._override_row_elements``——它
    继续用一个裸按钮，不套 ``column_set``）。
    """
    if not buttons:
        raise ValueError("button_row 至少需要一个按钮")
    count = len(buttons)
    if count == 2:
        flex_mode = "bisect"
    elif count == 3:
        flex_mode = "trisect"
    else:
        flex_mode = "flow"
    return {
        "tag": "column_set",
        "flex_mode": flex_mode,
        "columns": [{"tag": "column", "width": "auto", "elements": [button]} for button in buttons],
    }


#: 结构上被认定为"表单内交互组件"、必须携带非空 ``name`` 的标签集合。
_FORM_INTERACTIVE_TAGS = frozenset({"button", "select_static", "input"})


def assert_unique_named_form_elements(form_elements: Sequence[Mapping[str, Any]]) -> None:
    """校验一个 ``form`` 容器下的全部交互组件都带非空、且本次调用范围内互不相同的 ``name``。

    飞书 ``code=200530`` 只在真实点击缺 ``name`` 的表单内组件那一刻触发，建卡
    请求本身会被放行（见模块文档"为什么是 column_set 而不是别的容器"）——这
    正是本函数存在的理由：把"点击才会暴露"的缺陷提前到"建卡装配那一刻就失败
    关闭"，不依赖发送侧或真实点击兜底。校验不通过时抛 ``ValueError``——调用方
    （``management_card.render_management_card``）不吞这个异常，缺 name 或
    name 冲突必须在开发/测试阶段就暴露，不能带着一张结构非法的表单卡片上线。
    """
    names: list[str] = []

    def walk(elements: Sequence[Mapping[str, Any]]) -> None:
        for element in elements:
            tag = element.get("tag")
            if tag == "column_set":
                for column in element.get("columns", ()):
                    walk(column.get("elements", ()))
                continue
            if tag in _FORM_INTERACTIVE_TAGS:
                name = element.get("name")
                if not name:
                    raise ValueError(f"form 内交互组件缺少非空 name：tag={tag!r}")
                names.append(name)

    walk(form_elements)
    seen: set[str] = set()
    duplicates: set[str] = set()
    for name in names:
        if name in seen:
            duplicates.add(name)
        seen.add(name)
    if duplicates:
        raise ValueError(f"form 内交互组件 name 必须单卡唯一，重复：{sorted(duplicates)}")
