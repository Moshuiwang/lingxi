#!/usr/bin/env python3
"""依赖漏洞扫描的判定脚本：读 pip-audit / npm audit 的 JSON 结果、取严重度、对照带到期日的豁免清单。

只做判定，不做扫描；扫描由 CI 作业里钉版的 pip-audit、或 npm 自带的 npm audit 完成。退出码三态：
  0 = 没有未豁免的高 / 严重漏洞（中 / 低只在摘要里告警）；
  1 = 存在未豁免（含豁免已到期）的高 / 严重漏洞；
  2 = 未知：结果文件读不到或解析不了、某条漏洞取不到严重度、豁免清单格式错。
「未知」与「判红」都让作业失败——扫描器装不上、漏洞库或严重度接口不可达时不得静默判绿。

严重度来源是 OSV 记录（https://api.osv.dev/v1/vulns/<编号>）：优先 GHSA 记录的
database_specific.severity 标签，其次 CVSS v3 向量算基础分（≥ 9.0 严重、≥ 7.0 高、
≥ 4.0 中、其余低），两者都有取更高者；只有 CVSS v4 向量或什么都没有 → 未知。
测试用 --severity-file 注入同形状的记录，不走网络。
--format npm-audit 时，每条公告自带的严重度标签与 CVSS v3 向量（npm 报告里的 via 条目）被包成同形状
记录，走同一套取更高者规则，不另查 OSV；报告形状不符、npm 报了错、公告追溯不到的包一律「未知」。

豁免清单是只读输入：每行「编号 到期日(YYYY-MM-DD) 理由」，到期日含当天、过期即失效；
不接受环境变量或命令行追加条目，唤起方只能指定清单文件本身。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

EXIT_OK = 0
EXIT_RED = 1
EXIT_UNKNOWN = 2

SEVERITY_RANK = {"LOW": 1, "MODERATE": 2, "HIGH": 3, "CRITICAL": 4}
RED_LEVELS = frozenset({"HIGH", "CRITICAL"})
UNKNOWN = "未知"

VULN_ID_PATTERN = re.compile(
    r"^(GHSA-[0-9a-z]{4}-[0-9a-z]{4}-[0-9a-z]{4}|PYSEC-\d{4}-\d+|CVE-\d{4}-\d{4,})$"
)
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
NPM_ADVISORY_URL_PATTERN = re.compile(
    r"/advisories/(GHSA-[0-9a-z]{4}-[0-9a-z]{4}-[0-9a-z]{4})(?:[/?#]|$)"
)
NPM_REPORT_VERSION = 2
# npm 的严重度比本脚本多一档 info，按低处理（只告警）；标签不在表内的当作没有标签。
NPM_LEVELS = {
    "INFO": "LOW",
    "LOW": "LOW",
    "MODERATE": "MODERATE",
    "HIGH": "HIGH",
    "CRITICAL": "CRITICAL",
}
OSV_VULN_URL = "https://api.osv.dev/v1/vulns/"
DEFAULT_ALLOWLIST = Path(__file__).resolve().parent / "pip_audit_allowlist.txt"

# CVSS v3.x 基础分的固定权重（规范 §7.4）；PR 按 S:U / S:C 各一组。
_CVSS3_WEIGHTS: dict[str, dict[str, float]] = {
    "AV": {"N": 0.85, "A": 0.62, "L": 0.55, "P": 0.2},
    "AC": {"L": 0.77, "H": 0.44},
    "UI": {"N": 0.85, "R": 0.62},
    "CIA": {"H": 0.56, "L": 0.22, "N": 0.0},
}
_CVSS3_PR_WEIGHTS = {
    "U": {"N": 0.85, "L": 0.62, "H": 0.27},
    "C": {"N": 0.85, "L": 0.68, "H": 0.5},
}


class AuditInputError(Exception):
    """输入不可判定：结果文件、豁免清单或严重度来源有问题，一律对应退出码 2。"""


@dataclass(frozen=True)
class Finding:
    package: str
    version: str
    ids: tuple[str, ...]
    fix_versions: tuple[str, ...]
    description: str

    @property
    def primary_id(self) -> str:
        return self.ids[0]

    def lookup_order(self) -> list[str]:
        """查严重度的顺序：GHSA 记录带标签，先查；其余按原顺序。"""
        ghsa = sorted(i for i in self.ids if i.startswith("GHSA-"))
        return ghsa + [i for i in self.ids if not i.startswith("GHSA-")]


@dataclass(frozen=True)
class Exemption:
    vuln_id: str
    expires: date
    reason: str
    line_no: int


@dataclass(frozen=True)
class Severity:
    level: str | None
    label: str | None = None
    score: float | None = None
    source_id: str | None = None
    detail: str = ""

    def describe(self) -> str:
        if self.level is None:
            return f"{UNKNOWN}（{self.detail}）"
        parts = []
        if self.label:
            parts.append(f"标签 {self.label}")
        if self.score is not None:
            parts.append(f"CVSS {self.score:.1f}")
        return f"{self.level}（{'，'.join(parts)}，来源 {self.source_id}）"


@dataclass
class Verdict:
    red: list[tuple[Finding, Severity, str]] = field(default_factory=list)
    warn: list[tuple[Finding, Severity, str]] = field(default_factory=list)
    exempted: list[tuple[Finding, Severity, Exemption]] = field(default_factory=list)
    unknown: list[tuple[Finding, Severity]] = field(default_factory=list)
    unused_exemptions: list[Exemption] = field(default_factory=list)
    dependency_count: int = 0

    def exit_code(self) -> int:
        if self.unknown:
            return EXIT_UNKNOWN
        if self.red:
            return EXIT_RED
        return EXIT_OK


def _roundup(value: float) -> float:
    """CVSS v3.1 §附录 A 的 Roundup：先放大到十万分位取整，避免浮点误差改变结论。"""
    scaled = round(value * 100000)
    if scaled % 10000 == 0:
        return scaled / 100000
    return (scaled // 10000 + 1) / 10


def cvss3_base_score(vector: str) -> float | None:
    """由 CVSS v3.0 / v3.1 向量算基础分；形状不对返回 None（调用方据此判未知）。"""
    head, _, rest = vector.partition("/")
    if not head.startswith("CVSS:3."):
        return None
    metrics = dict(part.partition(":")[::2] for part in rest.split("/") if ":" in part)
    if metrics.get("S") not in ("U", "C"):
        return None
    scope_changed = metrics["S"] == "C"
    try:
        av = _CVSS3_WEIGHTS["AV"][metrics["AV"]]
        ac = _CVSS3_WEIGHTS["AC"][metrics["AC"]]
        pr = _CVSS3_PR_WEIGHTS[metrics["S"]][metrics["PR"]]
        ui = _CVSS3_WEIGHTS["UI"][metrics["UI"]]
        c, i, a = (_CVSS3_WEIGHTS["CIA"][metrics[key]] for key in ("C", "I", "A"))
    except KeyError:
        return None
    iss = 1 - (1 - c) * (1 - i) * (1 - a)
    if scope_changed:
        impact = 7.52 * (iss - 0.029) - 3.25 * (iss - 0.02) ** 15
    else:
        impact = 6.42 * iss
    if impact <= 0:
        return 0.0
    exploitability = 8.22 * av * ac * pr * ui
    total = impact + exploitability
    if scope_changed:
        total *= 1.08
    return _roundup(min(total, 10.0))


def level_from_score(score: float) -> str:
    if score >= 9.0:
        return "CRITICAL"
    if score >= 7.0:
        return "HIGH"
    if score >= 4.0:
        return "MODERATE"
    return "LOW"


def severity_from_record(record: dict, source_id: str) -> Severity | None:
    """从一条 OSV 记录读严重度；标签与 v3 分数都没有时返回 None，让调用方查下一个编号。"""
    if not isinstance(record, dict):
        return None
    label = (record.get("database_specific") or {}).get("severity")
    label = label.upper() if isinstance(label, str) and label.upper() in SEVERITY_RANK else None
    scores = [
        cvss3_base_score(entry["score"])
        for entry in record.get("severity") or []
        if isinstance(entry, dict) and entry.get("type") == "CVSS_V3" and "score" in entry
    ]
    valid_scores = [s for s in scores if s is not None]
    score = max(valid_scores) if valid_scores else None
    levels = [lvl for lvl in (label, level_from_score(score) if score is not None else None) if lvl]
    if not levels:
        return None
    level = max(levels, key=SEVERITY_RANK.__getitem__)
    return Severity(level=level, label=label, score=score, source_id=source_id)


def resolve_severity(finding: Finding, fetch: Callable[[str], dict | None]) -> Severity:
    """按 GHSA 优先的顺序逐个编号取记录，第一条给出标签或 v3 分数的记录即为结论。"""
    missing: list[str] = []
    for vuln_id in finding.lookup_order():
        record = fetch(vuln_id)
        if record is None:
            missing.append(vuln_id)
            continue
        severity = severity_from_record(record, vuln_id)
        if severity is not None:
            return severity
    queried = "、".join(finding.lookup_order())
    detail = f"严重度记录无标签也无 CVSS v3 向量，查过 {queried}"
    if len(missing) == len(finding.ids):
        detail = f"OSV 查不到任何编号：{queried}"
    return Severity(level=None, detail=detail)


class OsvClient:
    """按编号取 OSV 记录；404 视为无此记录，其余网络 / 协议错误一律抛未知。"""

    def __init__(self, base_url: str = OSV_VULN_URL, timeout: float = 20.0, attempts: int = 2):
        self.base_url = base_url if base_url.endswith("/") else base_url + "/"
        self.timeout = timeout
        self.attempts = attempts
        self._cache: dict[str, dict | None] = {}

    def fetch(self, vuln_id: str) -> dict | None:
        if vuln_id not in self._cache:
            self._cache[vuln_id] = self._fetch_uncached(vuln_id)
        return self._cache[vuln_id]

    def _fetch_uncached(self, vuln_id: str) -> dict | None:
        url = self.base_url + vuln_id
        last_error: Exception | None = None
        for _ in range(self.attempts):
            try:
                with urllib.request.urlopen(url, timeout=self.timeout) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as error:
                if error.code == 404:
                    return None
                last_error = error
            except (urllib.error.URLError, TimeoutError, ValueError, OSError) as error:
                last_error = error
        raise AuditInputError(
            f"未能取得严重度（外部服务不可用），非漏洞判定：严重度接口不可用：{url}：{last_error}"
        )


def load_severity_file(path: Path) -> Callable[[str], dict | None]:
    """测试注入：JSON 对象，键是编号、值是 OSV 记录同形状的对象。"""
    try:
        records = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise AuditInputError(f"严重度文件读不到或不是 JSON：{path}：{error}") from error
    if not isinstance(records, dict):
        raise AuditInputError(f"严重度文件顶层必须是对象：{path}")
    return records.get


def _merge_alias_groups(raw_vulns: list[dict]) -> list[list[dict]]:
    """pip-audit 对同一漏洞的 PYSEC / GHSA 两份记录各出一条，按编号交集合并成组。"""
    groups: list[tuple[set[str], list[dict]]] = []
    for vuln in raw_vulns:
        ids = {vuln["id"], *vuln.get("aliases", [])}
        hit = [g for g in groups if g[0] & ids]
        merged_ids, merged_vulns = set(ids), [vuln]
        for group in hit:
            merged_ids |= group[0]
            merged_vulns = group[1] + merged_vulns
            groups.remove(group)
        groups.append((merged_ids, merged_vulns))
    return [vulns for _, vulns in groups]


def _finding_from_group(package: str, version: str, group: list[dict]) -> Finding:
    first = group[0]
    ordered = [first["id"]]
    for vuln in group:
        for candidate in (vuln["id"], *sorted(vuln.get("aliases", []))):
            if candidate not in ordered:
                ordered.append(candidate)
    fixes = sorted({str(v) for vuln in group for v in vuln.get("fix_versions", [])})
    return Finding(
        package, version, tuple(ordered), tuple(fixes), str(first.get("description", ""))
    )


def load_findings(path: Path) -> tuple[list[Finding], int]:
    """读 pip-audit `--format json` 的结果；形状不对或有未能审计的包一律抛未知。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise AuditInputError(f"扫描结果读不到或不是 JSON：{path}：{error}") from error
    dependencies = data.get("dependencies") if isinstance(data, dict) else None
    if not isinstance(dependencies, list):
        raise AuditInputError(f"扫描结果缺少 dependencies 列表：{path}")
    findings: list[Finding] = []
    skipped: list[str] = []
    for dep in dependencies:
        if not isinstance(dep, dict) or "name" not in dep or "version" not in dep:
            raise AuditInputError(f"扫描结果里有形状不对的依赖条目：{dep!r}")
        if dep.get("skip_reason"):
            skipped.append(f"{dep['name']} {dep['version']}：{dep['skip_reason']}")
            continue
        vulns = dep.get("vulns")
        if not isinstance(vulns, list) or any("id" not in v for v in vulns):
            raise AuditInputError(f"扫描结果里 {dep['name']} 的 vulns 形状不对")
        for group in _merge_alias_groups(vulns):
            findings.append(_finding_from_group(str(dep["name"]), str(dep["version"]), group))
    if skipped:
        raise AuditInputError("扫描器未能审计以下依赖，无法判定：\n  " + "\n  ".join(skipped))
    return findings, len(dependencies)


