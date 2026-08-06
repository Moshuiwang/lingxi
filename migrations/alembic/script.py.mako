"""${message}

Revision ID: ${up_revision}
% if down_revision:
Revises: ${down_revision}
% else:
Revises: 无（这是链首）
% endif
Create Date: ${create_date}

写新 revision 前先读 migrations/README.md，三条硬要求：

1. **``revision`` 用可读 id**（``YYYYMMDD_<用途>``），不用随机十六进制。旧库接管和
   运维步骤要把它写进文档与命令行，可读性直接影响可操作性；长度上限 32。
2. **``downgrade()`` 要么真正逆转，要么显式 ``raise``。** 留一个空函数意味着
   ``alembic downgrade`` 会"成功"并把版本号退回去，而库结构没变——之后每一次
   upgrade 都建立在一个假的起点上。``scripts/ci/check_alembic_revisions.py``
   会拒绝空的 downgrade。
3. **新增 revision 后更新 migrations/README.md 里的 head revision id。** 同一个
   检查脚本会核对两者一致，README 过期就是门禁红。
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
${imports if imports else ""}
revision: str = ${repr(up_revision)}
down_revision: str | None = ${repr(down_revision)}
branch_labels: str | None = ${repr(branch_labels)}
depends_on: str | None = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
