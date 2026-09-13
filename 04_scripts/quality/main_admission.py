"""Required CI for Maintainer-reviewed candidate code and workflow.

This is evidence, not a signature or a branch-protection service. The caller must
authenticate repository/ref observations. V1 tests run on ephemeral GitHub hosted
runners with a stripped environment. Bubblewrap is optional runner hardening,
not a permanent admission prerequisite. Local tests use inert fixtures only.
"""
from __future__ import annotations

import argparse
import ast
import fnmatch
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
import yaml

REGISTRY = "02_configs/project_registry.json"
SCOPE = "04_scripts/quality/audit_changed_scope.py"
IMPLEMENTATION = "04_scripts/quality/main_admission.py"
SCHEMA = "02_configs/main_admission_result.schema.json"
WORKFLOW = ".github/workflows/trusted-main-admission.yml"
MAP = "02_configs/module_test_map.yaml"

TRUST_FILES = (REGISTRY, SCOPE, IMPLEMENTATION, SCHEMA, MAP,
               "04_scripts/quality/project_registry.py", "pyproject.toml",
               "requirements-dev.txt", WORKFLOW)
PRODUCTION = ("09_deploy", "04_scripts/automation", "04_scripts/runtime",
              "03_src/agri_research_agent/automation", "01_data", "06_outputs", "10_logs")

# Repository definitions are not observations of a live deployment. Keep this
# small role vocabulary independent of project ownership and execution grants.
_LIVE_DIRECTORIES = {'live', 'state', 'runtime', 'releases', 'evidence', 'credentials',
                     'grants', 'secrets', 'candidate-data', '.ssh'}
_LIVE_NAMES = {'release.json', 'release.manifest.json', 'deployment_result.json',
               'deployment_plan.json', 'candidate_result.json', 'current.json',
               'release_index.json', 'grant.json', 'production-policy.json'}
_STATE_SCHEMAS = ('controlled-runtime-', 'candidate-validation-record/',
                  'candidate-evidence-bundle/', 'host-runtime-policy/',
                  'production-execution-grant/', 'execution-grant/',
                  'production-release-request/', 'production-pre-release-validation/')
_TOOLING_SUFFIXES = {'.py', '.sh', '.ps1', '.json', '.yaml', '.yml', '.toml', '.conf',
                     '.ini', '.j2', '.jinja2', '.template', '.example'}


def production_artifact_role(path: str, source: bytes | None = None) -> str | None:
    """Classify Git artifacts, never authorize their execution on a server.

    Generated state wins over a source-looking extension. Structured state is
    inspected as data (never executed); schema definitions describe such state
    but do not contain an instance-level schema_version/grant/signature.
    Unknown deployment artifacts remain fail-closed. Python deployment commands
    are reviewed source; actually invoking them is a separate release action.
    """
    name = path.casefold().rsplit('/', 1)[-1]
    parts = path.casefold().split('/')
    if any(under(path.casefold(), p) for p in ('01_data', '06_outputs', '10_logs')):
        return 'PRODUCTION_MUTATION'
    if name.startswith('.env') or name.endswith('.env'):
        return 'PRODUCTION_MUTATION'
    if not any(under(path.casefold(), p) for p in PRODUCTION[:4]):
        return None
    if source and re.search(rb'-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----', source):
        return 'PRODUCTION_MUTATION'
    if name.endswith('.md'):
        return 'DOCUMENTATION'
    if (any(p in _LIVE_DIRECTORIES for p in parts[1:-1]) or name in _LIVE_NAMES
            or name.endswith(('.pem', '.key', '.p12', '.pfx', '.db', '.sqlite', '.sqlite3', '.parquet'))):
        # 04_scripts/runtime is the existing source package, not an allocated
        # runtime directory. Other nested runtime/state directories stay denied.
        if not (parts[:2] == ['04_scripts', 'runtime'] and
                not any(p in _LIVE_DIRECTORIES for p in parts[2:-1]) and
                name not in _LIVE_NAMES and Path(name).suffix in _TOOLING_SUFFIXES):
            return 'PRODUCTION_MUTATION'
    if source and name.endswith(('.json', '.yaml', '.yml', '.example', '.template')):
        try:
            value = json.loads(source) if name.endswith('.json') else yaml.safe_load(source)
        except (ValueError, UnicodeError, yaml.YAMLError):
            if name.endswith(('.json', '.yaml', '.yml')):
                return 'PRODUCTION_MUTATION'  # Unknown structured artifact, not established source.
            value = None  # Non-JSON environment/template syntax is legitimate.
        def state_instance(item):
            if isinstance(item, dict):
                version = item.get('schema_version')
                if isinstance(version, str) and version.startswith(_STATE_SCHEMAS):
                    return True
                if (isinstance(item.get('signature'), str) and 'payload' in item
                        or isinstance(item.get('private_key'), str)):
                    return True
                if (all(isinstance(item.get(k), str) for k in ('container_id', 'image_id'))
                        or all(isinstance(item.get(k), str) for k in ('release_id', 'generated_at'))):
                    return True
                return any(state_instance(v) for v in item.values())
            return isinstance(item, list) and any(state_instance(v) for v in item)
        if state_instance(value):
            return 'PRODUCTION_MUTATION'
    if Path(name).suffix in _TOOLING_SUFFIXES or name == 'dockerfile' or name.startswith('dockerfile.'):
        return 'PRODUCTION_TOOLING_SOURCE_CHANGE'
    return 'PRODUCTION_MUTATION'


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def git(repo: Path, *args: str) -> bytes:
    result = subprocess.run(["git", "-c", "core.quotepath=false", *args], cwd=repo,
                            capture_output=True, timeout=120, check=False)
    if result.returncode:
        raise ValueError("GIT_OBJECT_ERROR: " + result.stderr.decode("utf-8", "replace")[:500])
    return result.stdout


