"""Fresh-remote project preflight; create an isolated feature only with --create."""
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
    # No execution SHA is accepted from a chat or CLI parameter.
    registry.git(root, "fetch", "origin")
    live = registry.git(root, "ls-remote", "--exit-code", "origin", "refs/heads/main").split()[0]
    if registry.git(root, "rev-parse", "origin/main") != live:
        raise ValueError("REMOTE_MOVED_DURING_PREFLIGHT")
    mirror = registry.assert_main_mirror(root)
    data, project = registry.select_project(root, project_id)
    trusted = json.loads(registry.git(root, "show", f"origin/main:{registry.REGISTRY_PATH}"))
    if trusted != data:
        raise ValueError("REGISTRY_NOT_APPROVED_ON_ORIGIN_MAIN")
    if project["change_class"] != change_class or project["status"] != "ready":
        raise ValueError("PROJECT_CLASS_OR_READINESS_REQUIRES_SEPARATE_APPROVAL")
    if not branch.startswith("feat/") or branch == "feat/":
        raise ValueError("New business development requires feat/<name>")
    registry.git(root, "check-ref-format", "--branch", branch)
    if registry.git(root, "branch", "--list", branch):
        raise ValueError("BRANCH_ALREADY_EXISTS")
    if not destination.is_absolute():
        raise ValueError("WORKTREE_DESTINATION_MUST_BE_ABSOLUTE")
    destination = destination.resolve()
    workspaces = [Path(line[9:]).resolve() for line in registry.git(root,"worktree","list","--porcelain").splitlines() if line.startswith("worktree ")]
    if destination.exists() or any(p == destination or p in destination.parents for p in workspaces):
        raise ValueError("WORKTREE_DESTINATION_MUST_BE_NEW_AND_OUTSIDE_CALLER")
    result = {"project_id":project_id,"change_class":change_class,"baseline_head":live,
              "baseline_tree":registry.git(root,"rev-parse","origin/main^{tree}"),
              "branch":branch,"worktree":str(destination),"owned_paths":project["owned_paths"],
              "required_tests":project["required_tests"],"created":False,**mirror}
    result.update(reserved_paths=project.get("reserved_paths", []), future_owned_paths=project.get("future_owned_paths", []),
                  future_required_tests=project.get("future_required_tests", []))
    if create:
        # Recheck drift at the mutation boundary; never update the main checkout.
        if registry.git(root,"ls-remote","--exit-code","origin","refs/heads/main").split()[0] != live:
            raise ValueError("REMOTE_MOVED_BEFORE_CREATE")
        registry.assert_main_mirror(root)
        registry.git(root,"worktree","add","-b",branch,str(destination),live)
        result["created"] = True
        if registry.git(destination,"rev-parse","HEAD") != live or registry.git(destination,"status","--porcelain=v1"):
            raise ValueError("NEW_WORKTREE_IDENTITY_FAILED; preserve worktree and stop")
    return result


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