def _npm_record(advisory: dict) -> dict:
    """把 npm 公告的严重度标签与 CVSS v3 向量包成 OSV 同形状记录；两样都没有就是空记录。"""
    record: dict = {}
    label = advisory.get("severity")
    if isinstance(label, str) and label.upper() in NPM_LEVELS:
        record["database_specific"] = {"severity": NPM_LEVELS[label.upper()]}
    cvss = advisory.get("cvss")
    vector = cvss.get("vectorString") if isinstance(cvss, dict) else None
    if isinstance(vector, str):
        record["severity"] = [{"type": "CVSS_V3", "score": vector}]
    return record


def _npm_fix_text(fix_available: object) -> str:
    if isinstance(fix_available, dict) and "name" in fix_available and "version" in fix_available:
        major = "，跨主版本" if fix_available.get("isSemVerMajor") else ""
        return f"{fix_available['name']}@{fix_available['version']}{major}"
    return "npm audit fix 可修" if fix_available is True else "无"


def _npm_traces_to_advisory(
    name: str, vulnerabilities: dict, seen: frozenset = frozenset()
) -> bool:
    """via 里的字符串是「因依赖了另一个有漏洞的包而受影响」，顺着它必须走到一条真正的公告。"""
    entry = vulnerabilities.get(name)
    if name in seen or not isinstance(entry, dict):
        return False
    return any(
        isinstance(via, dict)
        or (isinstance(via, str) and _npm_traces_to_advisory(via, vulnerabilities, seen | {name}))
        for via in entry["via"]
    )


