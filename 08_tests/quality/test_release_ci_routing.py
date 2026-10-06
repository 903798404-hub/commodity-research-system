"""Keep expensive lifecycle replay tied to actual release consumers."""
import copy
import ast
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('release_ci_routing', ROOT / '04_scripts/quality/platform_ci.py')
ci = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ci)
MANIFEST = json.loads((ROOT / ci.RUNTIME_CONTRACT).read_text(encoding='utf-8'))
EXEMPT = [
    'requirements-dev.in', 'requirements-dev.txt',
    '04_scripts/quality/locks/windows-py312.in',
    '04_scripts/quality/locks/windows-py312.txt',
    '04_scripts/quality/locks/aggregate-py312.in',
    '04_scripts/quality/locks/aggregate-py312.txt',
    '08_tests/shared/test_module_test_map.py',
    '04_scripts/quality/full_regression.py',
    '04_scripts/quality/platform_test_plan.py',
    '04_scripts/quality/required_lane_fixtures.py',
    '08_tests/test_full_regression.py', '08_tests/test_main_admission.py',
    '08_tests/test_documentation_contract.py',
    '08_tests/quality/test_release_ci_routing.py',
    '08_tests/quality/test_required_lane_fixtures.py',
    '08_tests/quality/test_ci_dependency_locks.py',
]


def report(*paths):
    return dict(trusted_main={'commit': 'base'}, candidate={'commit': 'candidate'},
                changed_paths=[{'path': p} for p in paths])


@pytest.mark.parametrize('path', EXEMPT)
def test_known_nonconsumers_avoid_lifecycle_but_keep_required_and_full_tests(monkeypatch, path):
    calls = []
    def blob(repo, commit, name):
        calls.append((commit, name))
        return json.dumps(MANIFEST).encode()
    monkeypatch.setattr(ci.admission, 'blob', blob)
    value = report(path)
    assert not ci.requires_spread_release_e2e(value, ROOT)
    assert not ci.requires_spread_runtime_docker(value, ROOT)
    assert calls == [('base', ci.RUNTIME_CONTRACT), ('candidate', ci.RUNTIME_CONTRACT)]
    assert ci.requires_full({'lane': 'governance'})


@pytest.mark.parametrize('change', [
    'base-source', 'candidate-source', 'base-dependency', 'candidate-dependency',
    'base-dockerfile', 'candidate-dockerignore', 'base-compose',
    'missing-identity', 'invalid-json', 'empty-source', 'invalid-source',
    'missing-build', 'invalid-dependency',
])
def test_runtime_consumption_and_unknown_inventory_override_ci_exemptions(monkeypatch, change):
    path = 'requirements-dev.txt'
    def blob(repo, commit, name):
        value = copy.deepcopy(MANIFEST)
        if change == commit + '-source':
            value['source_inputs'].append(dict(path=path, role='resource'))
        elif change == commit + '-dependency':
            value['build']['dependency_contracts'].append(path)
        elif change == commit + '-dockerfile':
            value['build']['dockerfile'] = path
        elif change == commit + '-dockerignore':
            value['build']['dockerignore'] = path
        elif change == commit + '-compose':
            value['build']['compose_sources'].append(path)
        elif change == 'invalid-json':
            return b'{'
        elif change == 'empty-source':
            value['source_inputs'] = []
        elif change == 'invalid-source':
            value['source_inputs'].append(dict(path=path, role=None))
        elif change == 'missing-build':
            del value['build']
        elif change == 'invalid-dependency':
            value['build']['dependency_contracts'] = [None]
        return json.dumps(value).encode()
    monkeypatch.setattr(ci.admission, 'blob', blob)
    value = report(path)
    if change == 'missing-identity':
        del value['trusted_main']
    assert ci.requires_spread_release_e2e(value, ROOT)


