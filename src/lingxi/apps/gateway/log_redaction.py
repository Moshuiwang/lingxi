"""Gateway 进程内对第三方飞书 SDK 日志的凭据脱敏。

第三方 ``lark_oapi``（``lark-oapi==1.7.1``）建立长连接成功后以 ``INFO`` 级别
通过固定名为 ``"Lark"`` 的 logger 打印完整 WebSocket 连接 URL；该 logger 在
import 时就给自己挂了 stdout handler，不经过本进程 ``logging.basicConfig``
配置就能落地，且 ``conn_url`` 查询串携带一次性凭据材料。凭据不得进日志是
产品合同明令，唯一能做的是在进入任何 handler 之前拦截、改写。

两层安装：直接给 ``"Lark"`` logger 挂一个 filter（盖住它自带的 stdout
handler 与向上传播到 root 的路径），并给 root logger 当前已安装的 handler
也挂同一个 filter 类的独立实例（不依赖"SDK 固定用名字 Lark"这个可能随版本
变化的假设）。只在 ``main()`` 里、``logging.basicConfig()`` 之后调用一次——
第二层需要 root logger 已经有 handler 可挂；真正做替换的纯函数放在
``core/execution/audit``，这里只负责接进 ``logging`` 机制。
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

from lingxi.core.execution.audit import redact_query_parameter_values

#: 第三方 lark_oapi SDK 固定使用的 logger 名字（``lark_oapi/core/log.py``：
#: ``logging.getLogger("Lark")``），``lark-oapi==1.7.1``（本仓库锁定版本）实测
#: 确认。不是该 SDK 对外承诺的公开契约，只是已知实现事实；即使未来版本改名，
#: 下面 :func:`install_credential_redaction` 的 root handler 兜底仍然生效。
LARK_SDK_LOGGER_NAME = "Lark"


class CredentialQueryRedactingFilter(logging.Filter):
    """把日志记录渲染后的文本过一遍查询参数值脱敏，原地改写记录。

    在 ``filter()`` 里改写而不是丢弃记录（返回值恒为 ``True``）：目标是"看得到
    日志、看不到凭据"，不是"看不到这条日志"——静默丢弃连接成功这类正常运行
    信号会制造新的可观测性缺口。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """原地改写记录文本，返回值恒为 ``True``——只改写，不丢弃。"""
        # ``getMessage()`` 会先按 ``record.args`` 完成一次 ``%`` 风格格式化
        # （没有 args 时原样返回 ``record.msg``），因此不管第三方代码是用
        # ``logger.info(fmt, *args)`` 还是像 ``lark_oapi`` 那样自己先
        # ``.format()`` 好再传一整条字符串，这里都能拿到最终要输出的文本。
        message = record.getMessage()
        redacted = redact_query_parameter_values(message)
        if redacted != message:
            record.msg = redacted
            # 清空 args：msg 现在是已经渲染完成的最终文本，后续任何 handler/
            # formatter 再调用一次 getMessage() 都不该尝试对它做二次 % 替换。
            record.args = ()
        return True


def install_credential_redaction(
    *, source_logger_names: Iterable[str] = (LARK_SDK_LOGGER_NAME,)
) -> None:
    """安装两层脱敏：命名 logger 源头 + 当前 root handler 兜底。

    必须在 ``logging.basicConfig()`` 之后调用——第二层需要 root logger 已经
    有 handler 可挂。命名 logger 不要求对应的第三方模块已被 import 过：按
    名字取 logger 拿到的是进程内唯一、缓存的同一个对象，晚导入的第三方模块
    取到的还是这一个实例，不需要按 import 顺序安排调用时机。每次调用都新增
    filter 实例，不做去重判断：多份等价过滤器叠加只是重复无操作替换。
    """
    for name in source_logger_names:
        logging.getLogger(name).addFilter(CredentialQueryRedactingFilter())
    for handler in logging.getLogger().handlers:
        handler.addFilter(CredentialQueryRedactingFilter())
