"""Read-only Project Scope Gate for an explicit Git baseline and owned paths."""

from __future__ import annotations

import argparse
import fnmatch
import json
import subprocess
import sys
from pathlib import Path
from typing import Callable, Sequence

try:
    from . import project_registry
except ImportError:  # Direct CLI invocation
    import project_registry


PROJECT_ROOT = Path(__file__).resolve().parents[2]
FINDING_CLASSES = ("BLOCKER", "FOLLOW_UP", "OUT_OF_SCOPE", "INSUFFICIENT_EVIDENCE")
CHANGE_CLASSES = ("business", "shared")
# Minimum protections cannot be removed by a project's owned declaration/registry.
SHARED_PATH_PATTERNS = (
    "AGENTS.md",
    "**/AGENTS.md",
    "02_configs/project_registry.json",
    "02_configs/lutou_domestic_basis.yaml",
    "03_src/agri_research_agent/pipelines/async_contract_rollout.py",
    "03_src/agri_research_agent/pipelines/lutou_domestic_basis.py",
    "03_src/agri_research_agent/pipelines/tankan_*",
    "03_src/agri_research_agent/pipelines/lutou_*",
    "02_configs/app_catalog.yaml",
    "02_configs/public_*",
    "02_configs/*weather*.yaml",
    "02_configs/module_test_map.yaml",
    "03_src/agri_research_agent/shared/**",
    "03_src/agri_research_agent/market_data/**",
    "03_src/agri_research_agent/automation/**",
    "03_src/agri_research_agent/data_sources/lutou/**",
    "03_src/agri_research_agent/data_sources/tankan/**",
    "03_src/agri_research_agent/pipelines/public_*",
    "03_src/agri_research_agent/pipelines/lutou_weather.py",
    "04_scripts/quality/**",
    "04_scripts/automation/**",
    "04_scripts/weather/**",
    "04_scripts/*public*",
    "07_docs/0[0-6]_*.md",
    "07_docs/templates/**",
    "08_tests/shared/**",
    "08_tests/market_data/**",
    "08_tests/data_sources/lutou/**",
    "08_tests/data_sources/tankan/**",
    "08_tests/pipelines/test_public_*",
    "08_tests/pipelines/test_full_daily_windows_wrapper.py",
    "08_tests/pipelines/test_lutou_weather.py",
    "08_tests/test_*weather*",
    "08_tests/test_public_*",
    "08_tests/test_quality_controls.py",
    "05_apps/streamlit_app.py",
    "09_deploy/**",
    "Dockerfile",
    "docker-compose*.yml",
    "requirements*.txt",
    "pyproject.toml",
)
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
        git(project_root, "diff", "--name-only", "--no-renames", f"{baseline}...HEAD"),
        git(project_root, "diff", "--name-only", "--no-renames", "--cached"),
        git(project_root, "diff", "--name-only", "--no-renames"),
        "\n".join(_status_paths(git(project_root, "status", "--porcelain=v1", "--no-renames", "--untracked-files=all"))),
    )
    return sorted({_decode_git_path(line).replace("\\", "/") for source in sources for line in source.splitlines() if line})


def _is_allowed(path: str, allowed: Sequence[str]) -> bool:
    return any(project_registry.under_path(path, item) for item in allowed)


