"""Scoped manual CNF/AM state; sealed inputs are never write destinations."""
from __future__ import annotations

import os
from pathlib import Path

from agri_research_agent.shared.production_identity import OCIExecutionRequest
from agri_research_agent.shared.runtime_context import (
    RuntimeClassification, RuntimeContext, RuntimeMode, assert_runtime_write,
    load_runtime_identity,
)

ROOT = Path('/runtime')
SOURCE = Path(__file__).resolve().parents[3]
CNF_RELATIVE = Path('import-profit/operational/cnf/manual_cnf_quotes.parquet')
AM_RELATIVE = Path('import-profit/operational/am-results')


def operational_write_context() -> RuntimeContext:
    """Use the existing service OCI grant, never caller-selected Git authority."""
    identity = load_runtime_identity(ROOT)
    modes = {RuntimeClassification.FORMAL: RuntimeMode.PRODUCTION_WRITE,
             RuntimeClassification.CANDIDATE_VALIDATION: RuntimeMode.CANDIDATE_VALIDATION}
    if identity.classification not in modes or identity.module_id != 'shared-intraday':
        raise ValueError('manual CNF requires the declared spread runtime identity')
    request = OCIExecutionRequest(
        grant_path=Path('/run/market-data-grants/grant.json'),
        release_path=SOURCE / 'RELEASE.json',
        runtime_manifest_path=SOURCE / '02_configs/runtime_contracts/spread-production-runtime.json',
        runtime_root=ROOT, runtime_marker_path=ROOT / '.market-data-runtime.json')
    return RuntimeContext(modes[identity.classification], identity.module_id, ROOT,
        formal_identity=identity if identity.classification is RuntimeClassification.FORMAL else None,
        expected_runtime_id=identity.runtime_id, execution_request=request)


def validate_operational_write(context: RuntimeContext, cnf: Path, results: Path) -> None:
    """Read-only capability check, repeated immediately before every save."""
    expected = (context.runtime_root / CNF_RELATIVE, context.runtime_root / AM_RELATIVE)
    for target, declared in zip((Path(cnf), Path(results)), expected):
        if target.absolute() != declared or target.resolve() != declared:
            raise ValueError('manual CNF write path must be the unaliased operational path')
        assert_runtime_write(context, target)
    for directory in (cnf.parent, results):
        if not directory.is_dir() or not os.access(directory, os.W_OK | os.X_OK):
            raise ValueError('operational store missing or readonly')
        if hasattr(os, 'statvfs') and os.statvfs(directory).f_flag & os.ST_RDONLY:
            raise ValueError('operational mount is readonly')
        # Reject existing links/junctions before storage primitives create locks,
        # backups or result releases. No test write is needed during deployment.
        for item in directory.rglob('*'):
            if item.is_symlink() or item.resolve() != item.absolute():
                raise ValueError('operational store contains an aliased path')


def configured_operational_write():
    """Disabled stays readonly; enabled but incomplete must fail preflight."""
    enabled = os.environ.get('IMPORT_PROFIT_INTRADAY_ALLOW_CNF_SAVE', '0').strip()
    if enabled == '0':
        return None
    if enabled != '1':
        raise ValueError('invalid CNF save capability setting')
    context = operational_write_context()
    cnf = Path(os.environ.get('IMPORT_PROFIT_INTRADAY_CNF_STORE_PATH', ''))
    results = Path(os.environ.get('IMPORT_PROFIT_INTRADAY_AM_RESULT_ROOT', ''))
    validate_operational_write(context, cnf, results)
    return context
