"""Small, fail-closed development project registry (not an authorization service)."""
from __future__ import annotations

import json
import fnmatch
import re
import subprocess
from pathlib import Path, PurePosixPath

REGISTRY_PATH = "02_configs/project_registry.json"


def canonical_path(value: str) -> str:
    """Portable Windows-safe identity; registry existing-path syntax stays strict."""
    if not isinstance(value, str):
        raise ValueError("Invalid path type")
    value = relative_path(value.replace("\\", "/"))
    for part in value.split("/"):
        if (part.endswith((".", " ")) or any(ord(c) < 32 or c in '<>"|' for c in part)
                or re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", part)):
            raise ValueError(f"Ambiguous Windows path: {value}")
    return value.casefold()


def under_path(value: str, parent: str) -> bool:
    value, parent = canonical_path(value), canonical_path(parent)
    return value == parent or value.startswith(parent + "/")


def future_file(root: Path, value: str, *, test: bool = False, exact_file: bool = True) -> Path:
    """Exact file only, even after creation; never traverse links or junctions."""
    key = canonical_path(value)
    if (exact_file and not PurePosixPath(key).suffix) or key.startswith(".git/"):
        raise ValueError(f"Future path must name an exact file: {value}")
    if test and not (key.startswith("08_tests/") and PurePosixPath(key).name.startswith("test_") and key.endswith(".py")):
        raise ValueError(f"Future test outside test area: {value}")
    current = root.resolve()
    for part in value.replace("\\", "/").split("/"):
        # Resolve case aliases on case-sensitive hosts too, for portable governance.
        matches = [p for p in current.iterdir() if p.name.casefold() == part.casefold()] if current.is_dir() else []
        if len(matches) > 1:
            raise ValueError(f"Ambiguous case aliases: {value}")
        current = matches[0] if matches else current / part
        if current.is_symlink() or current.is_junction():
            raise ValueError(f"Future path traverses link: {value}")
    if root.resolve() not in current.resolve().parents or (current.exists() and not current.is_file()):
        raise ValueError(f"Future path is not a repository file: {value}")
    return current


def reserved_directory(root: Path, value: str) -> Path:
    """An explicit subtree: only its leaf may be absent; never create it."""
    relative_path(value)
    key = canonical_path(value)
    parts = key.split('/')
    if (len(parts) < 2 or any(p.startswith('.') for p in parts)
            or key in ('03_src/agri_research_agent', '07_docs/projects')
            or PurePosixPath(key).suffix):
        raise ValueError(f"Overbroad or invalid reserved directory: {value}")
    current = root.resolve()
    for index, part in enumerate(value.split('/')):
        matches = [p for p in current.iterdir() if p.name.casefold() == part.casefold()]
        if len(matches) > 1:
            raise ValueError(f"Ambiguous case aliases: {value}")
        current = matches[0] if matches else current / part
        if current.is_symlink() or current.is_junction():
            raise ValueError(f"Reserved path traverses link: {value}")
        if not current.exists():
            if index != len(parts) - 1:
                raise ValueError(f"Reserved parent namespace missing: {value}")
        elif not current.is_dir():
            raise ValueError(f"Reserved namespace is not a directory: {value}")
    if root.resolve() not in current.resolve().parents:
        raise ValueError(f"Reserved namespace outside repository: {value}")
    return current


def ownership_paths(project: dict) -> list[str]:
    return project['owned_paths'] + project.get('future_owned_paths', []) + project.get('reserved_paths', [])


def overlaps(left: str, right: str) -> bool:
    return under_path(left, right) or under_path(right, left)


def owns(project: dict, value: str) -> bool:
    return (any(under_path(value, p) for p in project["owned_paths"] + project.get("reserved_paths", []))
            or canonical_path(value) in {canonical_path(p) for p in project.get("future_owned_paths", [])})


def relative_path(value: str) -> str:
    if (not isinstance(value, str) or not value or value != value.strip()
            or "\\" in value or ":" in value or any(c in value for c in "*?[]\x00\r\n")
            or value.startswith("/") or any(p in ("", ".", "..") for p in value.split("/"))):
        raise ValueError(f"Invalid registry relative path: {value!r}")
    return PurePosixPath(value).as_posix()


def validate_registry(data: dict, root: Path) -> dict:
    if not isinstance(data, dict) or data.get("schema_version") not in ("project-registry/1", "project-registry/2", "project-registry/3", "project-registry/4"):
        raise ValueError("Invalid project registry schema")
    required_root = {"schema_version", "protected_paths", "projects"}
    optional_root = {"legacy_registry_commit"} if data["schema_version"] == "project-registry/4" else set()
    if not required_root <= set(data) or set(data) - required_root - optional_root:
        raise ValueError("Unexpected registry fields")
    if not isinstance(data["projects"], list) or not data["projects"]:
        raise ValueError("Registry projects must be nonempty")
    def paths(values):
        if not isinstance(values, list) or not all(isinstance(v,str) for v in values) or len(values) != len(set(values)):
            raise ValueError("Registry paths must be a unique list")
        for value in values:
            relative_path(value)
            resolved = (root / value).resolve()
            if root.resolve() not in resolved.parents or not resolved.exists():
                raise ValueError(f"Registry path missing or outside repository: {value}")
    paths(data["protected_paths"])
    legacy_records = {}
    if "legacy_registry_commit" in data:
        commit = data["legacy_registry_commit"]
        if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise ValueError("legacy_registry_commit must be an exact commit SHA")
        if git(root, "cat-file", "-t", commit) != "commit":
            raise ValueError("legacy_registry_commit is not a commit")
        git(root, "merge-base", "--is-ancestor", commit, "origin/main")
        legacy = json.loads(git(root, "show", f"{commit}:{REGISTRY_PATH}"))
        if not isinstance(legacy, dict) or legacy.get("schema_version") not in {"project-registry/1", "project-registry/2", "project-registry/3"}:
            raise ValueError("Legacy source must be Registry/1-3")
        validate_registry(legacy, root)
        trusted = json.loads(git(root, "show", f"origin/main:{REGISTRY_PATH}"))
        current_records = {p['project_id']: p for p in trusted['projects']}
        # A historical record cannot resurrect, unfreeze or downgrade a current record.
        legacy_records = {p['project_id']: p for p in legacy['projects']
                          if p == current_records.get(p['project_id']) and 'runtime_target' not in p}
    seen = set()
    keys = {"project_id", "change_class", "status", "owned_paths", "shared_dependencies",
            "forbidden_paths", "required_tests", "capabilities", "boundary_notes"}
    for item in data["projects"]:
        optional = {"future_owned_paths", "future_required_tests"} if data["schema_version"] != "project-registry/1" else set()
        if data["schema_version"] in ("project-registry/3", "project-registry/4"):
            optional.add("reserved_paths")
        runtime_keys = {"runtime_target", "runtime_contract"} if data["schema_version"] == "project-registry/4" else set()
        if not isinstance(item, dict) or not keys <= set(item) or set(item) - keys - optional - runtime_keys:
            raise ValueError("Invalid project fields")
        pid = item["project_id"]
        if not isinstance(pid, str) or not re.fullmatch(r"[a-z][a-z0-9-]*", pid) or pid in seen:
            raise ValueError("Invalid or duplicate project_id")
        seen.add(pid)
        if data["schema_version"] == "project-registry/4":
            unchanged_legacy = "runtime_target" not in item and item == legacy_records.get(pid)
            target = item.get("runtime_target")
            if not unchanged_legacy and (not isinstance(target, str) or target not in {"none", "library_only", "windows_git_worktree", "production_container"}):
                raise ValueError("Missing or unknown runtime_target")
            if target == "production_container":
                try:
                    from . import target_runtime_gate
                except ImportError:
                    import target_runtime_gate
                target_runtime_gate.read_contract(root, item)
            elif "runtime_contract" in item:
                raise ValueError("runtime_contract requires production_container target")
        if item["change_class"] not in ("business", "shared"):
            raise ValueError("Invalid project change_class")
        if item["status"] not in ("ready", "frozen", "needs-boundary-review"):
            raise ValueError("Invalid project status")
        for key in ("owned_paths", "shared_dependencies", "forbidden_paths", "required_tests"):
            paths(item[key])
        for key in optional:
            values = item.get(key, [])
            if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
                raise ValueError("Future paths must be a list of strings")
            if len({canonical_path(p) for p in values}) != len(values):
                raise ValueError("Duplicate future path identity")
            for value in values:
                if key == "reserved_paths":
                    reserved_directory(root, value)
                else:
                    future_file(root, value, test=key == "future_required_tests")
        if item["status"] == "ready" and (not (item["owned_paths"] or item.get("future_owned_paths") or item.get("reserved_paths")) or not (item["required_tests"] or item.get("future_required_tests"))):
            raise ValueError("Ready project needs owned paths and tests")
        if not isinstance(item["capabilities"], list) or not all(isinstance(x,str) and x for x in item["capabilities"]):
            raise ValueError("Invalid capabilities")
        if not isinstance(item["boundary_notes"], str) or not item["boundary_notes"]:
            raise ValueError("Missing boundary evidence/limitations")
    for item in data["projects"]:
        for value in item.get("future_owned_paths", []):
            if any(canonical_path(value) != canonical_path(p) and (under_path(value, p) or under_path(p, value)) for p in item.get("future_owned_paths", [])):
                raise ValueError(f"Future exact files cannot contain one another: {value}")
            if any(under_path(value, p) for p in item["forbidden_paths"] + item["shared_dependencies"]):
                raise ValueError(f"Future ownership conflicts with read-only/forbidden scope: {value}")
            for other in data["projects"]:
                if other is not item and (owns(other, value) or any(under_path(p, value) or under_path(value, p) for p in other.get("future_owned_paths", []))):
                    raise ValueError(f"Future ownership collision with {other['project_id']}: {value}")
            if item["change_class"] == "business":
                try:
                    from .audit_changed_scope import SHARED_PATH_PATTERNS, _is_shared
                except ImportError:
                    from audit_changed_scope import SHARED_PATH_PATTERNS, _is_shared
                if any(under_path(value, p) for p in data["protected_paths"]) or _is_shared(canonical_path(value), SHARED_PATH_PATTERNS):
                    raise ValueError(f"Future business path is protected: {value}")
        for value in item.get("future_required_tests", []):
            if not owns(item, value):
                raise ValueError(f"Future required test not owned by project: {value}")
            if any(under_path(value, p) for p in item["forbidden_paths"] + item["shared_dependencies"]):
                raise ValueError(f"Future test conflicts with read-only/forbidden scope: {value}")
            if any(other is not item and owns(other, value) for other in data["projects"]):
                raise ValueError(f"Future test ownership collision: {value}")
    # All ownership kinds participate, including ordinary ancestor/descendant paths.
    for index, item in enumerate(data['projects']):
        for other in data['projects'][index + 1:]:
            for value in ownership_paths(item):
                if any(overlaps(value, p) for p in ownership_paths(other)):
                    raise ValueError(f"Ownership collision: {item['project_id']} / {other['project_id']}: {value}")
        for value in item.get('reserved_paths', []):
            if any(overlaps(value, p) for p in item['shared_dependencies'] + item['forbidden_paths']):
                raise ValueError(f"Reservation conflicts with read-only/forbidden scope: {value}")
            if item['change_class'] == 'business':
                try:
                    from .audit_changed_scope import SHARED_PATH_PATTERNS, _is_shared
                except ImportError:
                    from audit_changed_scope import SHARED_PATH_PATTERNS, _is_shared
                if any(overlaps(value, p) for p in data['protected_paths']) or _is_shared(value, SHARED_PATH_PATTERNS) or _is_shared(value + '/probe.py', SHARED_PATH_PATTERNS):
                    raise ValueError(f"Reserved business namespace is protected: {value}")
    return data


def load_registry(root: Path) -> dict:
    try:
        data = json.loads((root / REGISTRY_PATH).read_text(encoding="utf-8"))
        return validate_registry(data, root)
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError) as exc:
        raise ValueError(f"Project registry unavailable/invalid: {exc}") from exc


