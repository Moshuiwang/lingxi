#!/usr/bin/env python3
"""裁决层 `Epic Verdict` 的判定：纯函数，工作流只负责取值与调用。

必需检查只按名字匹配，任何分支上的任何工作流都能产出一个同名作业，能区分来源的
只有 GitHub App 身份。裁决工作流 `.github/workflows/verdict.yml` 由 `workflow_run`
触发、一律运行默认分支上的那一份，以独立 App 身份把 `Epic Verdict` 写到触发 PR 的
头提交上；本模块决定它写什么，本身不联网、不读工作树。

规则按顺序，先命中先返回：

1. 触发运行不是 `pull_request` 事件、或不是 `.github/workflows/ci.yml` 产出的：不写检查。
2. 头提交来自外部仓库：写 failure，外部仓库不裁决。
3. 路径 a——同时满足：来源核验通过（头分支从未面向非受保护分支开过 PR，见
   `Taint`）、有面向受保护分支的 PR 当基线、头提交的 `.github/workflows` 子树与该
   基线相同：采信那次 `Epic Full` 的结论；只有 `success` 算通过，`skipped` /
   `neutral` / `cancelled` 一律判红。
4. 其余一律路径 b：由默认分支版 `ci.yml` 以头提交复跑一次，结论在复跑结束后由
   `finalize` 给出，同样只有 `success` 算通过。

基线只认 base 为默认分支或 `release/**` 的 PR（其余 base 一律忽略，见
`is_protected_base`）。`workflow_run.pull_requests[]` 为空时由工作流按头提交反查 PR，
两路同一规则；没有合格 PR 就以默认分支为基线做子树比对与差异清单，但不采信触发
运行（路径 b），也无法贴标签与评论。
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


def is_protected_base(base_ref: str, default_branch: str) -> bool:
    """基线只认受规则集保护的分支：默认分支或 `release/**`。

    `pull_requests[]` 与按头提交反查的结果都是不可信输入：同一头提交可以同时开一条
    PR 到攻击者自己的分支 x（x = 默认分支 + 放松的门禁），若拿 x 当基线，路径 a 就会
    采信 PR 自己那次运行。只有受保护分支上的定义才配当比较对象。
    """

    return base_ref == default_branch or base_ref.startswith("release/")


def select_baseline(facts: RunFacts) -> Baseline:
    """选基线：只在受保护 base 的 PR 里选，默认分支优先、其次头提交一致；没有就用默认分支。"""

    eligible = [
        pr for pr in facts.pull_requests if is_protected_base(pr.base_ref, facts.default_branch)
    ]

    def rank(pr: PullRequestRef) -> tuple[int, int]:
        return (
            0 if pr.base_ref == facts.default_branch else 1,
            0 if pr.head_sha == facts.head_sha else 1,
        )

    chosen = min(eligible, key=rank) if eligible else None
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
    """头提交与基线两侧 `.github/workflows` 子树的树对象 sha；取不到的一侧为 None。

    没有合格 PR 时基线是默认分支，那份子树只用于比对与差异清单，绝不据此采信触发
    运行：头提交的定义与默认分支相同，不等于触发它的那次运行用的是这份定义。
    """

    head: str | None
    base: str | None


TAINT_QUERY_OK = "ok"


@dataclass(frozen=True)
class Taint:
    """触发运行的来源核验：头分支是否曾面向非受保护分支开过 PR（含已关闭）。

    触发运行只带 `head_branch` / `head_sha`，不带「是哪条 PR 触发的」。攻击者可以保持
    头提交的定义与主干相同，另建可写分支 x（`ci.yml` 改成面向 x 触发、恒成功、名字仍叫
    Epic Full），开一条辅助 PR 到 x：那次运行 event / path / 仓库都合格，事后关掉辅助 PR，
    按头提交反查（只回 open + merged）就看不见它了。所以按头分支查 `state=all` 的全部
    PR，只要有一条 base 非受保护，这个头分支的所有运行都不得走路径 a；查询失败同样
    视为受染——查不到不等于没有。
    """

    status: str
    reasons: tuple[str, ...] = ()

    @property
    def tainted(self) -> bool:
        return self.status != TAINT_QUERY_OK or bool(self.reasons)

    @property
    def summary(self) -> str:
        if self.status != TAINT_QUERY_OK:
            return f"来源核验失败（{self.status or '未知'}），不采信触发运行"
        if self.reasons:
            return (
                "头分支曾面向非受保护分支开 PR（" + "、".join(self.reasons) + "），不采信触发运行"
            )
        return "来源核验通过"


def assess_taint(
    head_branch_pull_requests: Iterable[Mapping], default_branch: str, status: str
) -> Taint:
    """按头分支的全部 PR（`state=all`）判定受染；`status` 非 ok 直接受染。"""

    if status != TAINT_QUERY_OK:
        return Taint(status=status or "unknown")
    reasons = []
    for item in head_branch_pull_requests:
        base_ref = str((item.get("base") or {}).get("ref") or "")
        if not is_protected_base(base_ref, default_branch):
            state = str(item.get("state") or "?")
            reasons.append(f"#{item.get('number', '?')}→{base_ref or '?'}（{state}）")
    return Taint(status=TAINT_QUERY_OK, reasons=tuple(reasons))


def definitions_differ(trees: WorkflowTrees) -> bool:
    """头提交的门禁定义是否与基线不同；任一侧缺失也算不同（标签与差异评论据此触发）。"""

    return not (trees.head and trees.head == trees.base)


def decide(facts: RunFacts, baseline: Baseline, trees: WorkflowTrees, taint: Taint) -> Verdict:
    """给出初步裁决：跳过 / 拒绝 / 路径 a（有结论）/ 路径 b（等复跑）。

    路径 a 三个条件缺一不可：来源核验通过、有面向受保护分支的 PR 当基线、头提交的
    `.github/workflows` 子树与该基线相同。任何一条不成立都复跑，绝不采信触发运行。
    """

    early = precheck(facts)
    if early is not None:
        return early
    run_label = f"run {facts.run_id} 结论 {facts.conclusion or '无'}"
    tree_label = (
        f"{WORKFLOWS_DIRECTORY} 子树：头提交 {trees.head or '缺失'}，"
        f"基线 {baseline.label} {trees.base or '缺失'}"
    )
    if taint.tainted:
        why = taint.summary
    elif baseline.pull_request is None:
        why = f"没有面向受保护分支的 PR（基线暂取默认分支 {baseline.label}）"
    elif not definitions_differ(trees):
        return Verdict(
            path=PATH_A,
            write_check=True,
            conclusion=map_run_conclusion(facts.conclusion),
            summary=(
                f"路径 a（门禁定义与基线 {baseline.label} 相同，{taint.summary}）："
                f"采信 {run_label}；{tree_label}"
            ),
        )
    else:
        why = f"门禁定义与基线 {baseline.label} 不同"
    return Verdict(
        path=PATH_B,
        write_check=True,
        conclusion=None,
        summary=f"路径 b（{why}）：不采信 {run_label}，由默认分支版 ci.yml 复跑头提交；{tree_label}",
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
    trees = WorkflowTrees(head=args.head_tree or None, base=args.base_tree or None)
    head_branch_pulls: list = []
    if args.taint_query_status == TAINT_QUERY_OK and args.head_branch_pulls_file:
        head_branch_pulls = _load_json(args.head_branch_pulls_file)
    taint = assess_taint(head_branch_pulls, facts.default_branch, args.taint_query_status)
    verdict = decide(facts, baseline, trees, taint)
    definitions_changed = definitions_differ(trees)
    diff: list[str] = []
    listings = (args.base_listing_file, args.head_listing_file)
    if definitions_changed and all(path and Path(path).is_file() for path in listings):
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
            "definitions_changed": "true" if definitions_changed else "false",
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
    decision.add_argument("--head-branch-pulls-file")
    decision.add_argument(
        "--taint-query-status",
        default="",
        help="按头分支查 state=all 全部 PR 的结果：ok 或失败原因；非 ok 一律路径 b",
    )
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