def _npm_finding(package: str, advisory: dict, fix_text: str) -> tuple[Finding, dict]:
    url_match = NPM_ADVISORY_URL_PATTERN.search(str(advisory.get("url", "")))
    if url_match:
        vuln_id = url_match.group(1)
    elif "source" in advisory:
        vuln_id = f"npm-advisory-{advisory['source']}"
    else:
        raise AuditInputError(f"npm 公告既无 GHSA 链接也无编号，无法识别：{package}：{advisory!r}")
    finding = Finding(
        package,
        f"受影响范围 {advisory.get('range', '未给出')}",
        (vuln_id,),
        (fix_text,),
        str(advisory.get("title", "")),
    )
    return finding, _npm_record(advisory)


def load_npm_findings(path: Path) -> tuple[list[Finding], int, dict[str, dict]]:
    """读 `npm audit --json` 的结果，返回（公告列表，依赖总数，编号到严重度记录的映射）。

    npm 的 JSON 里，一个包的 via 既有公告对象，也有指向其他有漏洞的包的字符串；公告对象
    只出现在它所属的那个包下，所以只收对象。npm 报错（没有网络、公告服务不可用）时顶层是
    error 对象，同样判未知。
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise AuditInputError(f"扫描结果读不到或不是 JSON：{path}：{error}") from error
    if isinstance(data, dict) and isinstance(data.get("error"), dict):
        error = data["error"]
        parts = (error.get("code"), error.get("summary"), data.get("message"))
        raise AuditInputError(
            "npm audit 未给出审计结果（外部服务不可用或命令出错），非漏洞判定："
            + (" ".join(str(part) for part in parts if part) or "无错误详情")
        )
    if not isinstance(data, dict) or data.get("auditReportVersion") != NPM_REPORT_VERSION:
        raise AuditInputError(
            f"扫描结果不是 auditReportVersion {NPM_REPORT_VERSION} 的报告：{path}"
        )
    vulnerabilities = data.get("vulnerabilities")
    metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    dependencies = metadata.get("dependencies")
    counts = metadata.get("vulnerabilities")
    total = dependencies.get("total") if isinstance(dependencies, dict) else None
    counted = counts.get("total") if isinstance(counts, dict) else None
    if not isinstance(vulnerabilities, dict) or not isinstance(total, int):
        raise AuditInputError(f"扫描结果缺少 vulnerabilities 对象或依赖总数：{path}")
    if counted != len(vulnerabilities):
        raise AuditInputError(f"扫描结果的漏洞包数与 metadata 不一致，报告可能不完整：{path}")
    for name, entry in vulnerabilities.items():
        if not isinstance(entry, dict) or not isinstance(entry.get("via"), list):
            raise AuditInputError(f"扫描结果里 {name} 的 via 形状不对")
    findings: list[Finding] = []
    records: dict[str, dict] = {}
    for name, entry in vulnerabilities.items():
        if not _npm_traces_to_advisory(name, vulnerabilities):
            raise AuditInputError(f"扫描结果里 {name} 追溯不到任何公告，无法判定")
        fix_text = _npm_fix_text(entry.get("fixAvailable"))
        for advisory in (via for via in entry["via"] if isinstance(via, dict)):
            finding, record = _npm_finding(str(advisory.get("name", name)), advisory, fix_text)
            if records.setdefault(finding.primary_id, record) != record:
                raise AuditInputError(f"同一公告 {finding.primary_id} 在报告里给出了不同的严重度")
            findings.append(finding)
    return findings, total, records


def parse_allowlist(path: Path) -> list[Exemption]:
    """严格解析：空行与 # 注释跳过，其余行必须是「编号 到期日 理由」且编号不重复。"""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise AuditInputError(f"豁免清单读不到：{path}：{error}") from error
    exemptions: list[Exemption] = []
    seen: dict[str, int] = {}
    for line_no, raw in enumerate(lines, start=1):
        text = raw.strip()
        if not text or text.startswith("#"):
            continue
        fields = text.split(None, 2)
        if len(fields) != 3:
            raise AuditInputError(f"{path}:{line_no}：必须是「编号 到期日 理由」三段：{text!r}")
        vuln_id, expires_text, reason = fields
        if not VULN_ID_PATTERN.match(vuln_id):
            raise AuditInputError(f"{path}:{line_no}：编号形状不合法：{vuln_id!r}")
        if not DATE_PATTERN.match(expires_text):
            raise AuditInputError(f"{path}:{line_no}：到期日必须是 YYYY-MM-DD：{expires_text!r}")
        try:
            expires = date.fromisoformat(expires_text)
        except ValueError as error:
            raise AuditInputError(
                f"{path}:{line_no}：到期日不是有效日期：{expires_text!r}"
            ) from error
        if vuln_id in seen:
            raise AuditInputError(f"{path}:{line_no}：编号 {vuln_id} 与第 {seen[vuln_id]} 行重复")
        seen[vuln_id] = line_no
        exemptions.append(Exemption(vuln_id, expires, reason.strip(), line_no))
    return exemptions


