"""发布意图消费的纯编排断言（Issue #156 / S-C-01）。

认领断言：`V-权限-02`（写入后逐字段读回，一致才算发布完成）、`V-权限-03`（发布 A 不
改动 B 的行）、`V-权限-09`（同一 ``record_key`` 重复发布收敛到同一行；同邮箱不同
``record_key`` 失败关闭不新建）、`V-权限-10`（读回不一致不记发布完成并告警）、
`V-权限-11`（**新建行必须携带 Lingxi 签发的 ``token_cipher``；更新行的字段集不含它，
既有值既不被清空也不被覆盖**——2026-08-17 改写，见 ``core/permission/publish_row.py``
模块文档）、`V-权限-12`（旧版本不覆盖新版本；重入不产生第二行）。

外部表格以假传输层注入（形式沿用 ``tests/test_feishu_delivery_classification.py`` 与
花名册读取用例的既有先例）：**本文件不做任何真实飞书调用**。

否定面：
- 旧版本的意图**一次外部调用都不发**；
- 冲突时**既不更新也不新建**（整张假表逐字节不变）；
- 读回不一致时**不**判发布完成、**不**写 ``published_at``；
- 结果不明**不**被当成明确失败；未预期异常**不**被吞成"结果不明"。
"""

from __future__ import annotations

import unittest

from lingxi.core.permission.publish import (
    DEFAULT_MAX_ATTEMPTS,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_PUBLISHED,
    STATUS_SUPERSEDED,
    ClaimedPublish,
    ExistingPermissionRow,
    PermissionPublishExecutor,
    PermissionTableError,
    PublishAttempt,
    PublishFailureKind,
    PublishOutcome,
    publish_claim,
)
from lingxi.core.permission.publish_row import (
    CREATED_FIELD_NAMES,
    PUBLISHED_FIELD_NAMES,
    PublishRow,
)

FAKE_EMAIL = "jiaming.jia@example.invalid"
OTHER_EMAIL = "yiming.yi@example.invalid"
FAKE_NAME = "化名甲"
PERMISSIONS = '{"1011":["商务"]}'
UPDATED_AT = "2026-08-17T03:00:00Z"
#: biai-agent 加密规格 v1 的**公开测试向量密文**（非生产密钥、非生产令牌）。
#: 这里只需要一份形状合法的密文，不需要解开它。
TOKEN_CIPHER = "RklYRURJVjEyMzQ1Njc4OX5gpf2vKqJiLgzu2n4kug1V1rz6DDt1OCgAZVpg1pL+"


def _row(
    email: str = FAKE_EMAIL,
    *,
    permissions: str = PERMISSIONS,
    token_cipher: str | None = TOKEN_CIPHER,
) -> PublishRow:
    return PublishRow(
        record_key=email,
        email=email,
        name=FAKE_NAME,
        permissions=permissions,
        status="approved",
        updated_at=UPDATED_AT,
        token_cipher=token_cipher,
    )


def _claim(
    *,
    row: PublishRow | None = None,
    version: int = 1,
    current: int | None = 1,
    attempts: int = 1,
    payload: dict | None = None,
    user_id: str = "usr_A",
    created_record_id: str | None = None,
    outbox_id: str = "pub_1",
) -> ClaimedPublish:
    fields = payload if payload is not None else (row or _row()).snapshot_fields
    return ClaimedPublish(
        outbox_id=outbox_id,
        user_id=user_id,
        permission_version=version,
        payload=fields,
        attempts=attempts,
        current_permission_version=current,
        created_record_id=created_record_id,
    )


class FakeTable:
    """内存版发布表。记录每一次调用，便于断言"一次外部调用都没发"。"""

    def __init__(self, rows: list[dict] | None = None) -> None:
        self.rows: list[dict] = rows or []
        self.calls: list[str] = []
        self._next = len(self.rows) + 1
        #: 注入故障：方法名 → 要抛的异常。
        self.faults: dict[str, Exception] = {}
        #: 写入后平台"悄悄改掉"的字段（模拟读回不一致）。
        self.mutate_on_write: dict[str, object] = {}
        #: 每一次写入实际提交的字段集：``(动作, 字段字典)``。断言"更新集里没有
        #: ``token_cipher``"必须看**提交出去的那一份**，而不是事后看表里剩下什么——
        #: 后者在部分更新语义下永远是绿的。
        self.written: list[tuple[str, dict]] = []

    def _maybe_fail(self, name: str) -> None:
        self.calls.append(name)
        error = self.faults.pop(name, None)
        if error is not None:
            raise error

    def _find(self, record_id: str) -> dict:
        for row in self.rows:
            if row["record_id"] == record_id:
                return row
        raise PermissionTableError("feishu_code_1254043")

    def find_rows(self, *, record_key: str, email: str):
        self._maybe_fail("find_rows")
        matched = []
        for row in self.rows:
            fields = row["fields"]
            if str(fields.get("record_key", "")).casefold() == record_key.casefold() or str(
                fields.get("email", "")
            ).casefold() == email.casefold():
                matched.append(ExistingPermissionRow(row["record_id"], dict(fields)))
        return tuple(matched)

    def create_row(self, fields):
        self._maybe_fail("create_row")
        self.written.append(("create", dict(fields)))
        record_id = f"rec_{self._next}"
        self._next += 1
        stored = dict(fields)
        stored.update(self.mutate_on_write)
        self.rows.append({"record_id": record_id, "fields": stored})
        return record_id

    def update_row(self, record_id, fields):
        self._maybe_fail("update_row")
        self.written.append(("update", dict(fields)))
        row = self._find(record_id)
        # 部分更新：未列出的列保持原值（真实平台语义）。
        row["fields"].update(fields)
        row["fields"].update(self.mutate_on_write)

    def read_row(self, record_id):
        self._maybe_fail("read_row")
        return dict(self._find(record_id)["fields"])

    def snapshot(self) -> list[dict]:
        return [{"record_id": row["record_id"], "fields": dict(row["fields"])} for row in self.rows]


