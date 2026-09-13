"""Same-run full repository regression comparison; required lanes stay all-green.

No failure allowlist. A completed base result is the only debt baseline.
This executor is used outside both exact checkouts, including an older base.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
import platform_ci as ci

OPTIONS = ['-q', '-p', 'no:cacheprovider', '-o', 'xfail_strict=true']
ENV_KEYS = ('PYTHONDONTWRITEBYTECODE', 'PYTEST_DISABLE_PLUGIN_AUTOLOAD', 'PYTHONUTF8',
            'PYTEST_ADDOPTS', 'MPLBACKEND', 'TZ', 'LANG', 'LC_ALL')


def seal(value):
    return {**value, 'sha256': ci.platforms.digest(value)}


def verify(value):
    if value.get('sha256') != ci.platforms.digest({k: v for k, v in value.items() if k != 'sha256'}):
        raise ValueError('FULL_ARTIFACT_DIGEST_MISMATCH')


def make_plan(repo, required, output):
    """Keep every base/candidate test file and route Windows tests to hard CI."""
    identities = {'base': required['base'], 'candidate': required['candidate']}
    plans = {}
    for side, identity in identities.items():
        entries = ci.admission.tree(repo, identity['commit'])
        paths = sorted(p for p in entries if ci.admission.test_path(p))
        sources = {p: ci.admission.blob(repo, identity['commit'], p) for p in paths}
        policy = json.loads(ci.admission.blob(repo, identity['commit'], ci.POLICY))
        plans[side] = ci.platforms.plan(paths, sources, sources, policy,
                                      base=identities['base'], candidate=identity)
        hard_windows = {t['selector'] for t in required['lanes']['windows']}
        if not {t['selector'] for t in plans[side]['lanes']['windows']} <= hard_windows:
            raise ValueError('FULL_WINDOWS_TESTS_MUST_BE_HARD_REQUIRED')
    # Routing changes must not conceal a removed Linux node.
    if plans['base']['policy_sha256'] != plans['candidate']['policy_sha256']:
        raise ValueError('FULL_PLATFORM_POLICY_CHANGED_REQUIRES_SEPARATE_REVIEW')
    value = seal(dict(schema_version='full-regression-plan/1', identities=identities,
                      required_plan_sha256=required['plan_sha256'], plans=plans))
    ci.save(output, value)
    return value


def environment(repo):
    from matplotlib import font_manager
    # Use the actual checked-out project font contract, not an alternative policy.
    sys.path.insert(0, str(repo / '03_src'))
    from agri_research_agent.utils.matplotlib_config import configure_matplotlib_chinese_fonts
    family = configure_matplotlib_chinese_fonts()[0]
    font = font_manager.findfont(font_manager.FontProperties(family=[family]), fallback_to_default=False)
    configs = {p: ci.admission.digest((repo / p).read_bytes()) for p in
               ('pytest.ini', 'pyproject.toml', 'setup.cfg', 'conftest.py') if (repo / p).is_file()}
    configs.update({p.relative_to(repo).as_posix(): ci.admission.digest(p.read_bytes())
                    for p in (repo / '08_tests').rglob('conftest.py')})
    return dict(runner_os=os.environ['RUNNER_OS'], runner_image=os.environ['ImageOS'],
                runner_image_version=os.environ['ImageVersion'], python=sys.version,
                dependencies=sorted(subprocess.check_output([sys.executable, '-I', '-m', 'pip', 'freeze'], text=True).splitlines()),
                system_packages=sorted(subprocess.check_output(['dpkg-query', '-W'], text=True).splitlines()),
                font_family=family, font_sha256=ci.admission.digest(Path(font).read_bytes()),
                pytest_options=OPTIONS, pytest_config=configs,
                command='python -I -B full_regression.py run -> pytest.main(OPTIONS, --basetemp, --junitxml, exact-plan-selectors)',
                variables={k: os.environ.get(k) for k in ENV_KEYS})


class Collection(ci.Collector):
    def __init__(self):
        super().__init__()
        self.nodes = []
        self.collection_errors = []
        self.session_finished = False

    def pytest_collection_finish(self, session):
        self.nodes = [item.nodeid for item in session.items]

    def pytest_collectreport(self, report):
        if report.failed or report.skipped:
            self.collection_errors.append(report.nodeid)

    def pytest_sessionfinish(self, session, exitstatus):
        self.session_finished = True


def execute(plan, side, output):
    verify(plan)
    if os.environ.get('RUNNER_ENVIRONMENT') != 'github-hosted' or os.environ.get('RUNNER_OS') != 'Linux':
        raise ValueError('FULL_GITHUB_HOSTED_LINUX_REQUIRED')
    repo = Path.cwd()
    identity = plan['identities'][side]
    if dict(commit=ci.admission.text(repo, 'rev-parse', 'HEAD'),
            tree=ci.admission.text(repo, 'rev-parse', 'HEAD^{tree}')) != identity:
        raise ValueError('FULL_CHECKOUT_IDENTITY_MISMATCH')
    subplan = plan['plans'][side]
    for path, expected in subplan['candidate_test_sha256'].items():
        if ci.admission.digest((repo / path).read_bytes()) != expected:
            raise ValueError('FULL_TEST_SOURCE_MISMATCH')
    env = environment(repo)
    selectors = [t['selector'] for t in subplan['lanes']['linux']]
    if not selectors:
        raise ValueError('FULL_EMPTY_PLAN')
    before = {p: ci.admission.digest((repo / p).read_bytes()) for p, e in
              ci.admission.tree(repo, identity['commit']).items() if e['kind'] == 'blob'}
    output.mkdir(parents=True, exist_ok=True)
    collector = Collection()
    run_id, attempt = os.environ['GITHUB_RUN_ID'], os.environ['GITHUB_RUN_ATTEMPT']
    for key in list(os.environ):
        if any(word in key.upper() for word in ('TOKEN', 'PASSWORD', 'SECRET', 'CREDENTIAL')):
            os.environ.pop(key, None)
    import pytest
    with tempfile.TemporaryDirectory(prefix='required-pytest-') as tmp:
        code = int(pytest.main([*OPTIONS, '--basetemp=' + tmp,
                               '--junitxml=' + str(output / 'junit.xml'), *selectors], plugins=[collector]))
    mutated = any(not (repo / p).is_file() or (repo / p).is_symlink() or
                  ci.admission.digest((repo / p).read_bytes()) != h for p, h in before.items())
    value = seal(dict(schema_version='full-regression-result/1', side=side,
                      identity=identity, plan_sha256=plan['sha256'],
                      workflow_run_id=run_id, workflow_run_attempt=attempt,
                      environment=env, exit_code=code, session_finished=collector.session_finished,
                      collection_errors=collector.collection_errors, mutated=mutated,
                      collected_nodes=sorted(collector.nodes), tests=collector.results))
    ci.save(output / 'result.json', value)
    # Test failures are compared later; infrastructure/collection failures never become debt.
    return 0 if completed(value) else 1


def completed(receipt):
    nodes, tests = receipt['collected_nodes'], receipt['tests']
    return (receipt['exit_code'] in (0, 1) and receipt['session_finished'] is True
            and not receipt['collection_errors'] and receipt['mutated'] is False
            and bool(nodes) and len(nodes) == len(set(nodes)) and set(nodes) == set(tests)
            and all(v in {'passed', 'failed', 'skipped'} for v in tests.values())
            and (receipt['exit_code'] == 1) == ('failed' in tests.values()))


def compare(plan, receipts, *, run_id, attempt, job_result):
    verify(plan)
    if job_result != 'success' or set(receipts) != {'base', 'candidate'}:
        raise ValueError('FULL_REQUIRED_JOB_OR_RECEIPT_MISSING')
    for side, receipt in receipts.items():
        verify(receipt)
        expected = dict(schema_version='full-regression-result/1', side=side,
                        identity=plan['identities'][side], plan_sha256=plan['sha256'],
                        workflow_run_id=run_id, workflow_run_attempt=attempt)
        if any(receipt.get(k) != v for k, v in expected.items()):
            raise ValueError('FULL_RECEIPT_IDENTITY_MISMATCH')
        if not completed(receipt):
            raise ValueError('FULL_COLLECTION_OR_INFRASTRUCTURE_FAILURE')
        for node in receipt['collected_nodes']:
            if not any(node == t['selector'] or node.startswith(t['selector'] + '::') or
                       node.startswith(t['selector'] + '[') for t in plan['plans'][side]['lanes']['linux']):
                raise ValueError('FULL_UNPLANNED_NODE')
        for obligation in plan['plans'][side]['lanes']['linux']:
            selector = obligation['selector']
            count = sum(n == selector or n.startswith(selector + '::') or n.startswith(selector + '[')
                        for n in receipt['collected_nodes'])
            if count < obligation.get('minimum_cases', 1):
                raise ValueError('FULL_REQUIRED_COLLECTION_MISSING')
    if receipts['base']['environment'] != receipts['candidate']['environment']:
        raise ValueError('FULL_ENVIRONMENT_MISMATCH')
    base, candidate = (receipts[s]['tests'] for s in ('base', 'candidate'))
    outcomes = lambda tests, state: {n for n, v in tests.items() if v == state}
    bf, cf = outcomes(base, 'failed'), outcomes(candidate, 'failed')
    bs, cs = outcomes(base, 'skipped'), outcomes(candidate, 'skipped')
    categories = dict(NEW_FAILURE=sorted(cf - bf), NEW_SKIP=sorted(cs - bs),
                      REMOVED_BASE_TEST_NODE=sorted(set(base) - set(candidate)),
                      PRE_EXISTING_FAILURE=sorted(bf & cf), PRE_EXISTING_SKIP=sorted(bs & cs),
                      RESOLVED_FAILURE=sorted(bf & outcomes(candidate, 'passed')),
                      RESOLVED_SKIP=sorted(bs & outcomes(candidate, 'passed')),
                      ADDED_PASS=sorted((set(candidate) - set(base)) & outcomes(candidate, 'passed')))
    failed = any(categories[k] for k in ('NEW_FAILURE', 'NEW_SKIP', 'REMOVED_BASE_TEST_NODE'))
    return dict(result='FAIL' if failed else 'PASS', policy='NO_NEW_REGRESSION',
                base=plan['identities']['base'], candidate=plan['identities']['candidate'],
                plan_sha256=plan['sha256'], workflow_run_id=run_id, workflow_run_attempt=attempt,
                baseline_green=not (bf or bs), debt_class='TECHNICAL_DEBT', nodes=categories,
                counts={k + '_COUNT': len(v) for k, v in categories.items()},
                test_counts={s: {state: list(receipts[s]['tests'].values()).count(state)
                                for state in ('passed', 'failed', 'skipped')} for s in receipts})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['plan', 'run', 'compare'])
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--side', choices=['base', 'candidate'])
    parser.add_argument('--receipts', type=Path)
    args = parser.parse_args()
    if args.action == 'plan':
        make_plan(Path.cwd(), ci.read(args.plan), args.output)
        return 0
    if args.action == 'run':
        return execute(ci.read(args.plan), args.side, args.output)
    result = compare(ci.read(args.plan), {s: ci.read(args.receipts / s / 'result.json') for s in ('base', 'candidate')},
                     run_id=os.environ['GITHUB_RUN_ID'], attempt=os.environ['GITHUB_RUN_ATTEMPT'],
                     job_result=os.environ['FULL_JOB_RESULT'])
    ci.save(args.output, result)
    print(json.dumps(result['counts'], indent=2))
    return 0 if result['result'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