def text(repo: Path, *args: str) -> str:
    return git(repo, *args).decode("utf-8", "strict").strip()


def blob(repo: Path, commit: str, path: str) -> bytes:
    return git(repo, "show", f"{commit}:{path}")


def canonical(path: str) -> str:
    if (not path or path.startswith("/") or "\\" in path or ":" in path
            or any(c in path for c in "\x00\r\n*?[]")
            or any(p in ("", ".", "..") or p.endswith((".", " ")) for p in path.split("/"))):
        raise ValueError("UNSAFE_GIT_PATH")
    for part in path.split("/"):
        if part.casefold() == ".git" or re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", part):
            raise ValueError("UNSAFE_GIT_PATH")
    return path.casefold()


def under(path: str, root: str) -> bool:
    p, r = canonical(path), canonical(root)
    return p == r or p.startswith(r + "/")


def owns(project: dict, path: str) -> bool:
    return (any(under(path, p) for p in project["owned_paths"] + project.get("reserved_paths", []))
            or canonical(path) in {canonical(p) for p in project.get("future_owned_paths", [])})


def tree(repo: Path, commit: str) -> dict:
    entries = {}
    aliases = set()
    for item in git(repo, "ls-tree", "-r", "-z", commit).split(b"\0"):
        if not item:
            continue
        metadata, name = item.split(b"\t", 1)
        mode, kind, oid = metadata.decode("ascii").split()
        path = name.decode("utf-8", "strict")
        key = canonical(path)
        if key in aliases:
            raise ValueError("AMBIGUOUS_CASE_PATH")
        aliases.add(key)
        entries[path] = {"mode": mode, "kind": kind, "oid": oid}
    return entries


def changed(repo: Path, base: str, candidate: str, old: dict, new: dict) -> list:
    tokens = git(repo, "diff", "--name-status", "-z", "--find-renames", base, candidate, "--").split(b"\0")
    output, index = [], 0
    while index < len(tokens) and tokens[index]:
        status = tokens[index].decode("ascii")
        index += 1
        paths = [tokens[index].decode("utf-8", "strict")]
        index += 1
        if status.startswith(("R", "C")):
            paths.append(tokens[index].decode("utf-8", "strict"))
            index += 1
        for path in paths:
            output.append({"path": path, "status": status, "rename_paths": paths if len(paths) > 1 else [],
                           "old_mode": old.get(path, {}).get("mode"),
                           "new_mode": new.get(path, {}).get("mode"), "classifications": []})
    return output


def scope_patterns(source: bytes) -> tuple:
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "SHARED_PATH_PATTERNS" for t in node.targets):
            value = ast.literal_eval(node.value)
            if isinstance(value, tuple) and all(isinstance(x, str) for x in value):
                return value
    raise ValueError("TRUSTED_SCOPE_POLICY_UNREADABLE")