def select_project(root: Path, project_id: str) -> tuple[dict, dict]:
    registry = load_registry(root)
    for project in registry["projects"]:
        if project["project_id"] == project_id:
            return registry, project
    raise ValueError(f"Unknown project_id: {project_id}")


def git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, timeout=90)
    if result.returncode:
        raise ValueError(result.stderr.decode("utf-8", errors="replace").strip())
    return result.stdout.decode("utf-8").strip()


def assert_main_mirror(root: Path) -> dict:
    """Read every main checkout; never checkout, merge or reset main."""
    remote = git(root, "rev-parse", "origin/main")
    if git(root, "rev-parse", "refs/heads/main") != remote:
        raise ValueError("LOCAL_MAIN_NOT_MIRROR")
    mains = []
    for block in git(root, "worktree", "list", "--porcelain").split("\n\n"):
        lines = block.splitlines()
        if "branch refs/heads/main" in lines:
            path = Path(next(line[9:] for line in lines if line.startswith("worktree ")))
            if git(path, "status", "--porcelain=v1", "--untracked-files=all"):
                raise ValueError("LOCAL_MAIN_NOT_CLEAN")
            mains.append(str(path))
    if not mains:
        raise ValueError("MAIN_CHECKOUT_NOT_FOUND")
    return {"main_head": remote, "main_clean_mirror": True, "main_checkouts": mains}


