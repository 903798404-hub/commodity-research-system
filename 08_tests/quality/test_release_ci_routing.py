"""Keep expensive lifecycle replay tied to actual release consumers."""
import copy
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
