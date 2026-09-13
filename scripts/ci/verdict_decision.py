#!/usr/bin/env python3
"""裁决层 `Epic Verdict` 的判定：纯函数，工作流只负责取值与调用。

必需检查只按名字匹配，任何分支上的任何工作流都能产出一个同名作业，能区分来源的
只有 GitHub App 身份。裁决工作流 `.github/workflows/verdict.yml` 由 `workflow_run`
触发、一律运行默认分支上的那一份，以独立 App 身份把 `Epic Verdict` 写到触发 PR 的
头提交上；本模块决定它写什么，本身不联网、不读工作树。

规则按顺序，先命中先返回：

1. 触发运行不是 `pull_request` 事件、或不是 `.github/workflows/ci.yml` 产出的：不写检查。
2. 头提交来自外部仓库：写 failure，外部仓库不裁决。
3. 头提交的 `.github/workflows` 子树与基线分支相同、或与默认分支当前提交相同
   （路径 a）：采信那次 `Epic Full` 的结论；只有 `success` 算通过，`skipped` /
   `neutral` / `cancelled` 一律判红。与默认分支相同也算，是因为路径 b 复跑用的正是
   默认分支的定义，头提交已经是这份定义时，PR 自己那次运行与复跑等价。
4. 子树不同（路径 b，PR 改了门禁定义）：由默认分支版 `ci.yml` 复跑一次，结论在
   复跑结束后由 `finalize` 给出，同样只有 `success` 算通过。

`workflow_run.pull_requests[]` 为空时（例如事件送达时 PR 尚未关联）由工作流按头提交
反查 PR；仍找不到就以默认分支为基线——比较的对象仍是受保护分支上的定义，只是
无法贴标签与评论。
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

CHECK_NAME = "Epic Verdict"
TRIGGER_WORKFLOW_PATH = ".github/workflows/ci.yml"
WORKFLOWS_DIRECTORY = ".github/workflows"
CHANGED_GATE_LABEL = "门禁定义已变更"

PATH_A = "a"
PATH_B = "b"
PATH_REJECT = "拒绝"
PATH_SKIP = "跳过"

SUCCESS = "success"
FAILURE = "failure"


@dataclass(frozen=True)
class PullRequestRef:
    """事件里一条 PR 关联：只保留裁决要用的四个字段。"""

    number: int
    base_ref: str
    base_sha: str
    head_sha: str


@dataclass(frozen=True)
class RunFacts:
    """触发运行（`workflow_run`）的事实，全部来自事件载荷。"""

    repository: str
    default_branch: str
    event: str
    workflow_path: str
    head_repository: str | None
    head_sha: str
    head_branch: str | None
    conclusion: str | None
    run_id: int | None
    run_url: str
    pull_requests: tuple[PullRequestRef, ...]


@dataclass(frozen=True)
class Baseline:
    """裁决用的基线：来自哪条 PR、比较哪个提交。"""

    pull_request: PullRequestRef | None
    base_ref: str
    base_sha: str

    @property
    def label(self) -> str:
        return f"{self.base_ref}@{self.base_sha[:12]}" if self.base_sha else self.base_ref


@dataclass(frozen=True)
class Verdict:
    """一次裁决的结果；`conclusion` 为 None 表示要等复跑（路径 b）。"""

    path: str
    write_check: bool
    conclusion: str | None
    summary: str


def parse_run_facts(event: Mapping, fallback_pull_requests: Sequence[Mapping] = ()) -> RunFacts:
    """把 `workflow_run` 事件载荷拆成裁决需要的事实。

    `pull_requests[]` 为空时改用 `fallback_pull_requests`（工作流按头提交反查得到，
    形状与事件里的一致：`number` / `base.ref` / `base.sha` / `head.sha`）。
    """

    run = event.get("workflow_run") or {}
    repository = (event.get("repository") or {}).get("full_name") or ""
    head_repository = (run.get("head_repository") or {}).get("full_name")
    raw_pull_requests = run.get("pull_requests") or []
    if not raw_pull_requests:
        raw_pull_requests = list(fallback_pull_requests)
    pull_requests = tuple(
        PullRequestRef(
            number=int(item["number"]),
            base_ref=str((item.get("base") or {}).get("ref") or ""),
            base_sha=str((item.get("base") or {}).get("sha") or ""),
            head_sha=str((item.get("head") or {}).get("sha") or ""),
        )
        for item in raw_pull_requests
    )
    return RunFacts(
        repository=repository,
        default_branch=str((event.get("repository") or {}).get("default_branch") or "main"),
        event=str(run.get("event") or ""),
        workflow_path=str(run.get("path") or ""),
        head_repository=head_repository,
        head_sha=str(run.get("head_sha") or ""),
        head_branch=run.get("head_branch"),
        conclusion=run.get("conclusion"),
        run_id=run.get("id"),
        run_url=str(run.get("html_url") or ""),
        pull_requests=pull_requests,
    )


def select_baseline(facts: RunFacts) -> Baseline:
    """选基线：优先头提交一致的 PR，其次第一条 PR，都没有就用默认分支。"""

    chosen = next((pr for pr in facts.pull_requests if pr.head_sha == facts.head_sha), None)
    if chosen is None and facts.pull_requests:
        chosen = facts.pull_requests[0]
    if chosen is None:
        return Baseline(pull_request=None, base_ref=facts.default_branch, base_sha="")
    return Baseline(pull_request=chosen, base_ref=chosen.base_ref, base_sha=chosen.base_sha)


def precheck(facts: RunFacts) -> Verdict | None:
    """规则 1 与规则 2：不需要子树信息就能定的两种结果；都不命中返回 None。"""

    if facts.event != "pull_request" or facts.workflow_path != TRIGGER_WORKFLOW_PATH:
        return Verdict(
            path=PATH_SKIP,
            write_check=False,
            conclusion=None,
            summary=(
                f"不裁决：触发运行 event={facts.event or '?'}、path={facts.workflow_path or '?'}，"
                f"只裁决 pull_request 事件下 {TRIGGER_WORKFLOW_PATH} 的运行"
            ),
        )
    if not facts.head_repository or facts.head_repository != facts.repository:
        return Verdict(
            path=PATH_REJECT,
            write_check=True,
            conclusion=FAILURE,
            summary=(
                f"外部仓库不裁决：头提交来自 {facts.head_repository or '未知仓库'}，"
                f"本仓库是 {facts.repository}"
            ),
        )
    return None


def map_run_conclusion(conclusion: str | None) -> str:
    """只有 `success` 算通过；`skipped` / `neutral` 在 GitHub 语义里算通过，这里不算。"""

    return SUCCESS if conclusion == SUCCESS else FAILURE


@dataclass(frozen=True)
class WorkflowTrees:
    """三侧 `.github/workflows` 子树的树对象 sha；取不到的一侧为 None。"""

    head: str | None
    base: str | None
    default_branch: str | None = None


def decide(facts: RunFacts, baseline: Baseline, trees: WorkflowTrees) -> Verdict:
    """给出初步裁决：跳过 / 拒绝 / 路径 a（有结论）/ 路径 b（等复跑）。"""

    early = precheck(facts)
    if early is not None:
        return early
    run_label = f"run {facts.run_id} 结论 {facts.conclusion or '无'}"
    tree_label = (
        f"{WORKFLOWS_DIRECTORY} 子树：头提交 {trees.head or '缺失'}，"
        f"基线 {baseline.label} {trees.base or '缺失'}，"
        f"默认分支当前 {trees.default_branch or '缺失'}"
    )
    if trees.head and trees.head == trees.base:
        matched = f"与基线 {baseline.label} 相同"
    elif trees.head and trees.head == trees.default_branch:
        matched = "与默认分支当前提交相同"
    else:
        matched = ""
    if matched:
        return Verdict(
            path=PATH_A,
            write_check=True,
            conclusion=map_run_conclusion(facts.conclusion),
            summary=f"路径 a（门禁定义{matched}）：采信 {run_label}；{tree_label}",
        )
    return Verdict(
        path=PATH_B,
        write_check=True,
        conclusion=None,
        summary=(
            f"路径 b（门禁定义与基线、默认分支都不同）：不采信 {run_label}，"
            f"由默认分支版 ci.yml 复跑头提交；{tree_label}"
        ),
    )


def finalize(path: str, conclusion: str | None, rerun_result: str | None, summary: str) -> Verdict:
    """复跑结束后定终局结论：路径 b 只认复跑 `success`，其余路径沿用初步结论。"""

    if path == PATH_B:
        final = SUCCESS if rerun_result == SUCCESS else FAILURE
        return Verdict(
            path=path,
            write_check=True,
            conclusion=final,
            summary=f"{summary}；复跑结果 {rerun_result or '无'} → {final}",
        )
    if path in (PATH_A, PATH_REJECT) and conclusion in (SUCCESS, FAILURE):
        return Verdict(path=path, write_check=True, conclusion=conclusion, summary=summary)
    raise ValueError(f"无法定终局结论：path={path!r} conclusion={conclusion!r}")


def workflow_tree_diff(
    base_entries: Iterable[Mapping], head_entries: Iterable[Mapping]
) -> list[str]:
    """两份 `.github/workflows` 子树清单的差异（新增 / 删除 / 修改），按路径排序。"""

    def index(entries: Iterable[Mapping]) -> dict[str, str]:
        return {
            str(entry["path"]): str(entry["sha"])
            for entry in entries
            if entry.get("type") != "tree"
        }

    base, head = index(base_entries), index(head_entries)
    lines: list[str] = []
    for path in sorted(set(base) | set(head)):
        if path not in base:
            lines.append(f"新增 `{path}`")
        elif path not in head:
            lines.append(f"删除 `{path}`")
        elif base[path] != head[path]:
            lines.append(f"修改 `{path}`")
    return lines


def existing_check_run_id(
    check_runs: Iterable[Mapping], app_slug: str, name: str = CHECK_NAME
) -> int | None:
    """同一提交上本 App 已写过的同名 check run 编号（取最新一个）；没有返回 None。"""

    ids = [
        int(run["id"])
        for run in check_runs
        if run.get("name") == name and ((run.get("app") or {}).get("slug") == app_slug)
    ]
    return max(ids) if ids else None


@dataclass(frozen=True)
class CheckRunSpec:
    """写 check run 所需的字段。"""

    head_sha: str
    conclusion: str
    details_url: str
    external_id: str
    summary: str
    name: str = CHECK_NAME


def check_run_payload(spec: CheckRunSpec, *, update: bool) -> dict:
    """创建（POST）或更新（PATCH）check run 的请求体；更新时不带 `head_sha`。"""

    if spec.conclusion not in (SUCCESS, FAILURE):
        raise ValueError(f"check run 结论只能是 {SUCCESS} / {FAILURE}，不是 {spec.conclusion!r}")
    if not update and not spec.head_sha:
        raise ValueError("创建 check run 必须给出 head_sha")
    payload = {
        "name": spec.name,
        "status": "completed",
        "conclusion": spec.conclusion,
        "details_url": spec.details_url,
        "external_id": spec.external_id,
        "output": {
            "title": f"{spec.name}：{spec.conclusion}",
            "summary": spec.summary,
        },
    }
    if not update:
        payload["head_sha"] = spec.head_sha
    return payload


def write_github_output(path: str | None, values: Mapping[str, object]) -> None:
    """按 heredoc 形式写 `GITHUB_OUTPUT`，值里有换行也安全；没给路径就打印到标准输出。"""

    lines = []
    for key, value in values.items():
        text = "" if value is None else str(value)
        delimiter = f"EOF_{uuid.uuid4().hex}"
        lines.append(f"{key}<<{delimiter}\n{text}\n{delimiter}\n")
    rendered = "".join(lines)
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(rendered)
    else:
        sys.stdout.write(rendered)


def _load_json(path: str) -> object:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _command_plan(args: argparse.Namespace) -> None:
    fallback = _load_json(args.fallback_pulls_file) if args.fallback_pulls_file else []
    facts = parse_run_facts(_load_json(args.event_file), fallback)
    baseline = select_baseline(facts)
    early = precheck(facts)
    write_github_output(
        args.github_output,
        {
            "needs_trees": "false" if early is not None else "true",
            "head_sha": facts.head_sha,
            "pr_number": baseline.pull_request.number if baseline.pull_request else "",
            "base_ref": baseline.base_ref,
            "base_sha": baseline.base_sha,
            "base_label": baseline.label,
        },
    )


def _command_decide(args: argparse.Namespace) -> None:
    fallback = _load_json(args.fallback_pulls_file) if args.fallback_pulls_file else []
    facts = parse_run_facts(_load_json(args.event_file), fallback)
    baseline = select_baseline(facts)
    trees = WorkflowTrees(
        head=args.head_tree or None,
        base=args.base_tree or None,
        default_branch=args.default_tree or None,
    )
    verdict = decide(facts, baseline, trees)
    diff: list[str] = []
    listings = (args.base_listing_file, args.head_listing_file)
    if verdict.path == PATH_B and all(path and Path(path).is_file() for path in listings):
        diff = workflow_tree_diff(
            _load_json(args.base_listing_file), _load_json(args.head_listing_file)
        )
    write_github_output(
        args.github_output,
        {
            "path": verdict.path,
            "write_check": "true" if verdict.write_check else "false",
            "conclusion": verdict.conclusion or "",
            "summary": verdict.summary,
            "head_sha": facts.head_sha,
            "pr_number": baseline.pull_request.number if baseline.pull_request else "",
            "base_label": baseline.label,
            "run_id": facts.run_id or "",
            "run_url": facts.run_url,
            "diff": json.dumps(diff, ensure_ascii=False),
        },
    )
    print(
        f"裁决：path={verdict.path} conclusion={verdict.conclusion or '待复跑'}；{verdict.summary}"
    )


def _command_finalize(args: argparse.Namespace) -> None:
    verdict = finalize(args.path, args.conclusion or None, args.rerun_result or None, args.summary)
    write_github_output(
        args.github_output, {"conclusion": verdict.conclusion, "summary": verdict.summary}
    )
    print(f"终局：{verdict.conclusion}；{verdict.summary}")


def _command_check_run(args: argparse.Namespace) -> None:
    if args.action == "existing":
        listing = json.load(sys.stdin)
        found = existing_check_run_id(listing.get("check_runs") or [], args.app_slug)
        print(found if found is not None else "")
        return
    spec = CheckRunSpec(
        head_sha=args.head_sha,
        conclusion=args.conclusion,
        details_url=args.details_url,
        external_id=args.external_id,
        summary=args.summary,
    )
    print(json.dumps(check_run_payload(spec, update=args.update), ensure_ascii=False))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)

    plan = commands.add_parser("plan", help="选基线：输出要比较的 PR 与基线提交")
    plan.add_argument("--event-file", required=True)
    plan.add_argument("--fallback-pulls-file")
    plan.add_argument("--github-output")
    plan.set_defaults(func=_command_plan)

    decision = commands.add_parser("decide", help="给出初步裁决")
    decision.add_argument("--event-file", required=True)
    decision.add_argument("--fallback-pulls-file")
    decision.add_argument("--head-tree", default="")
    decision.add_argument("--base-tree", default="")
    decision.add_argument("--default-tree", default="")
    decision.add_argument("--head-listing-file")
    decision.add_argument("--base-listing-file")
    decision.add_argument("--github-output")
    decision.set_defaults(func=_command_decide)

    final = commands.add_parser("finalize", help="复跑结束后定终局结论")
    final.add_argument("--path", required=True)
    final.add_argument("--conclusion", default="")
    final.add_argument("--rerun-result", default="")
    final.add_argument("--summary", default="")
    final.add_argument("--github-output")
    final.set_defaults(func=_command_finalize)

    check_run = commands.add_parser("check-run", help="check run 的查找与请求体")
    check_run.add_argument("action", choices=["existing", "payload"])
    check_run.add_argument("--app-slug", default="")
    check_run.add_argument("--head-sha", default="")
    check_run.add_argument("--conclusion", default="")
    check_run.add_argument("--details-url", default="")
    check_run.add_argument("--external-id", default="")
    check_run.add_argument("--summary", default="")
    check_run.add_argument("--update", action="store_true")
    check_run.set_defaults(func=_command_check_run)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