def evaluate(
    findings: Iterable[Finding],
    fetch: Callable[[str], dict | None],
    exemptions: list[Exemption],
    today: date,
) -> Verdict:
    """逐条判定：有效豁免放行；到期豁免视同没有；高 / 严重判红、中 / 低告警、取不到判未知。"""
    verdict = Verdict()
    used: set[int] = set()
    for finding in findings:
        matched = [e for e in exemptions if e.vuln_id in finding.ids]
        valid = [e for e in matched if today <= e.expires]
        severity = resolve_severity(finding, fetch)
        if valid:
            used.update(e.line_no for e in valid)
            verdict.exempted.append((finding, severity, valid[0]))
            continue
        note = ""
        if matched:
            used.update(e.line_no for e in matched)
            note = "豁免已于 " + "、".join(e.expires.isoformat() for e in matched) + " 到期"
        if severity.level is None:
            verdict.unknown.append((finding, severity))
        elif severity.level in RED_LEVELS:
            verdict.red.append((finding, severity, note))
        else:
            verdict.warn.append((finding, severity, note))
    verdict.unused_exemptions = [e for e in exemptions if e.line_no not in used]
    return verdict


def _finding_row(finding: Finding, severity: Severity, note: str) -> str:
    aliases = "、".join(finding.ids[1:])
    ids = finding.primary_id + (f"（{aliases}）" if aliases else "")
    fixes = "、".join(finding.fix_versions) or "无"
    return (
        f"| {finding.package} | {finding.version} | {ids} | {severity.describe()} | {fixes} |"
        f" {note or '—'} |"
    )


