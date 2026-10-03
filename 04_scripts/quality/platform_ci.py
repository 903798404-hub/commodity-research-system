"""Mainstream hosted CI: plan exact candidate, execute OS lanes, aggregate jobs.

The candidate workflow is reviewed by the Maintainer, like an ordinary GitHub
repository. This is not a defense against a malicious workflow maintainer.
No entry point deploys, fetches production data, or grants release authority.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

if __package__:
    from . import main_admission as admission, platform_test_plan as platforms
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import main_admission as admission
    import platform_test_plan as platforms

POLICY = '04_scripts/quality/test_platforms.json'


def requires_spread_runtime_docker(report, repo):
    """Route runtime-source changes from the existing admission diff to Linux Docker."""
    changed = {item['path'] for item in report['changed_paths']}
    manifest = json.loads((repo/'02_configs/runtime_contracts/spread-production-runtime.json').read_text(encoding='utf-8'))
    packaged = {item['path'] for item in manifest['source_inputs']}
    exact = packaged | {
        '02_configs/runtime_contracts/spread-production-runtime.json',
        '04_scripts/runtime/validate_target_runtime.py',
        '04_scripts/runtime/pre_release_runtime.py',
        '04_scripts/runtime/routine_release.py',
        '09_deploy/spread_release/high_risk_execution.py',
        '08_tests/shared/high_risk_execution_docker_e2e.py',
        '08_tests/test_release_stabilize.py',
        '04_scripts/quality/platform_ci.py',
        '09_deploy/runtime_identity/host_authorization.py',
        '08_tests/test_spread_runtime_contract.py',
        '08_tests/test_target_runtime_validator.py',
        '.github/workflows/trusted-main-admission.yml',
    }
    return any(path in exact or path.startswith('09_deploy/spread_runtime/')
               for path in changed)


def requires_full(report):
    if report['lane'] not in {'business', 'strict', 'governance'}:
        raise ValueError('UNKNOWN_ADMISSION_LANE')
    return report['lane'] in {'strict', 'governance'}


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def save(path, data):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(data, indent=2)+'\n', encoding='utf-8')


def make_plan(repo, base, candidate, output):
    report = admission.admit(repo,base,candidate,'auto',output/'admission',plan_only=True,separate_full=True)
    if report['final_result'] != 'PLANNED':
        raise ValueError(report['failure_codes'])
    old = admission.tree(repo,base)
    policy = json.loads(admission.blob(repo,candidate,POLICY))
    if POLICY in old:
        previous = json.loads(admission.blob(repo,base,POLICY))
        for path, tests in previous['files'].items():
            for name, rule in tests.items():
                if rule['kind']=='WINDOWS_REQUIRED_TEST' and policy['files'].get(path,{}).get(name,{}).get('kind')!='WINDOWS_REQUIRED_TEST':
                    raise ValueError('REQUIRED_PLATFORM_POLICY_REDUCTION_OR_RECLASSIFICATION')
    required = report['test_plan']
    if requires_full(report):
        # Full comparison routes Windows-only cases to the hard platform job.
        # Include them even when this diff has no direct Windows module impact.
        required = sorted(set(required) | {path for path, rules in policy['files'].items()
            if any(rule['kind'] == 'WINDOWS_REQUIRED_TEST' for rule in rules.values())})
        report['test_plan'] = required
        save(output/'admission/main-admission.json', report)
    sources = {p:admission.blob(repo,candidate,p) for p in required}
    trusted = {p:admission.blob(repo,base,p) if p in old else sources[p] for p in required}
    plan = platforms.plan(required,trusted,sources,policy,base=report['trusted_main'],candidate=report['candidate'])
    plan['spread_runtime_docker'] = requires_spread_runtime_docker(report, repo)
    plan['plan_sha256'] = platforms.digest({k: v for k, v in plan.items() if k != 'plan_sha256'})
    save(output/'plan.json',plan)
    return plan


class Collector:
    def __init__(self): self.results = {}
    def pytest_runtest_logreport(self, report):
        prior=self.results.get(report.nodeid)
        if report.failed: self.results[report.nodeid]='failed'
        elif report.skipped and prior!='failed': self.results[report.nodeid]='skipped'
        elif report.when=='call' and prior not in ('failed','skipped'): self.results[report.nodeid]='passed'


def execute(platform, plan_path, output):
    if os.environ.get('RUNNER_ENVIRONMENT') != 'github-hosted':
        raise ValueError('GITHUB_HOSTED_REQUIRED')
    plan=read(plan_path)
    expected_os={'linux':'Linux','windows':'Windows'}[platform]
    if os.environ.get('RUNNER_OS') != expected_os:
        raise ValueError('WRONG_REQUIRED_RUNNER_OS')
    repo=Path.cwd()
    if admission.text(repo,'rev-parse','HEAD') != plan['candidate']['commit']:
        raise ValueError('CANDIDATE_COMMIT_MISMATCH')
    if admission.text(repo,'rev-parse','HEAD^{tree}') != plan['candidate']['tree']:
        raise ValueError('CANDIDATE_TREE_MISMATCH')
    for path,digest in plan['candidate_test_sha256'].items():
        if admission.digest((repo/path).read_bytes()) != digest:
            raise ValueError('CANDIDATE_TEST_HASH_MISMATCH')
    # Same plan from the same Actions run; no credentials passed into pytest.
    collector=Collector()
    selectors=[t['selector'] for t in plan['lanes'][platform]]
    if not selectors: raise ValueError('EMPTY_PLATFORM_PLAN')
    output.mkdir(parents=True,exist_ok=True)
    import pytest
    before={p:admission.digest((repo/p).read_bytes()) for p,e in admission.tree(repo,plan['candidate']['commit']).items() if e['kind']=='blob'}
    run_id=os.environ['GITHUB_RUN_ID']; attempt=os.environ['GITHUB_RUN_ATTEMPT']
    # Remove credential variables, preserve official runner tool/system paths.
    for key in list(os.environ):
        if any(word in key.upper() for word in ('TOKEN','PASSWORD','SECRET','CREDENTIAL')):
            os.environ.pop(key,None)
    with tempfile.TemporaryDirectory(prefix='required-pytest-') as tmp:
        code=pytest.main(['-q','-p','no:cacheprovider','-o','xfail_strict=true',
                          '--basetemp='+tmp,'--junitxml='+str(output/'junit.xml'),*selectors],plugins=[collector])
    mutated=any(not (repo/p).is_file() or (repo/p).is_symlink() or admission.digest((repo/p).read_bytes())!=d for p,d in before.items())
    receipt=dict(schema_version='platform-test-result/1',base=plan['base'],candidate=plan['candidate'],
                 plan_sha256=plan['plan_sha256'],platform=platform,runner_os=expected_os,
                 runner_environment='github-hosted',workflow_run_id=run_id,workflow_run_attempt=attempt,
                 tests=[dict(nodeid=n,outcome=o) for n,o in sorted(collector.results.items())])
    save(output/'result.json',receipt)
    return 1 if code or mutated or not collector.results or any(o!='passed' for o in collector.results.values()) else 0


def finish(plan_path, receipts_root, output, jobs):
    plan=read(plan_path)
    receipts={p:read(receipts_root/p/'result.json') for p,items in plan['lanes'].items() if items and (receipts_root/p/'result.json').is_file()}
    result=platforms.aggregate(plan,receipts,workflow_run_id=os.environ['GITHUB_RUN_ID'],
                               workflow_run_attempt=os.environ['GITHUB_RUN_ATTEMPT'],job_results=jobs)
    report=read(plan_path.parent/'admission/main-admission.json')
    if requires_full(report):
        full = read(output.parent / 'full-regression.json')
        expected = dict(result='PASS', base=plan['base'], candidate=plan['candidate'],
                        workflow_run_id=os.environ['GITHUB_RUN_ID'], workflow_run_attempt=os.environ['GITHUB_RUN_ATTEMPT'])
        if any(full.get(k) != v for k, v in expected.items()):
            raise ValueError('FULL_REGRESSION_REQUIRED')
        report['checks']['full_regression_comparison'] = full
    report['final_result']=result['result']
    report['failure_codes']=result['failure_codes']
    report['checks'].update(TECHNICAL_VALIDATION=result['result'],MAIN_ENTRY=result['result'],
                           platform_job_results=jobs,platform_plan_sha256=plan['plan_sha256'],
                           workflow_run_id=os.environ['GITHUB_RUN_ID'],workflow_run_attempt=os.environ['GITHUB_RUN_ATTEMPT'],
                           execution_source_commit=plan['candidate']['commit'],PRODUCTION_RELEASE_AUTHORIZED=False)
    observed=[t for receipt in receipts.values() for t in receipt['tests']]
    per_file=[]
    for path in report['test_plan']:
        tests=[t for t in observed if t['nodeid'].startswith(path+'::')]
        per_file.append(dict(path=path,result='PASS' if tests and all(t['outcome']=='passed' for t in tests) else 'FAIL',
                             test_count=len(tests),failed_or_skipped=sum(t['outcome']!='passed' for t in tests)))
    report['test_result']=dict(result=result['result'],test_count=len(observed),
        failed_or_skipped=sum(t['outcome']!='passed' for t in observed),per_test=per_file)
    import jsonschema
    jsonschema.validate(report,read(admission.SCHEMA))
    save(output,report)
    return 0 if result['result']=='PASS' else 1


def main():
    p=argparse.ArgumentParser();p.add_argument('action',choices=['plan','run','aggregate'])
    p.add_argument('--base');p.add_argument('--candidate');p.add_argument('--platform',choices=['linux','windows'])
    p.add_argument('--plan',type=Path);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--receipts',type=Path)
    a=p.parse_args()
    if a.action=='plan':
        plan=make_plan(Path.cwd(),a.base,a.candidate,a.output)
        if os.environ.get('GITHUB_OUTPUT'):
            with open(os.environ['GITHUB_OUTPUT'],'a',encoding='utf-8') as f:
                for lane,items in plan['lanes'].items(): f.write(lane+'='+str(bool(items)).lower()+'\n')
                report=read(a.output/'admission/main-admission.json')
                f.write('full='+str(requires_full(report)).lower()+'\n')
                f.write('base_commit='+plan['base']['commit']+'\n')
                f.write('spread_runtime_docker='+str(plan['spread_runtime_docker']).lower()+'\n')
        return 0
    if a.action=='run': return execute(a.platform,a.plan,a.output)
    return finish(a.plan,a.receipts,a.output,json.loads(os.environ['PLATFORM_JOB_RESULTS']))


if __name__=='__main__':
    raise SystemExit(main())
