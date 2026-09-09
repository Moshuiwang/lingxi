"""部署固定只读回读入口，stdout 只有非秘密兼容与版本状态。"""

import json
import os
import sys


def main():
    """不接受命令、SQL、目标身份或自定义路径参数。"""
    from lingxi.adapters.postgres_admin_followup import PostgresFollowupStore
    from lingxi.adapters.postgres_innertest_roster import PostgresInnertestRoster, binding_status

    if len(sys.argv) != 1:
        return 2
    dsn = os.environ.get("LINGXI_POSTGRES_DSN")
    scope = os.environ.get("LINGXI_INNERTEST_SCOPE")
    binding = os.environ.get("LINGXI_INNERTEST_BINDING_ID")
    if not dsn or not scope or not binding:
        print(json.dumps({"ok": False, "code": "configuration_missing"}))
        return 2
    try:
        result = dict(
            ok=True,
            schema_revision=1,
            roster=PostgresInnertestRoster(dsn, scope=scope).recovery_status(),
            binding=binding_status(dsn, scope=scope, binding_id=binding),
            followups=PostgresFollowupStore(dsn).recovery_status(),
        )
        print(json.dumps(result))
        return 0
    except Exception:
        print(json.dumps({"ok": False, "code": "roster_unavailable"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
