"""Frontend obligations are actual Node cases, bound to the same hosted run."""
from pathlib import Path
import subprocess
import xml.etree.ElementTree as ET
import json
import yaml

import pytest

from quality import frontend_ci, main_admission as admission, platform_test_plan as platforms

TEST = admission.USDA_FRONTEND_ROOT + '/src/utils/marketYear.test.ts'


def test_frontend_workflow_freezes_dependencies_and_requires_same_run_job():
    root = Path(__file__).resolve().parents[2]
    workflow = yaml.safe_load((root/admission.WORKFLOW).read_text(encoding='utf-8'))
    job = workflow['jobs']['frontend']
    assert job['runs-on'] == 'ubuntu-24.04'
    assert job['needs'] == 'plan' and "outputs.frontend == 'true'" in job['if']
    serialized = json.dumps(job)
    assert '--frozen-lockfile' in serialized and 'pnpm@10.12.1' in serialized
    assert any(s.get('with', {}).get('node-version') == '24' for s in job['steps'])
    assert '--platform frontend' in serialized and '/usda/' in serialized
    final = workflow['jobs']['final']
    assert 'frontend' in final['needs']
    assert 'needs.frontend.result' in json.dumps(final)
    artifact='platform-frontend-${{ github.run_id }}-${{ github.run_attempt }}'
    producer=next(s for s in job['steps'] if s.get('uses','').startswith('actions/upload-artifact@'))
    consumers=[s for s in final['steps'] if s.get('with',{}).get('name')==artifact]
    assert producer['with']['name']==artifact and len(consumers)==1
    assert consumers[0]['if']=="needs.plan.outputs.frontend == 'true'"
    assert consumers[0]['with']['path']=='${{ runner.temp }}/platform-results/frontend'
    assert 'pattern' not in consumers[0]['with'] and 'merge-multiple' not in consumers[0]['with']


def junit(path, source, *, outcome=None, summary=None):
    root = ET.Element('testsuites')
    case = ET.SubElement(root, 'testcase', name='actual case', file=str(source.resolve()))
    if outcome:
        ET.SubElement(case, outcome)
    counts = dict(tests=1, pass_=1, fail=0, cancelled=0, skipped=0, todo=0)
    counts.update(summary or {})
    for key, value in counts.items():
        root.append(ET.Comment(f'{key.removesuffix("_")} {value}'))
    path.write_bytes(ET.tostring(root))


def fixture_plan():
    identity = dict(base=dict(commit='a'*40, tree='b'*40), candidate=dict(commit='c'*40, tree='d'*40))
    plan = platforms.plan([TEST], {TEST: b'old'}, {TEST: b'new'},
                          {'schema_version': 'required-test-platforms/1', 'files': {}}, **identity)
    receipt = dict(schema_version='platform-test-result/1', **identity, plan_sha256=plan['plan_sha256'],
                   platform='frontend', runner_os='Linux', runner_environment='github-hosted',
                   workflow_run_id='123', workflow_run_attempt='1',
                   tests=[dict(nodeid=TEST+'::0:actual case', outcome='passed')])
    return plan, receipt


def test_frontend_is_not_routed_to_pytest_or_lost_from_source_hashes():
    plan, receipt = fixture_plan()
    assert plan['lanes'] == dict(linux=[], windows=[], frontend=[dict(selector=TEST, minimum_cases=1)])
    assert set(plan['candidate_test_sha256']) == {TEST}
    assert platforms.aggregate(plan, {'frontend': receipt}, workflow_run_id='123', workflow_run_attempt='1',
                               job_results={'frontend': 'success'})['result'] == 'PASS'


@pytest.mark.parametrize('mutation', ['missing', 'empty', 'skip', 'fail', 'job-fail', 'wrong-run', 'wrong-os', 'wrong-source'])
def test_frontend_aggregate_cannot_omit_or_reuse_evidence(mutation):
    plan, receipt = fixture_plan()
    receipts = {'frontend': receipt}
    jobs = {'frontend': 'success'}
    if mutation == 'missing': receipts.clear()
    elif mutation == 'empty': receipt['tests'].clear()
    elif mutation in {'skip', 'fail'}: receipt['tests'][0]['outcome'] = mutation
    elif mutation == 'job-fail': jobs['frontend'] = 'failure'
    elif mutation == 'wrong-run': receipt['workflow_run_id'] = 'old'
    elif mutation == 'wrong-os': receipt['runner_os'] = 'Windows'
    else: receipt['tests'][0]['nodeid'] = TEST.replace('marketYear', 'unplanned')+'::case'
    assert platforms.aggregate(plan, receipts, workflow_run_id='123', workflow_run_attempt='1',
                               job_results=jobs)['result'] == 'FAIL'


@pytest.mark.parametrize('path', ['src/utils/a.test.ts', admission.USDA_FRONTEND_ROOT+'/src/../a.test.ts',
                                 admission.USDA_FRONTEND_ROOT+'/public/a.test.ts',
                                 admission.USDA_FRONTEND_ROOT+'/scripts/a.ts'])
def test_frontend_only_supports_registered_layout(path):
    assert not admission.frontend_test_path(path)
    with pytest.raises(ValueError, match='INVALID_TEST_PATH'):
        platforms.plan([path], {}, {path: b'test'}, {'schema_version':'required-test-platforms/1','files':{}},
                       base=dict(commit='a'*40, tree='b'*40), candidate=dict(commit='c'*40, tree='d'*40))


@pytest.mark.parametrize('bad', ['empty', 'failure', 'skipped', 'todo', 'cancelled', 'missing-summary', 'wrong-source', 'entity'])
def test_node_junit_is_not_all_green_for_missing_or_nonpassing_cases(tmp_path, bad):
    report = tmp_path/'junit.xml'
    source = tmp_path/'test.ts'
    junit(report, source, outcome=bad if bad in {'failure', 'skipped'} else None,
          summary={bad: 1} if bad in {'todo', 'cancelled'} else None)
    if bad == 'empty': report.write_bytes(b'<testsuites/>')
    elif bad == 'missing-summary': report.write_bytes(report.read_bytes().split(b'<!--')[0]+b'</testsuites>')
    elif bad == 'wrong-source': source = tmp_path/'other.ts'
    elif bad == 'entity': report.write_bytes(b'<!DOCTYPE a [<!ENTITY x "a">]><testsuites/>')
    with pytest.raises(ValueError, match='FRONTEND_'):
        frontend_ci.junit_cases(report, TEST, source)


@pytest.mark.parametrize('build_code', [0, 1])
def test_adapter_executes_exact_file_and_propagates_build_failure(tmp_path, build_code):
    commands = []
    def run(args, **kwargs):
        commands.append(args)
        if args == ['node', '--version']: return subprocess.CompletedProcess(args, 0, 'v24.18.0\n')
        if args[-1] == '--version': return subprocess.CompletedProcess(args, 0, '10.12.1\n')
        if args[-1] == 'build': return subprocess.CompletedProcess(args, build_code)
        target = Path(next(a.split('=', 1)[1] for a in args if a.startswith('--test-reporter-destination=')))
        junit(target, tmp_path/TEST)
        return subprocess.CompletedProcess(args, 0)
    code, cases = frontend_ci.execute_tests(tmp_path, [TEST], tmp_path/'output', run=run)
    assert code == build_code and len(cases) == 1
    assert commands[2][-1] == 'src/utils/marketYear.test.ts'
    assert commands[2][:5] == ['node', '--import', 'tsx', '--test', '--test-reporter=junit']
    assert commands[-1][-2:] == ['pnpm', 'build']
