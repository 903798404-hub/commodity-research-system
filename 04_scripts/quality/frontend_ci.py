"""Execute exact USDA TypeScript files with real Node JUnit receipts and build.

Hosted provenance and source immutability are enforced by platform_ci. This
adapter never fetches USDA inputs, rebuilds public data, or deploys an app.
"""
from __future__ import annotations

from pathlib import Path
import os
import re
import stat
import subprocess
import xml.etree.ElementTree as ET

if __package__:
    from .main_admission import USDA_FRONTEND_ROOT, frontend_test_path
else:
    from main_admission import USDA_FRONTEND_ROOT, frontend_test_path


def junit_cases(path: Path, selector: str, source: Path) -> dict[str, str]:
    if not path.is_file() or path.is_symlink():
        raise ValueError('FRONTEND_JUNIT_MISSING_OR_UNSAFE')
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 8 * 1024 * 1024:
        raise ValueError('FRONTEND_JUNIT_UNSAFE')
    data = path.read_bytes()
    if b'<!DOCTYPE' in data or b'<!ENTITY' in data:
        raise ValueError('FRONTEND_JUNIT_UNSAFE')
    root = ET.fromstring(data)
    cases = list(root.iter('testcase'))
    # Node 24 includes counts as XML comments, including TODO/cancellation that
    # need not produce a failure element. None of these can count as all-green.
    counts = {k.decode(): int(v) for k, v in re.findall(
        rb'<!--\s*(tests|pass|fail|cancelled|skipped|todo)\s+(\d+)\s*-->', data)}
    if (set(counts) != {'tests', 'pass', 'fail', 'cancelled', 'skipped', 'todo'}
            or counts['tests'] != len(cases) or counts['pass'] != len(cases)
            or any(counts[k] for k in ('fail', 'cancelled', 'skipped', 'todo')) or not cases):
        raise ValueError('FRONTEND_TEST_FAILED_SKIPPED_OR_EMPTY')
    results = {}
    for i, case in enumerate(cases):
        if (not case.get('name') or not case.get('file')
                or Path(case.get('file')).resolve() != source.resolve()
                or any(c.tag in {'failure', 'error', 'skipped'} for c in case)):
            raise ValueError('FRONTEND_TEST_RESULT_INVALID')
        results[f'{selector}::{i}:{case.get("name")}'] = 'passed'
    return results


def execute_tests(repo: Path, selectors: list[str], output: Path, *, run=subprocess.run):
    if not selectors or any(not frontend_test_path(s) for s in selectors):
        raise ValueError('INVALID_FRONTEND_TEST_PLAN')
    app = repo / USDA_FRONTEND_ROOT
    version = run(['node', '--version'], capture_output=True, text=True, check=True, timeout=30).stdout.strip()
    if not re.fullmatch(r'v24\.\d+\.\d+', version):
        raise ValueError('FRONTEND_NODE_24_REQUIRED')
    corepack = 'corepack.cmd' if os.name == 'nt' else 'corepack'
    manager = run([corepack, 'pnpm', '--version'], cwd=app, capture_output=True,
                  text=True, check=True, timeout=30).stdout.strip()
    if manager != '10.12.1':
        raise ValueError('FRONTEND_PNPM_CONTRACT_MISMATCH')
    output.mkdir(parents=True, exist_ok=True)
    results = {}
    for i, selector in enumerate(selectors):
        junit = (output / f'frontend-{i:04d}.xml').resolve()
        relative = selector[len(USDA_FRONTEND_ROOT) + 1:]
        command = ['node', '--import', 'tsx', '--test', '--test-reporter=junit',
                   '--test-reporter-destination=' + str(junit), relative]
        completed = run(command, cwd=app, check=False, timeout=600)
        if completed.returncode:
            return completed.returncode, results
        results.update(junit_cases(junit, selector, repo / selector))
    # tsc and the /usda/ production asset build are required on the same source.
    build = run([corepack, 'pnpm', 'build'], cwd=app, check=False, timeout=600)
    return build.returncode, results
