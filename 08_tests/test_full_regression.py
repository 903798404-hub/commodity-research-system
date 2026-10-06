"""All-green main validation and historical diagnosis retain strict identity."""
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
    plan = full.seal(dict(identities=identities, required_plan_sha256='e'*64, plans={s: {'lanes': {'linux': [{'selector': '08_tests/test_example.py'}]}} for s in identities}))
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

@pytest.mark.parametrize('lane',['business','strict','governance'])
def test_full_lane_includes_windows_even_without_direct_windows_impact(tmp_path,monkeypatch,lane):
    import json
    ci=full.ci
    policy={'files':{'08_tests/test_windows.py':{'test_windows':{'kind':'WINDOWS_REQUIRED_TEST'}}}}
    report=dict(final_result='PLANNED',lane=lane,test_plan=['08_tests/test_logic.py'],
                trusted_main={},candidate={},changed_paths=[{'path':'03_src/logic.py'}])
    manifest = tmp_path / ci.RUNTIME_CONTRACT
    manifest.parent.mkdir(parents=True)
    manifest.write_bytes((ROOT / ci.RUNTIME_CONTRACT).read_bytes())
    monkeypatch.setattr(ci.admission,'admit',lambda *a,**k:copy.deepcopy(report))
    monkeypatch.setattr(ci.admission,'tree',lambda *a:{ci.POLICY:{},'08_tests/test_logic.py':{},'08_tests/test_windows.py':{}})
    monkeypatch.setattr(ci.admission,'blob',lambda repo,commit,path:json.dumps(policy).encode() if path==ci.POLICY else b'def test_logic(): pass')
    monkeypatch.setattr(ci.platforms,'plan',lambda required,*a,**k:{'selected':required})
    result=ci.make_plan(tmp_path,'base','candidate',tmp_path/'out')
    assert ('08_tests/test_windows.py' in result['selected']) == (lane!='business')


def test_release_replay_always_requires_real_image_acceptance(tmp_path, monkeypatch):
    import json
    ci = full.ci
    policy = {'files': {}}
    report = dict(final_result='PLANNED', lane='governance', test_plan=['08_tests/test_logic.py'],
                  trusted_main={}, candidate={}, changed_paths=[{'path':'04_scripts/quality/platform_ci.py'}])
    monkeypatch.setattr(ci.admission, 'admit', lambda *a, **k: copy.deepcopy(report))
    monkeypatch.setattr(ci.admission, 'tree', lambda *a: {ci.POLICY:{}, '08_tests/test_logic.py':{}})
    monkeypatch.setattr(ci.admission, 'blob', lambda repo, commit, path:
                        json.dumps(policy).encode() if path == ci.POLICY else b'def test_logic(): pass')
    monkeypatch.setattr(ci.platforms, 'plan', lambda required, *a, **k: {'selected':required})
    # A newly covered release control may not be in the old image-source map.
    monkeypatch.setattr(ci, 'requires_spread_runtime_docker', lambda *a: False)
    result = ci.make_plan(tmp_path, 'base', 'candidate', tmp_path / 'out')
    assert result['spread_release_e2e'] is True
    assert result['spread_runtime_docker'] is True
    assert result['plan_sha256'] == ci.platforms.digest({k:v for k,v in result.items() if k!='plan_sha256'})


def test_all_green_main_validation_needs_only_candidate_receipt():
    p, r = fixture()
    result = full.validate_candidate(p,r['candidate'],run_id='123',attempt='1',job_result='success')
    assert result['result']=='PASS' and result['policy']=='ALL_GREEN'
    assert result['required_plan_sha256']==p['required_plan_sha256']
    assert result['test_counts']==dict(passed=1,failed=0,skipped=0)
    assert result['receipt_sha256']==r['candidate']['sha256']


@pytest.mark.parametrize('outcome',['failed','skipped'])
def test_all_green_rejects_even_preexisting_debt(outcome):
    tests={'08_tests/test_example.py::test_good':outcome}
    p,r=fixture(tests,tests)
    assert compare(p,r)['result']=='PASS'  # Historical diagnosis, never the main gate.
    assert full.validate_candidate(p,r['candidate'],run_id='123',attempt='1',job_result='success')['result']=='FAIL'


@pytest.mark.parametrize('status',['failure','cancelled','skipped',None])
def test_all_green_cannot_use_unsuccessful_job(status):
    p,r=fixture()
    with pytest.raises(ValueError,match='JOB_OR_RECEIPT_MISSING'):
        full.validate_candidate(p,r['candidate'],run_id='123',attempt='1',job_result=status)


@pytest.mark.parametrize('field,value',[
    ('identity',{'commit':'f'*40,'tree':'a'*40}),('plan_sha256','old'),
    ('workflow_run_id','old'),('workflow_run_attempt','0'),('side','base')])
def test_all_green_cannot_reuse_wrong_identity_or_run(field,value):
    p,r=fixture();receipt=r['candidate'];receipt[field]=value
    receipt=full.seal({k:v for k,v in receipt.items() if k!='sha256'})
    with pytest.raises(ValueError,match='IDENTITY_MISMATCH'):
        full.validate_candidate(p,receipt,run_id='123',attempt='1',job_result='success')


@pytest.mark.parametrize('field,value',[
    ('exit_code',2),('session_finished',False),('collection_errors',['import failed']),
    ('mutated',True),('collected_nodes',[]),('tests',{}),('collected_nodes',['unreported'])])
