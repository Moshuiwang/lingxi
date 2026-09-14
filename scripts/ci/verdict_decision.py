#!/usr/bin/env python3
"""裁决层 `Epic Verdict` 的判定：纯函数，工作流只负责取值与调用。

必需检查只按名字匹配，任何分支上的任何工作流都能产出一个同名作业，能区分来源的
只有 GitHub App 身份。裁决工作流 `.github/workflows/verdict.yml` 由 `workflow_run`
触发、一律运行默认分支上的那一份，以独立 App 身份把 `Epic Verdict` 写到触发 PR 的
头提交上；本模块决定它写什么，本身不联网、不读工作树。

规则按顺序，先命中先返回：

1. 触发运行不是 `pull_request` 事件、或不是 `.github/workflows/ci.yml` 产出的：不写检查。
2. 头提交来自外部仓库：写 failure，外部仓库不裁决。
3. 路径 a——同时满足：触发运行留下的 OIDC 身份凭证验签通过且声明合格（谁签、签给
   哪次运行、面向默认分支，见 `check_token`）、头提交的 `.github/workflows`
   子树与凭证声明的 base 分支（运行时被合并进去的那个提交）相同：采信那次 `Epic Full`
   的结论；只有 `success` 算通过，`skipped` / `neutral` / `cancelled` 一律判红。
4. 其余一律路径 b：由默认分支版 `ci.yml` 以头提交复跑一次，结论在复跑结束后由
   `finalize` 给出，同样只有 `success` 算通过。

事件里的 `pull_requests[]` 与按头提交反查的结果只用于贴标签与差异清单（见
`select_baseline`），不再作为采信依据：触发运行属于哪条 PR，只信 GitHub 签发的凭证。
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import sys
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

CHECK_NAME = "Epic Verdict"
TRIGGER_WORKFLOW_PATH = ".github/workflows/ci.yml"
WORKFLOWS_DIRECTORY = ".github/workflows"
CHANGED_GATE_LABEL = "门禁定义已变更"

# OIDC 身份凭证：只签给本仓的裁决层这一个受众；签发方是 GitHub Actions 的 OIDC 提供者。
TOKEN_AUDIENCE = "lingxi-epic-verdict"
TOKEN_ISSUER = "https://token.actions.githubusercontent.com"
TOKEN_JWKS_URL = f"{TOKEN_ISSUER}/.well-known/jwks"
TOKEN_ARTIFACT_PREFIX = "epic-verdict-token-"
TOKEN_FILE_NAME = "token.jwt"

PATH_A = "a"
PATH_B = "b"
PATH_REJECT = "拒绝"
PATH_SKIP = "跳过"

SUCCESS = "success"
FAILURE = "failure"

TOKEN_OK = "ok"
TOKEN_MISSING = "missing"
TOKEN_INVALID = "invalid"


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
        if re.fullmatch(r"[0-9a-f]{40}", self.base_sha or ""):
            return f"{self.base_ref}@{self.base_sha[:12]}"
        return self.base_ref


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
    """受规则集保护的分支：默认分支或 `release/**`；只用于贴标签与差异清单，不是信任判断。"""

    return base_ref == default_branch or base_ref.startswith("release/")


def is_trusted_base(base_ref: str, default_branch: str) -> bool:
    """路径 a 只信与默认分支相等的已规范化分支名。"""

    return base_ref == default_branch


def select_baseline(facts: RunFacts) -> Baseline:
    """凭证不可用时的备用基线：只在受保护 base 的 PR 里选，默认分支优先；没有就用默认分支。

    这条基线只用于贴标签与差异清单，不决定采不采信触发运行——事件里的 PR 关联与
    反查结果都是攻击者可以影响的输入（同一头提交可以另开 PR 到自己的分支）。
    """

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
    """规则 1 与规则 2：不需要凭证与子树就能定的两种结果；都不命中返回 None。"""

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


# ---------------------------------------------------------------------------
# OIDC 身份凭证：验签（标准库 + cryptography）与声明核对
# ---------------------------------------------------------------------------


class TokenError(ValueError):
    """凭证不可用：格式、签名或声明不合格。"""


def _b64url_decode(segment: str) -> bytes:
    padded = segment + "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def parse_jwt(token: str) -> tuple[dict, dict, bytes, bytes]:
    """拆 JWT：返回 (头部, 声明, 待签名字节, 签名字节)。"""

    parts = token.strip().split(".")
    if len(parts) != 3 or not all(parts):
        raise TokenError("凭证不是三段式 JWT")
    try:
        header = json.loads(_b64url_decode(parts[0]))
        claims = json.loads(_b64url_decode(parts[1]))
        signature = _b64url_decode(parts[2])
    except (ValueError, UnicodeDecodeError) as error:
        raise TokenError(f"凭证段落无法解码：{error}") from error
    if not isinstance(header, dict) or not isinstance(claims, dict):
        raise TokenError("凭证头部或声明不是对象")
    signing_input = f"{parts[0]}.{parts[1]}".encode("ascii")
    return header, claims, signing_input, signature


def _int_from_b64url(value: str) -> int:
    return int.from_bytes(_b64url_decode(value), "big")


def verify_signature(token: str, jwks: Mapping) -> dict:
    """用 JWKS 验 RS256 签名，通过则返回声明；只认 `alg=RS256` 与 JWKS 里同 `kid` 的 RSA 键。"""

    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    header, claims, signing_input, signature = parse_jwt(token)
    if header.get("alg") != "RS256":
        raise TokenError(f"凭证算法不是 RS256：{header.get('alg')!r}")
    kid = header.get("kid")
    candidates = [
        key
        for key in (jwks.get("keys") or [])
        if isinstance(key, Mapping) and key.get("kty") == "RSA" and key.get("kid") == kid
    ]
    if not kid or not candidates:
        raise TokenError(f"JWKS 里没有凭证头部的 kid={kid!r}")
    jwk = candidates[0]
    try:
        public_key = rsa.RSAPublicNumbers(
            _int_from_b64url(str(jwk["e"])), _int_from_b64url(str(jwk["n"]))
        ).public_key()
    except (KeyError, ValueError) as error:
        raise TokenError(f"JWKS 键无法构造 RSA 公钥：{error}") from error
    try:
        public_key.verify(signature, signing_input, padding.PKCS1v15(), hashes.SHA256())
    except InvalidSignature as error:
        raise TokenError("凭证签名不符") from error
    return claims


@dataclass(frozen=True)
class ExpectedClaims:
    """裁决层对凭证声明的要求。"""

    repository: str
    run_id: str
    head_sha: str
    default_branch: str
    audience: str = TOKEN_AUDIENCE
    issuer: str = TOKEN_ISSUER
    workflow_path: str = TRIGGER_WORKFLOW_PATH


def normalize_branch(ref: str) -> str:
    """`refs/heads/main` 与 `main` 视为同一条分支。"""

    return ref[len("refs/heads/") :] if ref.startswith("refs/heads/") else ref


def pull_request_number_from_ref(ref: str) -> int | None:
    """`refs/pull/123/merge` → 123；其他形状返回 None。"""

    match = re.fullmatch(r"refs/pull/(\d+)/merge", ref or "")
    return int(match.group(1)) if match else None


def claim_problems(
    claims: Mapping, expected: ExpectedClaims, merge_parents: Sequence[str] | None
) -> list[str]:
    """逐条核对声明，返回全部不符项（空列表即合格）。

    `exp` 刻意不核：凭证只当签名记录用，重放已由 `run_id` 绑定挡住。`sha` 在
    `pull_request` 事件下是临时合并提交，要么直接等于头提交，要么其父提交里含头提交
    （`merge_parents` 由工作流按 `sha` 取回；取不到视为不符）。
    """

    problems: list[str] = []
    audience = claims.get("aud")
    audiences = audience if isinstance(audience, list) else [audience]
    if expected.audience not in audiences:
        problems.append(f"aud={audience!r}")
    if claims.get("iss") != expected.issuer:
        problems.append(f"iss={claims.get('iss')!r}")
    if claims.get("repository") != expected.repository:
        problems.append(f"repository={claims.get('repository')!r}")
    if str(claims.get("run_id")) != str(expected.run_id):
        problems.append(f"run_id={claims.get('run_id')!r}")
    if claims.get("event_name") != "pull_request":
        problems.append(f"event_name={claims.get('event_name')!r}")
    base_ref = normalize_branch(str(claims.get("base_ref") or ""))
    if not is_trusted_base(base_ref, expected.default_branch):
        problems.append(
            f"base_ref={claims.get('base_ref')!r} 不是默认分支（进 release/** 的 PR 一律复跑）"
        )
    workflow_ref = str(claims.get("workflow_ref") or "")
    if not workflow_ref.startswith(f"{expected.repository}/{expected.workflow_path}@"):
        problems.append(f"workflow_ref={workflow_ref!r}")
    sha = str(claims.get("sha") or "")
    if sha != expected.head_sha and expected.head_sha not in list(merge_parents or []):
        problems.append(f"sha={sha or '?'} 与头提交 {expected.head_sha} 无绑定")
    return problems


@dataclass(frozen=True)
class TokenCheck:
    """凭证核验结果：`ok` 才可作为采信依据。"""

    status: str
    reasons: tuple[str, ...] = ()
    claims: Mapping = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == TOKEN_OK

    @property
    def base_ref(self) -> str:
        return normalize_branch(str(self.claims.get("base_ref") or ""))

    @property
    def sha(self) -> str:
        return str(self.claims.get("sha") or "")

    @property
    def pull_request_number(self) -> int | None:
        return pull_request_number_from_ref(str(self.claims.get("ref") or ""))

    @property
    def summary(self) -> str:
        if self.status == TOKEN_MISSING:
            return "没有身份凭证（触发运行未留下制品）"
        if self.status == TOKEN_INVALID:
            return "身份凭证不合格（" + "；".join(self.reasons) + "）"
        return f"身份凭证验签通过（run {self.claims.get('run_id')}，base {self.base_ref}）"


def check_token(
    token: str | None,
    jwks: Mapping | None,
    expected: ExpectedClaims,
    merge_parents: Sequence[str] | None,
) -> TokenCheck:
    """验签 + 核声明；任何一步不过都不采信。"""

    if not token or not token.strip():
        return TokenCheck(status=TOKEN_MISSING)
    if not jwks:
        return TokenCheck(status=TOKEN_INVALID, reasons=("没有取到 JWKS，无法验签",))
    try:
        claims = verify_signature(token, jwks)
    except TokenError as error:
        return TokenCheck(status=TOKEN_INVALID, reasons=(str(error),))
    problems = claim_problems(claims, expected, merge_parents)
    if problems:
        return TokenCheck(status=TOKEN_INVALID, reasons=tuple(problems), claims=claims)
    return TokenCheck(status=TOKEN_OK, claims=claims)


def base_commitish_for(token: TokenCheck, facts: RunFacts, merge_parents: Sequence[str]) -> str:
    """凭证合格时的基线提交：合并提交的第一个父提交（运行时的 base 顶点），否则 base 分支名。"""

    if token.sha != facts.head_sha and merge_parents:
        return merge_parents[0]
    return token.base_ref


# ---------------------------------------------------------------------------
# 裁决
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkflowTrees:
    """头提交与基线两侧 `.github/workflows` 子树的树对象 sha；取不到的一侧为 None。"""

    head: str | None
    base: str | None


def definitions_differ(trees: WorkflowTrees) -> bool:
    """头提交的门禁定义是否与基线不同；任一侧缺失也算不同（标签与差异评论据此触发）。"""

    return not (trees.head and trees.head == trees.base)


def decide(facts: RunFacts, baseline: Baseline, trees: WorkflowTrees, token: TokenCheck) -> Verdict:
    """给出初步裁决：跳过 / 拒绝 / 路径 a（有结论）/ 路径 b（等复跑）。

    路径 a 两个条件缺一不可：凭证合格、头提交的 `.github/workflows` 子树与凭证声明的
    base 分支相同。任何一条不成立都复跑，绝不采信触发运行。
    """

    early = precheck(facts)
    if early is not None:
        return early
    run_label = f"run {facts.run_id} 结论 {facts.conclusion or '无'}"
    tree_label = (
        f"{WORKFLOWS_DIRECTORY} 子树：头提交 {trees.head or '缺失'}，"
        f"基线 {baseline.label} {trees.base or '缺失'}"
    )
    if not token.ok:
        why = token.summary
    elif not definitions_differ(trees):
        return Verdict(
            path=PATH_A,
            write_check=True,
            conclusion=map_run_conclusion(facts.conclusion),
            summary=(
                f"路径 a（{token.summary}，门禁定义与基线 {baseline.label} 相同）："
                f"采信 {run_label}；{tree_label}"
            ),
        )
    else:
        why = f"{token.summary}，但门禁定义与基线 {baseline.label} 不同"
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


# ---------------------------------------------------------------------------
# 命令行：工作流只做取值与调用
# ---------------------------------------------------------------------------


def _load_json(path: str) -> object:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _optional_json(path: str | None) -> object | None:
    if path and Path(path).is_file():
        return _load_json(path)
    return None


def _optional_text(path: str | None) -> str | None:
    if path and Path(path).is_file():
        return Path(path).read_text(encoding="utf-8")
    return None


@dataclass(frozen=True)
class Situation:
    """一次调用读到的全部输入：事实、备用基线、凭证核验、合并提交父提交。"""

    facts: RunFacts
    fallback_baseline: Baseline
    token: TokenCheck
    merge_parents: tuple[str, ...]

    @property
    def baseline(self) -> Baseline:
        if self.token.ok:
            base_sha = base_commitish_for(self.token, self.facts, self.merge_parents)
            pull_request = None
            number = self.token.pull_request_number
            if number is not None:
                pull_request = PullRequestRef(
                    number, self.token.base_ref, base_sha, self.facts.head_sha
                )
            return Baseline(
                pull_request=pull_request, base_ref=self.token.base_ref, base_sha=base_sha
            )
        return self.fallback_baseline

    @property
    def pull_request_number(self) -> str:
        pull_request = self.baseline.pull_request
        return str(pull_request.number) if pull_request else ""


def _situation(args: argparse.Namespace) -> Situation:
    fallback = _optional_json(getattr(args, "fallback_pulls_file", None)) or []
    facts = parse_run_facts(_load_json(args.event_file), fallback)
    merge_parents = tuple(
        str(item) for item in (_optional_json(getattr(args, "merge_parents_file", None)) or [])
    )
    expected = ExpectedClaims(
        repository=facts.repository,
        run_id=str(facts.run_id or ""),
        head_sha=facts.head_sha,
        default_branch=facts.default_branch,
        audience=getattr(args, "audience", TOKEN_AUDIENCE) or TOKEN_AUDIENCE,
    )
    token = check_token(
        _optional_text(getattr(args, "token_file", None)),
        _optional_json(getattr(args, "jwks_file", None)),
        expected,
        merge_parents,
    )
    return Situation(facts, select_baseline(facts), token, merge_parents)


def _command_verify(args: argparse.Namespace) -> None:
    """只验签与核不依赖合并提交的声明，输出凭证里的 `sha` 供工作流取父提交。"""

    situation = _situation(args)
    token = situation.token
    reasons = [reason for reason in token.reasons if not reason.startswith("sha=")]
    signature_ok = token.status == TOKEN_OK or (
        token.status == TOKEN_INVALID and token.claims and not reasons
    )
    write_github_output(
        args.github_output,
        {
            "token_status": TOKEN_OK if signature_ok else token.status,
            "token_reasons": "；".join(reasons),
            "token_sha": token.sha if token.claims else "",
            "head_sha": situation.facts.head_sha,
        },
    )
    print(f"凭证：{'验签通过、声明合格' if signature_ok else token.summary}")


def _command_plan(args: argparse.Namespace) -> None:
    situation = _situation(args)
    early = precheck(situation.facts)
    baseline = situation.baseline
    write_github_output(
        args.github_output,
        {
            "needs_trees": "false" if early is not None else "true",
            "head_sha": situation.facts.head_sha,
            "pr_number": situation.pull_request_number,
            "base_ref": baseline.base_ref,
            "base_sha": baseline.base_sha,
            "base_label": baseline.label,
            "token_status": situation.token.status,
            "token_summary": situation.token.summary,
        },
    )
    print(f"基线 {baseline.label}；{situation.token.summary}")


def _command_decide(args: argparse.Namespace) -> None:
    situation = _situation(args)
    baseline = situation.baseline
    trees = WorkflowTrees(head=args.head_tree or None, base=args.base_tree or None)
    verdict = decide(situation.facts, baseline, trees, situation.token)
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
            "head_sha": situation.facts.head_sha,
            "pr_number": situation.pull_request_number,
            "base_label": baseline.label,
            "run_id": situation.facts.run_id or "",
            "run_url": situation.facts.run_url,
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


def _add_situation_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--event-file", required=True)
    parser.add_argument("--fallback-pulls-file")
    parser.add_argument("--token-file", help="触发运行留下的身份凭证（JWT）")
    parser.add_argument("--jwks-file", help="GitHub OIDC 提供者的 JWKS")
    parser.add_argument("--merge-parents-file", help="凭证 sha 所指提交的父提交清单（JSON 数组）")
    parser.add_argument("--audience", default=TOKEN_AUDIENCE)
    parser.add_argument("--github-output")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)

    verify = commands.add_parser("verify", help="验签并核对声明，输出凭证里的 sha")
    _add_situation_arguments(verify)
    verify.set_defaults(func=_command_verify)

    plan = commands.add_parser("plan", help="选基线：输出要比较的 PR 与基线提交")
    _add_situation_arguments(plan)
    plan.set_defaults(func=_command_plan)

    decision = commands.add_parser("decide", help="给出初步裁决")
    _add_situation_arguments(decision)
    decision.add_argument("--head-tree", default="")
    decision.add_argument("--base-tree", default="")
    decision.add_argument("--head-listing-file")
    decision.add_argument("--base-listing-file")
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
