"""进程入口层：只做组装。

按[架构设计](../../../docs/技术设计/架构设计.md)，正式形态是 gateway / worker /
scheduler / admin 四个进程，各自一个子包，以 ``python -m lingxi.apps.<name>`` 启动。
目前存在 ``worker`` 的最薄入口（Issue #37）与 ``scheduler``（Issue #16，凭据轮换）。

本层的规则见[代码框架「二、三层之间的 import 规则」]
(../../../docs/技术设计/代码框架.md)：读配置、建连接、把 adapters 注入 core、处理
退出，**不写业务规则**。判定与审计属于 ``core``，SDK 细节属于 ``adapters``。
"""