class PublishClaimTest(unittest.TestCase):
    def test_create_then_readback_publishes(self) -> None:
        table = FakeTable()
        attempt = publish_claim(_claim(), transport=table)
        self.assertEqual(attempt.outcome, PublishOutcome.PUBLISHED)
        self.assertEqual(attempt.action, "create")
        self.assertEqual(attempt.external_record_id, "rec_1")
        self.assertEqual(len(table.rows), 1)
        # 新建集 = 更新集 + token_cipher（`V-权限-11` 前半）。
        self.assertEqual(set(table.rows[0]["fields"]), set(CREATED_FIELD_NAMES))
        self.assertEqual(table.rows[0]["fields"]["token_cipher"], TOKEN_CIPHER)

    def test_existing_row_is_updated_and_token_cipher_survives(self) -> None:
        """`V-权限-11`：更新集不含 ``token_cipher``，既有值原样留在表里。"""

        table = FakeTable(
            [
                {
                    "record_id": "rec_9",
                    "fields": {
                        "record_key": FAKE_EMAIL,
                        "email": FAKE_EMAIL,
                        "name": "旧名",
                        "permissions": "{}",
                        "status": "approved",
                        "updated_at": "2026-01-01T00:00:00Z",
                        "token_cipher": "业务侧写入的密文",
                    },
                }
            ]
        )
        attempt = publish_claim(_claim(), transport=table)
        self.assertEqual(attempt.outcome, PublishOutcome.PUBLISHED)
        self.assertEqual(attempt.action, "update")
        self.assertEqual(len(table.rows), 1)
        self.assertEqual(table.rows[0]["fields"]["token_cipher"], "业务侧写入的密文")
        self.assertEqual(table.rows[0]["fields"]["name"], FAKE_NAME)
        self.assertNotIn("create_row", table.calls)

    def test_update_field_set_never_contains_token_cipher(self) -> None:
        """`V-权限-11` 后半（**主动构造更新路径**，断言提交出去的字段集）。

        这里看的是 ``update_row`` 收到的那一份字典，不是事后表里剩下什么：飞书是部分
        更新，"表里那一列还在"在任何实现下都成立，证明不了我们没提交它。
        """

        table = FakeTable(
            [
                {
                    "record_id": "rec_9",
                    "fields": {
                        "record_key": FAKE_EMAIL,
                        "email": FAKE_EMAIL,
                        "name": "旧名",
                        "permissions": "{}",
                        "status": "approved",
                        "updated_at": "2026-01-01T00:00:00Z",
                        "token_cipher": "旧系统签发的密文",
                    },
                }
            ]
        )
        # 我们**手上有**自己签发的密文，但走的是更新路径——仍然一个字都不提交。
        attempt = publish_claim(_claim(row=_row(token_cipher=TOKEN_CIPHER)), transport=table)
        self.assertEqual(attempt.outcome, PublishOutcome.PUBLISHED)
        self.assertEqual([action for action, _ in table.written], ["update"])
        self.assertEqual(set(table.written[0][1]), set(PUBLISHED_FIELD_NAMES))
        self.assertNotIn("token_cipher", table.written[0][1])
        self.assertEqual(table.rows[0]["fields"]["token_cipher"], "旧系统签发的密文")

    def test_create_without_token_cipher_fails_closed(self) -> None:
        """`V-权限-11` 前半否定面：要新建却没有令牌 → 既不新建，也不退回六字段。"""

        table = FakeTable()
        attempt = publish_claim(_claim(row=_row(token_cipher=None)), transport=table)
        self.assertEqual(attempt.outcome, PublishOutcome.INVALID)
        self.assertEqual(attempt.error_code, "missing_token_cipher")
        self.assertEqual(attempt.action, "create")
        self.assertFalse(attempt.retryable)
        self.assertEqual(attempt.next_status(), STATUS_FAILED)
        self.assertEqual(table.rows, [])
        self.assertEqual(table.written, [])
        self.assertNotIn("create_row", table.calls)

    def test_update_without_token_cipher_still_publishes(self) -> None:
        """既有 26 行的形状：我们没有他们的令牌，但**更新**照常成立。"""

        table = FakeTable(
            [
                {
                    "record_id": "rec_9",
                    "fields": {
                        "record_key": FAKE_EMAIL,
                        "email": FAKE_EMAIL,
                        "name": "旧名",
                        "permissions": "{}",
                        "status": "approved",
                        "updated_at": "2026-01-01T00:00:00Z",
                        "token_cipher": "旧系统签发的密文",
                    },
                }
            ]
        )
        attempt = publish_claim(_claim(row=_row(token_cipher=None)), transport=table)
        self.assertEqual(attempt.outcome, PublishOutcome.PUBLISHED)
        self.assertEqual(attempt.action, "update")
        self.assertEqual(table.rows[0]["fields"]["token_cipher"], "旧系统签发的密文")

    def test_retry_after_a_dropped_cipher_never_converges_to_published(self) -> None:
        """**F1 的核心**：新建时平台漏写 ``token_cipher`` → 重试不得降格成"发布完成"。

        场景是真实的：``create_row`` 建成了行，但那一列没落地（或落成空）。本次读回
        不一致判 ``mismatch``；**重试**时按 ``record_key`` 命中该行走更新路径——如果更新
        路径完全不看那一列，就会写六列、读回一致、收敛成 ``published``。于是发布记成功，
        而这一行对问数 MCP 永远无效：探针会烧满十五分钟再转运维，运维拿到的分类还是
        "权限同步超时"，指向完全错误的方向。
        """

        table = FakeTable()
        table.mutate_on_write = {"token_cipher": ""}
        first = publish_claim(_claim(), transport=table)
        self.assertEqual(first.outcome, PublishOutcome.MISMATCH)
        self.assertEqual(first.mismatch_fields, ("token_cipher",))

        # 重试：行已经在表里，密文是空的。必须**补上**并读回验证，而不是绕过。
        table.mutate_on_write = {}
        second = publish_claim(
            _claim(attempts=2, created_record_id=first.external_record_id), transport=table
        )
        self.assertTrue(second.published)
        self.assertEqual(second.action, "update")
        self.assertEqual(len(table.rows), 1)
        # 补空洞时提交的是七字段（含密文），不是六字段。
        self.assertEqual(set(table.written[-1][1]), set(CREATED_FIELD_NAMES))
        self.assertEqual(table.rows[0]["fields"]["token_cipher"], TOKEN_CIPHER)

    def test_an_update_that_clears_an_existing_cipher_is_not_published(self) -> None:
        """更新路径**也要看一眼那一列还在不在**（B01 锚点）。

        这条与"补空洞"是两件事：这里既有行**本来有**密文，因此我们走六字段更新、
        一个字都不提交那一列；但平台在这次写入中把它清掉了。我们没提交它，不代表
        "发布完成"可以不管它——那个结论断言的是"这一行现在对 MCP 有效"，而一行没有
        ``token_cipher`` 的权限对 MCP 无效。不看这一眼，它就静默收敛成 ``published``。
        """

        table = FakeTable(
            [
                {
                    "record_id": "rec_9",
                    "fields": {
                        "record_key": FAKE_EMAIL,
                        "email": FAKE_EMAIL,
                        "name": "旧名",
                        "permissions": "{}",
                        "status": "approved",
                        "updated_at": "2026-01-01T00:00:00Z",
                        "token_cipher": "旧系统签发的密文",
                    },
                }
            ]
        )
        table.mutate_on_write = {"token_cipher": ""}
        attempt = publish_claim(_claim(), transport=table)
        self.assertEqual(attempt.outcome, PublishOutcome.MISMATCH)
        self.assertEqual(attempt.mismatch_fields, ("token_cipher",))
        self.assertFalse(attempt.published)
        # 走的确实是六字段更新路径：我们并没有提交那一列。
        self.assertEqual(set(table.written[0][1]), set(PUBLISHED_FIELD_NAMES))

    def test_an_update_whose_cipher_reads_back_missing_is_not_published(self) -> None:
        """同一条防线的另一种形态：那一列在读回结果里**整个不见了**。"""

        table = FakeTable(
            [
                {
                    "record_id": "rec_9",
                    "fields": {
                        "record_key": FAKE_EMAIL,
                        "email": FAKE_EMAIL,
                        "name": "旧名",
                        "permissions": "{}",
                        "status": "approved",
                        "updated_at": "2026-01-01T00:00:00Z",
                        "token_cipher": "旧系统签发的密文",
                    },
                }
            ]
        )
        original_read = table.read_row

        def read_without_cipher(record_id):
            fields = original_read(record_id)
            fields.pop("token_cipher", None)
            return fields

        table.read_row = read_without_cipher  # type: ignore[method-assign]
        attempt = publish_claim(_claim(), transport=table)
        self.assertEqual(attempt.outcome, PublishOutcome.MISMATCH)
        self.assertEqual(attempt.mismatch_fields, ("token_cipher",))

    def test_retry_still_fails_while_the_platform_keeps_dropping_the_cipher(self) -> None:
        """平台持续吞掉那一列时，每一轮都必须是 ``mismatch``，永远不收敛成成功。"""

        table = FakeTable()
        table.mutate_on_write = {"token_cipher": ""}
        first = publish_claim(_claim(), transport=table)
        second = publish_claim(
            _claim(attempts=2, created_record_id=first.external_record_id), transport=table
        )
        self.assertEqual(second.outcome, PublishOutcome.MISMATCH)
        self.assertEqual(second.mismatch_fields, ("token_cipher",))
        self.assertFalse(second.published)

    def test_retry_after_an_uncertain_create_fills_the_missing_cipher(self) -> None:
        """新建结果不明（读回超时）后重试：行已建但缺密文，同样必须补上再证明。"""

        table = FakeTable()
        table.faults["read_row"] = PermissionTableError("transport_error", definite=False)
        # 模拟"建是建了，但那一列没落地"。
        table.mutate_on_write = {"token_cipher": ""}
        first = publish_claim(_claim(), transport=table)
        self.assertEqual(first.outcome, PublishOutcome.UNCERTAIN)
        self.assertEqual(first.action, "create")

        table.mutate_on_write = {}
        second = publish_claim(
            _claim(attempts=2, created_record_id=first.external_record_id), transport=table
        )
        self.assertTrue(second.published)
        self.assertEqual(len(table.rows), 1)
        self.assertEqual(table.rows[0]["fields"]["token_cipher"], TOKEN_CIPHER)

    def test_update_is_not_published_when_the_row_has_no_cipher_and_we_have_none(self) -> None:
        """既没有既有密文、我们手上也没有 → 失败关闭，不写出一行对 MCP 无效的权限。"""

        table = FakeTable(
            [{"record_id": "rec_9", "fields": {"record_key": FAKE_EMAIL, "email": FAKE_EMAIL}}]
        )
        attempt = publish_claim(_claim(row=_row(token_cipher=None)), transport=table)
        self.assertEqual(attempt.outcome, PublishOutcome.INVALID)
        self.assertEqual(attempt.error_code, "missing_token_cipher")
        self.assertEqual(table.written, [])

    def test_a_row_we_created_whose_cipher_was_rewritten_is_a_mismatch(self) -> None:
        """我们建的行，密文却不是我们写进去的那一份 → ``mismatch``，不是"沿用既有令牌"。

        判据是 ``external_record_id`` 对得上——既有 26 行的该值永远是 ``None``，
        因此这条判定不会误伤旧系统签发的令牌（见下一条用例）。
        """

        table = FakeTable(
            [
                {
                    "record_id": "rec_9",
                    "fields": {
                        "record_key": FAKE_EMAIL,
                        "email": FAKE_EMAIL,
                        "token_cipher": "平台改写过的另一份密文",
                    },
                }
            ]
        )
        attempt = publish_claim(
            _claim(attempts=2, created_record_id="rec_9"), transport=table
        )
        self.assertEqual(attempt.outcome, PublishOutcome.MISMATCH)
        self.assertEqual(attempt.mismatch_fields, ("token_cipher",))
        self.assertEqual(table.written, [])

    def test_a_legacy_row_still_converges_after_a_failed_update(self) -> None:
        """**S-C-01「更新可重试收敛」的回归钉**（G1）。

        既有 26 行的一次更新读回不明 / 不一致之后，行 ID 会进 ``external_record_id``
        （那是审计语义，任何尝试都会写）。如果"这一行是我们建的"用那一列判，重试时判据
        就会成立、而旧密文当然不等于我方快照，于是这一行被判成**永久 mismatch**——
        一个本来只是"下一轮再试一次就好"的暂时故障，变成了再也发不出去的死结。

        出身只能由 ``created_record_id`` 表达，而它只在我们**真的建过**这一行时才非空。
        """

        legacy = {
            "record_id": "rec_9",
            "fields": {
                "record_key": FAKE_EMAIL,
                "email": FAKE_EMAIL,
                "name": "旧名",
                "permissions": "{}",
                "status": "approved",
                "updated_at": "2026-01-01T00:00:00Z",
                "token_cipher": "旧系统签发的密文",
            },
        }
        table = FakeTable([dict(legacy, fields=dict(legacy["fields"]))])
        table.faults["read_row"] = PermissionTableError("transport_error", definite=False)
        first = publish_claim(_claim(), transport=table)
        self.assertEqual(first.outcome, PublishOutcome.UNCERTAIN)
        self.assertEqual(first.action, "update")
        # 审计列确实记下了这一行——正是这一点让"拿它当出身"变得危险。
        self.assertEqual(first.external_record_id, "rec_9")

        # 重试：出身仍为 None（我们从没建过这一行），因此照常收敛。第一次的更新
        # 其实已经写进去了（只是读回不明），重试读到的既有行内容与待写行逐字段相同
        # → 「不变不回写」（rc25 S-1）：判发布完成、零外部写入。
        second = publish_claim(_claim(attempts=2), transport=table)
        self.assertTrue(second.published)
        self.assertEqual(second.action, "unchanged")
        self.assertEqual([action for action, _ in table.written], ["update"], "重试不再第二次写")
        self.assertEqual(table.rows[0]["fields"]["token_cipher"], "旧系统签发的密文")

    def test_an_uncertain_create_does_not_claim_provenance(self) -> None:
        """创建结果不明（**没拿到 ID**）时不认领出身：重试按普通路径收敛。

        那种情况下我们无法把"自己建的"与"并发写入方建的"区分开，改写判定会变成猜测；
        就绪探针是最终的门。
        """

        table = FakeTable()
        table.faults["create_row"] = PermissionTableError("transport_error", definite=False)
        first = publish_claim(_claim(), transport=table)
        self.assertEqual(first.outcome, PublishOutcome.UNCERTAIN)
        self.assertEqual(first.action, "create")
        self.assertIsNone(first.external_record_id)

        # 与此同时另一方建出了这一行（带着别的密文）。重试不做改写判定，走普通更新。
        table.rows.append(
            {
                "record_id": "rec_x",
                "fields": {
                    "record_key": FAKE_EMAIL,
                    "email": FAKE_EMAIL,
                    "name": "他人写的",
                    "permissions": "{}",
                    "status": "approved",
                    "updated_at": "2026-01-01T00:00:00Z",
                    "token_cipher": "别人写进去的密文",
                },
            }
        )
        second = publish_claim(_claim(attempts=2), transport=table)
        self.assertTrue(second.published)
        self.assertEqual(second.action, "update")

    def test_whitespace_only_cipher_counts_as_absent(self) -> None:
        """`G2`：``"   "`` 不是密文。裸真值判断会让它冒充"那一列还在"。"""

        for blank in ("   ", "\t", " 　 "):
            with self.subTest(blank=repr(blank)):
                table = FakeTable(
                    [
                        {
                            "record_id": "rec_9",
                            "fields": {
                                "record_key": FAKE_EMAIL,
                                "email": FAKE_EMAIL,
                                "name": "旧名",
                                "permissions": "{}",
                                "status": "approved",
                                "updated_at": "2026-01-01T00:00:00Z",
                                "token_cipher": blank,
                            },
                        }
                    ]
                )
                # 手上有密文 → 视同空洞，补上并按七字段读回。
                attempt = publish_claim(_claim(), transport=table)
                self.assertTrue(attempt.published)
                self.assertEqual(set(table.written[0][1]), set(CREATED_FIELD_NAMES))
                self.assertEqual(table.rows[0]["fields"]["token_cipher"], TOKEN_CIPHER)

    def test_a_readback_of_only_whitespace_is_not_published(self) -> None:
        """`G2` 的另一半：读回是纯空白同样不算"那一列还在"。"""

        table = FakeTable(
            [
                {
                    "record_id": "rec_9",
                    "fields": {
                        "record_key": FAKE_EMAIL,
                        "email": FAKE_EMAIL,
                        "name": "旧名",
                        "permissions": "{}",
                        "status": "approved",
                        "updated_at": "2026-01-01T00:00:00Z",
                        "token_cipher": "旧系统签发的密文",
                    },
                }
            ]
        )
        table.mutate_on_write = {"token_cipher": "   "}
        attempt = publish_claim(_claim(), transport=table)
        self.assertEqual(attempt.outcome, PublishOutcome.MISMATCH)
        self.assertEqual(attempt.mismatch_fields, ("token_cipher",))

    def test_a_legacy_row_we_never_created_keeps_its_own_cipher(self) -> None:
        """对照：既有 26 行的形状（出身为空）照常更新，不误伤。"""

        table = FakeTable(
            [
                {
                    "record_id": "rec_9",
                    "fields": {
                        "record_key": FAKE_EMAIL,
                        "email": FAKE_EMAIL,
                        "token_cipher": "旧系统签发的密文",
                    },
                }
            ]
        )
        attempt = publish_claim(_claim(attempts=2), transport=table)
        self.assertTrue(attempt.published)
        self.assertEqual(set(table.written[0][1]), set(PUBLISHED_FIELD_NAMES))
        self.assertEqual(table.rows[0]["fields"]["token_cipher"], "旧系统签发的密文")

    def test_create_readback_covers_token_cipher(self) -> None:
        """新建路径的读回把 ``token_cipher`` 一起比：平台改掉它同样不算发布完成。"""

        table = FakeTable()
        table.mutate_on_write = {"token_cipher": "平台改过的值"}
        attempt = publish_claim(_claim(), transport=table)
        self.assertEqual(attempt.outcome, PublishOutcome.MISMATCH)
        self.assertEqual(attempt.mismatch_fields, ("token_cipher",))
        self.assertFalse(attempt.published)
        # 只有字段名进结果，密文值不进（它是凭据材料）。
        self.assertNotIn(TOKEN_CIPHER, str(attempt.audit_facts()))

    def test_repeated_publish_converges_to_one_row(self) -> None:
        """`V-权限-09`：同一 ``record_key`` 重复发布不产生第二行。"""

        table = FakeTable()
        first = publish_claim(_claim(), transport=table)
        second = publish_claim(_claim(attempts=2), transport=table)
        self.assertTrue(first.published and second.published)
        # 同一内容第二次发布：既有行逐字段相同 → 「不变不回写」（rc25 S-1），仍收敛到
        # 同一行；内容真的变了才走 update（见 ``UnchangedRowTest``）。
        self.assertEqual(second.action, "unchanged")
        self.assertEqual(len(table.rows), 1)
        self.assertEqual(first.external_record_id, second.external_record_id)

    def test_same_email_other_record_key_fails_closed(self) -> None:
        """`V-权限-09` 否定面：既有行用了别的 ``record_key`` 口径 → 不更新也不新建。"""

        table = FakeTable(
            [
                {
                    "record_id": "rec_9",
                    "fields": {"record_key": "EMP-700123", "email": FAKE_EMAIL, "name": "旧名"},
                }
            ]
        )
        before = table.snapshot()
        attempt = publish_claim(_claim(), transport=table)
        self.assertEqual(attempt.outcome, PublishOutcome.CONFLICT)
        self.assertEqual(attempt.error_code, "record_key_mismatch")
        self.assertFalse(attempt.retryable)
        self.assertEqual(table.snapshot(), before)
        self.assertEqual(table.calls, ["find_rows"])

    def test_multiple_matches_fail_closed(self) -> None:
        table = FakeTable(
            [
                {"record_id": "rec_1", "fields": {"record_key": FAKE_EMAIL, "email": FAKE_EMAIL}},
                {"record_id": "rec_2", "fields": {"record_key": "x", "email": FAKE_EMAIL}},
            ]
        )
        before = table.snapshot()
        attempt = publish_claim(_claim(), transport=table)
        self.assertEqual(attempt.outcome, PublishOutcome.CONFLICT)
        self.assertEqual(attempt.error_code, "multiple_rows")
        self.assertEqual(table.snapshot(), before)

    def test_record_key_case_difference_still_updates(self) -> None:
        table = FakeTable(
            [
                {
                    "record_id": "rec_9",
                    "fields": {"record_key": FAKE_EMAIL.upper(), "email": FAKE_EMAIL.upper()},
                }
            ]
        )
        attempt = publish_claim(_claim(), transport=table)
        self.assertEqual(attempt.outcome, PublishOutcome.PUBLISHED)
        self.assertEqual(len(table.rows), 1)

    def test_stale_version_never_touches_the_table(self) -> None:
        """`V-权限-12`：旧版本的意图一次外部调用都不发。"""

        table = FakeTable()
        attempt = publish_claim(_claim(version=1, current=2), transport=table)
        self.assertEqual(attempt.outcome, PublishOutcome.SUPERSEDED)
        self.assertEqual(table.calls, [])
        self.assertEqual(attempt.next_status(), STATUS_SUPERSEDED)

    def test_missing_user_is_superseded_not_failed(self) -> None:
        attempt = publish_claim(_claim(current=None), transport=FakeTable())
        self.assertEqual(attempt.outcome, PublishOutcome.SUPERSEDED)
        self.assertEqual(attempt.detail, "user_missing")

    def test_readback_mismatch_is_not_published(self) -> None:
        """`V-权限-10`：平台收下的内容与我们决定发布的不同 → 不记发布完成。"""

        table = FakeTable()
        table.mutate_on_write = {"permissions": "{}"}
        attempt = publish_claim(_claim(), transport=table)
        self.assertEqual(attempt.outcome, PublishOutcome.MISMATCH)
        self.assertEqual(attempt.mismatch_fields, ("permissions",))
        self.assertFalse(attempt.published)
        self.assertTrue(attempt.retryable)
        self.assertEqual(attempt.failure_kind, PublishFailureKind.DEFINITE)

    def test_definite_rejection_is_not_uncertain(self) -> None:
        table = FakeTable()
        table.faults["create_row"] = PermissionTableError("feishu_code_91403")
        attempt = publish_claim(_claim(), transport=table)
        self.assertEqual(attempt.outcome, PublishOutcome.REJECTED)
        self.assertEqual(attempt.failure_kind, PublishFailureKind.DEFINITE)
        self.assertEqual(attempt.error_code, "feishu_code_91403")

    def test_indeterminate_failure_is_uncertain(self) -> None:
        table = FakeTable()
        table.faults["find_rows"] = PermissionTableError("transport_error", definite=False)
        attempt = publish_claim(_claim(), transport=table)
        self.assertEqual(attempt.outcome, PublishOutcome.UNCERTAIN)
        self.assertEqual(attempt.failure_kind, PublishFailureKind.INDETERMINATE)
        self.assertEqual(attempt.action, "none")

    def test_uncertain_readback_retry_converges_to_one_row(self) -> None:
        """新建成功但读回结果不明时，重试按 ``record_key`` 命中并更新，不建第二行。"""

        table = FakeTable()
        table.faults["read_row"] = PermissionTableError("transport_error", definite=False)
        first = publish_claim(_claim(), transport=table)
        self.assertEqual(first.outcome, PublishOutcome.UNCERTAIN)
        self.assertEqual(first.action, "create")
        self.assertEqual(first.external_record_id, "rec_1")

        second = publish_claim(_claim(attempts=2), transport=table)
        self.assertTrue(second.published)
        # 第一次其实已经建成（只是读回不明）：重试命中的既有行内容逐字段相同 →
        # 「不变不回写」（rc25 S-1），不建第二行、也不再写一次。
        self.assertEqual(second.action, "unchanged")
        self.assertEqual(len(table.rows), 1)
        self.assertEqual([action for action, _ in table.written], ["create"])

    def test_invalid_payload_is_not_retried(self) -> None:
        attempt = publish_claim(_claim(payload={"record_key": FAKE_EMAIL}), transport=FakeTable())
        self.assertEqual(attempt.outcome, PublishOutcome.INVALID)
        self.assertFalse(attempt.retryable)
        self.assertEqual(attempt.next_status(), STATUS_FAILED)
        # 只记异常类型，不回显快照内容。
        self.assertEqual(attempt.detail, "ValueError")

    def test_unexpected_exception_is_not_swallowed(self) -> None:
        table = FakeTable()
        table.faults["find_rows"] = RuntimeError("未预期缺陷")
        with self.assertRaises(RuntimeError):
            publish_claim(_claim(), transport=table)

    def test_publishing_one_user_leaves_another_row_untouched(self) -> None:
        """`V-权限-03`：改 A 的权限，B 的发布行逐字段不变。"""

        table = FakeTable(
            [
                {
                    "record_id": "rec_B",
                    "fields": {
                        "record_key": OTHER_EMAIL,
                        "email": OTHER_EMAIL,
                        "name": "化名乙",
                        "permissions": '{"2001":["运营"]}',
                        "status": "approved",
                        "updated_at": "2026-01-01T00:00:00Z",
                    },
                }
            ]
        )
        before = table.snapshot()[0]
        attempt = publish_claim(_claim(), transport=table)
        self.assertTrue(attempt.published)
        self.assertEqual(table.snapshot()[0], before)
        self.assertEqual(len(table.rows), 2)

    def test_published_attempt_cannot_carry_mismatch(self) -> None:
        with self.assertRaises(ValueError):
            PublishAttempt(
                outcome=PublishOutcome.PUBLISHED,
                outbox_id="pub_1",
                user_id="usr_A",
                permission_version=1,
                external_record_id="rec_1",
                mismatch_fields=("email",),
            )

    def test_published_attempt_requires_external_record_id(self) -> None:
        with self.assertRaises(ValueError):
            PublishAttempt(
                outcome=PublishOutcome.PUBLISHED,
                outbox_id="pub_1",
                user_id="usr_A",
                permission_version=1,
            )


