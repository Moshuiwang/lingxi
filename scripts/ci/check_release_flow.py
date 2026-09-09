#!/usr/bin/env python3
"""检查发布出口、正式提升和验收记录保护没有从工作流中脱落。"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def check(root: Path = ROOT) -> list[str]:
    failures = []
    publish = (root / ".github/workflows/publish.yml").read_text()
    promotion = (root / ".github/workflows/release.yml").read_text()
    ci = (root / ".github/workflows/ci.yml").read_text()
    owners = (root / ".github/CODEOWNERS").read_text()

    def code(text):
        return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))

    publish, promotion, ci, owners = map(code, (publish, promotion, ci, owners))
    on_block = publish.split("permissions:", 1)[0]
    if (
        "      - 'release/**'" not in on_block
        or "      - main" in on_block
        or "workflow_dispatch:" in on_block
    ):
        failures.append("候选打包只能由 release/** push 触发")
    for marker in (
        '--base-ref "${GITHUB_REF_NAME}"',
        "release_manifest.py prepare",
        "release_manifest.py candidate",
        "deploy/control_bundle.py --root",
        "--control-bundle",
        "--control-metadata",
    ):
        if marker not in publish:
            failures.append("候选发布缺少 " + marker)
    for marker in ("      - 'release/**'", '--base-ref "${{ github.base_ref }}"'):
        if marker not in ci:
            failures.append("完整检查没有绑定维护分支：" + marker)
    guard = "if: github.event_name == 'workflow_dispatch' && github.ref == 'refs/heads/main' && github.ref_protected"
    if promotion.count(guard) != 2 or "needs: [candidate]" not in promotion:
        failures.append("正式提升必须经过受保护 main 的验收检查")
    if "release_manifest.py promote" not in promotion or "--apply" not in promotion:
        failures.append("正式提升没有调用同一制品核验入口")
    if any(
        x in promotion
        for x in ("build_image.sh", "docker build", "packages: write", "control_bundle.py")
    ):
        failures.append("正式提升不得重新构建或写镜像")
    if "/deploy/releases/acceptance/ @Moshuiwang" not in owners:
        failures.append("正式验收记录缺少代码所有者保护")
    # 维护分支间不能靠同一份候选证明混用；测试直接验证读取者的拒绝分支。
    if "release_manifest.py" not in (root / "deploy/生产部署runbook.md").read_text():
        failures.append("生产操作说明未接入版本选择检查")
    if re.search(r"^\s*pull_request_target:", promotion, re.M):
        failures.append("正式提升不得由外部 PR 触发")
    return failures


if __name__ == "__main__":
    errors = check()
    if errors:
        raise SystemExit("\n".join(errors))
    print("发布分支与正式提升检查：通过")