@pytest.mark.parametrize('path', [
    '04_scripts/quality/locks/linux-py312.txt',
    '04_scripts/quality/locks/host-tools-py310.txt',
    '04_scripts/quality/locks/browser-py312.txt',
    '04_scripts/quality/new_tool.py', '04_scripts/quality/main_admission.py',
    '04_scripts/quality/platform_ci.py', '08_tests/shared/new_release_test.py',
    '08_tests/shared/high_risk_execution_docker_e2e.py',
    '08_tests/shared/test_production_grant.py',
    '09_deploy/spread_release/high_risk_execution.py',
    '04_scripts/runtime/routine_release.py', 'requirements.in', 'requirements.txt',
    '.github/workflows/trusted-main-admission.yml', 'Dockerfile',
])
def test_release_inputs_unknown_paths_and_mixed_changes_still_replay(monkeypatch, path):
    monkeypatch.setattr(ci.admission, 'blob', lambda *args: json.dumps(MANIFEST).encode())
    assert ci.requires_spread_release_e2e(report(path), ROOT)
    assert ci.requires_spread_release_e2e(report(*EXEMPT, path), ROOT)


def test_exempt_lock_profiles_are_confined_to_their_audited_ci_consumers():
    workflow = yaml.safe_load((ROOT / '.github/workflows/trusted-main-admission.yml').read_text(encoding='utf-8'))
    for lock, expected_job in [('windows-py312', 'windows'), ('aggregate-py312', 'final')]:
        consumers = {name for name, job in workflow['jobs'].items() if lock in json.dumps(job)}
        assert consumers == {expected_job}
    assert 'requirements-dev.' not in json.dumps(workflow)


def test_mapping_test_is_not_imported_by_the_release_or_runtime_consumers():
    consumers = [*list((ROOT / '04_scripts/runtime').rglob('*.py')),
                 *list((ROOT / '09_deploy').rglob('*.py')),
                 *list((ROOT / '03_src/agri_research_agent/shared').rglob('*.py')),
                 *list((ROOT / '08_tests/shared').glob('*e2e.py')),
                 ROOT / '08_tests/shared/host_release_timestamp_compatibility.py']
    assert consumers
    for path in consumers:
        assert 'test_module_test_map' not in path.read_text(encoding='utf-8'), path


@pytest.mark.parametrize('path', [
    '.github/workflows/trusted-main-admission.yml', '04_scripts/quality/platform_ci.py',
])
def test_ci_wiring_uses_real_success_and_rollback_without_unrelated_fault_replays(monkeypatch, path):
    monkeypatch.setattr(ci.admission, 'blob', lambda *args: json.dumps(MANIFEST).encode())
    assert ci.spread_release_profile(report(path), ROOT) == 'ci-wiring'
    assert ci.spread_release_profile(report(*EXEMPT, path, '07_docs/review.md'), ROOT) == 'ci-wiring'
    assert ci.spread_release_profile(report(path, '09_deploy/runtime_identity/host_authorization.py'), ROOT) == 'full'


@pytest.mark.parametrize('path', [
    '04_scripts/runtime/pre_release_runtime.py', '03_src/agri_research_agent/shared/runtime_context.py',
    '08_tests/shared/high_risk_execution_docker_e2e.py', '04_scripts/quality/new_tool.py',
    '09_deploy/spread_release/high_risk_execution.py',
])
def test_release_consumers_and_unknown_inputs_keep_all_six_real_cases(monkeypatch, path):
    monkeypatch.setattr(ci.admission, 'blob', lambda *args: json.dumps(MANIFEST).encode())
    assert ci.spread_release_profile(report(path), ROOT) == 'full'
    assert ci.spread_release_profile(report(*ci.CI_WIRING_FILES, path), ROOT) == 'full'


@pytest.mark.parametrize('commit', ['base', 'candidate'])
def test_wiring_packaged_by_either_identity_is_not_a_smoke_exemption(monkeypatch, commit):
    def blob(repo, identity, path):
        manifest = copy.deepcopy(MANIFEST)
        if identity == commit:
            manifest['source_inputs'].append(dict(path='04_scripts/quality/platform_ci.py', role='resource'))
        return json.dumps(manifest).encode()
    monkeypatch.setattr(ci.admission, 'blob', blob)
    assert ci.spread_release_profile(report('04_scripts/quality/platform_ci.py'), ROOT) == 'full'