def _existing_row(**overrides: object) -> dict:
    fields = {
        "record_key": FAKE_EMAIL,
        "email": FAKE_EMAIL,
        "name": FAKE_NAME,
        "permissions": PERMISSIONS,
        "status": "approved",
        "updated_at": "2026-01-01T00:00:00Z",
        "token_cipher": "旧系统签发的密文",
    }
    fields.update(overrides)
    return {"record_id": "rec_9", "fields": fields}


class UnchangedRowTest(unittest.TestCase):
    """「不变不回写」（rc25 S-1，Issue #540，`V-权限-16`）：既有行六个内容字段与待写行逐
    字段相同且密文仍在 → 判发布完成、``action="unchanged"``、零外部写入、``updated_at``
    逐字节不动；任一内容字段不同仍走 update；密文空洞与自建行密文改写守卫不受影响。

    变异锚点：把短路条件里的 ``existing_cipher`` 拿掉，
    ``test_a_cipher_hole_is_still_filled_even_when_content_matches`` 变红（空洞不再补写）；
    把整段短路删掉，``test_identical_row_is_published_without_touching_the_table`` 变红。"""

    def test_identical_row_is_published_without_touching_the_table(self) -> None:
        table = FakeTable([_existing_row()])
        attempt = publish_claim(_claim(), transport=table)
        self.assertEqual(attempt.outcome, PublishOutcome.PUBLISHED)
        self.assertTrue(attempt.published)
        self.assertEqual(attempt.action, "unchanged")
        self.assertEqual(attempt.external_record_id, "rec_9")
        self.assertEqual(table.written, [], "零外部写入")
        self.assertEqual(table.calls, ["find_rows"], "表调用只有查找")
        self.assertEqual(table.rows[0]["fields"]["updated_at"], "2026-01-01T00:00:00Z")
        self.assertEqual(attempt.audit_facts()["action"], "unchanged")

    def test_unchanged_is_decided_on_the_freshly_read_row_not_the_payload(self) -> None:
        """判据来自 ``find_rows`` 刚读回的行：快照里的 ``updated_at`` 与表里不同也算相同。"""

        table = FakeTable([_existing_row(updated_at="2025-12-31T00:00:00Z")])
        attempt = publish_claim(_claim(row=_row()), transport=table)
        self.assertEqual(attempt.action, "unchanged")
        self.assertEqual(table.written, [])

    def test_different_permissions_still_update(self) -> None:
        table = FakeTable([_existing_row(permissions='{"1011":["旧口径"]}')])
        attempt = publish_claim(_claim(), transport=table)
        self.assertEqual(attempt.action, "update")
        self.assertEqual([action for action, _ in table.written], ["update"])
        self.assertEqual(table.rows[0]["fields"]["permissions"], PERMISSIONS)

    def test_a_different_name_or_status_still_updates(self) -> None:
        """比较口径是六个内容字段（不只 permissions）。"""

        for overrides in ({"name": "旧名"}, {"status": "pending"}):
            with self.subTest(overrides=overrides):
                table = FakeTable([_existing_row(**overrides)])
                attempt = publish_claim(_claim(), transport=table)
                self.assertEqual(attempt.action, "update")

    def test_a_cipher_hole_is_still_filled_even_when_content_matches(self) -> None:
        """`V-权限-11` 不变：既有行密文为空时不短路，走新建集补写密文。"""

        table = FakeTable([_existing_row(token_cipher="")])
        attempt = publish_claim(_claim(), transport=table)
        self.assertEqual(attempt.outcome, PublishOutcome.PUBLISHED)
        self.assertEqual(attempt.action, "update")
        self.assertEqual(set(table.written[0][1]), set(CREATED_FIELD_NAMES))
        self.assertEqual(table.rows[0]["fields"]["token_cipher"], TOKEN_CIPHER)

    def test_own_row_with_a_rewritten_cipher_is_still_a_mismatch(self) -> None:
        """自建行密文改写守卫排在短路之前：内容相同也不能收敛成发布完成。"""

        table = FakeTable([_existing_row(token_cipher="平台改过的值")])
        attempt = publish_claim(_claim(created_record_id="rec_9"), transport=table)
        self.assertEqual(attempt.outcome, PublishOutcome.MISMATCH)
        self.assertEqual(attempt.mismatch_fields, ("token_cipher",))
        self.assertEqual(table.written, [])

    def test_record_key_case_difference_still_counts_as_changed(self) -> None:
        """内容比较是逐字节的：既有行 ``record_key`` 大小写不同 → 走 update 收敛口径。"""

        table = FakeTable([_existing_row(record_key=FAKE_EMAIL.upper(), email=FAKE_EMAIL.upper())])
        attempt = publish_claim(_claim(), transport=table)
        self.assertEqual(attempt.action, "update")


