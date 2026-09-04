"""Gateway 进程内对第三方飞书 SDK 日志的凭据脱敏（Issue #176）。

## 发现

第三方 ``lark_oapi``（``lark-oapi==1.7.1``）建立长连接成功后，以 ``INFO`` 级别
通过它自己的 logger 打印完整 WebSocket 连接 URL：

- 该 logger 固定名为 ``"Lark"``（``lark_oapi/core/log.py``：
  ``logger = logging.getLogger("Lark")``），并在 import 时就直接给自己挂了一个
  指向 ``sys.stdout`` 的 ``StreamHandler``——这条日志因此**不经过我们任何一处
  ``logging.basicConfig`` 配置就已经能落地**。
- ``lark_oapi.ws.Client.__init__`` 的 ``log_level`` 参数默认值是
  ``LogLevel.INFO``（本仓库的 ``adapters/feishu_longconn.py`` 未显式覆盖），
  构造函数无条件执行 ``logger.setLevel(log_level.value)``，把模块级默认的
  ``WARNING`` 收紧成 ``INFO``——这正是连接成功那条 ``logger.info("connected to
  {}", conn_url)``（``lark_oapi/ws/client.py``）会被真正输出的原因。
- ``conn_url`` 是飞书服务端签发的完整 URL，查询串携带 ``device_id``/
  ``service_id``/``ticket`` 一类一次性凭据材料（S-A-07 biai-stage 真实发现，
  见 Issue #176 正文；本文件不复述、不记录任何实际参数值）。

凭据不得进日志是产品合同明令（代码框架「三、横切约定」）；第三方 SDK 的这个
行为不在我们控制范围内，唯一能做的是在**进入任何 handler 之前**拦截、改写。

## 两层安装，理由分别写在各自的调用点

1. **直接给 ``"Lark"`` logger 挂一个 ``logging.Filter``**：Logger 级别的
   filter 在 ``Logger.handle()`` 里最先跑，跑完才轮到 ``callHandlers()`` 把
   记录分发给它自己的 handler（该 logger 自带的 stdout handler）与沿继承链
   向上传播后遇到的 handler（root 的 stderr handler，来自 gateway
   ``main()`` 的 ``logging.basicConfig``）。挂在源头意味着这两条出口在同一次
   过滤里一起被盖住，不需要分别处理。
2. **额外给 root logger 当前已安装的 handler 也挂同一个 filter 类的独立
   实例**：Handler 级别的 filter 对**任何**传播到它的记录都生效，不管来自
   哪个 logger、叫什么名字——这层不依赖"第三方 SDK 固定用名字 Lark"这个可能
   随版本变化的事实，是处置要求第 1 条"不能只依赖人工不查看日志"背后同一个
   精神的落地：不把整条防线押在单一、可能过期的假设上。

只在 ``apps/gateway/__init__.py`` 的 ``main()`` 里调用一次，且必须在
``logging.basicConfig()`` 之后——第 2 层需要 root logger 已经有 handler 可挂。
``core/`` 保持不碰 ``logging`` 全局状态的既有边界，因此这层"安装"逻辑放在
``apps/gateway/``，不是 ``core/``；真正做字符串替换的纯函数
:func:`lingxi.core.execution.audit.redact_query_parameter_values` 仍然放在
``core/`` 那唯一一份共享的脱敏出口里，这里只负责把它接进 ``logging`` 机制。
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
    """安装两层脱敏：命名 logger 源头 + 当前 root handler 兜底（Issue #176）。

    必须在 ``logging.basicConfig()`` 之后调用——第 2 层需要 root logger 已经
    有 handler 可以挂。命名 logger 不要求对应的第三方模块已经被 import 过：
    ``logging.getLogger(name)`` 按名字取的是进程内唯一、缓存在
    ``logging.Logger.manager`` 里的同一个 ``Logger`` 对象，晚导入的第三方模块
    调用 ``logging.getLogger("Lark")`` 拿到的还是这一个实例，这里提前挂上的
    filter 依然生效，不需要按 import 顺序小心安排调用时机。

    每次调用都会新增 filter 实例（不做"是否已安装"的去重判断）：本函数在真实
    进程里只在 ``main()`` 装配处调用一次；测试如果多次调用，多份等价的过滤器
    叠加执行只是重复对同一条已经脱敏的文本做无操作替换，不改变行为，不构成
    正确性问题。
    """

    for name in source_logger_names:
        logging.getLogger(name).addFilter(CredentialQueryRedactingFilter())
    for handler in logging.getLogger().handlers:
        handler.addFilter(CredentialQueryRedactingFilter())
