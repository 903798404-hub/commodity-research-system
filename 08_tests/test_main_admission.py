"""Real temporary Git objects and inert subprocess tests; never production input."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys

import jsonschema
import pytest
import yaml

from quality import audit_changed_scope as old_scope
from quality import main_admission as admission

ROOT = Path(__file__).resolve().parents[1]


def git(repo, *args):
    return subprocess.check_output(["git", *args], cwd=repo).decode("utf-8").strip()


def write(repo, path, content):
    p = repo / path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8", newline="\n")


def commit(repo):
    git(repo, "add", ".")
    git(repo, "-c", "commit.gpgsign=false", "commit", "-qm", "fixture")
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def fixture_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "feature")
    git(repo, "config", "user.name", "Admission Fixture")
    git(repo, "config", "user.email", "fixture@example.invalid")
    git(repo, "config", "core.autocrlf", "false")
    for path in admission.TRUST_FILES:
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / path, target)
    project = dict(project_id="alpha", change_class="business", status="ready",
                   owned_paths=["03_src/alpha.py", "08_tests/test_alpha.py", "03_src/extra.py"],
                   shared_dependencies=["03_src/shared.py"], forbidden_paths=["secret.py"],
                   required_tests=["08_tests/test_alpha.py"], capabilities=["fixture"], boundary_notes="fixture")
    other = dict(project, project_id="beta", owned_paths=["03_src/beta.py", "08_tests/test_beta.py"],
                 required_tests=["08_tests/test_beta.py"])
    registry = dict(schema_version="project-registry/1", protected_paths=["protected.py"], projects=[project, other])
    write(repo, admission.REGISTRY, json.dumps(registry))
    write(repo, admission.MAP, "modules: {}\n")
    write(repo, "pyproject.toml", '[tool.pytest.ini_options]\npythonpath = ["03_src"]\n')
    write(repo, "03_src/alpha.py", "VALUE = 1\n")
    write(repo, "03_src/beta.py", "VALUE = 1\n")
    write(repo, "03_src/shared.py", "VALUE = 1\n")
    write(repo, "secret.py", "VALUE = 1\n")
    write(repo, "protected.py", "VALUE = 1\n")
    write(repo, "08_tests/test_alpha.py", "from alpha import VALUE\ndef test_value():\n    assert VALUE in (1, 2)\n")
    write(repo, "08_tests/test_beta.py", "def test_beta():\n    assert True\n")
    base = commit(repo)
    git(repo, "update-ref", "refs/remotes/origin/main", base)
    return repo, base


def inert_executor(workspace, evidence, tests):
    # ONLY fixture source literals created above. The production CLI has no switch
    # for this executor and never executes arbitrary candidates on local Windows.
    result = subprocess.run([sys.executable, "-I", "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider",
                             "--junitxml=" + str(evidence / "junit.xml"), *tests],
                            cwd=workspace, capture_output=True, timeout=30,
                            env={"SYSTEMROOT": __import__('os').environ.get("SYSTEMROOT", ""),
                                 "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTHONDONTWRITEBYTECODE": "1"})
    (evidence / "pytest.log").write_bytes(result.stdout + result.stderr)
    return admission.junit_result(evidence / "junit.xml", result.returncode)


def evaluate(fixture_repo, tmp_path, executor=inert_executor):
    repo, base = fixture_repo
    head = commit(repo)
    result = admission.admit(repo, base, head, "alpha", tmp_path / "evidence", executor=executor)
    jsonschema.validate(result, json.loads((ROOT / admission.SCHEMA).read_text(encoding="utf-8")))
    return result, head


def test_normal_business_pass_and_identity(fixture_repo, tmp_path):
    repo, base = fixture_repo
    write(repo, "03_src/alpha.py", "VALUE = 2\n")
    result, head = evaluate(fixture_repo, tmp_path)
    assert result["final_result"] == "PASS", result
    assert result["test_result"]["test_count"] == 1
    assert result["ahead"] == 1 and result["behind"] == 0
    assert result["merge_base"] == base
    assert admission.evidence_valid(result, repo, base, head)
    assert result["trusted_main"]["tree"] == git(repo, "rev-parse", base + "^{tree}")
    assert result["candidate"]["tree"] == git(repo, "rev-parse", head + "^{tree}")


@pytest.mark.parametrize("path,category", [
    ("03_src/beta.py", "other-project"), ("03_src/shared.py", "shared"),
    ("protected.py", "protected"), ("secret.py", "forbidden"),
    ("09_deploy/evil.py", "production-control-plane"),
    ("04_scripts/automation/evil.py", "production-control-plane"),
    ("04_scripts/automation/full_daily_windows.py", "production-control-plane"),
    ("04_scripts/runtime/probe.py", "production-control-plane"),
    ("03_src/agri_research_agent/automation/full_daily.py", "production-control-plane"),
    ("01_data/probe.py", "production-control-plane"),
    (admission.SCOPE, "governance"), (admission.MAP, "governance"),
    (admission.IMPLEMENTATION, "governance"), (admission.WORKFLOW, "governance"),
    (admission.SCHEMA, "governance"), ("03_src/conftest.py", "governance"),
])
def test_scope_and_self_judge_attacks(fixture_repo, tmp_path, path, category):
    repo, _ = fixture_repo
    write(repo, path, "# Maintainer-reviewed source proposal\n")
    result, _ = evaluate(fixture_repo, tmp_path)
    allowed = path in {'03_src/shared.py','09_deploy/evil.py','04_scripts/automation/evil.py',
        '04_scripts/automation/full_daily_windows.py','04_scripts/runtime/probe.py',
        '03_src/agri_research_agent/automation/full_daily.py',admission.SCOPE,admission.IMPLEMENTATION}
    assert result['final_result'] == ('PASS' if allowed else 'FAIL'), result
    assert result['execution_environment']['production_access'] is False
    if result['lane']=='governance': assert result['checks']['MAINTAINER_REVIEW_REQUIRED']=='YES'



def test_registry_cannot_expand_ownership(fixture_repo, tmp_path):
    repo, _ = fixture_repo
    registry = json.loads((repo / admission.REGISTRY).read_text())
    registry["projects"][0]["owned_paths"].append("secret.py")
    registry["projects"][0]["forbidden_paths"] = []
    write(repo, admission.REGISTRY, json.dumps(registry))
    write(repo, "secret.py", "VALUE = 0\n")
    result, _ = evaluate(fixture_repo, tmp_path)
    assert result["final_result"] == "FAIL"
    assert "forbidden" in next(i for i in result["changed_paths"] if i["path"] == "secret.py")["classifications"]


def test_existing_project_executes_modified_candidate_test(fixture_repo, tmp_path):
    repo, _ = fixture_repo
    write(repo, "03_src/alpha.py", "VALUE = 2\n")
    write(repo, "08_tests/test_alpha.py", "def test_candidate_regression():\n    assert False, 'candidate executed'\n")
    result, _ = evaluate(fixture_repo, tmp_path)
    assert result["final_result"] == "FAIL"
    assert "REQUIRED_TEST_FAILED" in result["failure_codes"]
    assert result["test_result"]["test_count"] == 1


def test_deleted_required_test_fails_before_execution(fixture_repo, tmp_path):
    repo, _ = fixture_repo
    (repo / "08_tests/test_alpha.py").unlink()
    result, _ = evaluate(fixture_repo, tmp_path)
    assert result["test_result"]["result"] == "NOT_RUN"
    assert result["final_result"] == "FAIL"
    assert "REQUIRED_TEST_REMOVED" in result["failure_codes"]


@pytest.mark.parametrize("mode", ["120000", "160000"])
def test_symlink_and_gitlink_escalate(fixture_repo, tmp_path, mode):
    repo, base = fixture_repo
    oid = base if mode == "160000" else git(repo, "hash-object", "03_src/alpha.py")
    git(repo, "update-index", "--add", "--cacheinfo", f"{mode},{oid},03_src/extra.py")
    git(repo, "-c", "commit.gpgsign=false", "commit", "-qm", "special mode")
    head = git(repo, "rev-parse", "HEAD")
    result = admission.admit(repo, base, head, "alpha", tmp_path / "evidence")
    assert result["final_result"] == "FAIL"
    assert "UNSUPPORTED_GIT_MODE" in result["failure_codes"]


def test_stale_base_candidate_rejected(fixture_repo, tmp_path):
    repo, base = fixture_repo
    write(repo, "03_src/beta.py", "VALUE = 2\n")
    newer_main = commit(repo)
    git(repo, "checkout", "-qb", "old-candidate", base)
    write(repo, "03_src/alpha.py", "VALUE = 2\n")
    head = commit(repo)
    result = admission.admit(repo, newer_main, head, "alpha", tmp_path / "evidence")
    assert result["final_result"] == "FAIL" and result["behind"] == 1
    assert "NOT_STRICT_FAST_FORWARD" in result["failure_codes"]


@pytest.mark.parametrize("moving", ["candidate", "main"])
def test_pass_evidence_invalidated_by_new_sha(fixture_repo, tmp_path, moving):
    repo, base = fixture_repo
    write(repo, "03_src/alpha.py", "VALUE = 2\n")
    receipt, head = evaluate(fixture_repo, tmp_path)
    assert receipt["final_result"] == "PASS"
    write(repo, "03_src/alpha.py", "VALUE = 1\n")
    next_sha = commit(repo)
    assert not admission.evidence_valid(receipt, repo, next_sha if moving == "main" else base,
                                        next_sha if moving == "candidate" else head)


def test_business_fast_lane_exact_ff_without_completion_or_integration(fixture_repo, tmp_path, monkeypatch):
    """Local Git contract only; hosted provider authentication remains a GitHub check."""
    from quality import complete_project

    monkeypatch.setattr(complete_project, "complete", lambda *a, **k: pytest.fail("Completion is not a Business prerequisite"))
    repo, base = fixture_repo
    assert not (repo / "04_scripts/quality/complete_project.py").exists()
    write(repo, "03_src/alpha.py", "VALUE = 2\n")
    receipt, head = evaluate(fixture_repo, tmp_path)
    assert receipt["final_result"] == "PASS"
    assert receipt["test_result"]["test_count"] > 0
    assert admission.evidence_valid(receipt, repo, base, head)
    assert git(repo, "worktree", "list", "--porcelain").count("worktree ") == 1
    git(repo, "branch", "main", base)
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "--ff-only", head)
    assert git(repo, "rev-parse", "main") == receipt["candidate"]["commit"]
    assert git(repo, "rev-parse", "main^{tree}") == receipt["candidate"]["tree"]
    assert git(repo, "branch", "--list", "integration/*") == ""


def test_same_tree_new_commit_cannot_reuse_business_pass(fixture_repo, tmp_path):
    repo, base = fixture_repo
    write(repo, "03_src/alpha.py", "VALUE = 2\n")
    receipt, head = evaluate(fixture_repo, tmp_path)
    assert receipt["final_result"] == "PASS"
    git(repo, "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-qm", "new candidate identity")
    new_head = git(repo, "rev-parse", "HEAD")
    assert new_head != head
    assert git(repo, "rev-parse", new_head + "^{tree}") == receipt["candidate"]["tree"]
    assert not admission.evidence_valid(receipt, repo, base, new_head)


def test_shared_project_cannot_use_business_fast_lane_for_owned_source(fixture_repo, tmp_path):
    repo, _ = fixture_repo
    data = json.loads((repo / admission.REGISTRY).read_text())
    data["projects"][0]["change_class"] = "shared"
    write(repo, admission.REGISTRY, json.dumps(data))
    base = commit(repo)
    git(repo, "update-ref", "refs/remotes/origin/main", base)
    write(repo, "03_src/alpha.py", "VALUE = 2\n")
    result, head = evaluate((repo, base), tmp_path)
    assert result["lane"] == "strict"
    assert result["final_result"] == "PASS", result
    assert admission.evidence_valid(result, repo, base, head)


def test_candidate_cannot_supply_executor(fixture_repo, tmp_path):
    repo, _ = fixture_repo
    write(repo, admission.IMPLEMENTATION, "print('PASS')\n")
    forged_base = commit(repo)
    write(repo, "03_src/alpha.py", "VALUE = 2\n")
    head = commit(repo)
    result = admission.admit(repo, forged_base, head, "alpha", tmp_path / "evidence")
    assert result["final_result"] == "FAIL"
    assert "EXECUTOR_NOT_EXACT_CANDIDATE" in result["failure_codes"]


def test_dirty_worktree_is_not_candidate_content(fixture_repo, tmp_path):
    repo, base = fixture_repo
    write(repo, "03_src/alpha.py", "VALUE = 2\n")
    head = commit(repo)
    write(repo, "03_src/alpha.py", "VALUE = 99\n")
    write(repo, "untracked.py", "raise RuntimeError()\n")
    result = admission.admit(repo, base, head, "alpha", tmp_path / "evidence", executor=inert_executor)
    assert result["final_result"] == "PASS"
    assert [i["path"] for i in result["changed_paths"]] == ["03_src/alpha.py"]


@pytest.mark.parametrize("case", ["normal", "cross", "shared", "protected", "governance", "failure", "delete-test", "stale"])
def test_shadow_old_new_comparison(fixture_repo, tmp_path, case):
    repo, base = fixture_repo
    path = {"cross":"03_src/beta.py", "shared":"03_src/shared.py", "protected":"protected.py",
            "governance":admission.SCOPE}.get(case, "03_src/alpha.py")
    write(repo, path, "VALUE = 99\n" if case == "failure" else "VALUE = 2\n")
    if case == "delete-test":
        (repo / "08_tests/test_alpha.py").unlink()
    if case == "stale":
        base = commit(repo)
        git(repo, "checkout", "-qb", "stale", fixture_repo[1])
        write(repo, "03_src/alpha.py", "VALUE = 1\n# candidate\n")
    head = commit(repo)
    old = old_scope.run_audit(repo, base, ["03_src/alpha.py", "08_tests/test_alpha.py", "03_src/extra.py"],
                             shared_patterns=old_scope.SHARED_PATH_PATTERNS + ("protected.py", "03_src/shared.py"))
    old_dir = tmp_path / "old-tests"
    old_dir.mkdir()
    old_test = inert_executor(repo, old_dir, ["08_tests/test_alpha.py"])
    old_pass = (old["PROJECT_SCOPE"] == "PASS" and old_test["result"] == "PASS"
                and git(repo, "merge-base", base, head) == base)
    new = admission.admit(repo, base, head, "alpha", tmp_path / "new-evidence", executor=inert_executor)
    (tmp_path / "shadow-comparison.json").write_text(json.dumps({"case": case,
        "old_scope": old["PROJECT_SCOPE"], "old_required_tests": old_test["result"],
        "old_result": "PASS" if old_pass else "FAIL", "new_result": new["final_result"],
        "base": base, "candidate": head,
        "mismatch": not old_pass and new["final_result"] == "PASS"}, indent=2), encoding="utf-8")
    assert new["final_result"] == ("PASS" if case in {"normal","shared","governance"} else "FAIL"), new
    if case == "normal":
        assert old_pass and new["final_result"] == "PASS"


def test_schema_rejects_pass_with_failure():
    schema = json.loads((ROOT / admission.SCHEMA).read_text())
    jsonschema.Draft202012Validator.check_schema(schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"schema_version":"main-admission/1", "final_result":"PASS", "failure_codes":["ESCALATION_REQUIRED"]}, schema)


def test_workflow_shadow_permissions_and_trust_source():
    workflow = yaml.safe_load((ROOT / admission.WORKFLOW).read_text())
    assert workflow["name"] == "trusted-main-admission-v1"
    assert workflow["permissions"] == {"contents":"read"}
    triggers = workflow.get("on", workflow.get(True))
    assert "pull_request_target" not in triggers
    assert set(triggers) == {"pull_request", "push", "workflow_dispatch"}
    jobs = workflow["jobs"]
    assert jobs["final"]["name"] == "trusted-main-admission-v1"
    assert "always()" in jobs["final"]["if"]
    raw = (ROOT / admission.WORKFLOW).read_text()
    assert '${{ secrets.' not in raw
    assert "environment:" not in raw
    for step in jobs["plan"]["steps"]:
        if "uses" in step:
            assert __import__('re').fullmatch(r"actions/[a-z-]+@[0-9a-f]{40}", step['uses'])
        if step.get('with', {}).get('path') in {'trusted','candidate'}:
            assert step['with']['persist-credentials'] is False
    assert "platform_ci.py plan" in raw
    assert jobs["windows"]["runs-on"] == "windows-2022"
    assert set(jobs["final"]["needs"]) == {"plan", "linux", "windows", "full"}
    assert jobs['full']['strategy']['matrix']['side'] == ['base', 'candidate']
    assert jobs['full']['strategy']['fail-fast'] is False
    comparison = next(s for s in jobs['final']['steps'] if s.get('name') == 'Compare exact base and candidate full regression')
    assert comparison['env']['FULL_JOB_RESULT'] == '${{ needs.full.result }}'
    assert 'full_regression.py compare' in comparison['run']
    fresh_main = next(s for s in jobs['final']['steps'] if s.get('name') == 'Require plan and fresh main')
    assert fresh_main['env']['MAIN_READ_TOKEN'] == '${{ github.token }}'
    assert '/git/ref/heads/main' in fresh_main['run']
    for lane in ('linux', 'windows'):
        assert 'MAIN_READ_TOKEN' not in str(jobs[lane])


def test_exact_new_asset_registration():
    registry = json.loads((ROOT / admission.REGISTRY).read_text(encoding="utf-8"))
    project = next(p for p in registry["projects"] if p["project_id"] == "dev-governance")
    assert project["change_class"] == "shared"
    assert set(project["future_owned_paths"]) == {admission.WORKFLOW, admission.SCHEMA, "08_tests/test_main_admission.py"}
    assert project["future_required_tests"] == ["08_tests/test_main_admission.py"]
    assert not any(p == '.github' or '*' in p for p in project['future_owned_paths'])


def test_missing_sandbox_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setattr(admission.shutil, "which", lambda _: None)
    with pytest.raises(ValueError, match="SANDBOX_UNAVAILABLE"):
        admission.sandbox_command(tmp_path, tmp_path, ["08_tests/test_alpha.py"])


@pytest.mark.parametrize("operation", ["add", "delete", "rename", "mode"])
def test_git_object_operations(fixture_repo, tmp_path, operation):
    repo, base = fixture_repo
    if operation == "add":
        write(repo, "03_src/extra.py", "VALUE = 1\n")
    elif operation == "delete":
        (repo / "03_src/alpha.py").unlink()
    elif operation == "rename":
        git(repo, "mv", "03_src/alpha.py", "03_src/extra.py")
    else:
        git(repo, "update-index", "--chmod=+x", "03_src/alpha.py")
        git(repo, "-c", "commit.gpgsign=false", "commit", "-qm", "mode fixture")
    head = git(repo, "rev-parse", "HEAD") if operation == "mode" else commit(repo)
    result = admission.admit(repo, base, head, "alpha", tmp_path / "evidence", executor=inert_executor)
    assert result["changed_paths"]
    if operation == "add":
        assert result["final_result"] == "PASS"
        assert result["changed_paths"][0]["status"] == "A"
    elif operation == "delete":
        assert result["final_result"] == "FAIL"  # trusted import must fail
        assert result["changed_paths"][0]["status"] == "D"
    elif operation == "rename":
        assert result["final_result"] == "FAIL"
        assert {p["path"] for p in result["changed_paths"]} == {"03_src/alpha.py", "03_src/extra.py"}
        assert all(p["status"].startswith("R") for p in result["changed_paths"])
    else:
        assert result["changed_paths"][0]["old_mode"] == "100644"
        assert result["changed_paths"][0]["new_mode"] == "100755"


def test_export_ignores_git_archive_attributes(fixture_repo, tmp_path):
    repo, _ = fixture_repo
    write(repo, ".gitattributes", "03_src/alpha.py export-ignore\n")
    head = commit(repo)
    destination = tmp_path / "exported"
    admission.export(repo, head, destination)
    assert (destination / "03_src/alpha.py").read_bytes() == admission.blob(repo, head, "03_src/alpha.py")


@pytest.mark.parametrize("xml", [
    '<testsuites><testsuite tests="0"/></testsuites>',
    '<testsuite><testcase><skipped/></testcase></testsuite>',
    '<testsuite><testcase><failure/></testcase></testsuite>',
    '<testsuite><testcase><error/></testcase></testsuite>',
])
def test_empty_skipped_error_test_evidence_fails(tmp_path, xml):
    path = tmp_path / "junit.xml"
    path.write_text(xml)
    assert admission.junit_result(path, 0)["result"] == "FAIL"


def test_unsafe_xml_rejected(tmp_path):
    path = tmp_path / "junit.xml"
    path.write_text('<!DOCTYPE x [<!ENTITY a "b">]><testsuite/>')
    with pytest.raises(ValueError, match="UNSAFE_TEST_XML"):
        admission.junit_result(path, 0)


def test_sandbox_mount_contract(monkeypatch, tmp_path):
    monkeypatch.setattr(admission.sys, "platform", "linux")
    monkeypatch.setattr(admission.shutil, "which", lambda _: "/usr/bin/bwrap")
    command = admission.sandbox_command(tmp_path / "source", tmp_path / "evidence", ["08_tests/test_alpha.py"])
    assert all(flag in command for flag in ["--unshare-all", "--new-session", "--clearenv", "--die-with-parent"])
    assert command.count("--bind") == 1
    assert command[command.index("--bind") + 1] == str(tmp_path / "evidence")
    assert "--share-net" not in command
    assert "--dev-bind" not in command


def test_candidate_test_map_cannot_reduce_plan(fixture_repo, tmp_path):
    repo, _ = fixture_repo
    write(repo, admission.MAP, yaml.safe_dump({'modules':{'consumer':{
        'code_paths':['03_src/alpha.py'],'direct_tests':['08_tests/test_alpha.py'],
        'impact_tests':['08_tests/test_beta.py'],'dependents':[]}}}))
    base=commit(repo)
    write(repo, admission.MAP, 'modules: {}\n')
    result, _ = evaluate((repo,base), tmp_path)
    assert '08_tests/test_alpha.py' in result['test_plan']
    assert result['final_result']=='FAIL'
    assert 'TEST_POLICY_REDUCTION' in result['failure_codes']


def test_candidate_extra_output_never_enters_artifact(monkeypatch, tmp_path):
    import types
    monkeypatch.setenv('MAIN_ADMISSION_HARDENING', 'bubblewrap')
    monkeypatch.setattr(admission.sys, 'platform', 'linux')
    monkeypatch.setattr(admission.shutil, 'which', lambda _: '/usr/bin/bwrap')
    evidence = tmp_path / 'safe'
    evidence.mkdir()
    def fake_sandbox(command, **kwargs):
        raw = Path(command[command.index('--bind') + 1])
        assert raw != evidence
        (raw / 'junit.xml').write_text('<testsuite><testcase name="inert"/></testsuite>')
        (raw / 'untrusted-extra.txt').write_text('must never be uploaded')
        kwargs['stdout'].write(b'bounded fixture log')
        return types.SimpleNamespace(returncode=0)
    monkeypatch.setattr(admission.subprocess, 'run', fake_sandbox)
    result = admission.run_tests(tmp_path, evidence, ['08_tests/test_alpha.py'])
    assert result['result'] == 'PASS'
    assert {p.name for p in evidence.iterdir()} == {'junit.xml', 'pytest.log'}


def test_auto_governance_retains_classified_diff(fixture_repo, tmp_path):
    repo, base = fixture_repo
    write(repo, admission.WORKFLOW, '# candidate workflow\n')
    head = commit(repo)
    result = admission.admit(repo, base, head, 'auto', tmp_path / 'evidence')
    assert result['final_result'] == 'FAIL'
    assert result['failure_codes']
    assert result['changed_paths'][0]['path'] == admission.WORKFLOW
    assert 'governance' in result['changed_paths'][0]['classifications']


def test_hosted_baseline_does_not_require_bubblewrap(monkeypatch, tmp_path):
    import types
    monkeypatch.delenv('MAIN_ADMISSION_HARDENING', raising=False)
    monkeypatch.setenv('GITHUB_ACTIONS', 'true')
    monkeypatch.setenv('RUNNER_ENVIRONMENT', 'github-hosted')
    monkeypatch.setattr(admission.shutil, 'which', lambda _: None)
    def fake_runner(command, **kwargs):
        assert 'bwrap' not in command
        assert not any('TOKEN' in k or k.startswith('GITHUB_') for k in kwargs['env'])
        xml = Path(next(a.split('=', 1)[1] for a in command if a.startswith('--junitxml=')))
        xml.write_text('<testsuite><testcase name="fixture"/></testsuite>')
        return types.SimpleNamespace(returncode=0)
    monkeypatch.setattr(admission.subprocess, 'run', fake_runner)
    evidence = tmp_path / 'evidence'; evidence.mkdir()
    assert admission.run_tests(tmp_path, evidence, ['08_tests/test_alpha.py'])['result'] == 'PASS'


def bootstrap_candidate(fixture_repo):
    from quality import project_registry
    repo, _ = fixture_repo
    data = json.loads((repo / admission.REGISTRY).read_text())
    data['schema_version'] = 'project-registry/3'
    write(repo, '03_src/agri_research_agent/__init__.py', '')
    write(repo, admission.REGISTRY, json.dumps(data))
    base = commit(repo)
    git(repo, 'update-ref', 'refs/remotes/origin/main', base)
    project = project_registry.bootstrap_record('bootstrap-omega', data['protected_paths'])
    project.pop('runtime_target')
    data['projects'].append(project)
    write(repo, admission.REGISTRY, json.dumps(data))
    source, tests = project['reserved_paths']
    write(repo, source + '/value.py', 'VALUE = 7\n')
    write(repo, tests + '/test_project.py',
          'from agri_research_agent.bootstrap_omega.value import VALUE\ndef test_value():\n    assert VALUE == 7\n')
    return repo, base, data, source, tests


def test_bootstrap_registry_source_and_all_candidate_tests_pass(fixture_repo, tmp_path):
    repo, base, data, source, tests = bootstrap_candidate(fixture_repo)
    write(repo, tests + '/test_extra.py', 'def test_extra():\n    assert True\n')
    head = commit(repo)
    result = admission.admit(repo, base, head, 'auto', tmp_path/'evidence', executor=inert_executor)
    assert result['final_result'] == 'PASS', result
    assert set(result['test_plan']) == {tests+'/test_extra.py', tests+'/test_project.py'}
    assert result['lane']=='business' and result['checks']['MAINTAINER_REVIEW_REQUIRED']=='NO'
    assert result['test_result']['test_count'] == 2
    assert result['checks']['test_source_commit'] == head
    assert result['trusted_main']['commit'] == base
    jsonschema.validate(result, json.loads((ROOT/admission.SCHEMA).read_text()))


@pytest.mark.parametrize('attack', ['existing', 'root-policy', 'ownership', 'shared', 'protected',
    'production', 'runtime', 'no-tests', 'empty-policy', 'admission', 'scope', 'registry-code',
    'pytest-policy', 'conftest', 'second-project', 'foreign-file'])
def test_bootstrap_rejects_authority_changes(fixture_repo, tmp_path, attack):
    repo, base, data, source, tests = bootstrap_candidate(fixture_repo)
    if attack == 'existing': data['projects'][0]['required_tests'] = []
    elif attack == 'root-policy': data['protected_paths'] = []
    elif attack == 'ownership': data['projects'][-1]['reserved_paths'].append('03_src')
    elif attack == 'runtime': data['projects'][-1]['runtime_target'] = 'production_container'
    elif attack == 'empty-policy': data['projects'][-1]['future_required_tests'] = []
    elif attack == 'second-project': data['projects'].append(dict(data['projects'][-1], project_id='extra'))
    elif attack == 'no-tests': (repo/tests/'test_project.py').unlink()
    else:
        path = {'shared':'03_src/shared.py', 'protected':'protected.py',
                'production':'09_deploy/live/deployment_result.json', 'admission':admission.IMPLEMENTATION,
                'scope':admission.SCOPE, 'registry-code':'04_scripts/quality/project_registry.py',
                'pytest-policy':'pyproject.toml', 'conftest':tests+'/conftest.py',
                'foreign-file':'03_src/beta.py'}[attack]
        write(repo, path, '# candidate cannot change judge\n')
    write(repo, admission.REGISTRY, json.dumps(data))
    head = commit(repo)
    result = admission.admit(repo, base, head, 'auto', tmp_path/'evidence', executor=inert_executor)
    allowed={'shared','runtime','empty-policy','admission','scope','registry-code'}
    assert result['final_result'] == ('PASS' if attack in allowed else 'FAIL'), result
    assert result['execution_environment']['production_access'] is False


@pytest.mark.parametrize('body', ['', 'import pytest\ndef test_skip():\n    pytest.skip("candidate")\n',
                                  'def test_bad():\n    assert False\n'])
def test_bootstrap_requires_nonempty_passing_collection(fixture_repo, tmp_path, body):
    repo, base, data, source, tests = bootstrap_candidate(fixture_repo)
    write(repo, tests+'/test_extra.py', body)
    head = commit(repo)
    result = admission.admit(repo, base, head, 'auto', tmp_path/'evidence', executor=inert_executor)
    assert result['final_result'] == 'FAIL'
    assert 'REQUIRED_TEST_FAILED' in result['failure_codes']


def test_bootstrap_namespace_conflict_denied(fixture_repo, tmp_path):
    repo, base, data, source, tests = bootstrap_candidate(fixture_repo)
    # The trusted namespace is already reserved by an existing project.
    trusted = json.loads(git(repo, 'show', base+':'+admission.REGISTRY))
    trusted['projects'][0]['reserved_paths'] = [source]
    write(repo, admission.REGISTRY, json.dumps(trusted))
    base = commit(repo)
    data['projects'][0] = trusted['projects'][0]
    write(repo, admission.REGISTRY, json.dumps(data))
    head = commit(repo)
    result = admission.admit(repo, base, head, 'auto', tmp_path/'evidence', executor=inert_executor)
    assert result['failure_codes']


@pytest.mark.parametrize("body,expected", [
    ("def test_new_regression():\n    assert True\n", "PASS"),
    ("def test_new_regression():\n    assert False, 'candidate new test executed'\n", "FAIL"),
])
def test_existing_ready_owned_namespace_new_test_runs(fixture_repo, tmp_path, body, expected):
    repo, _ = fixture_repo
    data = json.loads((repo / admission.REGISTRY).read_text())
    data['projects'][0]['owned_paths'].append('08_tests/alpha')
    write(repo, admission.REGISTRY, json.dumps(data))
    base = commit(repo)
    write(repo, '08_tests/alpha/test_regression.py', body)
    result, head = evaluate((repo, base), tmp_path)
    assert result['final_result'] == expected, result
    assert result['test_result']['test_count'] == 2
    assert result['checks']['test_source_commit'] == head
    identity = next(t for t in result['test_identities'] if t['path'].endswith('test_regression.py'))
    assert identity['trusted_blob'] is None
    assert identity['candidate_blob'] == git(repo, 'rev-parse', head+':08_tests/alpha/test_regression.py')


def test_candidate_can_legitimately_replace_old_test_blob(fixture_repo, tmp_path):
    repo, base = fixture_repo
    write(repo, '03_src/alpha.py', 'VALUE = 3\n')
    write(repo, '08_tests/test_alpha.py', 'from alpha import VALUE\ndef test_new_contract():\n    assert VALUE == 3\n')
    result, head = evaluate(fixture_repo, tmp_path)
    assert result['final_result'] == 'PASS', result
    assert result['test_identities'][0]['candidate_blob'] != result['test_identities'][0]['trusted_blob']
    assert admission.evidence_valid(result, repo, base, head)


@pytest.mark.parametrize('kind', ['shared', 'governance'])
@pytest.mark.parametrize('satisfied', [False, True])
def test_strict_lane_exact_identity_and_required_impact_tests(fixture_repo, tmp_path, kind, satisfied):
    repo, _ = fixture_repo
    data = json.loads((repo / admission.REGISTRY).read_text())
    p = data['projects'][0]; p['change_class'] = 'shared'
    path = admission.IMPLEMENTATION if kind == 'governance' else '03_src/shared.py'
    p['owned_paths'].append(path)
    p['shared_dependencies'] = []
    write(repo, admission.REGISTRY, json.dumps(data))
    write(repo, admission.MAP, yaml.safe_dump({'modules': {'changed': {
        'code_paths': [path], 'direct_tests': [], 'impact_tests': ['08_tests/test_beta.py'],
        'dependents': [], 'full_regression_when_changed': False}}}))
    base = commit(repo)
    write(repo, path, (repo/path).read_text(encoding='utf-8')+'\n# proposed source change\n')
    if not satisfied:
        write(repo, '03_src/alpha.py', 'VALUE = 99\n')
    head = commit(repo)
    result = admission.admit(repo, base, head, 'alpha', tmp_path/'evidence', executor=inert_executor)
    assert result['lane'] == ('governance' if kind=='governance' else 'strict')
    assert result['test_plan'] == ['08_tests/test_alpha.py', '08_tests/test_beta.py']
    expected = 'PASS'
    assert result['final_result'] == (expected if satisfied else 'FAIL'), result
    assert admission.evidence_valid(result, repo, base, head) == satisfied
    assert result['candidate'] == {'commit':head, 'tree':git(repo, 'rev-parse', head+'^{tree}')}


def test_strict_policy_cannot_reduce_its_required_tests(fixture_repo, tmp_path):
    repo, _ = fixture_repo
    data = json.loads((repo / admission.REGISTRY).read_text())
    data['projects'][0]['change_class'] = 'shared'
    data['projects'][0]['owned_paths'].append(admission.REGISTRY)
    write(repo, admission.REGISTRY, json.dumps(data))
    base = commit(repo)
    data['projects'][0]['required_tests'] = []
    write(repo, admission.REGISTRY, json.dumps(data))
    result, _ = evaluate((repo, base), tmp_path)
    assert result['final_result'] == 'FAIL'
    assert 'TEST_POLICY_REDUCTION' in result['failure_codes']
    assert result['test_plan'] == ['08_tests/test_alpha.py']


def test_trusted_full_impact_plan_is_transitive():
    mapping = {'modules': {'a': {'code_paths':['03_src/a.py'], 'dependents':['b']},
                           'b': {'direct_tests':['08_tests/test_b.py'], 'full_regression_when_changed':True}}}
    tests, modules, full = admission.impact_plan(mapping, ['03_src/a.py'],
                                               {'08_tests/test_b.py':{}, '08_tests/test_c.py':{}})
    assert full and modules == ['a','b']
    assert tests == ['08_tests/test_b.py','08_tests/test_c.py']


def test_candidate_mutation_cannot_receive_commit_tree_pass(fixture_repo, tmp_path):
    repo, base = fixture_repo
    write(repo, '03_src/alpha.py', 'VALUE = 2\n')
    def mutate(workspace, evidence, tests):
        write(workspace, '03_src/alpha.py', 'VALUE = 99\n')
        return {'result':'PASS', 'test_count':1}
    result, _ = evaluate(fixture_repo, tmp_path, executor=mutate)
    assert result['final_result']=='FAIL'
    assert 'TESTED_TREE_CHANGED' in result['failure_codes']


def test_exported_git_metadata_is_exact_and_has_no_remote_credentials(fixture_repo, tmp_path):
    repo, base = fixture_repo
    write(repo, '03_src/alpha.py', 'VALUE = 2\n')
    head = commit(repo)
    workspace = tmp_path/'export'
    admission.export(repo, head, workspace)
    admission.export_git_identity(repo, workspace, base, head)
    assert git(workspace,'rev-parse','HEAD') == head
    assert git(workspace,'rev-parse','origin/main') == base
    assert git(workspace,'remote') == ''
    assert 'credential' not in (workspace/'.git/config').read_text()
    assert not (workspace/'.git/objects/info/alternates').exists()


def test_unmapped_impact_consumer_requires_full_suite_instead_of_dropping_coverage():
    mapping = {'modules': {'library': {'code_paths':['03_src/library.py'],
                                      'dependents':['unmapped.consumer']}}}
    tests, modules, full = admission.impact_plan(mapping, ['03_src/library.py'],
                                               {'08_tests/test_consumer.py':{}})
    assert full and tests == ['08_tests/test_consumer.py']


# Governance approval is deliberately not an API/input to candidate validation.
def governance_fixture(fixture_repo):
    repo, _ = fixture_repo
    data=json.loads((repo/admission.REGISTRY).read_text())
    project=data['projects'][0]
    project['change_class']='shared'
    project['owned_paths'] += [admission.IMPLEMENTATION, admission.REGISTRY, admission.WORKFLOW, 'pyproject.toml']
    write(repo, admission.REGISTRY, json.dumps(data))
    base=commit(repo)
    write(repo, admission.IMPLEMENTATION, (repo/admission.IMPLEMENTATION).read_text(encoding='utf-8')+'\n# proposed trusted-root upgrade\n')
    return repo,base


def test_governance_technical_pass_requires_root_acceptance(fixture_repo,tmp_path):
    repo,base=governance_fixture(fixture_repo);head=commit(repo)
    result=admission.admit(repo,base,head,'alpha',tmp_path/'evidence',executor=inert_executor)
    assert result['checks']['CHANGE_CLASS']=='GOVERNANCE_OR_CI'
    assert result['test_result']['result']=='PASS' and result['test_result']['test_count']==1
    assert result['checks']['TECHNICAL_VALIDATION']=='PASS'
    assert result['checks']['MAIN_ENTRY']==result['final_result']=='PASS'
    assert result['failure_codes']==[]
    assert admission.evidence_valid(result,repo,base,head)
    assert result['checks']['MAINTAINER_REVIEW_REQUIRED']=='YES'
    assert result['candidate']=={'commit':head,'tree':git(repo,'rev-parse',head+'^{tree}')}
    assert result['execution_environment']['production_access'] is False
    schema=json.loads((ROOT/admission.SCHEMA).read_text())
    jsonschema.validate(result,schema)
    result['final_result']='PASS'
    jsonschema.validate(result,schema)


@pytest.mark.parametrize('attack',['required-test','policy','ownership','ownership-metadata','test-config','workflow','production','scope'])
def test_governance_technical_failures_never_become_approval_states(fixture_repo,tmp_path,monkeypatch,attack):
    repo,base=governance_fixture(fixture_repo)
    monkeypatch.setenv('GOVERNANCE_APPROVAL_RUN_ID','123')
    monkeypatch.setenv('MAINTAINER_TRANSITION_APPROVAL','PRESENT')
    if attack=='required-test':(repo/'08_tests/test_alpha.py').unlink()
    elif attack in ('ownership','ownership-metadata','policy'):
        data=json.loads((repo/admission.REGISTRY).read_text())
        if attack=='policy':data['projects'][0]['required_tests']=[]
        else:
            data['projects'][0]['owned_paths'].append('secret.py')
            data['projects'][0]['forbidden_paths']=[]
            if attack=='ownership':write(repo,'secret.py','VALUE=2\n')
        write(repo,admission.REGISTRY,json.dumps(data))
    elif attack=='test-config':write(repo,'pyproject.toml','[tool.pytest.ini_options]\ntestpaths=[]\n')
    elif attack=='workflow':write(repo,admission.WORKFLOW,'name: trusted-main-admission-v1\njobs: {final: {runs-on: ubuntu-latest, steps: [{run: "true"}]}}\n')
    elif attack=='scope':write(repo,'unknown.py','VALUE=2\n')
    else:write(repo,'09_deploy/live/deployment_result.json','{"container_id":"live","image_id":"live"}\n')
    head=commit(repo)
    result=admission.admit(repo,base,head,'alpha',tmp_path/'evidence',executor=inert_executor)
    expected = 'PASS' if attack=='ownership-metadata' else 'FAIL'
    assert result['final_result']==expected,result
    assert result['checks']['MAINTAINER_REVIEW_REQUIRED']=='YES'
    if expected=='FAIL': assert result['failure_codes']


@pytest.mark.parametrize('claim',['file','admission','message','branch','registry','workflow-metadata','environment'])
def test_candidate_claims_never_approve_governance(fixture_repo,tmp_path,monkeypatch,claim):
    repo,base=governance_fixture(fixture_repo)
    if claim=='file':write(repo,'03_src/extra.py','APPROVAL=True\n')
    elif claim=='admission':write(repo,admission.IMPLEMENTATION,'print("PASS")\n')
    elif claim=='branch':git(repo,'branch','-m','TRUSTED_GOVERNANCE_TRANSITION_APPROVED')
    elif claim=='registry':
        data=json.loads((repo/admission.REGISTRY).read_text());data['approval']=True
        write(repo,admission.REGISTRY,json.dumps(data))
    elif claim=='workflow-metadata':
        text=(repo/admission.WORKFLOW).read_text();write(repo,admission.WORKFLOW,text.replace('name: trusted-main-admission-v1','name: approved=true',1))
    elif claim=='environment':
        monkeypatch.setenv('GOVERNANCE_APPROVAL_RUN_ID','123')
        monkeypatch.setenv('MAINTAINER_TRANSITION_APPROVAL','PRESENT')
    head=commit(repo)
    if claim=='message':
        git(repo,'commit','--allow-empty','-qm','TRUSTED_GOVERNANCE_TRANSITION_APPROVED approval=true')
        head=git(repo,'rev-parse','HEAD')
    result=admission.admit(repo,base,head,'alpha',tmp_path/'evidence',executor=inert_executor)
    if claim=='registry':
        assert result['final_result']=='FAIL'
        return
    assert result['final_result']=='PASS',result
    assert result['checks']['TECHNICAL_VALIDATION']=='PASS'
    assert admission.evidence_valid(result,repo,base,head)
    assert result['checks']['MAINTAINER_REVIEW_REQUIRED']=='YES'


@pytest.mark.parametrize('change_class',['business','shared'])
def test_business_shared_pass_without_governance_approval(fixture_repo,tmp_path,change_class):
    repo,base=fixture_repo
    if change_class=='shared':
        data=json.loads((repo/admission.REGISTRY).read_text());data['projects'][0]['change_class']=change_class
        write(repo,admission.REGISTRY,json.dumps(data));base=commit(repo)
    write(repo,'03_src/alpha.py','VALUE=2\n');head=commit(repo)
    result=admission.admit(repo,base,head,'alpha',tmp_path/'evidence',executor=inert_executor)
    assert result['final_result']==result['checks']['MAIN_ENTRY']=='PASS'
    assert result['checks']['TECHNICAL_VALIDATION']=='PASS'
    assert result['checks']['CHANGE_CLASS']==('BUSINESS' if change_class=='business' else 'STRICT_SHARED')


@pytest.mark.parametrize('project_id,prefix',[
    ('soybean-pm','03_src/agri_research_agent/import_profit/'),
    ('domestic-spread-status','03_src/agri_research_agent/application/domestic_spreads.py'),
    ('usda','11_独立应用/USDA平衡表/src/'),
    ('international-spread','05_apps/international_spread_page.py'),
    ('notification-push','03_src/agri_research_agent/alerts/'),
])
def test_real_business_boundaries_do_not_select_governance(project_id,prefix):
    registry=json.loads((ROOT/admission.REGISTRY).read_text(encoding='utf-8'))
    project=next(p for p in registry['projects'] if p['project_id']==project_id)
    names=git(ROOT,'-c','core.quotepath=false','ls-files').splitlines()
    path=next(p for p in names if p.startswith(prefix))
    assert project['change_class']=='business'
    assert not admission.is_governance_transition([{'path':path}],registry)
    assert admission.classify(path,project,registry,admission.scope_patterns((ROOT/admission.SCOPE).read_bytes()))==['owned']


def test_no_candidate_approval_interface_or_issuer_remains():
    import inspect
    assert set(inspect.signature(admission.admit).parameters)=={'repo','base','candidate','project_id','evidence','executor','plan_only','separate_full'}
    assert inspect.signature(admission.admit).parameters['separate_full'].default is False
    with pytest.raises(ValueError, match='FULL_SEPARATION_REQUIRES_HOSTED_PLAN_ONLY'):
        admission.admit(None, None, None, None, None, separate_full=True)
    workflow=yaml.safe_load((ROOT/admission.WORKFLOW).read_text())
    assert 'maintainer-transition-approval' not in workflow['jobs']
    assert workflow['permissions']=={'contents':'read'}
    assert not (ROOT/'04_scripts/quality/governance_transition.py').exists()


def test_workflow_top_level_execution_settings_cannot_bypass_guard():
    assert not admission.workflow_contract_valid(b'name: CI\njobs: {}\n')
    source=(ROOT/admission.WORKFLOW).read_bytes()
    assert admission.workflow_contract_valid(source)
    assert admission.workflow_contract_valid(source+b'\n# same-candidate workflow maintenance\n')
    assert not admission.workflow_contract_valid(source.replace(b'contents: read',b'contents: write'))


# Platform planning preserves required coverage; integration remains separate.
def _platform_fixture():
    from quality import platform_test_plan as platforms
    path = "08_tests/test_mixed.py"
    source = b"def test_logic(): pass\ndef test_windows(): pass\n"
    policy = {"schema_version": "required-test-platforms/1", "files": {path: {
        "test_logic": {"kind": "CROSS_PLATFORM_TEST", "reason": "pure contract"},
        "test_windows": {"kind": "WINDOWS_REQUIRED_TEST", "reason": "Windows OS lock"}}}}
    identities = dict(base={"commit": "a"*40, "tree": "b"*40}, candidate={"commit": "c"*40, "tree": "d"*40})
    result = platforms.plan([path], {path:source}, {path:source}, policy, **identities)
    receipts = {platform:dict(schema_version="platform-test-result/1", **identities,
                plan_sha256=result["plan_sha256"], platform=platform,
                runner_os={"linux":"Linux", "windows":"Windows"}[platform], runner_environment="github-hosted",
                workflow_run_id="123", workflow_run_attempt="1",
                tests=[{"nodeid":row["selector"], "outcome":"passed"} for row in rows])
                for platform,rows in result["lanes"].items()}
    return platforms, path, source, policy, identities, result, receipts


def test_platform_plan_partitions_without_skip_or_loss():
    platforms,path,source,policy,identities,p,receipts = _platform_fixture()
    assert [r["selector"] for r in p["lanes"]["linux"]] == [path+"::test_logic"]
    assert [r["selector"] for r in p["lanes"]["windows"]] == [path+"::test_windows"]
    assert platforms.aggregate(p,receipts,workflow_run_id="123",workflow_run_attempt="1",
                               job_results={"linux":"success","windows":"success"})["result"] == "PASS"


@pytest.mark.parametrize("mutation", ["missing-windows","windows-fail","windows-skip","linux-fail","wrong-os","wrong-tree",
                                      "wrong-run","wrong-attempt","job-fail","empty","duplicate","unplanned","missing-case"])
def test_platform_aggregate_fails_closed(mutation):
    platforms,path,source,policy,identities,p,receipts = _platform_fixture()
    jobs={"linux":"success","windows":"success"}
    if mutation=="missing-windows": receipts.pop("windows")
    elif mutation=="windows-fail": receipts["windows"]["tests"][0]["outcome"]="failed"
    elif mutation=="windows-skip": receipts["windows"]["tests"][0]["outcome"]="skipped"
    elif mutation=="linux-fail": receipts["linux"]["tests"][0]["outcome"]="failed"
    elif mutation=="wrong-os": receipts["windows"]["runner_os"]="Linux"
    elif mutation=="wrong-tree": receipts["windows"]["candidate"]={"commit":"c"*40,"tree":"e"*40}
    elif mutation=="wrong-run": receipts["windows"]["workflow_run_id"]="old"
    elif mutation=="wrong-attempt": receipts["windows"]["workflow_run_attempt"]="0"
    elif mutation=="job-fail": jobs["windows"]="failure"
    elif mutation=="empty": receipts["windows"]["tests"]=[]
    elif mutation=="duplicate": receipts["windows"]["tests"]*=2
    elif mutation=="unplanned": receipts["linux"]["tests"].append(receipts["windows"]["tests"][0])
    else:
        p["lanes"]["windows"][0]["minimum_cases"]=2
        p["plan_sha256"]=platforms.digest({k:v for k,v in p.items() if k!="plan_sha256"})
        for r in receipts.values(): r["plan_sha256"]=p["plan_sha256"]
    assert platforms.aggregate(p,receipts,workflow_run_id="123",workflow_run_attempt="1",job_results=jobs)["result"]=="FAIL"


@pytest.mark.parametrize("candidate_source", [None, b"def test_logic(): pass\n"])
def test_platform_required_windows_test_cannot_be_deleted(candidate_source):
    platforms,path,source,policy,identities,p,receipts = _platform_fixture()
    with pytest.raises(ValueError, match="REQUIRED_TEST_REMOVED"):
        platforms.plan([path],{path:source},{} if candidate_source is None else {path:candidate_source},policy,**identities)


def test_platform_parameter_case_removal_rejected():
    platforms,path,source,policy,identities,p,receipts = _platform_fixture()
    source=b"import pytest\ndef test_logic(): pass\n@pytest.mark.parametrize('case',[1,2])\ndef test_windows(case): pass\n"
    with pytest.raises(ValueError,match="REQUIRED_TEST_REMOVED"):
        platforms.plan([path],{path:source},{path:source.replace(b'[1,2]',b'[1]')},policy,**identities)


def test_platform_unknown_added_mixed_test_cannot_evade_windows():
    platforms,path,source,policy,identities,p,receipts = _platform_fixture()
    new=source+b"def test_added(): pass\n"
    result=platforms.plan([path],{path:source},{path:new},policy,**identities)
    assert all(path+"::test_added" in [r["selector"] for r in rows] for rows in result["lanes"].values())


def test_platform_business_without_windows_dependency_needs_only_linux():
    platforms,path,source,policy,identities,p,receipts = _platform_fixture()
    p=platforms.plan(["08_tests/test_business.py"],{}, {"08_tests/test_business.py":b"def test_new(): pass"},policy,**identities)
    assert p["lanes"]["windows"]==[]
    receipts={"linux":dict(receipts["linux"],plan_sha256=p["plan_sha256"],tests=[{"nodeid":"08_tests/test_business.py::test_new","outcome":"passed"}])}
    assert platforms.aggregate(p,receipts,workflow_run_id="123",workflow_run_attempt="1",job_results={"linux":"success"})["result"]=="PASS"


def test_platform_wrapper_inventory_preserves_every_current_case():
    from quality import platform_test_plan as platforms
    policy=json.loads((ROOT/"04_scripts/quality/test_platforms.json").read_text(encoding="utf-8"))
    path="08_tests/pipelines/test_full_daily_windows_wrapper.py"
    source=(ROOT/path).read_bytes()
    p=platforms.plan([path],{path:source},{path:source},policy,
                    base={"commit":"a"*40,"tree":"b"*40},candidate={"commit":"c"*40,"tree":"d"*40})
    assert sum(v["minimum_cases"] for values in p["lanes"].values() for v in values)==75
    assert len(p["lanes"]["linux"])==46 and len(p["lanes"]["windows"])==6
    assert any("test_provider_preflight_is_read_only" in v["selector"] for v in p["lanes"]["linux"])
    assert any("test_os_releases_lock" in v["selector"] for v in p["lanes"]["windows"])

@pytest.mark.parametrize('passing',[True,False])
def test_atomic_governance_owner_mapping_workflow_and_source(fixture_repo,tmp_path,passing):
    repo,base=fixture_repo
    data=json.loads((repo/admission.REGISTRY).read_text())
    data['projects'].append(dict(project_id='new-wrapper',change_class='shared',status='ready',
        owned_paths=['03_src/shared.py','08_tests/test_wrapper.py'],shared_dependencies=[],
        forbidden_paths=[],required_tests=['08_tests/test_wrapper.py'],capabilities=['fixture'],boundary_notes='same candidate'))
    write(repo,admission.REGISTRY,json.dumps(data))
    write(repo,'03_src/shared.py','VALUE = '+('2' if passing else '3')+'\n')
    write(repo,'08_tests/test_wrapper.py','from shared import VALUE\ndef test_wrapper(): assert VALUE == 2\n')
    write(repo,admission.MAP,yaml.safe_dump({'modules':{'wrapper':{
        'code_paths':['03_src/shared.py'],'direct_tests':['08_tests/test_wrapper.py'],
        'impact_tests':['08_tests/test_alpha.py'],'dependents':[],'full_regression_when_changed':False}}}))
    write(repo,admission.WORKFLOW,(repo/admission.WORKFLOW).read_text()+'\n# atomic CI maintenance\n')
    head=commit(repo)
    result=admission.admit(repo,base,head,'auto',tmp_path/'evidence',executor=inert_executor)
    assert result['final_result']==('PASS' if passing else 'FAIL'),result
    assert result['checks']['CHANGE_CLASS']=='GOVERNANCE_OR_CI'
    assert result['checks']['MAINTAINER_REVIEW_REQUIRED']=='YES'
    assert set(result['test_plan'])=={'08_tests/test_wrapper.py','08_tests/test_alpha.py'}
    assert result['execution_environment']['production_access'] is False


def test_unowned_shared_source_gets_consumer_tests_without_owner_gate(fixture_repo,tmp_path):
    repo,base=fixture_repo
    write(repo,'03_src/shared.py','VALUE = 2\n')
    head=commit(repo)
    result=admission.admit(repo,base,head,'alpha',tmp_path/'evidence',executor=inert_executor)
    assert result['final_result']=='PASS',result
    assert result['lane']=='strict'
    assert result['checks']['unowned_paths']==['03_src/shared.py']
    assert set(result['test_plan'])=={'08_tests/test_alpha.py','08_tests/test_beta.py'}
    assert result['checks']['full_regression'] is True


def test_platform_plan_only_is_not_required_check_success(fixture_repo,tmp_path):
    repo,base=fixture_repo
    write(repo,'03_src/alpha.py','VALUE=2\n');head=commit(repo)
    def forbidden(*args): raise AssertionError('planning must not execute pytest')
    result=admission.admit(repo,base,head,'alpha',tmp_path/'evidence',executor=forbidden,plan_only=True)
    assert result['final_result']=='PLANNED',result
    assert result['checks']['TECHNICAL_VALIDATION']=='NOT_RUN'
    assert not admission.evidence_valid(result,repo,base,head)


@pytest.mark.parametrize('path,content', [
    ('09_deploy/runtime_identity/host_authorization.py', b'def issue_grant(): pass\n'),
    ('09_deploy/runtime_identity/candidate_validation_record.py', b'VALUE = 1\n'),
    ('09_deploy/runtime_identity/new_collector.py', b'VALUE = 1\n'),
    ('04_scripts/runtime/validate_target_runtime.py', b'VALUE = 1\n'),
    ('09_deploy/new_tool/grant.schema.json', b'{"type":"object","properties":{"schema_version":{"const":"production-execution-grant/3"},"private_key":{"type":"string"}}}'),
    ('09_deploy/new_tool/settings.json', b'{"timeout_seconds":60}'),
    ('09_deploy/new_tool/compose.production.yml', b'services: {app: {image: example}}'),
    ('09_deploy/new_tool/Dockerfile.app', b'FROM example'),
    ('09_deploy/new_tool/production.env.example', b'PUBLIC_URL=http://example.invalid'),
    ('09_deploy/new_tool/runtime.yml.j2', b'{{ runtime_template }}'),
    ('09_deploy/new_tool/deploy.sh', b'docker start "$1"\n'),
])
def test_production_tooling_definitions_are_source(path, content):
    assert admission.production_artifact_role(path, content) == 'PRODUCTION_TOOLING_SOURCE_CHANGE'


@pytest.mark.parametrize('path,content', [
    ('01_data/current.json', b'{}'),
    ('06_outputs/database.csv', b'data'),
    ('10_logs/run.json', b'{}'),
    ('09_deploy/live/probe.py', b'# still live state'),
    ('09_deploy/releases/current.json', b'{}'),
    ('09_deploy/runtime_identity/state/status.yaml', b'status: active'),
    ('09_deploy/runtime_identity/grants/issuer.py', b'# not a source namespace'),
    ('09_deploy/runtime_identity/evidence/observations.json', b'{}'),
    ('09_deploy/tool/release.json', b'{}'),
    ('09_deploy/tool/deployment_result.json', b'{}'),
    ('09_deploy/tool/production.env', b'PASSWORD=secret'),
    ('09_deploy/tool/.env.local', b'PASSWORD=secret'),
    ('09_deploy/tool/private.pem', b'private'),
    ('09_deploy/tool/database.sqlite', b'data'),
    ('09_deploy/tool/unknown.bin', b'unknown'),
    ('09_deploy/tool/config.json', b'not-json'),
    ('09_deploy/tool/config.json', b'{"schema_version":"production-execution-grant/3"}'),
    ('09_deploy/tool/config.yaml', b'schema_version: host-runtime-policy/5'),
    ('09_deploy/tool/config.example', b'{"schema_version":"controlled-runtime-result/1"}'),
    ('09_deploy/tool/config.json', b'{"signature":"signed","payload":{}}'),
    ('09_deploy/tool/config.json', b'{"container_id":"live","image_id":"live"}'),
    ('09_deploy/tool/config.json', b'{"release_id":"live","generated_at":"now"}'),
    ('09_deploy/tool/config.json', b'{"private_key":"secret"}'),
    ('09_deploy/tool/source.py', b'KEY="-----BEGIN OPENSSH PRIVATE KEY-----"'),
])
def test_live_state_credentials_and_unknown_artifacts_remain_blocked(path, content):
    assert admission.production_artifact_role(path, content) == 'PRODUCTION_MUTATION'


@pytest.mark.parametrize('passing', [True, False])
def test_tooling_source_enters_strict_impact_tests_without_production_authority(fixture_repo, tmp_path, passing):
    repo, base = fixture_repo
    path = '09_deploy/runtime_identity/host_authorization.py'
    mapping = {'modules': {'host': {'code_paths': [path], 'direct_tests': ['08_tests/test_alpha.py'],
                                  'impact_tests': ['08_tests/test_beta.py'], 'dependents': [],
                                  'full_regression_when_changed': False}}}
    write(repo, admission.MAP, yaml.safe_dump(mapping))
    base = commit(repo)
    write(repo, path, 'def issue_grant(): pass\n')
    if not passing: write(repo, '03_src/alpha.py', 'VALUE=99\n')
    head = commit(repo)
    result = admission.admit(repo, base, head, 'alpha', tmp_path/'evidence', executor=inert_executor)
    assert result['lane'] == 'strict'
    assert result['final_result'] == ('PASS' if passing else 'FAIL'), result
    assert 'UNAUTHORIZED_PRODUCTION_CHANGE' not in result['failure_codes']
    assert set(result['test_plan']) == {'08_tests/test_alpha.py', '08_tests/test_beta.py'}
    assert result['checks']['production_artifact_roles'][path] == 'PRODUCTION_TOOLING_SOURCE_CHANGE'
    assert result['checks']['PRODUCTION_RELEASE_AUTHORIZED'] is False
    assert result['execution_environment']['production_access'] is False
    assert result['candidate'] == {'commit': head, 'tree': git(repo, 'rev-parse', head+'^{tree}')}


@pytest.mark.parametrize('operation', ['add', 'modify', 'delete', 'rename', 'disguise'])
def test_admission_blocks_real_state_on_both_sides_of_diff(fixture_repo, tmp_path, operation):
    repo, base = fixture_repo
    state = '09_deploy/tool/config.json'
    live = '{"schema_version":"controlled-runtime-result/1","container_id":"live","image_id":"live"}'
    if operation != 'add':
        write(repo, state, live)
        base = commit(repo)
    if operation == 'add': write(repo, state, live)
    elif operation == 'modify': write(repo, state, live.replace('live', 'changed'))
    elif operation == 'delete': (repo/state).unlink()
    elif operation == 'rename': (repo/state).rename(repo/'09_deploy/tool/new-config.json')
    else: write(repo, state, '{"timeout_seconds":60}')
    head = commit(repo)
    def forbidden(*args): pytest.fail('production mutation must not reach test execution')
    result = admission.admit(repo, base, head, 'auto', tmp_path/'evidence', executor=forbidden)
    assert result['final_result'] == 'FAIL'
    assert 'UNAUTHORIZED_PRODUCTION_CHANGE' in result['failure_codes']
    assert result['checks']['PRODUCTION_RELEASE_AUTHORIZED'] is False