def test_audited_ci_helpers_are_not_loaded_by_any_release_or_application_consumer():
    stems = {Path(path).stem for path in EXEMPT if path.endswith('.py')}
    consumers = [*list((ROOT / '04_scripts/runtime').rglob('*.py')),
                 *list((ROOT / '09_deploy').rglob('*.py')),
                 *list((ROOT / '03_src').rglob('*.py')),
                 *list((ROOT / '08_tests/shared').glob('*e2e.py')),
                 ROOT / '08_tests/shared/host_release_timestamp_compatibility.py']
    assert consumers
    for path in consumers:
        for node in ast.walk(ast.parse(path.read_bytes())):
            if isinstance(node, ast.Import):
                assert not any(alias.name.split('.')[-1] in stems for alias in node.names), path
            elif isinstance(node, ast.ImportFrom):
                assert (node.module or '').split('.')[-1] not in stems, path
                assert not any(alias.name in stems for alias in node.names), path
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert not any(node.value == stem + '.py' or node.value.endswith('/' + stem + '.py')
                               or node.value == '04_scripts.quality.' + stem for stem in stems), path


def test_producer_profiles_and_workflow_match_the_sealed_plan_contract():
    source = ast.parse((ROOT / '08_tests/shared/high_risk_execution_docker_e2e.py').read_bytes())
    definitions = [node for node in source.body if isinstance(node, ast.Assign)
                   and any(isinstance(target, ast.Name) and target.id == 'CASE_PROFILES' for target in node.targets)]
    assert len(definitions) == 1
    producer = ast.literal_eval(definitions[0].value)
    assert {name: tuple(case for case, _ in paths) for name, paths in producer.items()} == ci.RELEASE_CASE_PROFILES
    workflow = yaml.safe_load((ROOT / '.github/workflows/trusted-main-admission.yml').read_text(encoding='utf-8'))
    assert workflow['jobs']['plan']['outputs']['spread_release_profile'] == '${{ steps.plan.outputs.spread_release_profile }}'
    replay = next(step for step in workflow['jobs']['linux']['steps'] if 'high_risk_execution_docker_e2e.py' in step.get('run', ''))
    assert "--case-profile '${{ needs.plan.outputs.spread_release_profile }}'" in replay['run']
    assert 'GITHUB_RUN_ATTEMPT="$GITHUB_RUN_ATTEMPT"' in replay['run']


def replay_fixture(tmp_path, profile):
    plan = dict(candidate=dict(commit='a' * 40, tree='b' * 40), spread_release_e2e=True,
                spread_release_profile=profile)
    names = ci.RELEASE_CASE_PROFILES[profile]
    injections = dict(success='NONE', failure='POST_START', authorization='AUTHORIZATION',
                      **{'aged-authorization': 'AUTHORIZATION', 'directory-drift': 'DIRECTORY_DRIFT', 'rollback-timeout': 'ROLLBACK_TIMEOUT'})
    raw = b'{"actual_probe":"PASS"}'
    root = tmp_path / 'linux/high-risk-execution-docker-evidence'
    root.mkdir(parents=True)
    (root / 'probe.json').write_bytes(raw)
    receipt = dict(status='PASS', candidate=plan['candidate'], case_profile=profile, required_cases=list(names),
                   workflow_run_id='123', workflow_run_attempt='1', temporary_private_keys_removed=True,
                   production_acceptance='NOT_EXECUTED',
                   paths=[dict(case=name, status='PASS', path=injections[name]) for name in names],
                   expired_record=dict(original_verifier_rejected=True),
                   execution_output_allocation=dict(result='PASS', uid=0, gid=0, mode='0o700',
                       previous_evidence_preserved=True, rejected=['previous-attempt', 'public-directory', 'symlink', 'missing-parent', 'relative']),
                   evidence_index=[dict(artifact_path='probe.json', sha256=hashlib.sha256(raw).hexdigest())])
    ci.save(tmp_path / 'linux/high-risk-execution-docker-evidence.json', receipt)
    return plan, receipt


@pytest.mark.parametrize('profile', ['ci-wiring', 'full'])
def test_final_gate_accepts_only_the_actual_planned_profile(tmp_path, profile):
    plan, _ = replay_fixture(tmp_path, profile)
    result = ci.validate_release_replay(plan, tmp_path, '123', '1')
    assert result['result'] == 'PASS' and result['profile'] == profile
    assert result['cases'] == list(ci.RELEASE_CASE_PROFILES[profile])
    assert result['indexed_evidence_verified'] == 1 and result['production_authorized'] is False


