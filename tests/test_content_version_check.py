"""`scripts/ci/check_content_version.py` 的判定用例（Issue #190）。

这道门禁的全部价值在于它**会变红**：一条只会通过的检查等于没有检查。因此下面每个
用例都主动构造一次违规——改文案不动版本、复用旧版本号、用刷新命令绕过递增——并断言
它被具体地拒绝。最后一条用例反过来跑真实仓库文件，防止检查因为文件结构变化而空转。
"""

from __future__ import annotations

import importlib.util
import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).parents[1]
SCRIPT = REPOSITORY_ROOT / "scripts" / "ci" / "check_content_version.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("content_version_check_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


CHECK = _load_script()

BASE_CONTENT = """\
[meta]
version = "2026-01-01"

[texts]
"gateway.new_session" = "已开启新会话，可以开始提问。"
"worker.status" = "{action} · {elapsed_seconds} 秒"

[cards."query.status"]
title = "正在查询"
body = "{status}"
button_labels = ["停止"]
"""


def run(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = CHECK.main(argv)
    return code, out.getvalue(), err.getvalue()


class ContentVersionGateTest(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="issue190-content-version-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.content = self.root / "content.toml"
        self.lock = self.root / "content.lock.toml"
        self.content.write_text(BASE_CONTENT, encoding="utf-8")
        code, _, err = self.refresh()
        self.assertEqual(code, 0, err)

    # ---- 夹具操作 ----

    def check(self) -> tuple[int, str, str]:
        return run(["--content", str(self.content), "--lock", str(self.lock)])

    def refresh(self) -> tuple[int, str, str]:
        return run(["--content", str(self.content), "--lock", str(self.lock), "--refresh"])

    def rewrite_content(self, *, old: str, new: str) -> None:
        text = self.content.read_text(encoding="utf-8")
        self.assertIn(old, text)
        self.content.write_text(text.replace(old, new), encoding="utf-8")

    # ---- 正常路径 ----

    def test_matched_pair_passes(self) -> None:
        code, out, err = self.check()
        self.assertEqual(code, 0, err)
        self.assertIn("2026-01-01", out)
        self.assertIn("5 条用户可见文案", out)

    def test_formatting_only_change_does_not_require_a_bump(self) -> None:
        """摘要与 TOML 排版、注释、键顺序无关：只调格式不该逼人递增版本。"""
        self.content.write_text(
            "# 一条新注释\n"
            '[cards."query.status"]\n'
            'button_labels = ["停止"]\n'
            'body   = "{status}"\n'
            'title = "正在查询"\n\n'
            "[texts]\n"
            '"worker.status" = "{action} · {elapsed_seconds} 秒"\n'
            '"gateway.new_session" = "已开启新会话，可以开始提问。"\n\n'
            "[meta]\n"
            'version = "2026-01-01"\n',
            encoding="utf-8",
        )
        code, _, err = self.check()
        self.assertEqual(code, 0, err)

    # ---- 否定测试：这些必须红 ----

    def test_text_change_without_version_bump_is_red(self) -> None:
        """本条就是 Issue #190 要挡的情况：改了用户可见文案却没动版本。"""
        self.rewrite_content(old="已开启新会话，可以开始提问。", new="新会话已开启，请提问。")
        code, _, err = self.check()
        self.assertEqual(code, 1)
        self.assertIn("texts.gateway.new_session", err)
        self.assertIn("[meta] version", err)
        self.assertIn("--refresh", err)

    def test_card_button_label_change_without_bump_is_red(self) -> None:
        self.rewrite_content(old='["停止"]', new='["停止执行"]')
        code, _, err = self.check()
        self.assertEqual(code, 1)
        self.assertIn("cards.query.status.button_labels[0]", err)

    def test_added_and_removed_keys_are_named(self) -> None:
        self.rewrite_content(
            old='"worker.status" = "{action} · {elapsed_seconds} 秒"',
            new='"worker.failed" = "本次任务未取得可用结果。"',
        )
        code, _, err = self.check()
        self.assertEqual(code, 1)
        self.assertIn("新增的键：texts.worker.failed", err)
        self.assertIn("删除的键：texts.worker.status", err)

    def test_version_bump_without_refreshing_the_lock_is_red(self) -> None:
        """递增了版本却忘了把登记放进同一个提交，同样必须红。"""
        self.rewrite_content(old='version = "2026-01-01"', new='version = "2026-01-02"')
        self.rewrite_content(old="已开启新会话，可以开始提问。", new="新会话已开启，请提问。")
        code, _, err = self.check()
        self.assertEqual(code, 1)
        self.assertIn("版本已递增但登记没跟上", err)

    def test_refresh_refuses_to_replace_the_bump(self) -> None:
        """刷新命令不得成为绕过递增的后门：版本没动时必须拒绝写入。"""
        self.rewrite_content(old="已开启新会话，可以开始提问。", new="新会话已开启，请提问。")
        before = self.lock.read_bytes()
        code, _, err = self.refresh()
        self.assertEqual(code, 1)
        self.assertIn("拒绝刷新", err)
        self.assertEqual(self.lock.read_bytes(), before, "被拒绝的刷新不得改动登记文件")

    def test_reusing_a_retired_version_is_red(self) -> None:
        """回退到用过的版本号会让同一个版本对应两批文案，必须挡住。"""
        self.rewrite_content(old='version = "2026-01-01"', new='version = "2026-01-02"')
        self.rewrite_content(old="已开启新会话，可以开始提问。", new="新会话已开启，请提问。")
        self.assertEqual(self.refresh()[0], 0)
        self.rewrite_content(old='version = "2026-01-02"', new='version = "2026-01-01"')
        code, _, err = self.check()
        self.assertEqual(code, 1)
        self.assertIn("不能复用", err)

    def test_unknown_top_level_table_fails_closed(self) -> None:
        """新增顶层表可能整批都是用户可见文案，检查不认识就必须红，而不是静默放行。"""
        self.content.write_text(
            BASE_CONTENT + '\n[banners]\n"maintenance" = "系统维护中"\n', encoding="utf-8"
        )
        code, _, err = self.check()
        self.assertEqual(code, 1)
        self.assertIn("未登记的顶层表", err)
        self.assertIn("banners", err)

    def test_missing_lock_file_is_red(self) -> None:
        self.lock.unlink()
        code, _, err = self.check()
        self.assertEqual(code, 1)
        self.assertIn("不存在", err)

    def test_hand_edited_lock_digest_is_red(self) -> None:
        """登记文件被手工改过也要红，否则 digest 与逐键摘要可以悄悄分家。"""
        lines = self.lock.read_text(encoding="utf-8").splitlines()
        replaced = ['digest = "sha256:' + "9" * 64 + '"' if line.startswith("digest = ") else line for line in lines]
        self.assertNotEqual(replaced, lines)
        self.lock.write_text("\n".join(replaced) + "\n", encoding="utf-8")
        code, _, err = self.check()
        self.assertEqual(code, 1)
        self.assertIn("手工编辑", err)

    # ---- 刷新路径 ----

    def test_refresh_after_bump_registers_and_turns_green(self) -> None:
        self.rewrite_content(old='version = "2026-01-01"', new='version = "2026-01-02"')
        self.rewrite_content(old="已开启新会话，可以开始提问。", new="新会话已开启，请提问。")
        code, out, err = self.refresh()
        self.assertEqual(code, 0, err)
        self.assertIn("2026-01-01 → 2026-01-02", out)
        self.assertIn('"2026-01-01",', self.lock.read_text(encoding="utf-8"))
        self.assertEqual(self.check()[0], 0)

    def test_refresh_is_idempotent(self) -> None:
        code, out, _ = self.refresh()
        self.assertEqual(code, 0)
        self.assertIn("无需刷新", out)


class RepositoryFilesTest(unittest.TestCase):
    """反过来跑真实文件，防止这份检查因为仓库结构变化而变成空转。"""

    def test_repository_content_and_lock_are_in_sync(self) -> None:
        code, out, err = run([])
        self.assertEqual(code, 0, err)
        self.assertIn("内容目录版本纪律：通过", out)


if __name__ == "__main__":
    unittest.main()
