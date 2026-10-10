"""Opt-in service storage using the already declared manual-CNF writable root."""
from __future__ import annotations

import os
from pathlib import Path

from agri_research_agent.soybean_margin import runtime as soybean_runtime
from .store import SOURCE, local_database, authorize_local


def deployed() -> bool:
    return bool(os.getenv("MARKET_DATA_GIT_HEAD") or
                os.getenv("MARKET_DATA_EXECUTION_GRANT") or
                (SOURCE / "RELEASE.json").exists())


def authorize_server(path: Path) -> None:
    if os.getenv("COMMODITY_IMPORT_SERVER_ENABLED") != "1":
        raise ValueError("菜籽和棕油服务器入口尚未启用")
    if not deployed():
        raise ValueError("服务器保存必须具有受保护的运行身份")
    # This validator verifies the marker, protected application credential,
    # allowed writable roots, exact path, aliases and mount writability.
    soybean_runtime.validate_cnf_write(path)


def storage_context(*, server=False):
    if server or deployed() or os.getenv("COMMODITY_IMPORT_SERVER_ENABLED") == "1":
        path = soybean_runtime.ROOT / soybean_runtime.RELATIVE
        authorize_server(path)
        return path, authorize_server, True
    return local_database(), authorize_local, os.getenv("COMMODITY_IMPORT_LOCAL_PREVIEW") == "1"
