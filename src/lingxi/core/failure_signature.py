"""跨进程日志与只读查询共用的失败签名安全边界。"""

import re

_MAX_FAILURE_SIGNATURE_CHARS = 64

#: 连类型名都取不到时的显式占位（例如某个对象的 ``__name__`` 被清成空串）。
#: **不是 ``None``**：这一列的存在意义就是"任何终态都留得下一个可查的记号"。
UNKNOWN_FAILURE_SIGNATURE = "unknown"

# 异常类型来自 Python 运行时，模块名和限定类名都可能被 SDK 动态改成用户输入。
# 不能用字符洗掉括号/空格来"净化"它：``ou_x`` 这类值只要本身由允许字符组成，
# 洗完仍会原样留在日志和 /admin trace。这里把完整类型身份只作为 SHA-256 输入，
# 出口只保留固定的类别词与 160-bit 摘要；摘要不是可逆编码，也不接收异常正文。
_FAILURE_SIGNATURE_DIGEST_HEX_CHARS = 40
_FAILURE_SIGNATURE_PREFIX = "exception"
_FAILURE_SIGNATURE_FAMILIES = frozenset(
    {"builtin", "database", "http", "sdk", "runtime", "external"}
)
_FAILURE_SIGNATURE_PATTERN = re.compile(
    rf"^{re.escape(_FAILURE_SIGNATURE_PREFIX)}\."
    rf"(?:{'|'.join(sorted(_FAILURE_SIGNATURE_FAMILIES))})\."
    rf"[0-9a-f]{{{_FAILURE_SIGNATURE_DIGEST_HEX_CHARS}}}$"
)

# 这类签名不是异常类型，而是结构化外因的固定分类；只允许已经批准的字面量
# 穿过跨进程报告。未来新增结构化外因必须在这里登记，不能让任意字符串借白名单
# 之名进入 task 或低敏日志。
_STABLE_FAILURE_SIGNATURES = frozenset({"mcp.query.http_502", UNKNOWN_FAILURE_SIGNATURE})


def sanitize_failure_signature(value: str) -> str:
    """只接受固定分类或本模块生成的摘要，拒绝任意报告字符串。

    报告跨 worker/gateway 进程传递，``failure.signature`` 不能被当作可信的类型名。
    旧实现把 ``Key (feishu_open_id)=(ou_x)`` 洗成
    ``Keyfeishu_open_idou_x``，等于把敏感值换一种可逆形式继续持久化；现在任何不
    符合固定形状的值都降为 ``unknown``，不做字符替换，也不截取原文。
    """
    if not isinstance(value, str):
        return UNKNOWN_FAILURE_SIGNATURE
    if value in _STABLE_FAILURE_SIGNATURES:
        return value
    if _FAILURE_SIGNATURE_PATTERN.fullmatch(value):
        return value
    return UNKNOWN_FAILURE_SIGNATURE