def render_summary(verdict: Verdict, label: str, today: date) -> str:
    """Markdown 摘要：判红 / 告警 / 已豁免 / 未知四张表 + 豁免清单里未命中的条目。"""
    code = verdict.exit_code()
    conclusion = {EXIT_OK: "通过", EXIT_RED: "判红", EXIT_UNKNOWN: "未知（判失败）"}[code]
    total = len(verdict.red) + len(verdict.warn) + len(verdict.exempted) + len(verdict.unknown)
    lines = [
        f"## 依赖漏洞扫描：{label} —— {conclusion}（退出码 {code}）",
        "",
        f"- 扫描对象 {verdict.dependency_count} 个依赖；去重后 {total} 条已公布漏洞；"
        f"判红 {len(verdict.red)}、告警 {len(verdict.warn)}、已豁免 {len(verdict.exempted)}、"
        f"未知 {len(verdict.unknown)}；判定日 {today.isoformat()}",
    ]
    header = (
        "| 包 | 版本 | 编号 | 严重度 | 修复版本 | 备注 |\n| --- | --- | --- | --- | --- | --- |"
    )
    sections = (
        ("### 未豁免的高 / 严重（判红）", [(f, s, n) for f, s, n in verdict.red]),
        ("### 告警（中 / 低，不判红）", [(f, s, n) for f, s, n in verdict.warn]),
        ("### 严重度未知（判失败，不静默放行）", [(f, s, "") for f, s in verdict.unknown]),
        (
            "### 已豁免（未到期）",
            [(f, s, f"到期 {e.expires.isoformat()}：{e.reason}") for f, s, e in verdict.exempted],
        ),
    )
    for title, rows in sections:
        if rows:
            lines += ["", title, "", header, *(_finding_row(f, s, n) for f, s, n in rows)]
    if verdict.unused_exemptions:
        lines += ["", "### 豁免清单里未命中的条目（可删除）", ""]
        for exemption in verdict.unused_exemptions:
            state = "已到期" if today > exemption.expires else "未到期"
            lines.append(
                f"- {exemption.vuln_id}（{state}，到期 {exemption.expires.isoformat()}）：{exemption.reason}"
            )
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--audit-json", required=True, type=Path, help="扫描器的 JSON 输出（格式见 --format）"
    )
    parser.add_argument(
        "--format",
        choices=("pip-audit", "npm-audit"),
        default="pip-audit",
        help="扫描结果的来源：pip-audit --format json，或 npm audit --json",
    )
    parser.add_argument("--allowlist", type=Path, default=DEFAULT_ALLOWLIST, help="豁免清单文件")
    parser.add_argument("--today", default=None, help="判定日 YYYY-MM-DD（默认 UTC 今天）")
    parser.add_argument("--severity-file", type=Path, default=None, help="离线严重度记录（测试）")
    parser.add_argument("--osv-url", default=OSV_VULN_URL, help="OSV 记录接口前缀")
    parser.add_argument("--label", default="依赖漏洞扫描", help="摘要标题里的标签，如 extra 名")
    return parser