class NextStatusTest(unittest.TestCase):
    def _attempt(self, outcome: PublishOutcome, attempts: int) -> PublishAttempt:
        return PublishAttempt(
            outcome=outcome,
            outbox_id="pub_1",
            user_id="usr_A",
            permission_version=1,
            attempts=attempts,
            mismatch_fields=("email",) if outcome is PublishOutcome.MISMATCH else (),
        )

    def test_retryable_returns_to_pending_until_attempts_run_out(self) -> None:
        self.assertEqual(self._attempt(PublishOutcome.UNCERTAIN, 1).next_status(), STATUS_PENDING)
        self.assertEqual(
            self._attempt(PublishOutcome.UNCERTAIN, DEFAULT_MAX_ATTEMPTS).next_status(),
            STATUS_FAILED,
        )

    def test_conflict_never_retries(self) -> None:
        self.assertEqual(self._attempt(PublishOutcome.CONFLICT, 1).next_status(), STATUS_FAILED)

    def test_mismatch_is_retryable_but_bounded(self) -> None:
        self.assertEqual(self._attempt(PublishOutcome.MISMATCH, 1).next_status(), STATUS_PENDING)
        self.assertEqual(
            self._attempt(PublishOutcome.MISMATCH, 9).next_status(max_attempts=3), STATUS_FAILED
        )

    def test_published_and_superseded_are_terminal(self) -> None:
        published = PublishAttempt(
            outcome=PublishOutcome.PUBLISHED,
            outbox_id="pub_1",
            user_id="usr_A",
            permission_version=1,
            external_record_id="rec_1",
        )
        self.assertEqual(published.next_status(), STATUS_PUBLISHED)
        self.assertEqual(self._attempt(PublishOutcome.SUPERSEDED, 1).next_status(), STATUS_SUPERSEDED)


