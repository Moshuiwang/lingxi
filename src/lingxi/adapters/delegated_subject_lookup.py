"""专用授权主体标识的只读查询——从 ``adapters/delegated_credentials.py`` 拆出来的
单一函数。

为什么单独成一个模块：``delegated_credentials.py`` 本身在别的函数里
``from cryptography.fernet import Fernet``（密文读写），而依赖闭包检查按整个
源文件扫描延迟 import，不区分"gateway 只调用这一个只读函数"——一旦 gateway 的
import 闭包里出现那个模块名，检查就会认定 gateway 需要声明 ``cryptography``
依赖，但 gateway 组明确不含它（gateway 不碰 Fernet）。把这一个纯只读函数单独
拆出来，gateway 就能只 import 这个零 cryptography 依赖的模块，不必为了一个只
读查询背上整条 Fernet 依赖链。

``delegated_credentials.py`` 从本模块重新导出 ``DELEGATED_PURPOSE`` 与
``registered_delegated_subject_open_id``，供既有调用点继续用同一个 import
路径，不需要改一行。
"""

from __future__ import annotations

from lingxi.adapters.postgres import DEFAULT_POSTGRES_TIMEOUTS, PostgresTimeouts, connect

#: 登记表里唯一允许的用途。与 ``adapters/delegated_credentials.py`` 逐字一致
#: （该文件从本模块重新导出这个常量，不重复定义）——新增用途要走迁移，不能靠
#: 调用方传字符串。
DELEGATED_PURPOSE = "org_directory_sync"


def registered_delegated_subject_open_id(
    dsn: str, *, timeouts: PostgresTimeouts = DEFAULT_POSTGRES_TIMEOUTS
) -> str | None:
    """读取正式登记的专用授权主体标识。只读登记表，不碰凭据文件、不碰
    refresh_token。

    做成模块级函数而不是只挂在 ``HostFileDelegatedCredentialVault`` 上，是为
    让不该持有凭据的进程也能拿到这个标识：gateway 既没有 Fernet 主密钥、也不
    该有宿主机凭据文件路径，经由那个类去读等于逼它构造一个持有解密能力的对象。

    登记表里没有行、或值为空白时返回 ``None``；调用方不得读成其他含义。
    """
    with connect(dsn, timeouts=timeouts) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT subject_open_id FROM feishu_delegated_subject WHERE purpose = %s",
            (DELEGATED_PURPOSE,),
        )
        row = cursor.fetchone()
    if row is None or not isinstance(row[0], str) or not row[0].strip():
        return None
    return row[0].strip()
