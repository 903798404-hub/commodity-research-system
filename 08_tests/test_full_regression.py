"""Full debt comparison never weakens required/platform results."""
import copy
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('full_regression_under_test', ROOT / '04_scripts/quality/full_regression.py')
full = importlib.util.module_from_spec(spec)
spec.loader.exec_module(full)


def fixture(base=None, candidate=None):
    base = base or {'08_tests/test_example.py::test_good': 'passed'}
    candidate = candidate if candidate is not None else dict(base)
    identities = {s: dict(commit=c * 40, tree=t * 40) for s, c, t in [('base', 'a', 'b'), ('candidate', 'c', 'd')]}
    plan = full.seal(dict(identities=identities, plans={s: {'lanes': {'linux': [{'selector': '08_tests/test_example.py'}]}} for s in identities}))
    receipts = {}
    for side, tests in [('base', base), ('candidate', candidate)]:
        receipts[side] = full.seal(dict(schema_version='full-regression-result/1', side=side,
            identity=identities[side], plan_sha256=plan['sha256'], workflow_run_id='123', workflow_run_attempt='1',
            environment={'python': '3.12', 'font': 'Noto'}, exit_code=int('failed' in tests.values()),
            session_finished=True, collection_errors=[], mutated=False, collected_nodes=sorted(tests),
            collected_files=['08_tests/test_example.py'], tests=tests))
    return plan, receipts


def compare(plan, receipts, **kwargs):
    return full.compare(plan, receipts, run_id='123', attempt='1', job_result=kwargs.get('job_result', 'success'))


def test_green_base_stays_green():
    p, r = fixture()
    assert compare(p, r)['result'] == 'PASS'
    assert compare(p, r)['baseline_green'] is True


@pytest.mark.parametrize('state', ['failed', 'skipped'])
def test_green_base_cannot_turn_red_or_skip(state):
    p, r = fixture(candidate={'08_tests/test_example.py::test_good': state})
    assert compare(p, r)['result'] == 'FAIL'


def test_dynamic_debt_and_improvements_and_added_pass():
    b = {'08_tests/test_example.py::test_' + n: s for n, s in [('f', 'failed'), ('s', 'skipped'), ('fix', 'failed'), ('unskip', 'skipped')]}
    c = {**b, '08_tests/test_example.py::test_fix': 'passed', '08_tests/test_example.py::test_unskip': 'passed', '08_tests/test_example.py::test_new': 'passed'}
    p, r = fixture(b, c)
    result = compare(p, r)
    assert result['result'] == 'PASS'
    for category in ('PRE_EXISTING_FAILURE', 'PRE_EXISTING_SKIP', 'RESOLVED_FAILURE', 'RESOLVED_SKIP', 'ADDED_PASS'):
        assert result['counts'][category + '_COUNT'] == 1
    # Once fixed on main, the same failing node is no longer debt.
    p, r = fixture(c, b)
    assert compare(p, r)['result'] == 'FAIL'


@pytest.mark.parametrize('before,after', [('failed', 'skipped'), ('skipped', 'failed')])
def test_switching_debt_outcome_is_a_new_regression(before, after):
    key = '08_tests/test_example.py::test_debt'
    p, r = fixture({key: before}, {key: after})
    assert compare(p, r)['result'] == 'FAIL'


@pytest.mark.parametrize('state', ['passed', 'failed', 'skipped'])
def test_deleting_any_collected_base_node_fails(state):
    p, r = fixture({'08_tests/test_example.py::test_keep': 'passed', '08_tests/test_example.py::test_removed[2]': state}, {'08_tests/test_example.py::test_keep': 'passed'})
    result = compare(p, r)
    assert result['result'] == 'FAIL'
    assert result['nodes']['REMOVED_BASE_TEST_NODE'] == ['08_tests/test_example.py::test_removed[2]']


@pytest.mark.parametrize('state', ['failed', 'skipped'])
def test_added_tests_must_pass(state):
    p, r = fixture(candidate={'08_tests/test_example.py::test_good': 'passed', '08_tests/test_example.py::test_new': state})
    assert compare(p, r)['result'] == 'FAIL'


@pytest.mark.parametrize('field,value', [
    ('exit_code', 2), ('exit_code', 3), ('exit_code', 4), ('exit_code', 5),
    ('session_finished', False), ('collection_errors', ['broken import']), ('mutated', True),
    ('collected_nodes', []), ('collected_nodes', ['08_tests/test_example.py::test_missing']),
])
def test_incomplete_collection_and_infrastructure_never_count_as_debt(field, value):
    p, r = fixture()
    r['base'][field] = value
    r['base'] = full.seal({k: v for k, v in r['base'].items() if k != 'sha256'})
    with pytest.raises(ValueError, match='COLLECTION_OR_INFRASTRUCTURE'):
        compare(p, r)


@pytest.mark.parametrize('field,value', [('identity', {'commit': 'e' * 40, 'tree': 'f' * 40}),
    ('plan_sha256', 'old'), ('workflow_run_id', 'old'), ('workflow_run_attempt', '0'), ('side', 'candidate')])
def test_old_base_or_wrong_run_or_plan_receipt_fails(field, value):
    p, r = fixture()
    r['base'][field] = value
    r['base'] = full.seal({k: v for k, v in r['base'].items() if k != 'sha256'})
    with pytest.raises(ValueError, match='IDENTITY_MISMATCH'):
        compare(p, r)


