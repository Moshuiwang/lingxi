"""迁移连接串的解析与校验。

**本模块刻意不 import alembic、也不 import 任何第三方包。** 它被 `env.py` 使用，
但校验逻辑本身是纯字符串处理，抽出来有两个具体好处：

1. 用例可以在**没装 migrate extra 的机器**上跑。代码框架第四节要求
   `PYTHONPATH=src python3 -m unittest discover -s tests` 在无外部依赖的机器上可运行；
   之前这几条断言只能通过子进程跑 `python -m alembic` 来验，于是在没有 alembic 的
   环境里表现为 FAIL 而不是 skip——一条本该处处成立的纯逻辑断言，因为验证方式绑了
   工具链而变得不可验证。
2. `env.py` 是被 alembic exec 的脚本，不是可 import 的模块；把可测的部分挪出来，
   剩下的 `env.py` 就只剩接线。

连接串本身**绝不出现在任何异常消息里**——它可能带口令，而门禁与部署日志都会留痕
（断言 V-迁移-06）。下面所有报错只说变量名、scheme 和路径片段。
"""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

DSN_VARIABLE = "LINGXI_MIGRATION_DSN"
REQUIRED_SCHEME = "postgresql+psycopg"
# 允许写成裸 scheme（运维手写、从业务 DSN 复制都是这个形状），由本模块补齐驱动。
BARE_SCHEMES = ("postgresql", "postgres")

# 迁移不能 import ``lingxi.adapters.postgres``：迁移工具链与运行时依赖必须保持隔离。
# 因此这里是一次性迁移自己的有限配置，而不是把语句超时取消。DDL 允许比业务默认值
# 更长，但仍必须在有限时间内结束；锁等待上限更短，避免迁移无限期等业务事务。
MIGRATION_CONNECT_TIMEOUT_SECONDS = 5
MIGRATION_STATEMENT_TIMEOUT_SECONDS = 60
MIGRATION_LOCK_TIMEOUT_SECONDS = 10


def migration_connect_args() -> dict[str, object]:
    """返回 SQLAlchemy/psycopg3 在线迁移的独立有限连接参数。"""

    return {
        "connect_timeout": MIGRATION_CONNECT_TIMEOUT_SECONDS,
        "options": (
            f"-c statement_timeout={MIGRATION_STATEMENT_TIMEOUT_SECONDS}s "
            f"-c lock_timeout={MIGRATION_LOCK_TIMEOUT_SECONDS}s"
        ),
    }


class MigrationDsnError(RuntimeError):
    """连接串不可用。消息保证不含连接串本身。"""


def normalize_database_url(raw: str | None) -> str:
    """校验并归一化迁移连接串，返回可直接交给 SQLAlchemy 的 URL。

    三条规则，缺一条都会让「跑错库」变成一次沉默的成功：

    1. **必须有值。** 不回落到 `alembic.ini` 的默认串（那里没有），也不回落到业务的
       `LINGXI_POSTGRES_DSN`：迁移要 DDL 权限、业务进程不要，本就该是不同的角色。
    2. **scheme 必须落到 psycopg3。** 裸 `postgresql://` 交给 SQLAlchemy 会让它去找
       默认的 psycopg2 驱动，报的错与真实原因（scheme 没写驱动）差得很远；这里补齐，
       其余驱动一律拒绝。
    3. **必须显式指定库名。** `postgresql://user@host` 与 `postgresql:///` 都是合法
       URL，libpq 会回落到「与用户名同名的库」——于是少写一个 `/db` 不报错，而是把
       整套 DDL 应用到了另一个库上，并且迁移会「成功」。
    """

    if raw is None or not raw.strip():
        raise MigrationDsnError(
            f"缺少环境变量 {DSN_VARIABLE}，未连接任何数据库。"
            "迁移不使用 alembic.ini 里的默认连接串，也不回落到 LINGXI_POSTGRES_DSN。"
        )

    parts = urlsplit(raw.strip())
    scheme = parts.scheme
    if scheme in BARE_SCHEMES:
        parts = parts._replace(scheme=REQUIRED_SCHEME)
    elif scheme != REQUIRED_SCHEME:
        raise MigrationDsnError(
            f"{DSN_VARIABLE} 的 scheme 是 {scheme!r}，本仓库只使用 psycopg3。"
            f"请写成 {REQUIRED_SCHEME}:// 或裸 postgresql://（由迁移侧补齐驱动）。"
        )

    database = parts.path.lstrip("/")
    if not database:
        raise MigrationDsnError(
            f"{DSN_VARIABLE} 没有指定数据库名（URL 里缺少 /<库名>）。"
            "不补默认值：libpq 会回落到与用户名同名的库，"
            "那样一次笔误就会把 DDL 应用到别的库上，而且不会报错。"
        )
    if "/" in database:
        raise MigrationDsnError(f"{DSN_VARIABLE} 的路径部分是 {database!r}，不是单个库名。")

    return urlunsplit(parts)