class FakeStore:
    def __init__(self, claims: list[ClaimedPublish], *, fail_complete: bool = False) -> None:
        self._claims = list(claims)
        self.completed: list[tuple[PublishAttempt, str]] = []
        self._fail_complete = fail_complete
        #: 每次认领时收到的本轮排除清单，供 F2 的断言比对。
        self.excludes: list[tuple[str, ...]] = []

    def claim_next(self, *, exclude=()):
        # **真的按 exclude 跳过**：只记不跳的替身会让"本轮排除"这条断言在假 store 上
        # 恒绿，而它恰恰是本轮要证明的行为。
        self.excludes.append(tuple(exclude))
        for index, candidate in enumerate(self._claims):
            if candidate.outbox_id in exclude:
                continue
            return self._claims.pop(index)
        return None

    def complete(self, attempt, *, status):
        if self._fail_complete:
            raise RuntimeError("记账失败")
        self.completed.append((attempt, status))


class RecordingAudit:
    def __init__(self) -> None:
        self.entries: list[tuple[str, dict]] = []

    def record(self, action: str, /, **fields: object) -> None:
        self.entries.append((action, dict(fields)))


class ExecutorTest(unittest.TestCase):
    def test_run_once_consumes_until_queue_is_empty(self) -> None:
        table = FakeTable()
        store = FakeStore(
            [
                _claim(user_id="usr_A", outbox_id="pub_1"),
                _claim(row=_row(OTHER_EMAIL), user_id="usr_B", outbox_id="pub_2"),
            ]
        )
        audit = RecordingAudit()
        alerts: list[PublishAttempt] = []
        executor = PermissionPublishExecutor(
            store=store, transport=table, audit=audit, on_alert=alerts.append
        )
        attempts = executor.run_once()
        self.assertEqual(len(attempts), 2)
        self.assertTrue(all(item.published for item in attempts))
        self.assertEqual([status for _, status in store.completed], ["published", "published"])
        self.assertEqual(alerts, [])
        self.assertEqual(
            [action for action, _ in audit.entries],
            ["permission_publish.published", "permission_publish.published"],
        )

    def test_failed_attempt_alerts_and_records(self) -> None:
        table = FakeTable()
        table.mutate_on_write = {"name": "被平台改掉的名字"}
        store = FakeStore([_claim()])
        audit = RecordingAudit()
        alerts: list[PublishAttempt] = []
        executor = PermissionPublishExecutor(
            store=store, transport=table, audit=audit, on_alert=alerts.append
        )
        (attempt,) = executor.run_once()
        self.assertEqual(attempt.outcome, PublishOutcome.MISMATCH)
        self.assertEqual(alerts, [attempt])
        self.assertEqual(alerts[0].alert_kind, "permission_publish_mismatch")
        self.assertEqual(store.completed[0][1], STATUS_PENDING)

    def test_superseded_does_not_alert(self) -> None:
        store = FakeStore([_claim(version=1, current=5)])
        alerts: list[PublishAttempt] = []
        executor = PermissionPublishExecutor(
            store=store, transport=FakeTable(), audit=RecordingAudit(), on_alert=alerts.append
        )
        (attempt,) = executor.run_once()
        self.assertEqual(attempt.outcome, PublishOutcome.SUPERSEDED)
        self.assertEqual(alerts, [])

    def test_complete_failure_is_recorded_then_raised(self) -> None:
        store = FakeStore([_claim()], fail_complete=True)
        audit = RecordingAudit()
        executor = PermissionPublishExecutor(store=store, transport=FakeTable(), audit=audit)
        with self.assertRaises(RuntimeError):
            executor.run_once()
        self.assertEqual(audit.entries[0][0], "permission_publish.complete_failed")
        self.assertEqual(audit.entries[0][1]["error"], "RuntimeError")

    def test_audit_facts_carry_no_personal_values(self) -> None:
        table = FakeTable()
        store = FakeStore([_claim()])
        audit = RecordingAudit()
        PermissionPublishExecutor(store=store, transport=table, audit=audit).run_once()
        rendered = repr(audit.entries)
        self.assertNotIn(FAKE_EMAIL, rendered)
        self.assertNotIn(FAKE_NAME, rendered)

    def test_the_claim_identity_reaches_the_bookkeeping_unchanged(self) -> None:
        """记账要能绑定到**本次认领**，前提是认领标识被如实带下去（P3-1 的非 SQL 那半）。

        ``attempts`` 在每次认领时自增，是"哪一次认领"的天然版本号；store 的
        ``complete`` 用它做守卫（真库用例
        `test_a_stale_completer_cannot_overwrite_the_new_claimer`）。这里钉住的是它在
        编排层不被改写或写死——一旦被写死，那条守卫就永远比对一个假的次数。
        """

        store = FakeStore([_claim(attempts=3)])
        executor = PermissionPublishExecutor(
            store=store, transport=FakeTable(), audit=RecordingAudit()
        )
        (attempt,) = executor.run_once()
        self.assertEqual(attempt.attempts, 3)
        self.assertEqual(store.completed[0][0].attempts, 3)

    def test_limit_bounds_one_round(self) -> None:
        store = FakeStore([_claim(outbox_id=f"pub_{index}") for index in range(1, 4)])
        executor = PermissionPublishExecutor(
            store=store, transport=FakeTable(), audit=RecordingAudit()
        )
        self.assertEqual(len(executor.run_once(limit=2)), 2)

    def test_without_alert_hook_nothing_is_silently_dropped(self) -> None:
        table = FakeTable()
        table.faults["find_rows"] = PermissionTableError("transport_error", definite=False)
        audit = RecordingAudit()
        executor = PermissionPublishExecutor(
            store=FakeStore([_claim()]), transport=table, audit=audit
        )
        (attempt,) = executor.run_once()
        self.assertEqual(attempt.outcome, PublishOutcome.UNCERTAIN)
        self.assertEqual(audit.entries[0][0], "permission_publish.uncertain")


