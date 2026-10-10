"""Separate commodity storage using the existing workbench service authority."""
from __future__ import annotations

import os
from pathlib import Path

from agri_research_agent.shared.runtime_context import (
    RuntimeAuthorizationError, RuntimeClassification, assert_runtime_write,
    establish_application_service_context, load_runtime_identity,
)
from .store import SOURCE, authorize_local, local_database

ROOT = Path("/runtime")
STORAGE = Path("import-profit/operational/cnf/commodity-import")


def deployed() -> bool:
    return bool(os.getenv("MARKET_DATA_GIT_HEAD") or
                os.getenv("MARKET_DATA_EXECUTION_GRANT") or
                (SOURCE / "RELEASE.json").exists())


def database_path() -> Path:
    if not deployed():
        return local_database()
    if os.getenv("COMMODITY_IMPORT_LOCAL_PREVIEW") == "1":
        raise ValueError("正式或候选环境不能启用本地保存标志")
    try:
        identity = load_runtime_identity(ROOT)
    except (OSError, ValueError, RuntimeAuthorizationError):
        raise ValueError("菜籽棕油需要有效的工作台运行身份") from None
    if (identity.classification not in {RuntimeClassification.FORMAL,
                                       RuntimeClassification.CANDIDATE_VALIDATION}
            or identity.module_id != "shared-intraday"):
        raise ValueError("菜籽棕油运行身份或模块不符")
    operational = ROOT / STORAGE.parent
    path = ROOT / STORAGE / "research.sqlite3"
    if (not operational.is_dir() or operational.resolve() != operational.absolute()
            or path.resolve() != path.absolute()):
        raise ValueError("菜籽棕油独立存储目录缺失或存在路径别名")
    return path


def save_enabled() -> bool:
    flag = "COMMODITY_IMPORT_ALLOW_SAVE" if deployed() else "COMMODITY_IMPORT_LOCAL_PREVIEW"
    return os.getenv(flag) == "1"


def authorize_write(path: Path) -> None:
    if not deployed():
        authorize_local(path)
        return
    if not save_enabled():
        raise ValueError("菜籽棕油正式保存入口未启用")
    expected = database_path()
    if Path(path).absolute() != expected or Path(path).resolve() != expected:
        raise ValueError("菜籽棕油只能写入声明的独立业务库")
    try:
        context = establish_application_service_context(
            service_id="spread-dashboard", module_id="shared-intraday", runtime_root=ROOT)
        assert_runtime_write(context, path)
    except (OSError, ValueError, RuntimeAuthorizationError):
        raise ValueError("菜籽棕油保存需要有效的工作台服务写入权限") from None
