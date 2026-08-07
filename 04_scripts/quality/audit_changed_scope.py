"""Read-only incremental audit for an explicit Git baseline and allowed scope."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Callable, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[2]
FINDING_CLASSES = ("BLOCKER", "FOLLOW_UP", "OUT_OF_SCOPE", "INSUFFICIENT_EVIDENCE")
FULL_REGRESSION_REASONS = (
    "candidate",
    "common-loader",
    "common-data-structure",
    "dependency",
    "compose-or-docker",
    "cron",
    "release-tool",
    "user-request",
)


class WorktreeChangedError(RuntimeError):
    """Raised when the audit would otherwise report a moving target."""


def _git(project_root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "core.quotepath=true", *args],
        cwd=project_root,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.decode("utf-8", errors="replace").strip() or f"git {' '.join(args)} 失败")
    return result.stdout.decode("ascii")


def _safe_paths(project_root: Path, values: Sequence[str], label: str) -> list[str]:
    normalized: list[str] = []
    root = project_root.resolve()
    for value in values:
        candidate = Path(value)
        if candidate.is_absolute() or ".." in candidate.parts or not value.strip():
            raise ValueError(f"{label} 必须是非空的仓库内相对路径: {value!r}")
        resolved = (root / candidate).resolve()
        if root != resolved and root not in resolved.parents:
            raise ValueError(f"{label} 不在仓库内: {value!r}")
        normalized.append(candidate.as_posix().rstrip("/"))
    return normalized


def _decode_git_path(value: str) -> str:
    """Decode Git's ASCII C-style quoted path without relying on the Windows locale."""
    value = value.strip()
    if not (value.startswith('"') and value.endswith('"')):
        return value
    raw = value[1:-1]
    output = bytearray()
    index = 0
    escapes = {"a": 7, "b": 8, "f": 12, "n": 10, "r": 13, "t": 9, "v": 11}
    while index < len(raw):
        character = raw[index]
        if character != "\\":
            output.extend(character.encode("ascii"))
            index += 1
            continue
        index += 1
        escaped = raw[index]
        if escaped in "01234567":
            output.append(int(raw[index : index + 3], 8))
            index += 3
        elif escaped in escapes:
            output.append(escapes[escaped])
            index += 1
        else:
            output.extend(escaped.encode("ascii"))
            index += 1
    return output.decode("utf-8")


def _status_paths(status: str) -> list[str]:
    paths: list[str] = []
    for line in status.splitlines():
        if len(line) >= 4:
            paths.append(_decode_git_path(line[3:]).replace("\\", "/"))
    return paths


def _changed_paths(project_root: Path, baseline: str, git: Callable[..., str]) -> list[str]:
    sources = (
        git(project_root, "diff", "--name-only", f"{baseline}..HEAD"),
        git(project_root, "diff", "--name-only", "--cached"),
        git(project_root, "diff", "--name-only"),
        "\n".join(_status_paths(git(project_root, "status", "--porcelain=v1", "--untracked-files=all"))),
    )
    return sorted({_decode_git_path(line).replace("\\", "/") for source in sources for line in source.splitlines() if line})


def _is_allowed(path: str, allowed: Sequence[str]) -> bool:
    return any(path == item or path.startswith(f"{item}/") for item in allowed)


def parse_finding(value: str) -> dict[str, object]:
    """Parse CLASS|description|evidence|related-file|suggested-task without writing it."""
    parts = [part.strip() for part in value.split("|", 4)]
    if len(parts) != 5 or parts[0] not in FINDING_CLASSES or not parts[1]:
        raise ValueError("--finding 格式为 CLASS|问题描述|证据|相关文件|建议后续任务")
    classification, description, evidence, related_file, suggested_task = parts
    return {
        "classification": classification,
        "description": description,
        "evidence": evidence,
        "related_files": [related_file] if related_file else [],
        "blocking_current_feature": classification == "BLOCKER",
        "suggested_follow_up": suggested_task,
    }