@pytest.mark.parametrize('fault', [
    'missing', 'wrong-profile', 'partial', 'duplicate', 'failed', 'wrong-injection',
    'wrong-commit', 'wrong-run', 'wrong-attempt', 'private-key-retained', 'expiry-missing',
    'allocation-missing', 'index-missing', 'index-tamper', 'index-traversal', 'unknown-profile',
    'wrong-declared-cases',
])
def test_final_gate_rejects_missing_partial_and_cross_run_replays(tmp_path, fault):
    plan, receipt = replay_fixture(tmp_path, 'full')
    path = tmp_path / 'linux/high-risk-execution-docker-evidence.json'
    if fault == 'missing':
        path.unlink()
    elif fault == 'wrong-profile': receipt['case_profile'] = 'ci-wiring'
    elif fault == 'partial': receipt['paths'].pop()
    elif fault == 'duplicate': receipt['paths'][-1] = receipt['paths'][0]
    elif fault == 'failed': receipt['paths'][0]['status'] = 'FAIL'
    elif fault == 'wrong-injection': receipt['paths'][0]['path'] = 'AUTHORIZATION'
    elif fault == 'wrong-commit': receipt['candidate'] = dict(commit='c' * 40, tree='b' * 40)
    elif fault == 'wrong-run': receipt['workflow_run_id'] = '122'
    elif fault == 'wrong-attempt': receipt['workflow_run_attempt'] = '2'
    elif fault == 'private-key-retained': receipt['temporary_private_keys_removed'] = False
    elif fault == 'expiry-missing': del receipt['expired_record']
    elif fault == 'allocation-missing': del receipt['execution_output_allocation']
    elif fault == 'index-missing': receipt['evidence_index'] = []
    elif fault == 'index-tamper': (tmp_path / 'linux/high-risk-execution-docker-evidence/probe.json').write_bytes(b'tampered')
    elif fault == 'index-traversal': receipt['evidence_index'][0]['artifact_path'] = '../probe.json'
    elif fault == 'unknown-profile': plan['spread_release_profile'] = 'fastest'
    elif fault == 'wrong-declared-cases': receipt['required_cases'] = ['success', 'failure']
    if fault != 'missing': ci.save(path, receipt)
    with pytest.raises((ValueError, FileNotFoundError)):
        ci.validate_release_replay(plan, tmp_path, '123', '1')


@pytest.mark.parametrize('platform_result', ['PASS', 'FAIL'])
def test_main_aggregate_requires_actual_replay_before_pass_and_preserves_platform_failures(tmp_path, monkeypatch, platform_result):
    plan = dict(candidate=dict(commit='a' * 40, tree='b' * 40), spread_release_e2e=True,
                spread_release_profile='full', plan_sha256='sealed-plan', base={}, lanes={'linux': []})
    ci.save(tmp_path / 'plan.json', plan)
    ci.save(tmp_path / 'admission/main-admission.json', dict(lane='business', checks={}, test_plan=[]))
    monkeypatch.setattr(ci.platforms, 'aggregate', lambda *a, **k: dict(result=platform_result, failure_codes=[] if platform_result == 'PASS' else ['MISSING_PLATFORM_RECEIPT']))
    monkeypatch.setenv('GITHUB_RUN_ID', '123')
    monkeypatch.setenv('GITHUB_RUN_ATTEMPT', '1')
    output = tmp_path / 'final/main-admission.json'
    if platform_result == 'PASS':
        with pytest.raises(FileNotFoundError):
            ci.finish(tmp_path / 'plan.json', tmp_path / 'receipts', output, {'linux': 'success'})
        assert not output.exists()
    else:
        import jsonschema
        monkeypatch.setattr(jsonschema, 'validate', lambda *a: None)
        assert ci.finish(tmp_path / 'plan.json', tmp_path / 'receipts', output, {'linux': 'failure'}) == 1
        result = ci.read(output)
        assert result['final_result'] == result['checks']['MAIN_ENTRY'] == 'FAIL'
        assert result['failure_codes'] == ['MISSING_PLATFORM_RECEIPT']
