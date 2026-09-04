"""专用授权主体标识的只读查询——从 ``adapters/delegated_credentials.py`` 拆出来的
单一函数（opus 批量审查 P1 修复，专用主体结构性出口前置 A3）。

**为什么单独成一个模块，不是留在 ``delegated_credentials.py`` 里被 import**：
``registered_delegated_subject_open_id`` 自己的文档字符串早就写明它"做成模块级
函数……是为了让不该持有凭据的进程也能拿到这一个标识……而 gateway 既没有 Fernet
主密钥、也不该有宿主机凭据文件路径"——但 ``delegated_credentials.py`` 这个文件
本身在别的函数里 ``from cryptography.fernet import Fernet``（``HostFileDelegated
CredentialVault`` 的密文读写）。``scripts/ci/check_installed_package.py`` 的静态
闭包检查按**整个源文件**扫描延迟 import，不区分"gateway 只调用这一个函数"——
一旦 gateway 的 import 闭包里出现 ``lingxi.adapters.delegated_credentials`` 这个
模块名，检查就会（正确地）认定 gateway 需要声明 ``cryptography`` 依赖，而
``pyproject.toml`` 的 ``gateway`` 组明确写着"这一组不含 cryptography——gateway 不碰
Fernet"（2026-08-18 裁定：首次开通编排连同它的 MCP 令牌加解密住在 scheduler）。

把这一个纯只读函数单独拆出来，gateway 就能只 import 这个零 cryptography 依赖的
模块，不必为了一个只读查询把整条 Fernet 依赖链背上——这不是绕过检查，是让
``registered_delegated_subject_open_id`` 原本就声明的"gateway 可以安全调用"这句
话在依赖声明层面真正成立。

``adapters/delegated_credentials.py`` 从本模块重新导出 ``DELEGATED_PURPOSE`` 与
``registered_delegated_subject_open_id``，供 scheduler/admin_bootstrap 既有调用点
（两者镜像都已声明 cryptography）继续用同一个 import 路径，不需要改一行。
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
    """读取正式登记的专用授权主体标识。**只读登记表，不碰凭据文件、不碰 refresh_token。**

    做成模块级函数而不是只挂在 ``HostFileDelegatedCredentialVault`` 上，是为了让
    **不该持有凭据的进程**也能拿到这一个标识：首次开通编排（Epic D）必须知道"哪个
    `open_id` 是专用授权账号"才能在判定第一步就把它排除（`V-身份-02`），而 gateway 既没有
    Fernet 主密钥、也不该有宿主机凭据文件路径。经由那个类去读，等于逼 gateway 去构造一个
    持有解密能力的对象——2026-08-08 的事故正是"多一个进程碰同一份专用授权凭据"的形状。

    登记表里没有行、或值为空白时返回 ``None``。调用方据此判断"还没有专用主体"，
    **不得**把 ``None`` 当成"任何人都不是专用主体"以外的含义。
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
