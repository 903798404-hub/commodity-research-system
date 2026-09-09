"""Shadow main admission. Invoke only from an independently selected trusted main.

This is evidence, not a signature or a branch-protection service. The caller must
authenticate repository/ref observations. V1 tests run on ephemeral GitHub hosted
runners with a stripped environment. Bubblewrap is optional Shadow hardening,
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
                sys.executable, "-I", "-m", "pytest", "-q", "-o", "xfail_strict=true", "-p", "no:cacheprovider",
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
            command = [sys.executable, "-I", "-m", "pytest", "-q", "-o", "xfail_strict=true",
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


def admit(repo: Path, base: str, candidate: str, project_id: str, evidence: Path, *, executor=run_tests) -> dict:
    receipt = {"schema_version": "main-admission/1", "mode": "shadow", "final_result": "FAIL",
               "business_scope": "FAIL", "failure_codes": [], "trusted_main": None, "candidate": None,
               "merge_base": None, "ahead": None, "behind": None, "trusted_governance": {},
               "registry_blob_digest": None, "diff_digest": None, "changed_paths": [], "test_plan": [],
               "test_identities": [], "test_result": {"result": "NOT_RUN"},
               "execution_environment": {"python": platform.python_version(), "platform": platform.platform(),
                   "executable_sha256": digest(Path(sys.executable).read_bytes()),
                   "dependencies": sorted(f"{d.metadata['Name']}=={d.version}" for d in importlib.metadata.distributions()),
                   "sandbox": ("bubblewrap" if os.environ.get("MAIN_ADMISSION_HARDENING") == "bubblewrap"
                               else "github-hosted-ephemeral") if executor is run_tests else "test-fixture-only",
                   "production_access": False}, "checks": {}}
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
        if sources[IMPLEMENTATION] != Path(__file__).read_bytes():
            raise ValueError("EXECUTOR_NOT_TRUSTED_MAIN")
        receipt["trusted_governance"] = {p: digest(b) for p, b in sources.items()}
        receipt["registry_blob_digest"] = digest(sources[REGISTRY])
        import yaml
        import jsonschema
        mapping = yaml.safe_load(sources[MAP])
        if not isinstance(mapping, dict):
            raise ValueError("INVALID_TRUSTED_TEST_MAP")
        schema = json.loads(sources[SCHEMA])
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
        if project_id == "auto":
            candidates = [p for p in registry["projects"] if p["change_class"] == "business"
                          and p["status"] == "ready" and any(owns(p, i["path"]) for i in paths)]
            if len(candidates) != 1:
                failures.extend(["ESCALATION_REQUIRED", "AMBIGUOUS_OR_NONBUSINESS_PROJECT"])
                project = dict(project_id="unresolved", change_class="shared", status="needs-boundary-review",
                               owned_paths=[], shared_dependencies=[], forbidden_paths=[], required_tests=[])
            else:
                project = candidates[0]
        else:
            project = next(p for p in registry["projects"] if p["project_id"] == project_id)
        receipt["checks"]["project_id"] = project["project_id"]
        for item in paths:
            item["classifications"] = classify(item["path"], project, registry, patterns)
        receipt["changed_paths"] = paths
        receipt["diff_digest"] = digest(json.dumps(paths, sort_keys=True, ensure_ascii=False).encode("utf-8"))
        if (project["change_class"] != "business" or project["status"] != "ready"
                or not paths or any(p["classifications"] != ["owned"] for p in paths)):
            failures.append("ESCALATION_REQUIRED")
        if any(e["mode"] not in {"100644", "100755"} for e in list(old.values()) + list(new.values())):
            failures.extend(["UNSUPPORTED_GIT_MODE", "ESCALATION_REQUIRED"])
        receipt["business_scope"] = "FAIL" if "ESCALATION_REQUIRED" in failures else "PASS"
        tests = list(dict.fromkeys(project["required_tests"] + project.get("future_required_tests", [])))
        receipt["test_plan"] = tests
        receipt["checks"]["test_policy"] = "trusted Registry; module map recorded, not automated"
        if not tests or any(p not in old or not p.startswith("08_tests/") or not p.endswith(".py") for p in tests):
            failures.extend(["TRUSTED_TEST_UNAVAILABLE", "ESCALATION_REQUIRED"])
        else:
            receipt["test_identities"] = [{"path": p, "trusted_blob": old[p]["oid"],
                                          "sha256": digest(blob(repo, base, p))} for p in tests]
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
        # Scope denial never executes candidate. A deleted/modified ordinary test
        # remains in the trusted overlay and therefore cannot reduce test count.
        if not failures:
            with tempfile.TemporaryDirectory(prefix="admission-") as directory:
                workspace = Path(directory)
                export(repo, candidate, workspace)
                # Discard all candidate tests, then restore the entire trusted suite
                # and fixtures, including deleted required tests and conftest files.
                suite = workspace / "08_tests"
                if suite.exists():
                    shutil.rmtree(suite)
                export(repo, base, workspace, trusted_tests=True)
                test_evidence = evidence / "tests"
                test_evidence.mkdir()
                per_test = []
                for index, test in enumerate(tests):
                    destination = test_evidence / f"test-{index:04d}"
                    destination.mkdir()
                    per_test.append({"path": test, **executor(workspace, destination, [test])})
                receipt["test_result"] = {
                    "result": "PASS" if all(r.get("result") == "PASS" and r.get("test_count", 0) > 0 for r in per_test) else "FAIL",
                    "test_count": sum(r.get("test_count", 0) for r in per_test),
                    "failed_or_skipped": sum(r.get("failed_or_skipped", 0) for r in per_test),
                    "per_test": per_test}
                if receipt["test_result"].get("result") != "PASS":
                    failures.append("REQUIRED_TEST_FAILED")
                if any(p not in new for p in tests):
                    failures.append("TRUSTED_TEST_DELETED")
        receipt["failure_codes"] = sorted(set(failures))
        receipt["final_result"] = "PASS" if not failures else "FAIL"
        jsonschema.validate(receipt, schema)
    except Exception as exc:
        receipt["final_result"] = "FAIL"
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
