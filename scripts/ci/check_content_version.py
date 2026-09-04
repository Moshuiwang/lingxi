#!/usr/bin/env python3
"""内容目录的版本纪律门禁（Issue #190）：用户可见文案变了，版本标识必须跟着变。

`src/lingxi/config/content.toml` 的 `[meta] version` 会随投递与审计事件落库，是
「用户当时看到的是哪一版文案」的唯一追溯依据。但它此前只是一句口头约定：改了文案
不改版本，没有任何东西会红（PR #182 就是这样过去的，审核同时确认「历史同样如此」）。
一个没人强制的版本号会逐渐变成看起来存在、实际不可信的信号，排查线上文案问题时
反而误导人。产品负责人 2026-08-17 就 Issue #190 选定方案 1：由会变红的程序门禁强制
「文案变更即递增版本」。

**怎么做到静态、确定性、不看 git 历史**：把 content.toml 里 `[texts]` 与 `[cards]`
下的每一个文本叶子摊平成 (键路径, 文案) 对，算出一份与 TOML 排版、注释、键顺序都
无关的规范化摘要，再与紧邻的 `content.lock.toml` 中登记的 (version, digest) 配对比对。
文案变了而版本没动，两者就对不上——红。它只读这两个文件，不比 base 分支、不连网络，
所以本机与 CI 得到同一个结论；同理，只调整排版或给 TOML 加注释不会误红。

**登记不会被静默刷新**：`--refresh` 在「文案变了但版本还是老值」时**拒绝写入**，
并要求先递增版本。因此这道门禁不可能被一条随手的刷新命令绕过去。

修复办法（门禁红了照着做）：
  1. 把 src/lingxi/config/content.toml 的 [meta] version 改成一个没用过的新标识
     （惯例是当天日期，形如 2026-08-17）；
  2. 运行 python3 scripts/ci/check_content_version.py --refresh 刷新登记；
  3. content.toml 与 content.lock.toml 必须进同一个提交。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tomllib
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CONTENT_PATH = REPOSITORY_ROOT / "src" / "lingxi" / "config" / "content.toml"
LOCK_PATH = REPOSITORY_ROOT / "src" / "lingxi" / "config" / "content.lock.toml"

# content.toml 顶层只允许这三张表，其中这两张放用户可见文案。多出一张表就失败关闭：
# 新表很可能同样是用户可见文案，而这份检查还不知道要不要把它算进摘要——宁可红一次
# 让人来更新检查，也不要让一整批文案静默地不受版本纪律约束。
CONTENT_TABLES = ("meta", "texts", "cards")
VISIBLE_TABLES = ("texts", "cards")
LOCK_FIELDS = ("version", "digest", "retired_versions", "keys")
# 逐键短摘要只用于在失败时指名道姓地说「是这几个键变了」，不承担防篡改职责；
# 整体 digest 才是门禁比对的封印。
KEY_DIGEST_LENGTH = 16

FIX_STEPS = (
    "  修复办法：",
    "    1. 把 content.toml 的 [meta] version 改成一个没用过的新标识（惯例是当天日期，形如 2026-08-17）；",
    "    2. 运行 python3 scripts/ci/check_content_version.py --refresh 刷新登记；",
    "    3. 把 content.toml 与 content.lock.toml 放进同一个提交。",
)

REFRESH_HINT = "  运行 python3 scripts/ci/check_content_version.py --refresh 刷新登记，并与 content.toml 放进同一个提交。"

LOCK_HEADER = (
    "# 用户可见文案的版本登记（Issue #190：文案变更必须递增版本）。",
    "# 由 scripts/ci/check_content_version.py 生成与校验，请不要手工编辑。",
    "# 改完 content.toml 的文案后：先递增 [meta] version，再运行",
    "#   python3 scripts/ci/check_content_version.py --refresh",
)


def flatten_visible(document: Any) -> tuple[list[tuple[str, str]], list[str]]:
    """把用户可见文案摊平成排序无关的 (键路径, 文案) 对。"""

    errors: list[str] = []
    pairs: list[tuple[str, str]] = []

    if not isinstance(document, dict):
        return [], ["content.toml 顶层必须是表"]

    unknown = sorted(set(document) - set(CONTENT_TABLES))
    if unknown:
        errors.append(
            "content.toml 出现未登记的顶层表："
            + "、".join(unknown)
            + "；若它同样包含用户可见文案，请先把它加进 "
            "scripts/ci/check_content_version.py 的 VISIBLE_TABLES 再刷新登记"
        )
    missing = [name for name in CONTENT_TABLES if name not in document]
    if missing:
        errors.append("content.toml 缺少顶层表：" + "、".join(missing))

    for table in VISIBLE_TABLES:
        if table in document:
            _walk(table, document[table], pairs, errors)

    if not pairs and not errors:
        errors.append("content.toml 里一条用户可见文案都没解析到：文件结构被改动了")
    return pairs, errors


def _walk(path: str, value: Any, pairs: list[tuple[str, str]], errors: list[str]) -> None:
    if isinstance(value, str):
        pairs.append((path, value))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _walk(f"{path}[{index}]", item, pairs, errors)
    elif isinstance(value, dict):
        for key in sorted(value):
            _walk(f"{path}.{key}", value[key], pairs, errors)
    else:
        # 数字、布尔、时间不可能是用户可见文案；出现即结构异常，失败关闭。
        errors.append(f"content.toml 的 {path} 不是文本，也不是文本的表或数组")


def content_digest(pairs: list[tuple[str, str]]) -> str:
    """与 TOML 排版无关的规范化摘要：只由键路径与文案内容决定。"""

    canonical = json.dumps(sorted(pairs), ensure_ascii=False, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def key_digests(pairs: list[tuple[str, str]]) -> dict[str, str]:
    return {
        path: hashlib.sha256(value.encode("utf-8")).hexdigest()[:KEY_DIGEST_LENGTH]
        for path, value in pairs
    }


def load_content(path: Path) -> tuple[dict[str, Any] | None, list[str]]:
    document, errors = _load_toml(path, "内容目录")
    if document is None:
        return None, errors
    meta = document.get("meta")
    version = meta.get("version") if isinstance(meta, dict) else None
    if not isinstance(version, str) or not version.strip():
        errors.append(f"{path} 的 [meta] version 必须是非空文本")
    return document, errors


def load_lock(path: Path) -> tuple[dict[str, Any] | None, list[str]]:
    lock, errors = _load_toml(path, "版本登记")
    if lock is None:
        return None, errors

    unexpected = sorted(set(lock) - set(LOCK_FIELDS))
    if unexpected:
        errors.append(f"{path} 出现未知字段：" + "、".join(unexpected))
    for field in LOCK_FIELDS:
        if field not in lock:
            errors.append(f"{path} 缺少字段 {field}")
    if errors:
        return None, errors

    if not isinstance(lock["version"], str) or not lock["version"].strip():
        errors.append(f"{path} 的 version 必须是非空文本")
    if not isinstance(lock["digest"], str) or not lock["digest"].startswith("sha256:"):
        errors.append(f"{path} 的 digest 必须是 sha256: 开头的文本")
    if not isinstance(lock["retired_versions"], list) or not all(
        isinstance(item, str) for item in lock["retired_versions"]
    ):
        errors.append(f"{path} 的 retired_versions 必须是文本数组")
    if not isinstance(lock["keys"], dict) or not all(
        isinstance(item, str) for item in lock["keys"].values()
    ):
        errors.append(f"{path} 的 [keys] 必须是「键路径 = 短摘要」的表")
    if errors:
        return None, errors
    return lock, errors


def _load_toml(path: Path, kind: str) -> tuple[dict[str, Any] | None, list[str]]:
    try:
        with path.open("rb") as stream:
            return tomllib.load(stream), []
    except FileNotFoundError:
        return None, [f"{kind}文件不存在：{path}"]
    except (OSError, tomllib.TOMLDecodeError) as error:
        return None, [f"{kind}文件无法解析：{path}（{error}）"]


def describe_changes(computed: dict[str, str], locked: dict[str, str]) -> list[str]:
    """指出到底哪几个键变了，让开发者不必自己去比对整份文件。"""

    lines: list[str] = []
    changed = sorted(key for key in computed.keys() & locked.keys() if computed[key] != locked[key])
    added = sorted(computed.keys() - locked.keys())
    removed = sorted(locked.keys() - computed.keys())
    if changed:
        lines.append("  文案已改动的键：" + "、".join(changed))
    if added:
        lines.append("  新增的键：" + "、".join(added))
    if removed:
        lines.append("  删除的键：" + "、".join(removed))
    if not lines:
        lines.append("  逐键摘要一致但整体摘要不符：登记文件疑似被手工编辑过")
    return lines


def evaluate(content: dict[str, Any], lock: dict[str, Any]) -> list[str]:
    """在两份文件都已解析成功之后，判断版本与文案是否配对。"""

    pairs, errors = flatten_visible(content)
    if errors:
        return errors

    version = content["meta"]["version"].strip()
    digest = content_digest(pairs)
    computed_keys = key_digests(pairs)
    same_version = version == lock["version"]
    same_content = digest == lock["digest"] and computed_keys == lock["keys"]

    failures: list[str] = []
    if version in lock["retired_versions"]:
        failures.append(
            f"content.toml 的版本 {version!r} 是已经登记过的旧标识，不能复用："
            "同一个版本号对应两批不同的文案，落库的追溯依据就失效了。请改用一个新标识。"
        )

    if same_content and same_version:
        return failures

    if same_version:
        # 版本没动而文案变了：Issue #190 要挡的正是这一种。
        failures.append(
            f"content.toml 的用户可见文案变了，但 [meta] version 还是 {version!r}——"
            "版本号已经不能代表用户实际看到的文案。"
        )
        failures.extend(describe_changes(computed_keys, lock["keys"]))
        failures.extend(FIX_STEPS)
        return failures

    failures.append(
        f"content.toml 的版本是 {version!r}，content.lock.toml 登记的是 "
        f"{lock['version']!r}：版本已递增但登记没跟上。"
    )
    if not same_content:
        failures.extend(describe_changes(computed_keys, lock["keys"]))
    failures.append(REFRESH_HINT)
    return failures


def render_lock(version: str, digest: str, retired: list[str], keys: dict[str, str]) -> str:
    lines = list(LOCK_HEADER)
    lines.append(f"version = {json.dumps(version, ensure_ascii=False)}")
    lines.append(f"digest = {json.dumps(digest, ensure_ascii=False)}")
    lines.append("")
    lines.append("# 用过的旧版本标识，不得复用：同一个版本号对应两批文案会让落库的追溯依据失效。")
    if retired:
        lines.append("retired_versions = [")
        lines.extend(f"  {json.dumps(item, ensure_ascii=False)}," for item in retired)
        lines.append("]")
    else:
        lines.append("retired_versions = []")
    lines.append("")
    lines.append("# 每条用户可见文案的短摘要，用于在门禁失败时指出具体是哪几个键变了。")
    lines.append("[keys]")
    lines.extend(
        f"{json.dumps(path, ensure_ascii=False)} = {json.dumps(keys[path], ensure_ascii=False)}"
        for path in sorted(keys)
    )
    return "\n".join(lines) + "\n"


def run_check(content_path: Path, lock_path: Path) -> int:
    content, errors = load_content(content_path)
    lock, lock_errors = load_lock(lock_path)
    errors = errors + lock_errors
    if content is not None and lock is not None and not errors:
        errors = evaluate(content, lock)

    if errors:
        print("内容目录版本纪律检查失败：", file=sys.stderr)
        for error in errors:
            print(error if error.startswith(" ") else f"- {error}", file=sys.stderr)
        return 1

    pairs, _ = flatten_visible(content)
    print(
        f"内容目录版本纪律：通过（版本 {content['meta']['version'].strip()}，"
        f"{len(pairs)} 条用户可见文案）"
    )
    return 0


def run_refresh(content_path: Path, lock_path: Path) -> int:
    """显式刷新登记。它**只登记已经递增过的版本**，绝不替开发者跳过递增这一步。"""

    content, errors = load_content(content_path)
    if errors or content is None:
        _print_refresh_failure(errors)
        return 1

    pairs, structure_errors = flatten_visible(content)
    if structure_errors:
        _print_refresh_failure(structure_errors)
        return 1

    version = content["meta"]["version"].strip()
    digest = content_digest(pairs)
    keys = key_digests(pairs)

    if lock_path.exists():
        lock, lock_errors = load_lock(lock_path)
        if lock_errors or lock is None:
            _print_refresh_failure(lock_errors)
            return 1
        retired = list(lock["retired_versions"])
        same_content = digest == lock["digest"] and keys == lock["keys"]
        if version == lock["version"]:
            if same_content:
                print(f"内容目录版本纪律：登记已是最新（版本 {version}），无需刷新")
                return 0
            _print_refresh_failure(
                [
                    f"拒绝刷新：content.toml 的文案变了，但 [meta] version 还是 {version!r}。"
                    "刷新登记不能代替递增版本，否则这道门禁等于不存在。",
                    *describe_changes(keys, lock["keys"]),
                    *FIX_STEPS,
                ]
            )
            return 1
        if version in retired:
            _print_refresh_failure(
                [f"拒绝刷新：版本 {version!r} 是已经登记过的旧标识，不能复用；请改用一个新标识。"]
            )
            return 1
        if lock["version"] not in retired:
            retired.append(lock["version"])
        previous = lock["version"]
    else:
        # 首次建立登记（一次性校准）。此时没有可信的历史摘要可比，退休清单留给人工补齐。
        retired = []
        previous = None

    lock_path.write_text(render_lock(version, digest, retired, keys), encoding="utf-8")
    moved = f"{previous} → {version}" if previous else version
    print(f"内容目录版本登记已刷新：{moved}（{len(pairs)} 条用户可见文案）")
    return 0


def _print_refresh_failure(errors: list[str]) -> None:
    print("内容目录版本登记刷新失败：", file=sys.stderr)
    for error in errors:
        print(error if error.startswith(" ") else f"- {error}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="校验 content.toml 的用户可见文案与版本登记是否配对（Issue #190）"
    )
    parser.add_argument("--content", type=Path, default=CONTENT_PATH, help="内容目录路径")
    parser.add_argument("--lock", type=Path, default=LOCK_PATH, help="版本登记路径")
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="在已经递增版本之后重写登记；文案变了而版本没动时会拒绝写入",
    )
    args = parser.parse_args(argv)
    if args.refresh:
        return run_refresh(args.content, args.lock)
    return run_check(args.content, args.lock)


if __name__ == "__main__":
    raise SystemExit(main())
