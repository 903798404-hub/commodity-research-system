"""Explicit candidate gate: run every declared test, never accept claimed PASS evidence."""
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


def complete(root: Path, project_id: str) -> dict:
    data, project = registry.select_project(root, project_id)
    if project['status'] != 'ready':
        raise ValueError('Project is not ready')
    if data != json.loads(registry.git(root, 'show', f'origin/main:{registry.REGISTRY_PATH}')):
        raise ValueError('Completion requires approved registry on origin/main')
    tests = project['required_tests'] + project.get('future_required_tests', [])
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
    runtime_evidence = target_runtime_gate.validate_target(root, project)
    if before != identity():
        raise ValueError('Candidate changed during completion')
    return {'PROJECT_COMPLETION': 'PASS', 'project_id': project_id, 'head': before[0],
            'candidate_content_sha256': before[1], 'executed_tests': results,
            'target_runtime_evidence': runtime_evidence}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', required=True)
    args = parser.parse_args(argv)
    try:
        _, project = registry.select_project(scope.PROJECT_ROOT, args.project)
        if scope.main(['--project', args.project, '--change-class', project['change_class']]) != 0:
            raise ValueError('Project Scope must PASS before completion')
        print(json.dumps(complete(scope.PROJECT_ROOT, args.project), ensure_ascii=False, indent=2))
        return 0
    except target_runtime_gate.RuntimeValidationBlocked as exc:
        print(json.dumps({'PROJECT_COMPLETION': 'BLOCKED', 'TARGET_RUNTIME_CONTAINER_VALIDATION': 'BLOCKED', 'reason': str(exc)}))
        return 3
    except (ValueError, OSError, subprocess.SubprocessError, ET.ParseError) as exc:
        print(json.dumps({'PROJECT_COMPLETION': 'FAIL', 'reason': str(exc)}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