def _is_shared(path: str, patterns: Sequence[str]) -> bool:
    return any(fnmatch.fnmatchcase(path.replace("\\", "/").casefold(), pattern.casefold()) for pattern in patterns)


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
    exact_allowed: Sequence[str] = (),
    dependencies: Sequence[str] = (),
    tests: Sequence[str] = (),
    configs: Sequence[str] = (),
    pages: Sequence[str] = (),
    data_paths: Sequence[str] = (),
    known_existing: Sequence[str] = (),
    findings: Sequence[dict[str, object]] = (),
    full_regression_reasons: Sequence[str] = (),
    change_class: str = "business",
    shared_patterns: Sequence[str] = SHARED_PATH_PATTERNS,
    git: Callable[..., str] = _git,
) -> dict[str, object]:
    """Return a JSON-ready read-only report; fail if the worktree moves mid-audit."""
    git(project_root, "rev-parse", "--verify", f"{baseline}^{{commit}}")
    current_head = git(project_root, "rev-parse", "HEAD").strip()
    status_before = git(project_root, "status", "--porcelain=v1", "--no-renames", "--untracked-files=all")

    allowed_paths = _safe_paths(project_root, allowed, "--allow")
    exact_keys = {project_registry.canonical_path(p) for p in exact_allowed}
    for value in exact_allowed:
        project_registry.future_file(project_root, value)
    def permitted(path):
        return _is_allowed(path, allowed_paths) or project_registry.canonical_path(path) in exact_keys
    if not allowed_paths and not exact_keys:
        raise ValueError("至少提供一个 --owned/--allow 文件或目录")
    if change_class not in CHANGE_CLASSES:
        raise ValueError(f"未知 change class: {change_class}")
    known_existing_paths = _safe_paths(project_root, known_existing, "--known-existing")
    changed = _changed_paths(project_root, baseline, git)
    preexisting_changes = [path for path in changed if _is_allowed(path, known_existing_paths)]
    audited_changes = [path for path in changed if path not in preexisting_changes]
    out_of_scope = [path for path in audited_changes if not permitted(path)]
    # Shared changes can never be hidden with --known-existing. An isolated feature
    # worktree containing any protected change must classify the whole task as shared.
    shared_changes = [path for path in changed if _is_shared(path, shared_patterns)]
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
    if change_class == "business" and shared_changes:
        report_findings.append({
            "classification": "SHARED_CHANGE",
            "description": "普通业务 feature 修改了 shared、Weather producer、Public refresh 或部署基础设施",
            "evidence": "Project Scope Gate 检出 shared paths",
            "related_files": shared_changes,
            "blocking_current_feature": True,
            "suggested_follow_up": "停止并另建明确分类为 shared infrastructure change 的任务",
        })

    status_after = git(project_root, "status", "--porcelain=v1", "--no-renames", "--untracked-files=all")
    if status_before != status_after or current_head != git(project_root, "rev-parse", "HEAD").strip():
        raise WorktreeChangedError("审计期间工作区发生变化；报告已作废，请在稳定工作区重新运行")

    has_blocker = any(
        item["classification"] in {"BLOCKER", "INSUFFICIENT_EVIDENCE", "SHARED_CHANGE"}
        for item in report_findings
    )
    recommended_level = "full" if full_regression_reasons else "impact"
    allow_next_stage = not has_blocker and not out_of_scope
    return {
        "audit_baseline": baseline,
        "comparison": f"{baseline}...HEAD",
        "current_head": current_head,
        "change_class": change_class,
        "PROJECT_SCOPE": "PASS" if allow_next_stage else "FAIL",
        "SHARED_CHANGE": "YES" if shared_changes else "NO",
        "changed_files": changed,
        "known_existing_scope": known_existing_paths,
        "preexisting_changes": preexisting_changes,
        "audited_changed_files": audited_changes,
        "allowed_scope": allowed_paths,
        "owned_paths": allowed_paths,
        "future_owned_paths": list(exact_allowed),
        "allowed_changed_files": [path for path in audited_changes if permitted(path)],
        "out_of_scope_changes": out_of_scope,
        "shared_changes": shared_changes,
        "declared_direct_dependencies": _safe_paths(project_root, dependencies, "--dependency"),
        "related_special_tests": _safe_paths(project_root, tests, "--test"),
        "related_configs": _safe_paths(project_root, configs, "--config"),
        "related_page_entries": _safe_paths(project_root, pages, "--page"),
        "related_runtime_data_paths": _safe_paths(project_root, data_paths, "--data-path"),
        "recommended_test_level": recommended_level,
        "full_regression_reasons": list(full_regression_reasons),
        "findings": report_findings,
        "allow_next_stage": allow_next_stage,
        "next_stage_reason": "项目范围通过且无阻断发现" if allow_next_stage else "存在阻断、shared change 或范围外变化",
        "read_only": True,
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", default="origin/main", help="merge-base 比较基线，默认 origin/main")
    parser.add_argument("--change-class", choices=CHANGE_CLASSES, default="business", help="普通业务变更或经明确批准的 shared infrastructure change")
    parser.add_argument("--project", help="Project Registry 中的 project_id；普通业务 CLI 必填")
    parser.add_argument("--owned", "--allow", dest="allowed", nargs="+", metavar="PATH", help="低层测试/明确批准 shared 任务的范围；不可扩大 business registry")
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
        registry = None
        project = None
        protected = SHARED_PATH_PATTERNS
        allowed = args.allowed
        exact_allowed = []
        if args.project:
            if args.baseline != "origin/main" or args.known_existing:
                raise ValueError("Project mode requires origin/main and cannot exempt known-existing changes")
            registry, project = project_registry.select_project(PROJECT_ROOT, args.project)
            if project["change_class"] != args.change_class:
                raise ValueError("Registry change_class cannot be overridden; shared tasks require explicit --change-class shared")
            if project["status"] != "ready":
                raise ValueError(f"Project is {project['status']}; scope registration is not unfreeze approval")
            if args.change_class == "business":
                if allowed:
                    raise ValueError("Business --project cannot override --owned")
                trusted = project_registry.git(PROJECT_ROOT, "show", f"origin/main:{project_registry.REGISTRY_PATH}")
                if json.loads(trusted) != registry:
                    raise ValueError("Business registry differs from origin/main; separate shared approval required")
            if allowed and any(not project_registry.owns(project, path) for path in allowed):
                raise ValueError("--owned may only narrow registry scope")
            if allowed:
                exact_allowed = [p for p in allowed if not _is_allowed(p, project["owned_paths"])]
                allowed = [p for p in allowed if p not in exact_allowed]
            else:
                allowed = project["owned_paths"]
                exact_allowed = project.get("future_owned_paths", [])
            protected += tuple(pattern for path in registry["protected_paths"] for pattern in (path, path + "/**"))
            project_registry.assert_main_mirror(PROJECT_ROOT)
            branch = project_registry.git(PROJECT_ROOT, "branch", "--show-current")
            if not branch or branch == "main":
                raise ValueError("Project Gate requires independent non-main branch/worktree")
        elif args.change_class == "business":
            raise ValueError("Business CLI requires --project; --owned is a low-level shared/test interface")
        findings = [parse_finding(value) for value in args.finding]
        report = run_audit(
            PROJECT_ROOT,
            args.baseline,
            allowed or [],
            exact_allowed=exact_allowed,
            dependencies=args.dependency,
            tests=args.test,
            configs=args.config,
            pages=args.page,
            data_paths=args.data_path,
            known_existing=args.known_existing,
            findings=findings,
            full_regression_reasons=args.full_regression_reason,
            change_class=args.change_class,
            shared_patterns=protected,
        )
        if project:
            forbidden = [p for p in report["changed_files"] if _is_allowed(p, project["forbidden_paths"])]
            if forbidden:
                report["PROJECT_SCOPE"] = "FAIL"
                report["allow_next_stage"] = False
                report["next_stage_reason"] = "Registry forbidden paths changed"
            report.update(project_id=args.project, registry=project_registry.REGISTRY_PATH,
                          forbidden_changes=forbidden, required_tests=project["required_tests"],
                          future_required_tests=project.get("future_required_tests", []),
                          shared_dependencies=project["shared_dependencies"])
    except (RuntimeError, ValueError, WorktreeChangedError) as exc:
        print(f"审计失败: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["allow_next_stage"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