@pytest.mark.parametrize('field', ['python', 'dependencies', 'system_packages', 'font', 'pytest_config', 'runner_image_version', 'variables'])
def test_environment_mismatch_is_not_debt(field):
    p, r = fixture()
    r['candidate']['environment'][field] = 'different'
    r['candidate'] = full.seal({k: v for k, v in r['candidate'].items() if k != 'sha256'})
    with pytest.raises(ValueError, match='ENVIRONMENT_MISMATCH'):
        compare(p, r)


@pytest.mark.parametrize('status', ['failure', 'cancelled', 'skipped', None])
def test_unsuccessful_hosted_job_is_not_debt(status):
    p, r = fixture()
    with pytest.raises(ValueError, match='JOB_OR_RECEIPT_MISSING'):
        compare(p, r, job_result=status)


def test_missing_receipt_or_digest_corruption_fails():
    p, r = fixture()
    with pytest.raises(ValueError, match='JOB_OR_RECEIPT_MISSING'):
        compare(p, {'candidate': r['candidate']})
    r['base']['tests']['08_tests/test_example.py::test_good'] = 'failed'
    with pytest.raises(ValueError, match='DIGEST_MISMATCH'):
        compare(p, r)


def test_collection_plugin_records_collection_errors_and_complete_nodes():
    from types import SimpleNamespace
    plugin = full.Collection()
    plugin.pytest_collectreport(SimpleNamespace(failed=True, skipped=False, nodeid='broken.py'))
    plugin.pytest_collection_finish(SimpleNamespace(items=[SimpleNamespace(nodeid='test.py::test_x')]))
    plugin.pytest_sessionfinish(None, 2)
    assert plugin.collection_errors == ['broken.py']
    assert plugin.nodes == ['test.py::test_x']
    assert plugin.session_finished


def test_full_fallback_separation_keeps_explicit_impact_tests():
    mapping = {'modules': {'shared': {'code_paths': ['shared.py'], 'direct_tests': ['08_tests/test_direct.py'],
        'impact_tests': ['08_tests/test_consumer.py'], 'full_regression_when_changed': True}}}
    tests, _, required = full.ci.admission.impact_plan(mapping, ['shared.py'], {'08_tests/test_debt.py': {}}, include_full=False)
    assert required
    assert set(tests) == {'08_tests/test_direct.py', '08_tests/test_consumer.py'}


def test_required_platform_failure_and_skip_still_fail():
    platforms = full.ci.platforms
    identities = dict(commit='a' * 40, tree='b' * 40)
    source = b'def test_good(): pass\n'
    plan = platforms.plan(['08_tests/test_example.py'], {'08_tests/test_example.py': source},
        {'08_tests/test_example.py': source}, {'schema_version': 'required-test-platforms/1', 'files': {}}, base=identities, candidate=identities)
    for state in ['failed', 'skipped']:
        receipt = dict(schema_version='platform-test-result/1', base=identities, candidate=identities,
            plan_sha256=plan['plan_sha256'], platform='linux', runner_os='Linux', runner_environment='github-hosted',
            workflow_run_id='123', workflow_run_attempt='1', tests=[dict(nodeid='08_tests/test_example.py::test_good', outcome=state)])
        assert platforms.aggregate(plan, {'linux': receipt}, workflow_run_id='123', workflow_run_attempt='1', job_results={'linux': 'success'})['result'] == 'FAIL'


@pytest.mark.parametrize('lane,expected', [('business', False), ('strict', True), ('governance', True)])
def test_governance_and_strict_both_require_full_comparison(lane, expected):
    assert full.ci.requires_full({'lane': lane}) is expected


def test_unknown_lane_cannot_silently_skip_full_comparison():
    with pytest.raises(ValueError, match='UNKNOWN_ADMISSION_LANE'):
        full.ci.requires_full({'lane': 'unknown'})


def test_full_import_time_assertion_file_must_be_collected_but_needs_no_fake_node():
    p, r = fixture()
    for s in ['base', 'candidate']:
        p['plans'][s]['lanes']['linux'].append({'selector': '08_tests/test_import_checks.py', 'minimum_cases': 1})
        r[s]['collected_files'].append('08_tests/test_import_checks.py')
    p = full.seal({k: v for k, v in p.items() if k != 'sha256'})
    for s in r:
        r[s]['plan_sha256'] = p['sha256']
        r[s] = full.seal({k: v for k, v in r[s].items() if k != 'sha256'})
    assert compare(p, r)['result'] == 'PASS'
    r['candidate']['collected_files'].remove('08_tests/test_import_checks.py')
    r['candidate'] = full.seal({k: v for k, v in r['candidate'].items() if k != 'sha256'})
    with pytest.raises(ValueError, match='FULL_REQUIRED_COLLECTION_MISSING'):
        compare(p, r)


def test_import_time_failure_is_collection_failure_not_legacy_debt():
    from types import SimpleNamespace
    plugin = full.Collection()
    plugin.pytest_collectreport(SimpleNamespace(nodeid='08_tests/test_import_checks.py', failed=True, skipped=False))
    assert plugin.collected_files == {'08_tests/test_import_checks.py'}
    assert plugin.collection_errors == ['08_tests/test_import_checks.py']


def test_function_selector_nodes_prove_parent_collection_without_parent_report():
    from types import SimpleNamespace
    plugin = full.Collection()
    plugin.pytest_collection_finish(SimpleNamespace(items=[SimpleNamespace(nodeid='08_tests/test_selected.py::test_case[1]')]))
    assert plugin.collected_files == {'08_tests/test_selected.py'}
    assert plugin.nodes == ['08_tests/test_selected.py::test_case[1]']
