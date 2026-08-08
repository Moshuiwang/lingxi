#!/usr/bin/env python3
"""受控环境中的「四达文档会议助手」正式重授权入口。

这是一次性初始化/恢复操作，不是常驻服务，也不是普通员工入口。正式入口随
``src/lingxi/apps/reauthorize`` 发布；本文件只保留给旧的受控调用方作薄转发，
避免 scripts/ 被排除出生产镜像后仍有人误以为它是生产入口。

需要由受控环境注入的变量：

* ``LINGXI_POSTGRES_DSN``
* ``LINGXI_DELEGATED_CREDENTIAL_KEY``
* ``LINGXI_DELEGATED_CREDENTIAL_PATH``
* ``LINGXI_FEISHU_APP_ID`` / ``LINGXI_FEISHU_APP_SECRET``
* ``LINGXI_FEISHU_BASE_URL``
* ``LINGXI_FEISHU_REDIRECT_URI``
* ``LINGXI_FEISHU_AUTHORIZATION_ENDPOINT``
* ``LINGXI_FEISHU_SCOPE``（必须含 ``offline_access``）
* ``LINGXI_OAUTH_BRIDGE_URL`` / ``LINGXI_OAUTH_BRIDGE_TOKEN``

可选变量：``LINGXI_DELEGATED_REAUTH_STATE_PATH``、
``LINGXI_DELEGATED_REAUTH_STATE_KEY``、``LINGXI_DELEGATED_SUBJECT_OPEN_ID``、
``LINGXI_OAUTH_BRIDGE_WAIT_SECONDS``。
未提供主体时从正式 ``feishu_delegated_subject`` 登记表读取，不能从回调参数猜测。
"""

from __future__ import annotations

from lingxi.apps.reauthorize import main


if __name__ == "__main__":
    raise SystemExit(main())
