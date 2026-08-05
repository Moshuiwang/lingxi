"""进程入口。

`apps/` **只做组装**：读环境变量、建连接、把 adapters 注入 core、处理信号与退出。
业务规则一律不写在这里。每个进程一个子包，以 ``python -m lingxi.apps.<name>``
启动，对应 Docker Compose 的一个服务。
"""