def _parse_today(text: str | None) -> date:
    if text is None:
        return datetime.now(UTC).date()
    if not DATE_PATTERN.match(text):
        raise AuditInputError(f"--today 必须是 YYYY-MM-DD：{text!r}")
    return date.fromisoformat(text)


def _load_report(args: argparse.Namespace) -> tuple[list[Finding], int, Callable]:
    """按 --format 读扫描结果，同时给出取严重度记录的函数。"""
    if args.format == "npm-audit":
        findings, dependency_count, records = load_npm_findings(args.audit_json)
        return findings, dependency_count, records.get
    findings, dependency_count = load_findings(args.audit_json)
    if args.severity_file:
        return findings, dependency_count, load_severity_file(args.severity_file)
    return findings, dependency_count, OsvClient(args.osv_url).fetch


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.format == "npm-audit" and args.severity_file:
        parser.error("--severity-file 只用于 pip-audit 格式；npm-audit 的严重度取自报告本身")
    try:
        today = _parse_today(args.today)
        findings, dependency_count, fetch = _load_report(args)
        exemptions = parse_allowlist(args.allowlist)
        verdict = evaluate(findings, fetch, exemptions, today)
    except AuditInputError as error:
        message = (
            f"## 依赖漏洞扫描：{args.label} —— 未知（判失败，退出码 {EXIT_UNKNOWN}）\n\n{error}\n"
        )
        _emit(message)
        return EXIT_UNKNOWN
    verdict.dependency_count = dependency_count
    _emit(render_summary(verdict, args.label, today))
    return verdict.exit_code()


def _emit(text: str) -> None:
    """摘要同时打到标准输出与作业摘要（后者只在 GitHub Actions 里存在）。"""
    sys.stdout.write(text)
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as handle:
            handle.write(text)


if __name__ == "__main__":
    sys.exit(main())
