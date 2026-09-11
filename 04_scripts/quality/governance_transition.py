"""GitHub-native maintainer transition approval; no candidate-side approval files.

The sole issuer is a successful workflow_dispatch of the trusted main workflow.
Its run-name binds base/commit/tree in GitHub's server-side run metadata. Never
trust a caller-provided actor, local receipt, branch name, or workflow title alone.
"""
from __future__ import annotations

import argparse
import json
import os
import re
from urllib.parse import quote
from urllib.request import Request, urlopen

REPOSITORY = "903798404-hub/commodity-research-system"
WORKFLOW = ".github/workflows/trusted-main-admission.yml"
MARKER = "TRUSTED_GOVERNANCE_TRANSITION_APPROVED"


def approval_title(base: str, candidate: str, tree: str) -> str:
    if not all(re.fullmatch(r"[0-9a-f]{40}", s) for s in (base, candidate, tree)):
        raise ValueError("FULL_TRANSITION_IDENTITY_REQUIRED")
    return "|".join((MARKER, base, candidate, tree))


def github_get(path: str) -> dict:
    if not path.startswith("/") or ".." in path or "?" in path:
        raise ValueError("INVALID_GITHUB_API_PATH")
    token = os.environ.get("GH_TOKEN", "")
    if not token:
        raise ValueError("GITHUB_READ_TOKEN_REQUIRED")
    request = Request("https://api.github.com/repos/" + REPOSITORY + path,
                      headers={"Authorization": "Bearer " + token,
                               "Accept": "application/vnd.github+json",
                               "X-GitHub-Api-Version": "2022-11-28"})
    with urlopen(request, timeout=30) as response:
        return json.load(response)


def validate_external_approval(base, candidate, tree, run_id, *, get=github_get, issuing=False):
    """All authority comes from fresh GitHub GETs. get injection is tests only."""
    if not re.fullmatch(r"[1-9][0-9]*", str(run_id or "")):
        raise ValueError("GOVERNANCE_TRANSITION_PENDING")
    expected_title = approval_title(base, candidate, tree)
    run = get("/actions/runs/" + str(run_id))
    if (run.get("repository", {}).get("full_name") != REPOSITORY
            or run.get("event") != "workflow_dispatch"
            or run.get("head_branch") != "main" or run.get("head_sha") != base
            or run.get("path") != WORKFLOW or run.get("display_title") != expected_title
            or run.get("run_attempt") != 1):
        raise ValueError("UNTRUSTED_TRANSITION_APPROVAL_RUN")
    if not issuing and (run.get("status") != "completed" or run.get("conclusion") != "success"):
        raise ValueError("GOVERNANCE_TRANSITION_PENDING")
    workflow = get("/actions/workflows/" + str(run["workflow_id"]))
    if workflow.get("path") != WORKFLOW or workflow.get("state") != "active":
        raise ValueError("UNTRUSTED_TRANSITION_APPROVAL_WORKFLOW")
    actors = [run.get("actor", {}), run.get("triggering_actor", {})]
    for actor in actors:
        if actor.get("type") != "User" or not actor.get("login"):
            raise ValueError("MAINTAINER_TRANSITION_APPROVAL_REQUIRED")
        permission = get("/collaborators/" + quote(actor["login"], safe="") + "/permission")
        if (permission.get("user", {}).get("id") != actor.get("id")
                or not (permission.get("permission") == "admin"
                        or permission.get("role_name") == "maintain")):
            raise ValueError("MAINTAINER_TRANSITION_APPROVAL_REQUIRED")
    if get("/git/ref/heads/main")["object"]["sha"] != base:
        raise ValueError("TRANSITION_BASE_CHANGED")
    commit = get("/git/commits/" + candidate)
    if commit.get("sha") != candidate or commit.get("tree", {}).get("sha") != tree:
        raise ValueError("TRANSITION_CANDIDATE_IDENTITY_CHANGED")
    return {"source": "github-trusted-main-workflow-dispatch", "run_id": int(run_id),
            "base": base, "candidate": candidate, "tree": tree,
            "maintainer": actors[0]["login"], "production_authorization": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--issue", action="store_true", required=True)
    args = parser.parse_args()
    if (os.environ.get("GITHUB_REPOSITORY") != REPOSITORY
            or os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
            or os.environ.get("GITHUB_REF") != "refs/heads/main"
            or os.environ.get("TRANSITION_INTENT") != MARKER):
        raise ValueError("TRUSTED_MAIN_MANUAL_DISPATCH_REQUIRED")
    base = os.environ["TRANSITION_BASE"]
    candidate = os.environ["TRANSITION_CANDIDATE"]
    tree = os.environ["TRANSITION_TREE"]
    if os.environ.get("GITHUB_SHA") != base:
        raise ValueError("TRANSITION_BASE_CHANGED")
    # The issuer loads ONLY trusted main code; it never executes candidate code.
    from pathlib import Path
    import importlib.util
    spec = importlib.util.spec_from_file_location("trusted_admission", Path(__file__).with_name("main_admission.py"))
    admission = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(admission)
    repo = Path(__file__).resolve().parents[2]
    old, new = admission.tree(repo, base), admission.tree(repo, candidate)
    registry = json.loads(admission.blob(repo, base, admission.REGISTRY))
    changed = admission.changed(repo, base, candidate, old, new)
    if not admission.is_governance_transition(changed, registry):
        raise ValueError("TRANSITION_NOT_AVAILABLE_TO_BUSINESS_OR_SHARED")
    result = validate_external_approval(base, candidate, tree,
                                       os.environ["GITHUB_RUN_ID"], issuing=True)
    print(json.dumps({"MAINTAINER_TRANSITION_APPROVAL": "PRESENT", **result}))


if __name__ == "__main__":
    main()