class RoundExclusionTest(unittest.TestCase):
    """**一条意图一轮最多认领一次**（Epic C 冻结缺陷 F2）。

    修复前：``claim_next`` 按 ``(created_at, id)`` 取最老的一条，而一次失败只把状态写回
    ``pending``、不改 ``created_at``——它仍然是最老的那一条，于是在**同一轮里**被立刻
    重新认领。真库实测下快失败形态的 5 次重试在 0.195 秒内烧完转 ``failed``。

    本组用一个"失败即放回队列"的替身重现那个形状：没有本轮排除时它会被反复取出。
    """

    def _executor(self, store) -> PermissionPublishExecutor:
        table = FakeTable()
        table.faults["find_rows"] = PermissionTableError("http_500", definite=True)
        return PermissionPublishExecutor(
            store=store, transport=table, audit=RecordingAudit()
        )

    def test_a_failed_intent_is_not_reclaimed_in_the_same_round(self) -> None:
        store = RequeueingStore([_claim(outbox_id="pub_1")])

        attempts = self._executor(store).run_once(limit=5)

        self.assertEqual(len(attempts), 1, "同一条意图在一轮里只能被认领一次")
        self.assertEqual(store.excludes, [(), ("pub_1",)])

    def test_the_exclusion_does_not_starve_other_users(self) -> None:
        """否定断言：**跳过不等于少消费**。

        本轮排除只把已经认领过的那条排除在候选之外，别人的意图照常被取走——否则修
        F2 会造出一个更糟的缺陷：一个人失败就让本轮剩下的预算全部空转。
        """

        store = RequeueingStore(
            [
                _claim(user_id="usr_A", outbox_id="pub_1"),
                _claim(row=_row(OTHER_EMAIL), user_id="usr_B", outbox_id="pub_2"),
            ]
        )

        attempts = self._executor(store).run_once(limit=5)

        self.assertEqual([item.outbox_id for item in attempts], ["pub_1", "pub_2"])
        self.assertEqual(store.excludes, [(), ("pub_1",), ("pub_1", "pub_2")])

    def test_the_caller_can_own_the_round(self) -> None:
        """一轮的边界可以在调用方那一层。

        生产形态是 ``run_once(limit=1)`` × N（调度职责要逐条查停止信号与时间预算），
        那时累积的已认领清单由职责传进来——见
        ``lingxi.apps.scheduler.permission_publish.PermissionPublishDuty._publish``。
        """

        store = RequeueingStore([_claim(outbox_id="pub_1")])
        executor = self._executor(store)

        self.assertEqual(len(executor.run_once(limit=1)), 1)
        self.assertEqual(executor.run_once(limit=1, exclude=("pub_1",)), ())
        self.assertEqual(store.excludes, [(), ("pub_1",)])

    def test_the_next_round_may_claim_it_again(self) -> None:
        """排除的作用域是**一轮**：下一轮它照样能被认领，否则重试就永远不会发生。"""

        store = RequeueingStore([_claim(outbox_id="pub_1")])
        executor = self._executor(store)

        self.assertEqual(len(executor.run_once(limit=5)), 1)
        self.assertEqual(len(executor.run_once(limit=5)), 1)


class RequeueingStore:
    """"失败就回 ``pending``"的 outbox 替身：``complete(status='pending')`` 把那条意图
    放回队列**最前面**（它的 ``created_at`` 没变，仍然是最老的一条）。

    这正是真库 ``claim_next`` 的行为，也是 F2 的成因；没有本轮排除时它会被无限重取。
    """

    def __init__(self, claims: list[ClaimedPublish]) -> None:
        self._claims = list(claims)
        self.completed: list[tuple[PublishAttempt, str]] = []
        self.excludes: list[tuple[str, ...]] = []

    def claim_next(self, *, exclude=()):
        self.excludes.append(tuple(exclude))
        for index, candidate in enumerate(self._claims):
            if candidate.outbox_id in exclude:
                continue
            return self._claims.pop(index)
        return None

    def complete(self, attempt: PublishAttempt, *, status: str) -> None:
        self.completed.append((attempt, status))
        if status != "pending":
            return
        self._claims.insert(
            0,
            _claim(
                user_id=attempt.user_id,
                outbox_id=attempt.outbox_id,
                attempts=attempt.attempts + 1,
            ),
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
