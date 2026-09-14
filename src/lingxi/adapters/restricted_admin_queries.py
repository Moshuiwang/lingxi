"""受限通道只读三工具的数据库适配与通道分发：只读口零写库，准备工具另见 ``restricted_admin_prepare``。

身份仍由扩员服务的 ``authenticate`` 每请求现读；这里只在拿到已验证主体之后查数据。
只读与准备工具每次调用都写一行结构化审计（工具名、结果码、耗时、追溯号），不含查询正文。
"""

from __future__ import annotations

import time
from pathlib import Path

from lingxi.adapters.innertest_request import remaining
from lingxi.adapters.postgres import DEFAULT_POSTGRES_TIMEOUTS, PostgresTimeouts, connect
from lingxi.core.admin.innertest import TOOL_NAMES, InnertestError, envelope
from lingxi.core.admin.restricted_tools import (
    DEFAULT_PENDING_ACTIONS,
    PREPARE_TOOL_NAMES,
    READ_ONLY_TOOL_NAMES,
    candidate_lists,
    pending_action_item,
    pending_actions_result,
    permission_sources_result,
    trace_result,
    user_status_result,
)
from lingxi.core.ids import new_id

RECENT_EVENT_WINDOW_HOURS = 24 * 7
RECENT_EVENT_LIMIT = 20

_PENDING_COLUMNS = (
    "id, action_type, target_open_id, initiated_by_open_id, status, card_delivered, reason,"
    " created_at, confirm_deadline_at, decided_at, decided_by_open_id, payload"
)


class RestrictedAdminQueries:
    """三只读工具；查询口是既有 ``PostgresAdminQueries`` 与阶段表的只读方法。"""

    def __init__(
        self,
        dsn,
        *,
        queries,
        followups,
        metric_map_path: Path | None,
        timeouts: PostgresTimeouts = DEFAULT_POSTGRES_TIMEOUTS,
    ):
        """映射文件每次调用现读，读不到时结果里明确说明而不是猜。"""
        self._dsn, self._timeouts = dsn, timeouts
        self._queries, self._followups = queries, followups
        self._metric_map_path = metric_map_path

    def get_user_status(self, principal, *, call_trace_id, identifier=None, trace_id=None):
        """标识走用户状态 + 最近事件；追溯号走追溯视图；查无即 ``not_found``。"""
        del principal
        if trace_id is not None:
            trace = self._queries.trace_lookup(trace_id=trace_id)
            return trace_result(trace_id=call_trace_id, lookup=trace_id, trace=trace)
        open_id, status = self._status(identifier)
        events = self._queries.recent_events(
            identifier=open_id, window_hours=RECENT_EVENT_WINDOW_HOURS, limit=RECENT_EVENT_LIMIT
        )
        return user_status_result(
            trace_id=call_trace_id,
            identifier=identifier,
            open_id=open_id,
            status=status,
            events=events,
        )

    def _status(self, identifier):
        """邮箱形态先反查；查无不猜相似人，预算耗尽不再发起后续查询。"""
        open_id = self._queries.resolve_identifier(identifier=identifier)
        status = self._queries.user_status(identifier=open_id)
        if status is None:
            raise InnertestError("not_found")
        remaining()
        return open_id, status

    def get_user_permission_sources(self, principal, *, call_trace_id, identifier):
        """银河摘要、本地覆盖与合成结果都在同一次现读上计算。"""
        del principal
        open_id, status = self._status(identifier)
        metric_map, positions = self.catalog()
        return permission_sources_result(
            trace_id=call_trace_id,
            identifier=identifier,
            open_id=open_id,
            status=status,
            metric_map=metric_map,
            candidates=candidate_lists(metric_map=metric_map, positions=positions),
        )

    def catalog(self):
        """两份映射文件各自失败关闭为「不可读」，不用随包默认顶替外置文件。"""
        from lingxi.adapters.company_function_metric_map_file import (
            load_company_function_metric_map,
        )
        from lingxi.adapters.role_function_map_file import load_role_function_map

        try:
            metric_map = load_company_function_metric_map(self._metric_map_path)
        except Exception:
            metric_map = None
        try:
            positions = tuple(load_role_function_map().keys())
        except Exception:
            positions = None
        return metric_map, positions

    def get_pending_actions(
        self, principal, *, call_trace_id, pending_action_id=None, limit=None, status=None
    ):
        """只投影本人发起的动作；别人的编号与查无同样是 ``not_found``。"""
        rows = self.pending_rows(
            principal.open_id,
            pending_action_id=pending_action_id,
            limit=DEFAULT_PENDING_ACTIONS if limit is None else limit,
            status=status,
        )
        items = []
        for row in rows:
            remaining()
            items.append(self._item(row))
        return pending_actions_result(
            trace_id=call_trace_id, items=items, single=pending_action_id is not None
        )

    def _item(self, row):
        """一行动作 + 它的全部阶段引用，投影成只读形状。"""
        return pending_action_item(
            row, self._followups.list_for_action(pending_action_id=row["id"])
        )

    def action_item(self, open_id, pending_action_id):
        """本人的一条动作投影；不是本人发起或查无即 ``None``，供准备工具回显结果。"""
        rows = self.pending_rows(open_id, pending_action_id=pending_action_id, limit=1, status=None)
        return self._item(rows[0]) if rows else None

    def pending_rows(self, open_id, *, pending_action_id, limit, status):
        """固定列、固定谓词的只读 SELECT，不接受客户端 SQL。"""
        with connect(self._dsn, timeouts=self._timeouts) as connection, connection.cursor() as cur:
            if pending_action_id is not None:
                cur.execute(
                    f"SELECT {_PENDING_COLUMNS} FROM pending_action "
                    "WHERE id=%s AND initiated_by_open_id=%s",
                    (pending_action_id, open_id),
                )
            elif status is not None:
                cur.execute(
                    f"SELECT {_PENDING_COLUMNS} FROM pending_action "
                    "WHERE initiated_by_open_id=%s AND status=%s "
                    "ORDER BY created_at DESC, id DESC LIMIT %s",
                    (open_id, status, limit),
                )
            else:
                cur.execute(
                    f"SELECT {_PENDING_COLUMNS} FROM pending_action "
                    "WHERE initiated_by_open_id=%s ORDER BY created_at DESC, id DESC LIMIT %s",
                    (open_id, limit),
                )
            names = [column.name for column in cur.description]
            return [dict(zip(names, row, strict=True)) for row in cur.fetchall()]


