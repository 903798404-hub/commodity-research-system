"""Reuse the protected workbench manual-CNF mount; never fall back in deployment."""
from __future__ import annotations
import os
from pathlib import Path
from agri_research_agent.shared.runtime_context import (
    RuntimeAuthorizationError, RuntimeClassification, assert_runtime_write,
    establish_application_service_context, load_runtime_identity,
)

SOURCE = Path(__file__).resolve().parents[3]
ROOT = Path("/runtime")
STORAGE = Path("import-profit/operational/cnf/sugar-import/sugar-import.sqlite3")


def deployed():
    return bool(os.getenv("MARKET_DATA_GIT_HEAD") or os.getenv("MARKET_DATA_EXECUTION_GRANT")
                or (SOURCE / "RELEASE.json").exists())


def storage_context():
    if deployed():
        if os.getenv("SUGAR_IMPORT_LOCAL_PREVIEW") == "1":
            raise ValueError("正式环境不能使用本机白糖预览身份")
        try:
            identity = load_runtime_identity(ROOT)
        except (OSError, ValueError, RuntimeAuthorizationError):
            raise ValueError("白糖利润看板缺少有效的工作台运行身份") from None
        if (identity.classification not in {RuntimeClassification.FORMAL, RuntimeClassification.CANDIDATE_VALIDATION}
                or identity.module_id != "shared-intraday"):
            raise ValueError("白糖利润看板运行身份不符")
        path = ROOT / STORAGE
        if not (ROOT / STORAGE.parent.parent).is_dir() or path.resolve() != path.absolute():
            raise ValueError("白糖独立业务目录无效")
        return path, ROOT / "01_data/processed/sugar_import_history", authorize_write, os.getenv("SUGAR_IMPORT_ALLOW_SAVE") == "1"
    if os.getenv("SUGAR_IMPORT_ALLOW_SAVE") == "1":
        raise ValueError("白糖服务器保存缺少正式身份")
    raw = os.getenv("SUGAR_IMPORT_LOCAL_ROOT")
    local = Path(raw).absolute() if raw else SOURCE / "01_data/candidates/sugar_import_profit/local-preview"
    if local.resolve() != local or not local.is_relative_to(SOURCE / "01_data/candidates/sugar_import_profit"):
        raise ValueError("本机白糖预览必须使用独立候选目录")
    return local / "sugar-import.sqlite3", local / "history", authorize_write, os.getenv("SUGAR_IMPORT_LOCAL_PREVIEW") == "1"


def authorize_write(path):
    expected, _, _, enabled = storage_context()
    if not enabled or Path(path).absolute() != expected or Path(path).resolve() != expected:
        raise ValueError("白糖利润保存权限或路径无效")
    if deployed():
        try:
            context = establish_application_service_context(service_id="spread-dashboard", module_id="shared-intraday", runtime_root=ROOT)
            assert_runtime_write(context, path)
        except (OSError, ValueError, RuntimeAuthorizationError):
            raise ValueError("白糖利润保存需要有效的工作台服务写入权限") from None