def test_all_green_rejects_incomplete_collection_or_source_mutation(field,value):
    p,r=fixture();receipt=r['candidate'];receipt[field]=value
    receipt=full.seal({k:v for k,v in receipt.items() if k!='sha256'})
    with pytest.raises(ValueError,match='COLLECTION_OR_INFRASTRUCTURE'):
        full.validate_candidate(p,receipt,run_id='123',attempt='1',job_result='success')


def test_all_green_rejects_missing_import_only_file_and_parameter_cases():
    p,r=fixture();receipt=r['candidate']
    p['plans']['candidate']['lanes']['linux'].append({'selector':'08_tests/test_import_only.py'})
    p=full.seal({k:v for k,v in p.items() if k!='sha256'})
    receipt['plan_sha256']=p['sha256'];receipt=full.seal({k:v for k,v in receipt.items() if k!='sha256'})
    with pytest.raises(ValueError,match='REQUIRED_COLLECTION_MISSING'):
        full.validate_candidate(p,receipt,run_id='123',attempt='1',job_result='success')
    receipt['collected_files'].append('08_tests/test_import_only.py')
    receipt=full.seal({k:v for k,v in receipt.items() if k!='sha256'})
    assert full.validate_candidate(p,receipt,run_id='123',attempt='1',job_result='success')['result']=='PASS'
    p['plans']['candidate']['lanes']['linux'][0]={'selector':'08_tests/test_example.py::test_good','minimum_cases':2}
    p=full.seal({k:v for k,v in p.items() if k!='sha256'})
    receipt['plan_sha256']=p['sha256'];receipt=full.seal({k:v for k,v in receipt.items() if k!='sha256'})
    with pytest.raises(ValueError,match='REQUIRED_COLLECTION_MISSING'):
        full.validate_candidate(p,receipt,run_id='123',attempt='1',job_result='success')


@pytest.mark.parametrize('change',['file','function','parameter_cases','class_method','unittest_method'])
def test_full_plan_preserves_base_test_declarations_without_executing_base(tmp_path,monkeypatch,change):
    import json
    ci=full.ci;identity=dict(base=dict(commit='a'*40,tree='b'*40),candidate=dict(commit='c'*40,tree='d'*40))
    before=b"raise RuntimeError('base must not execute')\nimport pytest\n@pytest.mark.parametrize('x',[1,2])\ndef test_case(x):pass\nclass TestSuite:\n def test_method(self):pass\nclass CatalogChecks(unittest.TestCase):\n def test_unit(self):pass\n"
    after={'file':None,'function':b'def test_other():pass\n','parameter_cases':before.replace(b'[1,2]',b'[1]'),'class_method':before.replace(b'test_method',b'helper'),'unittest_method':before.replace(b'test_unit',b'helper')}[change]
    policy={'schema_version':'required-test-platforms/1','files':{}}
    def tree(repo,commit):return {} if commit==identity['candidate']['commit'] and after is None else {'08_tests/test_example.py':{'kind':'blob'}}
    def blob(repo,commit,path):
        if path==ci.POLICY:return json.dumps(policy).encode()
        return before if commit==identity['base']['commit'] else after
    monkeypatch.setattr(ci.admission,'tree',tree);monkeypatch.setattr(ci.admission,'blob',blob)
    required={**identity,'plan_sha256':'e'*64,'lanes':{'windows':[]}}
    # Candidate needs a nonempty plan before the explicit removal comparison.
    if change=='file':
        def tree(repo,commit):return {'08_tests/test_other.py':{'kind':'blob'}} if commit==identity['candidate']['commit'] else {'08_tests/test_example.py':{'kind':'blob'}}
        monkeypatch.setattr(ci.admission,'tree',tree)
        original=blob
        monkeypatch.setattr(ci.admission,'blob',lambda repo,commit,path:b'def test_other():pass\n' if path=='08_tests/test_other.py' else original(repo,commit,path))
    with pytest.raises(ValueError,match='FULL_TEST_(FILE|DECLARATION)_REMOVED'):
        full.make_plan(tmp_path,required,tmp_path/'plan.json')


@pytest.mark.parametrize('field,value', [
    ('result', 'FAIL'), ('policy', 'NO_NEW_REGRESSION'),
    ('base', {'commit': 'old'}), ('candidate', {'commit': 'old'}),
    ('required_plan_sha256', 'other-plan'),
    ('workflow_run_id', 'previous-run'), ('workflow_run_attempt', '0'),
])
def test_final_aggregate_rejects_untrusted_full_summary(tmp_path, monkeypatch, field, value):
    ci = full.ci
    p, receipts = fixture()
    summary = full.validate_candidate(p, receipts['candidate'], run_id='123', attempt='1', job_result='success')
    source = b'def test_good(): pass\n'
    path = '08_tests/test_example.py'
    plan = ci.platforms.plan([path], {path: source}, {path: source},
        {'schema_version': 'required-test-platforms/1', 'files': {}},
        base=p['identities']['base'], candidate=p['identities']['candidate'])
    summary['required_plan_sha256'] = plan['plan_sha256']
    summary[field] = value
    ci.save(tmp_path/'plan.json', plan)
    ci.save(tmp_path/'admission/main-admission.json', {'lane': 'governance'})
    ci.save(tmp_path/'final/full-regression.json', summary)
    monkeypatch.setenv('GITHUB_RUN_ID', '123')
    monkeypatch.setenv('GITHUB_RUN_ATTEMPT', '1')
    with pytest.raises(ValueError, match='FULL_REGRESSION_REQUIRED'):
        ci.finish(tmp_path/'plan.json', tmp_path/'receipts', tmp_path/'final/main-admission.json', {'linux':'success'})
    assert not (tmp_path/'final/main-admission.json').exists()