class RestrictedChannelService:
    """受限通道的工具分发：扩员三工具交既有服务，只读与准备工具各交适配并逐次审计。

    分发表就是登记式工具集合的三段：没有任何名字会落到确认、取消或执行——通道里根本
    没有这样的方法可分发。
    """

    def __init__(self, *, innertest, queries, audit, prepare=None):
        """``innertest`` 同时是身份权威；``audit`` 不接收查询正文；``prepare`` 未装配即拒绝。"""
        self._innertest, self._queries, self._audit = innertest, queries, audit
        self._prepare = prepare

    def authenticate(self, uid):
        """身份链一字不改：内核 UID → 受保护绑定 → 登记表角色，每请求现读。"""
        return self._innertest.authenticate(uid)

    def call(self, principal, name, args):
        """工具名不是可执行字符串，只分发登记过的固定方法。"""
        if name in TOOL_NAMES:
            return self._innertest.call(principal, name, args)
        if name in READ_ONLY_TOOL_NAMES:
            return self._call_audited(self._queries, principal, name, args)
        if name in PREPARE_TOOL_NAMES and self._prepare is not None:
            return self._call_audited(self._prepare, principal, name, args)
        raise InnertestError("invalid_request")

    def _call_audited(self, target, principal, name, args):
        """每次调用一行审计：成功、业务拒绝与意外异常都留痕，异常不带正文。"""
        trace_id, started = new_id("trc"), time.monotonic()
        failure = None
        try:
            result = getattr(target, name)(principal, call_trace_id=trace_id, **args)
            code = result["code"]
        except InnertestError as error:
            result, code = envelope(error.code, trace_id=trace_id, state="rejected"), error.code
        except Exception as error:
            result, code, failure = None, "error:" + type(error).__name__, error
        self._audit.record(
            "admin.restricted." + name,
            binding_id=principal.binding_id,
            result_code=code,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            trace_id=trace_id,
            pending_action_id=(result or {}).get("pending_action_id"),
        )
        if failure is not None:
            raise InnertestError("query_unavailable") from failure
        return result
