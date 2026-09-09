"""Strict Completion for shared/governance/production lanes; optional for ordinary business.

Ordinary business main acceptance uses hosted trusted-main-admission-v1 PASS,
human approval and exact candidate Commit/Tree fast-forward, without requiring
this command or an integration worktree. Calling this command still runs every
declared test and all applicable runtime checks; it never imports claimed PASS.
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
import xml.etree.ElementTree as ET

try:
    from . import project_registry as registry, audit_changed_scope as scope, target_runtime_gate
except ImportError:
    import project_registry as registry
    import audit_changed_scope as scope
    import target_runtime_gate


def complete(root: Path, project_id: str, *, candidate_record: Path | None = None) -> dict:
    data, project = registry.select_project(root, project_id)
    if project['status'] != 'ready':
        raise ValueError('Project is not ready')
    tests = project['required_tests'] + project.get('future_required_tests', [])
    trusted = json.loads(registry.git(root, 'show', f'origin/main:{registry.REGISTRY_PATH}'))
    if data != trusted:
        bootstrapped, discovered = registry.local_bootstrap(root, scope.SHARED_PATH_PATTERNS, require_tests=True)
        if bootstrapped != project:
            raise ValueError('ESCALATION_REQUIRED: existing Registry cannot change its own test policy')
        tests = discovered
    paths = []
    for name in tests:
        path = registry.future_file(root, name)
        if not path.is_file():
            raise ValueError(f'Required test missing at completion: {name}')
        paths.append(path)

    def identity():
        # Bind evidence to candidate bytes, including untracked implementation files.
        names = subprocess.check_output(['git', '-C', str(root), 'ls-files', '--cached', '--others', '--exclude-standard', '-z']).decode('utf-8').split('\0')
        digest = hashlib.sha256()
        for name in sorted(set(names) - {''}):
            path = root / name
            digest.update(name.encode('utf-8') + b'\0')
            digest.update(path.read_bytes() if path.is_file() else b'<missing>')
        return registry.git(root, 'rev-parse', 'HEAD'), digest.hexdigest()

    before = identity()
    record_before = target_runtime_gate.record_bytes(candidate_record) if candidate_record is not None else None
    authority_before = registry.git(root, 'rev-parse', 'origin/main') if candidate_record is not None else None
    results = []
    with tempfile.TemporaryDirectory(prefix='project-completion-') as directory:
        for number, (name, path) in enumerate(zip(tests, paths)):
            report = Path(directory) / f'{number}.xml'
            command = [sys.executable, '-B', '-m', 'pytest', str(path), '-q', '-o', 'addopts=', '-p', 'no:cacheprovider', f'--junitxml={report}']
            env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'PYTEST_ADDOPTS': ''}
            result = subprocess.run(command, cwd=root, env=env, timeout=1800, check=False)
            if result.returncode or not report.is_file():
                raise ValueError(f'Required test execution failed: {name} ({result.returncode})')
            cases = ET.parse(report).findall('.//testcase')
            if not cases or any(case.find(tag) is not None for case in cases for tag in ('failure', 'error', 'skipped')):
                raise ValueError(f'Required test must run and PASS without skip/xfail: {name}')
            results.append({'path': name, 'passed': len(cases), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
    runtime_evidence = (target_runtime_gate.validate_target(root, project)
                        if candidate_record is None else
                        target_runtime_gate.validate_target(root, project, candidate_record=candidate_record))
    if before != identity():
        raise ValueError('Candidate changed during completion')
    authentication = {}
    if candidate_record is not None:
        if (target_runtime_gate.record_bytes(candidate_record) != record_before
                or registry.git(root, 'rev-parse', 'origin/main') != authority_before):
            raise ValueError('Candidate record or authority changed during completion')
        # The exact snapshotted bytes have just passed the approved verifier.
        # Keep authentication metadata outside the unchanged evidence/1 schema.
        payload = json.loads(record_before)['payload']
        authentication['authenticated_candidate_record'] = {
            'schema_version': 'candidate-validation-record/1',
            'sha256': hashlib.sha256(record_before).hexdigest(), 'record_id': payload['record_id'],
            'authority_commit': authority_before, 'expires_at': payload['expires_at'],
        }
    return {'PROJECT_COMPLETION': 'PASS', 'project_id': project_id, 'head': before[0],
            'candidate_content_sha256': before[1], 'executed_tests': results,
            'target_runtime_evidence': runtime_evidence, **authentication}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', required=True)
    parser.add_argument('--candidate-record', type=Path,
                        help='Authenticated Linux candidate-validation-record; never raw PASS evidence')
    args = parser.parse_args(argv)
    try:
        _, project = registry.select_project(scope.PROJECT_ROOT, args.project)
        if scope.main(['--project', args.project, '--change-class', project['change_class']]) != 0:
            raise ValueError('Project Scope must PASS before completion')
        print(json.dumps(complete(scope.PROJECT_ROOT, args.project, candidate_record=args.candidate_record), ensure_ascii=False, indent=2))
        return 0
    except target_runtime_gate.RuntimeValidationBlocked as exc:
        print(json.dumps({'PROJECT_COMPLETION': 'BLOCKED', 'TARGET_RUNTIME_CONTAINER_VALIDATION': 'BLOCKED', 'reason': str(exc)}))
        return 3
    except (ValueError, OSError, subprocess.SubprocessError, ET.ParseError) as exc:
        print(json.dumps({'PROJECT_COMPLETION': 'FAIL', 'reason': str(exc)}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