def bootstrap_record(project_id: str, protected_paths=()) -> dict:
    """Fixed business-only metadata; no candidate-chosen authority or test policy."""
    if not isinstance(project_id, str) or not re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", project_id):
        raise ValueError("ESCALATION_REQUIRED: invalid bootstrap project identity")
    slug = project_id.replace("-", "_")
    return dict(project_id=project_id, change_class="business", status="ready",
                owned_paths=[], reserved_paths=[f"03_src/agri_research_agent/{slug}", f"08_tests/{slug}"],
                shared_dependencies=[], forbidden_paths=[p for p in protected_paths if p != REGISTRY_PATH], required_tests=[],
                future_required_tests=[f"08_tests/{slug}/test_project.py"], runtime_target="none",
                capabilities=["ordinary business source and tests"],
                boundary_notes="Auto bootstrap: own source/tests only; no shared, governance or production permissions.")


def validate_bootstrap(trusted: dict, candidate: dict, base_files, candidate_files,
                       changed_files, patterns, *, require_tests=True) -> tuple[dict, list[str]]:
    """Validate an exact append against trusted rules and Git/file identities."""
    def deny(reason):
        raise ValueError("ESCALATION_REQUIRED: " + reason)
    if trusted.get("schema_version") not in {"project-registry/3", "project-registry/4"}:
        deny("bootstrap requires existing reserved namespace schema")
    old = trusted["projects"]
    new = candidate.get("projects", [])
    if not isinstance(new, list) or len(new) != len(old) + 1 or new[:-1] != old:
        deny("existing Registry projects changed")
    if not isinstance(new[-1], dict):
        deny("invalid project record")
    project = bootstrap_record(new[-1].get("project_id"), trusted["protected_paths"])
    if trusted["schema_version"] == "project-registry/3":
        project.pop("runtime_target")
    if new[-1] != project or candidate != dict(trusted, projects=old + [project]):
        deny("noncanonical bootstrap metadata or Registry policy changed")
    if any(p["project_id"] == project["project_id"] for p in old):
        deny("project already exists")
    roots = project["reserved_paths"]
    for namespace in roots:
        if any(overlaps(namespace, p) for p in base_files):
            deny("namespace already contains trusted files")
        for other in old:
            if any(overlaps(namespace, p) for p in ownership_paths(other) + other["shared_dependencies"]):
                deny("namespace ownership/read-only conflict")
        if any(overlaps(namespace, p) for p in trusted["protected_paths"]):
            deny("protected namespace")
        if any(fnmatch.fnmatchcase(probe.casefold(), pattern.casefold())
               for probe in (namespace, namespace + "/probe.py") for pattern in patterns):
            deny("shared namespace")
    forbidden_names = {"agents.md", "conftest.py", "pytest.ini", "pyproject.toml", "setup.cfg", "setup.py",
                       ".gitattributes", ".gitmodules", "dockerfile"}
    for path in changed_files:
        if path == REGISTRY_PATH:
            continue
        canonical_path(path)
        if not any(under_path(path, root) for root in roots):
            deny("change outside new business namespace: " + path)
        if (any(part.startswith(".") for part in path.split("/"))
                or PurePosixPath(path).name.casefold() in forbidden_names
                or PurePosixPath(path).name.casefold().startswith(("requirements", "docker-compose"))
                or any(fnmatch.fnmatchcase(path.casefold(), pattern.casefold()) for pattern in patterns)):
            deny("governance/shared file in bootstrap namespace: " + path)
    tests = sorted(p for p in candidate_files if under_path(p, roots[1])
                   and PurePosixPath(p).name.startswith("test_") and p.endswith(".py"))
    if require_tests and (not tests or project["future_required_tests"][0] not in tests):
        deny("bootstrap requires test_project.py and all discovered test files")
    return project, tests


def local_bootstrap(root: Path, patterns, *, require_tests=False) -> tuple[dict, list[str]]:
    trusted = json.loads(git(root, "show", f"origin/main:{REGISTRY_PATH}"))
    candidate = json.loads((root / REGISTRY_PATH).read_text(encoding="utf-8"))
    base_files = git(root, "ls-tree", "-r", "--name-only", "-z", "origin/main").split("\0")
    files = [p for p in git(root, "ls-files", "--cached", "--others", "--exclude-standard", "-z").split("\0")
             if p and (root / p).exists()]
    changes = set()
    for args in [("diff", "--name-only", "--no-renames", "-z", "origin/main...HEAD"),
                 ("diff", "--name-only", "--no-renames", "-z", "HEAD"),
                 ("ls-files", "--others", "--exclude-standard", "-z")]:
        changes.update(p for p in git(root, *args).split("\0") if p)
    project, tests = validate_bootstrap(trusted, candidate, [p for p in base_files if p], files,
                                        changes, patterns, require_tests=require_tests)
    for p in files:
        if owns(project, p):
            future_file(root, p, exact_file=False)
    return project, tests
