"""Fresh-remote START / RESUME; --create starts a missing isolated feature."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

try:
    from . import project_registry as registry
except ImportError:
    import project_registry as registry

ROOT = Path(__file__).resolve().parents[2]


def prepare(root: Path, project_id: str, branch: str, destination: Path, *, change_class="business", create=False) -> dict:
    from importlib import import_module
    scope = import_module((__package__ + "." if __package__ else "") + "audit_changed_scope")
    registry.git(root, "fetch", "origin")
    live = registry.git(root, "ls-remote", "--exit-code", "origin", "refs/heads/main").split()[0]
    if registry.git(root, "rev-parse", "origin/main") != live:
        raise ValueError("REMOTE_MOVED_DURING_PREFLIGHT")
    mirror = registry.assert_main_mirror(root) if change_class != "business" else {}
    trusted = json.loads(registry.git(root, "show", f"origin/main:{registry.REGISTRY_PATH}"))
    if not branch.startswith("feat/") or branch == "feat/":
        raise ValueError("New business development requires feat/<name>")
    registry.git(root, "check-ref-format", "--branch", branch)
    if not destination.is_absolute():
        raise ValueError("WORKTREE_DESTINATION_MUST_BE_ABSOLUTE")
    destination = destination.resolve()
    workspaces = {}
    for block in registry.git(root, "worktree", "list", "--porcelain").split("\n\n"):
        lines = block.splitlines()
        path = next((Path(line[9:]).resolve() for line in lines if line.startswith("worktree ")), None)
        if path:
            workspaces[path] = next((line[7:] for line in lines if line.startswith("branch ")), None)
    exists = bool(registry.git(root, "branch", "--list", branch))
    resumed = destination in workspaces
    if resumed:
        if workspaces[destination] != "refs/heads/" + branch or not exists:
            raise ValueError("WORKTREE_BRANCH_IDENTITY_CONFLICT")
        binding = registry.git(root, "config", "--list")
        bindings = dict(line.split("=", 1) for line in binding.splitlines() if "=" in line)
        if bindings.get("branch." + branch + ".project-id", project_id) != project_id:
            raise ValueError("WORKTREE_PROJECT_IDENTITY_CONFLICT")
        if registry.git(destination, "rev-parse", "--show-toplevel").replace("\\", "/").casefold() != destination.as_posix().casefold():
            raise ValueError("WORKTREE_IDENTITY_CONFLICT")
        data, project = registry.select_project(destination, project_id)
    else:
        if exists:
            raise ValueError("BRANCH_ALREADY_EXISTS: worktree identity conflict")
        if destination.exists() or any(p in destination.parents for p in workspaces):
            raise ValueError("WORKTREE_DESTINATION_MUST_BE_NEW_AND_OUTSIDE_CALLER")
        data = trusted
        project = next((p for p in trusted["projects"] if p["project_id"] == project_id), None)
    bootstrap = project_id not in {p["project_id"] for p in trusted["projects"]}
    if bootstrap:
        if change_class != "business":
            raise ValueError("ESCALATION_REQUIRED: bootstrap is ordinary business only")
        if not resumed:
            project = registry.bootstrap_record(project_id, trusted["protected_paths"])
            if trusted['schema_version'] == 'project-registry/3':
                project.pop('runtime_target')
            data = dict(trusted, projects=trusted["projects"] + [project])
        base_files = registry.git(root, "ls-tree", "-r", "--name-only", "-z", live).split("\0")
        registry.validate_bootstrap(trusted, data, [p for p in base_files if p], [],
                                    [registry.REGISTRY_PATH], scope.SHARED_PATH_PATTERNS, require_tests=False)
    elif (data != trusted and not (resumed and change_class == "business"
          and not scope.requires_main_mirror(project)
          and scope.unchanged_registry_before_main_additions(destination, data, trusted))):
        raise ValueError("ESCALATION_REQUIRED: existing Registry changed")
    if project["change_class"] != change_class or project["status"] != "ready":
        raise ValueError("PROJECT_CLASS_OR_READINESS_REQUIRES_SEPARATE_APPROVAL")
    if scope.requires_main_mirror(project) and not mirror:
        mirror = registry.assert_main_mirror(root)
    if resumed:
        report = scope.run_audit(destination, "origin/main", project["owned_paths"] + project.get("reserved_paths", []),
                                 exact_allowed=project.get("future_owned_paths", []), change_class=change_class,
                                 shared_patterns=scope.SHARED_PATH_PATTERNS + tuple(
                                     pattern for path in trusted["protected_paths"] for pattern in (path, path + "/**")))
        if not report['allow_next_stage']:
            raise ValueError("UNKNOWN_DIRTY_STATE_OR_OWNERSHIP_CONFLICT")
        for name in report['changed_files']:
            if (destination / name).exists():
                registry.future_file(destination, name, exact_file=False)
    if not resumed and create:
        if registry.git(root, "ls-remote", "--exit-code", "origin", "refs/heads/main").split()[0] != live:
            raise ValueError("REMOTE_MOVED_BEFORE_CREATE")
        if scope.requires_main_mirror(project):
            registry.assert_main_mirror(root)
        registry.git(root, "worktree", "add", "-b", branch, str(destination), live)
        if registry.git(destination, "rev-parse", "HEAD") != live or registry.git(destination, "status", "--porcelain=v1"):
            raise ValueError("NEW_WORKTREE_IDENTITY_FAILED; preserve worktree and stop")
        if bootstrap:
            (destination / registry.REGISTRY_PATH).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    active = destination if resumed or create else root
    if resumed or create:
        registry.git(root, "config", "branch." + branch + ".project-id", project_id)
    changes = scope._changed_paths(active, "origin/main", scope._git) if resumed or create else []
    return {"project_id": project_id, "change_class": change_class,
            "action": "RESUMED" if resumed else "STARTED" if create else "PREFLIGHT",
            "auto_bootstrap": bootstrap, "created": create and not resumed,
            "baseline_head": live, "baseline_tree": registry.git(root, "rev-parse", "origin/main^{tree}"),
            "head": registry.git(active, "rev-parse", "HEAD"), "tree": registry.git(active, "rev-parse", "HEAD^{tree}"),
            "branch": branch, "worktree": str(destination), "changed_files": changes,
            "git_common_dir": registry.git(active, "rev-parse", "--path-format=absolute", "--git-common-dir"),
            "merge_base": registry.git(active, "merge-base", "HEAD", "origin/main"),
            "owned_paths": project["owned_paths"], "reserved_paths": project.get("reserved_paths", []),
            "future_owned_paths": project.get("future_owned_paths", []), "required_tests": project["required_tests"],
            "future_required_tests": project.get("future_required_tests", []), **mirror}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project",required=True)
    parser.add_argument("--branch",required=True)
    parser.add_argument("--worktree",required=True,type=Path)
    parser.add_argument("--change-class",choices=("business","shared"),default="business")
    parser.add_argument("--create",action="store_true")
    args=parser.parse_args(argv)
    try:
        result=prepare(ROOT,args.project,args.branch,args.worktree,change_class=args.change_class,create=args.create)
    except (ValueError,OSError) as exc:
        print(json.dumps({"PROJECT_START":"FAIL","reason":str(exc)},ensure_ascii=False))
        return 1
    print(json.dumps({"PROJECT_START":"PASS",**result},ensure_ascii=False,indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
