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
    result = subprocess.run([sys.executable, "-I", "-m", "pytest", "-q", "-p", "no:cacheprovider",
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
    (admission.SCOPE, "governance"), (admission.MAP, "governance"),
    (admission.IMPLEMENTATION, "governance"), (admission.WORKFLOW, "governance"),
    (admission.SCHEMA, "governance"), ("03_src/conftest.py", "governance"),
])
def test_scope_and_self_judge_attacks(fixture_repo, tmp_path, path, category):
    repo, _ = fixture_repo
    write(repo, path, "# candidate declares itself PASS\n")
    result, _ = evaluate(fixture_repo, tmp_path, executor=lambda *a: pytest.fail("candidate must not execute"))
    assert result["final_result"] == "FAIL"
    assert "ESCALATION_REQUIRED" in result["failure_codes"]
    assert category in next(i for i in result["changed_paths"] if i["path"] == path)["classifications"]


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


def test_weakening_tests_does_not_hide_failure(fixture_repo, tmp_path):
    repo, _ = fixture_repo
    write(repo, "03_src/alpha.py", "VALUE = 99\n")
    write(repo, "08_tests/test_alpha.py", "def test_fake():\n    assert True\n")
    result, _ = evaluate(fixture_repo, tmp_path)
    assert result["final_result"] == "FAIL"
    assert "REQUIRED_TEST_FAILED" in result["failure_codes"]
    assert result["test_result"]["test_count"] == 1


def test_deleted_trusted_test_still_executes(fixture_repo, tmp_path):
    repo, _ = fixture_repo
    (repo / "08_tests/test_alpha.py").unlink()
    result, _ = evaluate(fixture_repo, tmp_path)
    assert result["test_result"]["result"] == "PASS"
    assert result["test_result"]["test_count"] == 1
    assert result["final_result"] == "FAIL"  # old Completion also rejects deletion
    assert "TRUSTED_TEST_DELETED" in result["failure_codes"]


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


def test_candidate_cannot_supply_executor(fixture_repo, tmp_path):
    repo, _ = fixture_repo
    write(repo, admission.IMPLEMENTATION, "print('PASS')\n")
    forged_base = commit(repo)
    write(repo, "03_src/alpha.py", "VALUE = 2\n")
    head = commit(repo)
    result = admission.admit(repo, forged_base, head, "alpha", tmp_path / "evidence")
    assert result["final_result"] == "FAIL"
    assert "EXECUTOR_NOT_TRUSTED_MAIN" in result["failure_codes"]


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
    assert not (not old_pass and new["final_result"] == "PASS"), "SHADOW_MISMATCH=BLOCKER"
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
    assert jobs["final"]["if"] == "always()"
    raw = (ROOT / admission.WORKFLOW).read_text()
    assert '${{ secrets.' not in raw
    assert "environment:" not in raw
    for step in jobs["shadow"]["steps"]:
        if "uses" in step:
            assert __import__('re').fullmatch(r"actions/[a-z-]+@[0-9a-f]{40}", step['uses'])
        if step.get('with', {}).get('path') in {'trusted','candidate'}:
            assert step['with']['persist-credentials'] is False
    assert "python -I trusted/04_scripts/quality/main_admission.py" in raw


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
    write(repo, admission.MAP, "modules: {}\nrequired_tests: []\n")
    result, _ = evaluate(fixture_repo, tmp_path)
    assert result["test_plan"] == ["08_tests/test_alpha.py"]
    assert result["final_result"] == "FAIL"


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
    assert 'ESCALATION_REQUIRED' in result['failure_codes']
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
    assert result['test_plan'] == [tests+'/test_extra.py', tests+'/test_project.py']
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
                'production':'09_deploy/evil.py', 'admission':admission.IMPLEMENTATION,
                'scope':admission.SCOPE, 'registry-code':'04_scripts/quality/project_registry.py',
                'pytest-policy':'pyproject.toml', 'conftest':tests+'/conftest.py',
                'foreign-file':'03_src/beta.py'}[attack]
        write(repo, path, '# candidate cannot change judge\n')
    write(repo, admission.REGISTRY, json.dumps(data))
    head = commit(repo)
    def forbidden(*args): pytest.fail('Denied bootstrap executed candidate tests')
    result = admission.admit(repo, base, head, 'auto', tmp_path/'evidence', executor=forbidden)
    assert result['final_result'] == 'FAIL'
    assert 'ESCALATION_REQUIRED' in result['failure_codes'], result


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
    assert 'ESCALATION_REQUIRED' in result['failure_codes']