def run_audit(
    project_root: Path,
    baseline: str,
    allowed: Sequence[str],
    *,
    dependencies: Sequence[str] = (),
    tests: Sequence[str] = (),
    configs: Sequence[str] = (),
    pages: Sequence[str] = (),
    data_paths: Sequence[str] = (),
    known_existing: Sequence[str] = (),
    findings: Sequence[dict[str, object]] = (),
    full_regression_reasons: Sequence[str] = (),
    git: Callable[..., str] = _git,
) -> dict[str, object]:
    """Return a JSON-ready read-only report; fail if the worktree moves mid-audit."""
    git(project_root, "rev-parse", "--verify", f"{baseline}^{{commit}}")
    current_head = git(project_root, "rev-parse", "HEAD").strip()
    status_before = git(project_root, "status", "--porcelain=v1", "--untracked-files=all")

    allowed_paths = _safe_paths(project_root, allowed, "--allow")
    if not allowed_paths:
        raise ValueError("至少提供一个 --allow 文件或目录")
    known_existing_paths = _safe_paths(project_root, known_existing, "--known-existing")
    changed = _changed_paths(project_root, baseline, git)
    preexisting_changes = [path for path in changed if _is_allowed(path, known_existing_paths)]
    audited_changes = [path for path in changed if path not in preexisting_changes]
    out_of_scope = [path for path in audited_changes if not _is_allowed(path, allowed_paths)]
    report_findings = list(findings)
    report_findings.extend(
        {
            "classification": "OUT_OF_SCOPE",
            "description": "变化文件不在本次明确允许范围内",
            "evidence": f"git diff/status 检出: {path}",
            "related_files": [path],
            "blocking_current_feature": False,
            "suggested_follow_up": "将其保留在原任务，或另建并批准范围明确的任务",
        }
        for path in out_of_scope
    )

    status_after = git(project_root, "status", "--porcelain=v1", "--untracked-files=all")
    if status_before != status_after:
        raise WorktreeChangedError("审计期间工作区发生变化；报告已作废，请在稳定工作区重新运行")

    has_blocker = any(
        item["classification"] in {"BLOCKER", "INSUFFICIENT_EVIDENCE"} for item in report_findings
    )
    recommended_level = "full" if full_regression_reasons else "impact"
    return {
        "audit_baseline": baseline,
        "current_head": current_head,
        "changed_files": changed,
        "known_existing_scope": known_existing_paths,
        "preexisting_changes": preexisting_changes,
        "audited_changed_files": audited_changes,
        "allowed_scope": allowed_paths,
        "allowed_changed_files": [path for path in audited_changes if _is_allowed(path, allowed_paths)],
        "out_of_scope_changes": out_of_scope,
        "declared_direct_dependencies": _safe_paths(project_root, dependencies, "--dependency"),
        "related_special_tests": _safe_paths(project_root, tests, "--test"),
        "related_configs": _safe_paths(project_root, configs, "--config"),
        "related_page_entries": _safe_paths(project_root, pages, "--page"),
        "related_runtime_data_paths": _safe_paths(project_root, data_paths, "--data-path"),
        "recommended_test_level": recommended_level,
        "full_regression_reasons": list(full_regression_reasons),
        "findings": report_findings,
        "allow_next_stage": not has_blocker and not out_of_scope,
        "next_stage_reason": "无阻断发现且无范围外变化" if not has_blocker and not out_of_scope else "存在 BLOCKER、证据不足或范围外变化",
        "read_only": True,
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True, help="本次审计的完整或可解析 Git SHA")
    parser.add_argument("--allow", required=True, nargs="+", metavar="PATH", help="明确允许修改的文件或目录")
    parser.add_argument("--known-existing", action="append", default=[], metavar="PATH", help="preflight 已记录、非本任务的稳定原有修改")
    parser.add_argument("--dependency", action="append", default=[], metavar="PATH")
    parser.add_argument("--test", action="append", default=[], metavar="PATH")
    parser.add_argument("--config", action="append", default=[], metavar="PATH")
    parser.add_argument("--page", action="append", default=[], metavar="PATH")
    parser.add_argument("--data-path", action="append", default=[], metavar="PATH")
    parser.add_argument("--finding", action="append", default=[], metavar="RECORD")
    parser.add_argument("--full-regression-reason", action="append", choices=FULL_REGRESSION_REASONS, default=[])
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        findings = [parse_finding(value) for value in args.finding]
        report = run_audit(
            PROJECT_ROOT,
            args.baseline,
            args.allow,
            dependencies=args.dependency,
            tests=args.test,
            configs=args.config,
            pages=args.page,
            data_paths=args.data_path,
            known_existing=args.known_existing,
            findings=findings,
            full_regression_reasons=args.full_regression_reason,
        )
    except (RuntimeError, ValueError, WorktreeChangedError) as exc:
        print(f"审计失败: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["allow_next_stage"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
