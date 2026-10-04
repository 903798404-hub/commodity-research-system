"""Verify the existing spread service capability before any manual CNF write."""
from __future__ import annotations

import os
from pathlib import Path

from agri_research_agent.shared.runtime_context import (
    RuntimeClassification, assert_runtime_write, load_runtime_identity,
    establish_application_service_context,
)

ROOT = Path('/runtime')
SOURCE = Path(__file__).resolve().parents[3]
RELATIVE = Path('import-profit/operational/cnf/cnf.sqlite3')


def validate_cnf_write(path: Path) -> None:
    path = Path(path)
    deployed = bool(os.getenv('MARKET_DATA_GIT_HEAD') or
                    os.getenv('MARKET_DATA_EXECUTION_GRANT') or
                    (SOURCE / 'RELEASE.json').exists())
    if deployed:
        identity = load_runtime_identity(ROOT)
        if (identity.classification not in {RuntimeClassification.FORMAL,
                                           RuntimeClassification.CANDIDATE_VALIDATION}
                or identity.module_id != 'shared-intraday'):
            raise ValueError('CNF保存需要有效的工作台运行身份')
        if path.absolute() != ROOT / RELATIVE or path.resolve() != ROOT / RELATIVE:
            raise ValueError('CNF保存路径与声明的运行目录不符')
        context = establish_application_service_context(
            service_id='spread-dashboard', module_id='shared-intraday', runtime_root=ROOT)
        assert_runtime_write(context, path)
    elif os.getenv('SOYBEAN_MARGIN_LOCAL_PREVIEW') != '1':
        raise ValueError('CNF保存入口尚未配置有效运行身份')
    if path.absolute() != path.resolve() or not path.parent.is_dir():
        raise ValueError('CNF存储目录不存在或存在路径别名')
    if not os.access(path.parent, os.W_OK | os.X_OK):
        raise ValueError('CNF存储目录不可写')
    if hasattr(os, 'statvfs') and os.statvfs(path.parent).f_flag & os.ST_RDONLY:
        raise ValueError('CNF存储目录为只读')