def classify(path: str, project: dict, registry: dict, patterns: tuple) -> list:
    result = []
    if owns(project, path):
        result.append("owned")
    if any(p["project_id"] != project["project_id"] and owns(p, path) for p in registry["projects"]):
        result.append("other-project")
    if any(under(path, p) for p in project["forbidden_paths"]):
        result.append("forbidden")
    if any(under(path, p) for p in registry["protected_paths"]):
        result.append("protected")
    if (any(fnmatch.fnmatchcase(path.casefold(), p.casefold()) for p in patterns)
            or any(under(path, p) for p in project["shared_dependencies"])):
        result.append("shared")
    if (path in TRUST_FILES or under(path, ".github") or under(path, "04_scripts/quality")
            or Path(path).name.casefold() in {"agents.md", "conftest.py", "pytest.ini", "setup.cfg", "setup.py", ".gitattributes", ".gitmodules"}
            or path.startswith("requirements") or under(path, "07_docs/templates")
            or path in {"08_tests/test_main_admission.py", "08_tests/test_project_registry.py", "08_tests/test_quality_controls.py"}):
        result.append("governance")
    if any(under(path, p) for p in PRODUCTION) or path.startswith(".env"):
        result.append("production-control-plane")
    return result or ["other-project"]


def export(repo: Path, commit: str, destination: Path, *, trusted_tests=False) -> None:
    """Materialize regular Git bytes only; no filters, hooks, links or production data."""
    entries = {p: e for p, e in tree(repo, commit).items() if e["mode"] in {"100644", "100755"}
               and not any(under(p, root) for root in ("01_data", "06_outputs", "10_logs"))
               and not p.startswith(".env")}
    if trusted_tests:
        entries = {p: e for p, e in entries.items() if under(p, "08_tests")
                   or p in {"pyproject.toml", "pytest.ini", "setup.cfg"} or Path(p).name == "conftest.py"}
    # git archive applies export-ignore/export-subst, so it cannot identify the
    # tested tree. cat-file copies exact blob bytes regardless of attributes.
    result = subprocess.run(["git", "cat-file", "--batch"], cwd=repo,
                            input="".join(e["oid"] + "\n" for e in entries.values()).encode("ascii"),
                            capture_output=True, timeout=120, check=True)
    position = 0
    for path, entry in entries.items():
        newline = result.stdout.index(b"\n", position)
        oid, kind, size = result.stdout[position:newline].decode("ascii").split()
        if oid != entry["oid"] or kind != "blob":
            raise ValueError("MATERIALIZATION_IDENTITY_MISMATCH")
        position = newline + 1
        payload = result.stdout[position:position + int(size)]
        position += int(size) + 1
        target = destination / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        target.chmod(0o755 if entry["mode"] == "100755" else 0o644)


def export_git_identity(repo: Path, workspace: Path, base: str, candidate: str) -> None:
    """Give repo-aware tests isolated Git objects, never caller config or credentials."""
    git(workspace, "init", "-q")
    git(workspace, "config", "core.autocrlf", "false")
    git(workspace, "-c", "protocol.file.allow=always", "fetch", "--quiet", "--no-tags",
        "--no-write-fetch-head", str(repo.resolve()), candidate)
    git(workspace, "update-ref", "refs/heads/admission-candidate", candidate)
    git(workspace, "symbolic-ref", "HEAD", "refs/heads/admission-candidate")
    git(workspace, "update-ref", "refs/remotes/origin/main", base)
    git(workspace, "read-tree", candidate)


def sandbox_command(workspace: Path, evidence: Path, tests: list[str]) -> list[str]:
    if sys.platform != "linux" or not shutil.which("bwrap"):
        raise ValueError("SANDBOX_UNAVAILABLE")
    command = ["bwrap", "--unshare-all", "--die-with-parent", "--new-session", "--cap-drop", "ALL", "--clearenv"]
    # Never bind /, /home, /run or the GitHub runner workspace. A new PID/network
    # namespace prevents candidate code from reaching the judge or credentials.
    for path in dict.fromkeys(("/usr", "/bin", "/lib", "/lib64", sys.prefix, sys.base_prefix)):
        if Path(path).exists():
            command += ["--ro-bind", path, path]
    command += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
                "--ro-bind", str(workspace), "/workspace", "--bind", str(evidence), "/evidence",
                "--chdir", "/workspace", "--setenv", "HOME", "/tmp",
                "--setenv", "PYTHONDONTWRITEBYTECODE", "1", "--setenv", "PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1",
                "--setenv", "LD_LIBRARY_PATH", str(Path(sys.base_prefix) / "lib"),
                "--setenv", "PATH", str(Path(sys.executable).parent) + ":/usr/bin:/bin",
                sys.executable, "-I", "-B", "-m", "pytest", "-q", "-o", "xfail_strict=true", "-p", "no:cacheprovider",
                "--basetemp=/tmp/pytest", "--junitxml=/evidence/junit.xml", *tests]
    return command


