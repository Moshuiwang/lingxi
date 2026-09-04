#!/usr/bin/env python3
"""把一个 Docker 镜像展开成**规范化内容清单**，用于判定两次构建是否等价（断言 V-部署-06）。

为什么不直接比 image ID 或 manifest digest：镜像配置里有 `created` 时间戳和带时间戳的
`history`，两者都随构建时刻变化，所以**同一份 rootfs 每次构建的 digest 必然不同**。
拿 digest 当等价判据会得到"永远不等价"这个无用结论；反过来，如果哪天它们真的相等了，
那也只是加分项，不能替代内容比对。

所以判据是**内容级**的：逐 layer 列出每个条目的
（路径 / 类型 / 权限位 / uid / gid / 大小 / 内容 sha256 / 链接目标），
排序后逐行比较。刻意排除两样：

  - **mtime**：`COPY` 会保留源文件 mtime，而 mtime 来自 checkout 时刻，两个 clone
    必然不同。它不影响程序行为。
  - **打包顺序**：tar 内条目顺序不保证稳定，排序即可消除。

需要处理的不确定性中最隐蔽的一类是 `.pyc`：pip 默认生成的字节码用时间戳失效模式，
文件头内嵌**源文件 mtime**，于是"内容"本身就带上了构建机的时间。Dockerfile 因此用
`--no-compile` + `compileall --invalidation-mode unchecked-hash`（PEP 552，改为内嵌
源码哈希）。本脚本不为此做任何特殊处理——那样只会把问题藏起来；它就该被如实地比出来。

用法：

    python3 scripts/ci/image_manifest.py lingxi-scheduler:probe > a.txt
    python3 scripts/ci/image_manifest.py lingxi-scheduler:other > b.txt
    diff -u a.txt b.txt
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import subprocess
import sys
import tarfile
import tempfile

# tar 条目类型 → 清单里的可读名字。用可读名而不是原始字节，是为了让 diff 输出
# 在出问题时能直接读懂，而不是再去查 tar 规范。
ENTRY_KINDS = {
    tarfile.REGTYPE: "file",
    tarfile.AREGTYPE: "file",
    tarfile.LNKTYPE: "hardlink",
    tarfile.SYMTYPE: "symlink",
    tarfile.DIRTYPE: "dir",
    tarfile.FIFOTYPE: "fifo",
    tarfile.CHRTYPE: "chardev",
    tarfile.BLKTYPE: "blockdev",
}


def save_image(reference: str, destination: pathlib.Path) -> None:
    """`docker save` 到本地目录。用 plain docker，不用 buildx——本机没有它。"""

    archive = destination / "image.tar"
    with archive.open("wb") as handle:
        subprocess.run(["docker", "save", reference], stdout=handle, check=True)
    with tarfile.open(archive) as tar:
        # data 过滤器：只解普通文件与目录，拒绝绝对路径与 `..`（Python 3.12 起可用）。
        # 解的是我们自己刚 save 出来的归档，仍然用它——解归档时放宽限制是个坏习惯，
        # 而这里放宽没有任何收益。
        tar.extractall(destination / "unpacked", filter="data")
    archive.unlink()


def layer_blobs(root: pathlib.Path) -> list[pathlib.Path]:
    """按顺序取出各 layer 的 tar。同时支持 docker 旧格式与 OCI layout。

    Docker 29 的 `docker save` 可能给出任一种：旧格式有顶层 manifest.json，
    OCI layout 是 index.json + blobs/sha256/。两种都要认，否则本检查会在某次
    Docker 升级后静默地找不到 layer——而"找不到 layer"若被当成"没有差异"，
    这个门禁就变成了永远绿的摆设。
    """

    manifest_path = root / "manifest.json"
    if manifest_path.is_file():
        entries = json.loads(manifest_path.read_text(encoding="utf-8"))
        layers = [root / name for name in entries[0]["Layers"]]
        if all(path.is_file() for path in layers):
            return layers

    index_path = root / "index.json"
    if index_path.is_file():
        index = json.loads(index_path.read_text(encoding="utf-8"))

        def blob(digest: str) -> pathlib.Path:
            algorithm, value = digest.split(":", 1)
            return root / "blobs" / algorithm / value

        manifest_digest = index["manifests"][0]["digest"]
        manifest = json.loads(blob(manifest_digest).read_text(encoding="utf-8"))
        # 多架构 manifest list：本仓库只构建单架构，取第一个即可，但要能认出来。
        if "manifests" in manifest:
            manifest = json.loads(
                blob(manifest["manifests"][0]["digest"]).read_text(encoding="utf-8")
            )
        return [blob(layer["digest"]) for layer in manifest["layers"]]

    raise SystemExit(f"{root}：既没有 manifest.json 也没有 index.json，认不出这个 save 格式")


def describe_layer(path: pathlib.Path, index: int) -> list[str]:
    lines: list[str] = []
    with tarfile.open(path) as tar:
        for member in tar:
            kind = ENTRY_KINDS.get(member.type, f"type-{member.type!r}")
            digest = "-"
            if member.isreg():
                extracted = tar.extractfile(member)
                if extracted is not None:
                    hasher = hashlib.sha256()
                    for chunk in iter(lambda: extracted.read(1024 * 1024), b""):
                        hasher.update(chunk)
                    digest = hasher.hexdigest()
            target = member.linkname or "-"
            lines.append(
                f"L{index:03d}\t{member.name}\t{kind}\t{member.mode:04o}\t"
                f"{member.uid}/{member.gid}\t{member.size}\t{digest}\t{target}"
            )
    # 排序消除打包顺序的影响；同一 layer 内容相同则清单逐字节相同。
    return sorted(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", help="镜像引用，例如 lingxi-scheduler:20260806-abcdef123456")
    parser.add_argument("--output", type=pathlib.Path, help="写入文件；省略则打到 stdout")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="lingxi-manifest-") as workspace:
        root = pathlib.Path(workspace)
        save_image(args.image, root)
        unpacked = root / "unpacked"
        lines: list[str] = []
        for index, blob in enumerate(layer_blobs(unpacked)):
            lines.extend(describe_layer(blob, index))

    header = [
        "# Lingxi 镜像规范化内容清单（断言 V-部署-06）",
        "# 列：layer\t路径\t类型\t权限\tuid/gid\t大小\t内容sha256\t链接目标",
        "# 刻意排除 mtime 与 tar 内打包顺序，理由见 scripts/ci/image_manifest.py 的模块说明。",
        f"# 条目数：{len(lines)}",
    ]
    text = "\n".join(header + lines) + "\n"
    if args.output:
        args.output.write_text(text, encoding="utf-8")
        print(f"已写出 {len(lines)} 条内容清单 → {args.output}")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