def run_tests(workspace: Path, evidence: Path, tests: list[str]) -> dict:
    hardening = os.environ.get("MAIN_ADMISSION_HARDENING") == "bubblewrap"
    if not hardening and not (os.environ.get("GITHUB_ACTIONS") == "true"
                             and os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted"):
        raise ValueError("EPHEMERAL_HOSTED_RUNNER_REQUIRED")
    # Candidate output is quarantined. Only bounded, validated regular bytes are
    # copied to the artifact directory; uploader must never see candidate links.
    with tempfile.TemporaryDirectory(prefix="admission-untrusted-output-") as raw_directory:
        raw = Path(raw_directory)
        if hardening:
            command = sandbox_command(workspace, raw, tests)
        else:
            command = [sys.executable, "-I", "-B", "-m", "pytest", "-q", "-o", "xfail_strict=true",
                       "-p", "no:cacheprovider", "--basetemp=" + str(raw / "tmp"),
                       "--junitxml=" + str(raw / "junit.xml"), *tests]
        with tempfile.TemporaryFile() as log:
            try:
                result = subprocess.run(command, stdout=log, stderr=log, timeout=600, check=False,
                                        cwd=workspace, env={"PATH": os.defpath, "HOME": str(raw),
                                            "PYTHONDONTWRITEBYTECODE": "1", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"})
            finally:
                log.seek(0, 2)
                log.seek(max(0, log.tell() - 65536))
                with (evidence / "pytest.log").open("xb") as output:
                    output.write(log.read())
        parsed = junit_result(raw / "junit.xml", result.returncode)
        if (raw / "junit.xml").exists():
            with (evidence / "junit.xml").open("xb") as output:
                output.write((raw / "junit.xml").read_bytes())
        return parsed


def junit_result(path: Path, returncode: int) -> dict:
    # Treat sandbox output as hostile; never follow a candidate-created link.
    data = b""
    if path.is_symlink():
        raise ValueError("UNSAFE_TEST_EVIDENCE")
    if path.exists():
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 8 * 1024 * 1024:
            raise ValueError("UNSAFE_TEST_EVIDENCE")
        data = path.read_bytes()
    if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
        raise ValueError("UNSAFE_TEST_XML")
    cases = list(ET.fromstring(data).iter("testcase")) if data else []
    failed = sum(any(c.tag in {"failure", "error", "skipped"} for c in case) for case in cases)
    return {"result": "PASS" if returncode == 0 and cases and not failed else "FAIL",
            "exit_code": returncode, "test_count": len(cases), "failed_or_skipped": failed,
            "junit_sha256": digest(data) if data else None}


def evidence_valid(receipt: dict, repo: Path, current_main: str, current_candidate: str) -> bool:
    """Freshness check only; callers must authenticate the receipt's publisher."""
    try:
        return (receipt["final_result"] == "PASS" and not receipt["failure_codes"]
                and receipt["trusted_main"] == {"commit": current_main, "tree": text(repo, "rev-parse", current_main + "^{tree}")}
                and receipt["candidate"] == {"commit": current_candidate, "tree": text(repo, "rev-parse", current_candidate + "^{tree}")})
    except (ValueError, KeyError):
        return False


def test_path(path: str) -> bool:
    return path.startswith("08_tests/") and path.endswith(".py") and Path(path).name.startswith("test_")


def impact_plan(mapping: dict, paths: list[str], old: dict, *, include_full=True) -> tuple[list[str], list[str], bool]:
    modules = mapping.get("modules", {})
    selected = {name for name, module in modules.items()
                if any(under(path, root) for path in paths for root in module.get("code_paths", []))}
    pending = list(selected)
    unresolved = False
    while pending:
        for name in modules[pending.pop()].get("dependents", []):
            if name not in modules:
                unresolved = True
                continue
            if name not in selected:
                selected.add(name)
                pending.append(name)
    full = unresolved or any(modules[name].get("full_regression_when_changed", False) for name in selected)
    tests = [t for name in sorted(selected) for key in ("direct_tests", "impact_tests")
             for t in modules[name].get(key, [])]
    if full and include_full:
        tests += sorted(p for p in old if test_path(p))
    return list(dict.fromkeys(tests)), sorted(selected), full


def policy_reductions(repo, candidate, sources, registry, mapping, paths):
    changed_paths = {p["path"] for p in paths}
    try:
        if REGISTRY in changed_paths:
            proposed = json.loads(blob(repo, candidate, REGISTRY))
            if set(proposed)-set(registry):
                raise ValueError("INVALID_REGISTRY_METADATA")
            indexed = {p["project_id"]: p for p in proposed["projects"]}
            for project in registry["projects"]:
                after = indexed.get(project["project_id"], {})
                before_tests = set(project["required_tests"] + project.get("future_required_tests", []))
                after_tests = set(after.get("required_tests", []) + after.get("future_required_tests", []))
                if not before_tests <= after_tests:
                    return ["TEST_POLICY_REDUCTION"]
        if MAP in changed_paths:
            import yaml
            proposed = yaml.safe_load(blob(repo, candidate, MAP))["modules"]
            for name, module in mapping.get("modules", {}).items():
                after = proposed.get(name, {})
                if any(not set(module.get(key, [])) <= set(after.get(key, []))
                       for key in ("code_paths", "direct_tests", "impact_tests", "dependents")):
                    return ["TEST_POLICY_REDUCTION"]
                if module.get("full_regression_when_changed") and not after.get("full_regression_when_changed"):
                    return ["TEST_POLICY_REDUCTION"]
    except (ValueError, KeyError, TypeError):
        return ["INVALID_CANDIDATE_TEST_POLICY"]
    return []


def is_governance_transition(paths, registry):
    governance = next((p for p in registry["projects"] if p["project_id"] == "dev-governance"), {})
    required = governance.get("required_tests", []) + governance.get("future_required_tests", [])
    return any(i["path"] in TRUST_FILES or i["path"] in required
               or under(i["path"], "04_scripts/quality") or under(i["path"], ".github")
               or Path(i["path"]).name in {"AGENTS.md", "conftest.py", "pytest.ini", "setup.cfg", "setup.py"}
               or i["path"].startswith("requirements")
               for i in paths)


def workflow_contract_valid(source):
    """Reviewable CI contract, not a proof against a malicious Maintainer."""
    import yaml
    value = yaml.safe_load(source)
    if not isinstance(value, dict) or value.get('permissions') != {'contents': 'read'}:
        return False
    jobs = value.get('jobs', {})
    if not any(j.get('name') == 'trusted-main-admission-v1' for j in jobs.values()):
        return False
    return all(not j.get('environment') and not j.get('services') for j in jobs.values())


def admit(repo: Path, base: str, candidate: str, project_id: str, evidence: Path, *, executor=run_tests, plan_only=False, separate_full=False) -> dict:
    receipt = {"schema_version": "main-admission/1", "mode": "required", "lane": "business", "final_result": "FAIL",
               "business_scope": "FAIL", "failure_codes": [], "trusted_main": None, "candidate": None,
               "merge_base": None, "ahead": None, "behind": None, "trusted_governance": {},
               "registry_blob_digest": None, "diff_digest": None, "changed_paths": [], "test_plan": [],
               "test_identities": [], "test_result": {"result": "NOT_RUN"},
               "execution_environment": {"python": platform.python_version(), "platform": platform.platform(),
                   "executable_sha256": digest(Path(sys.executable).read_bytes()),
                   "dependencies": sorted(f"{d.metadata['Name']}=={d.version}" for d in importlib.metadata.distributions()),
                   "sandbox": ("bubblewrap" if os.environ.get("MAIN_ADMISSION_HARDENING") == "bubblewrap"
                               else "github-hosted-ephemeral") if executor is run_tests else "test-fixture-only",
                   "production_access": False}, "checks": {"TECHNICAL_VALIDATION": "FAIL", "MAIN_ENTRY": "FAIL",
                                                          "PRODUCTION_RELEASE_AUTHORIZED": False}}
    failures = receipt["failure_codes"]
    evidence.mkdir(parents=True, exist_ok=False)
    try:
        if sys.version_info[:2] != (3, 12):
            raise ValueError("PYTHON_312_REQUIRED")
        if not all(re.fullmatch(r"[0-9a-f]{40}", c) for c in (base, candidate)):
            raise ValueError("FULL_COMMIT_SHA_REQUIRED")
        for label, commit in (("trusted_main", base), ("candidate", candidate)):
            if text(repo, "cat-file", "-t", commit) != "commit":
                raise ValueError("COMMIT_REQUIRED")
            receipt[label] = {"commit": commit, "tree": text(repo, "rev-parse", commit + "^{tree}")}
        sources = {p: blob(repo, base, p) for p in TRUST_FILES}
        if executor is run_tests and blob(repo, candidate, IMPLEMENTATION).replace(b"\r\n", b"\n") != Path(__file__).read_bytes().replace(b"\r\n", b"\n"):
            raise ValueError("EXECUTOR_NOT_EXACT_CANDIDATE")
        receipt["trusted_governance"] = {p: digest(b) for p, b in sources.items()}
        receipt["registry_blob_digest"] = digest(sources[REGISTRY])
        import yaml
        import jsonschema
        mapping = yaml.safe_load(sources[MAP])
        if not isinstance(mapping, dict):
            raise ValueError("INVALID_TRUSTED_TEST_MAP")
        schema = json.loads(blob(repo, candidate, SCHEMA))
        jsonschema.Draft202012Validator.check_schema(schema)
        registry = json.loads(sources[REGISTRY])
        patterns = scope_patterns(sources[SCOPE])
        old, new = tree(repo, base), tree(repo, candidate)
        receipt["trusted_governance"]["trusted_test_fixture_tree"] = digest(json.dumps(
            {p: e for p, e in old.items() if p.startswith("08_tests/")}, sort_keys=True).encode())
        receipt["merge_base"] = text(repo, "merge-base", base, candidate)
        behind, ahead = map(int, text(repo, "rev-list", "--left-right", "--count", base + "..." + candidate).split())
        receipt.update(ahead=ahead, behind=behind)
        if behind or not ahead or receipt["merge_base"] != base:
            failures.append("NOT_STRICT_FAST_FORWARD")
        paths = changed(repo, base, candidate, old, new)
        proposed = json.loads(blob(repo, candidate, REGISTRY))
        current_projects = proposed['projects']
        if len({p['project_id'] for p in current_projects}) != len(current_projects):
            failures.append('INVALID_REGISTRY_METADATA')
        for path in set(old) | set(new):
            if sum(owns(p,path) for p in current_projects) > 1:
                failures.append('OWNERSHIP_METADATA_COLLISION')
                break
        if not set(registry['protected_paths']) <= set(proposed['protected_paths']):
            failures.append('SCOPE_POLICY_REDUCTION')
        # Ownership describes responsibility; it does not grant source or release authority.
        # Include BOTH sides so reassignment cannot shed the old owner's tests.
        relevant = lambda p: any(owns(p, i['path']) for i in paths)
        projects = [p for p in registry['projects'] + current_projects if relevant(p)]
        projects = list({json.dumps(p, sort_keys=True): p for p in projects}.values())
        explicit = next((p for p in current_projects if p['project_id'] == project_id), None)
        if explicit and explicit not in projects:
            projects.append(explicit)
        transition = is_governance_transition(paths, registry)
        additions = current_projects[len(registry['projects']):]
        ordinary_registration = (current_projects[:len(registry['projects'])] == registry['projects']
            and len(additions)==1 and additions[0]['change_class']=='business'
            and proposed['protected_paths']==registry['protected_paths']
            and all(i['path']==REGISTRY or (owns(additions[0],i['path']) and
                    not any(under(i['path'],r) for r in registry['protected_paths']) and
                    not any(fnmatch.fnmatchcase(i['path'],r) for r in patterns)) for i in paths))
        if ordinary_registration:
            transition=False
            receipt['checks']['bootstrap']='NEW_BUSINESS_METADATA'

        strict = transition or any(p['change_class'] == 'shared' for p in projects) or any('shared' in classify(i['path'],p,registry,patterns) for i in paths for p in projects if not (ordinary_registration and i['path']==REGISTRY))
        artifact_roles = {}
        for item in paths:
            path = item['path']
            # Inspect both sides: deletion/rename cannot conceal existing state.
            roles = [production_artifact_role(path, blob(repo, revision, path))
                     for revision, entries in ((base, old), (candidate, new)) if path in entries]
            role = ('PRODUCTION_MUTATION' if 'PRODUCTION_MUTATION' in roles else
                    'PRODUCTION_TOOLING_SOURCE_CHANGE' if 'PRODUCTION_TOOLING_SOURCE_CHANGE' in roles else
                    next((r for r in roles if r is not None), None))
            if role:
                artifact_roles[path] = role
        strict = strict or 'PRODUCTION_TOOLING_SOURCE_CHANGE' in artifact_roles.values()
        receipt['checks']['production_artifact_roles'] = artifact_roles
        unowned = [i['path'] for i in paths if not any(owns(p, i['path']) for p in projects) and not (ordinary_registration and i['path']==REGISTRY)]
        if unowned:
            strict = strict or any(any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)
                                  or any(under(path, r) for r in registry['protected_paths']) for path in unowned)
        receipt['lane'] = 'governance' if transition else 'strict' if strict else 'business'
        receipt['checks'].update(CHANGE_CLASS='GOVERNANCE_OR_CI' if transition else 'STRICT_SHARED' if strict else 'BUSINESS',
                                MAINTAINER_REVIEW_REQUIRED='YES' if transition else 'NO',
                                PROJECT_EXISTENCE_APPROVAL_REQUIRED='NO',
                                project_id=project_id, project_ids=sorted({p['project_id'] for p in projects}),
                                unowned_paths=unowned)
        for item in paths:
            path = item['path']
            owners = [p for p in registry['projects'] if owns(p, path)]
            fallback=dict(project_id='unowned',owned_paths=[],shared_dependencies=[],forbidden_paths=[])
            diagnostic = owners or projects or [fallback]
            categories=set(c for p in diagnostic for c in classify(path,p,registry,patterns))
            if not owners: categories.add('unowned')
            if any(any(under(path,x) for x in p['forbidden_paths']) for p in registry['projects'] if p['project_id']==project_id):
                categories.add('forbidden')
            item['classifications']=sorted(categories)
            if '/' not in path and path not in {'AGENTS.md','README.md','pyproject.toml','.gitattributes'} and not path.startswith('requirements') and not owners:
                failures.append('SCOPE_VIOLATION')
            # An explicitly scoped module cannot quietly modify another existing module.
            if explicit and project_id != 'dev-governance' and any(p['project_id'] != project_id for p in owners):
                failures.append('SCOPE_VIOLATION')
            if artifact_roles.get(path) == 'PRODUCTION_MUTATION':
                failures.append('UNAUTHORIZED_PRODUCTION_CHANGE')
            if any(any(under(path, x) for x in p['forbidden_paths']) for p in ([explicit] if explicit else [])) and not transition:
                failures.append('SCOPE_VIOLATION')
        business_owners = {p['project_id'] for p in current_projects if p['change_class']=='business' and relevant(p)}
        if len(business_owners) > 1:
            failures.append('UNRELATED_MODULE_SCOPE')
        receipt["changed_paths"] = paths
        receipt["diff_digest"] = digest(json.dumps(paths, sort_keys=True, ensure_ascii=False).encode("utf-8"))
        if not paths:
            failures.append("ESCALATION_REQUIRED")
        if any(e["mode"] not in {"100644", "100755"} for e in list(old.values()) + list(new.values())):
            failures.extend(["UNSUPPORTED_GIT_MODE", "ESCALATION_REQUIRED"])
        receipt["business_scope"] = "FAIL" if failures else "PASS"
        tests = [t for p in projects for t in p['required_tests'] + p.get('future_required_tests', [])]
        if strict:
            for source_mapping in (mapping, yaml.safe_load(blob(repo,candidate,MAP))):
                impact, modules, full = impact_plan(source_mapping, [p['path'] for p in paths], old, include_full=not separate_full)
                tests += impact
                receipt['checks'].update(impact_modules=modules, full_regression=full or receipt['checks'].get('full_regression', False))
            governance = next((p for p in registry['projects'] if p['project_id']=='dev-governance'), {})
            tests += governance.get('required_tests', []) + governance.get('future_required_tests', [])
            # An unmapped shared source requires broad consumer coverage, never an owner bootstrap failure.
            if unowned and not receipt['checks'].get('impact_modules'):
                if not separate_full:
                    tests += [p for p in old if test_path(p)]
                receipt['checks']['full_regression'] = True
        required = list(dict.fromkeys(tests))
        tests += [i["path"] for i in paths if i["path"] in new and test_path(i["path"])
]
        tests = list(dict.fromkeys(tests))
        receipt["test_plan"] = tests
        receipt["checks"]["trusted_required_tests"] = required
        receipt["checks"]["test_policy"] = "trusted required/impact/full UNION candidate changed owned tests"
        receipt["checks"]["test_source_commit"] = candidate
        if any(p not in new for p in required):
            failures.append("REQUIRED_TEST_REMOVED")
        if not tests or any(p not in new or not test_path(p) for p in tests):
            failures.append("TRUSTED_TEST_UNAVAILABLE")
        receipt["test_identities"] = [{"path": p, "trusted_blob": old.get(p, {}).get("oid"),
                                      "candidate_blob": new[p]["oid"],
                                      "sha256": digest(blob(repo, candidate, p))} for p in tests if p in new]
        for item in paths:
            path = item["path"]
            if Path(path).name in {"conftest.py", "pytest.ini", "setup.cfg"}:
                failures.append("TRUSTED_TEST_CONFIG_CHANGED")
            elif Path(path).name == "pyproject.toml":
                import tomllib
                before_config = tomllib.loads(blob(repo, base, path).decode()).get("tool", {}).get("pytest", {})
                after_config = tomllib.loads(blob(repo, candidate, path).decode()).get("tool", {}).get("pytest", {})
                if before_config != after_config:
                    failures.append("TRUSTED_TEST_CONFIG_CHANGED")
        # Policy edits may add coverage but cannot remove trusted obligations.
        failures.extend(policy_reductions(repo, candidate, sources, registry, mapping, paths))
        if WORKFLOW in {p["path"] for p in paths}:
            if not workflow_contract_valid(blob(repo, candidate, WORKFLOW)):
                failures.append("INVALID_WORKFLOW_CONTRACT")
        try:
            git(repo, "diff", "--check", base, candidate, "--")
            receipt["checks"]["diff_check"] = "PASS"
        except ValueError:
            failures.append("DIFF_CHECK_FAILED")
        try:
            for p in {i["path"] for i in paths if i["path"].endswith(".py") and i["path"] in new}:
                compile(blob(repo, candidate, p), p, "exec", dont_inherit=True)
            receipt["checks"]["syntax"] = "PASS"
        except (SyntaxError, ValueError):
            failures.append("SYNTAX_FAILED")
        # Only the trusted plan/config judges the exact candidate test versions.
        if not failures and not plan_only:
            with tempfile.TemporaryDirectory(prefix="admission-") as directory:
                workspace = Path(directory)
                export(repo, candidate, workspace)
                for path in set(old) | set(new):
                    if Path(path).name in {"conftest.py", "pytest.ini", "setup.cfg", "pyproject.toml"}:
                        target = workspace / path
                        if path in old:
                            target.parent.mkdir(parents=True, exist_ok=True)
                            target.write_bytes(blob(repo, base, path))
                        elif target.exists():
                            target.unlink()
                export_git_identity(repo, workspace, base, candidate)
                test_evidence = evidence / "tests"
                test_evidence.mkdir()
                materialized = {p.relative_to(workspace).as_posix(): digest(p.read_bytes())
                                for p in workspace.rglob("*") if p.is_file()
                                and ".git" not in p.relative_to(workspace).parts}
                per_test = []
                for index, test in enumerate(tests):
                    destination = test_evidence / f"test-{index:04d}"
                    destination.mkdir()
                    per_test.append({"path": test, **executor(workspace, destination, [test])})
                    if any((workspace / p).is_symlink() or not (workspace / p).is_file()
                           or digest((workspace / p).read_bytes()) != expected
                           for p, expected in materialized.items()):
                        failures.append("TESTED_TREE_CHANGED")
                        break
                    if text(workspace, "rev-parse", "HEAD") != candidate:
                        failures.append("TESTED_TREE_CHANGED")
                        break
                receipt["test_result"] = {
                    "result": "PASS" if all(r.get("result") == "PASS" and r.get("test_count", 0) > 0 for r in per_test) else "FAIL",
                    "test_count": sum(r.get("test_count", 0) for r in per_test),
                    "failed_or_skipped": sum(r.get("failed_or_skipped", 0) for r in per_test),
                    "per_test": per_test}
                if receipt["test_result"].get("result") != "PASS":
                    failures.append("REQUIRED_TEST_FAILED")
        # CI success and Maintainer integration are separate decisions.
        receipt["failure_codes"] = sorted(set(failures))
        receipt["checks"]["TECHNICAL_VALIDATION"] = "FAIL" if failures else "NOT_RUN" if plan_only else "PASS"
        receipt["final_result"] = "FAIL" if failures else "PLANNED" if plan_only else "PASS"
        receipt["checks"]["MAIN_ENTRY"] = receipt["final_result"]
        jsonschema.validate(receipt, schema)
    except Exception as exc:
        receipt["final_result"] = "FAIL"
        receipt["checks"].update(TECHNICAL_VALIDATION="FAIL", MAIN_ENTRY="FAIL")
        receipt["failure_codes"] = sorted(set(failures + ["ADMISSION_ERROR", str(exc)[:300]]))
    (evidence / "main-admission.json").write_text(json.dumps(receipt, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return receipt


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args(argv)
    result = admit(args.repo.resolve(), args.base, args.candidate, args.project, args.evidence.resolve())
    print(json.dumps({"MAIN_ADMISSION": result["final_result"], "failure_codes": result["failure_codes"]}))
    return 0 if result["final_result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
