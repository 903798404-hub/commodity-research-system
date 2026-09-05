"""Read-only identity and consumer preflight for the spread-dashboard image."""
from __future__ import annotations

import argparse
from datetime import date
import os
from pathlib import Path
import sys


SOURCE_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SOURCE_ROOT / "03_src"))
sys.path.insert(0, str(SOURCE_ROOT / "04_scripts"))
sys.path.insert(0, str(SOURCE_ROOT / "05_apps"))

from capture_public_intraday import MODULE_ID, initialize_execution_identity  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402
from agri_research_agent.application.domestic_spreads import load_domestic_spread_database  # noqa: E402
from agri_research_agent.import_profit.runtime_store import (  # noqa: E402
    load_runtime_release_dataset,
    resolve_current_runtime_release,
)
from agri_research_agent.market_data.activated_runtime import resolve_domestic_spread_path  # noqa: E402
from agri_research_agent.market_data.intraday import (  # noqa: E402
    MarketSession,
    load_intraday_snapshot,
    load_latest_intraday_snapshot,
)
from agri_research_agent.shared.runtime_context import (  # noqa: E402
    RuntimeClassification,
    RuntimeMode,
    assert_runtime_write,
    load_runtime_identity,
)


RUNTIME_ROOT = Path("/runtime")
SNAPSHOT_ROOT = RUNTIME_ROOT / "import-profit" / "snapshots"
SOYBEAN_RUNTIME_ROOT = RUNTIME_ROOT / "import-profit" / "history"
RESULT_ROOT = RUNTIME_ROOT / "import-profit" / "results"
HISTORICAL_CNF_CACHE = RUNTIME_ROOT / "import-profit" / "cnf" / "historical_cnf_cache.parquet"
CAPTURE_SNAPSHOT_ROOT = RUNTIME_ROOT / "capture-snapshots"
CONFIG_PATH = SOURCE_ROOT / "02_configs" / "import_profit_soybean.yaml"


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    result.add_argument("--identity-kind", choices=("oci_container",), required=True)
    result.set_defaults(runtime_root=RUNTIME_ROOT, snapshot_root=SNAPSHOT_ROOT,
                        soybean_runtime_root=SOYBEAN_RUNTIME_ROOT, result_root=RESULT_ROOT,
                        historical_cnf_cache=HISTORICAL_CNF_CACHE,
                        capture_snapshot_root=CAPTURE_SNAPSHOT_ROOT)
    return result


def initialize_preflight_identity(args):
    """Select OCI first, then derive the permitted readonly/candidate mode."""
    if args.identity_kind != "oci_container":
        raise ValueError("preflight requires explicit OCI identity selection")
    identity = load_runtime_identity(args.runtime_root)
    if identity.module_id != MODULE_ID:
        raise ValueError("runtime marker module identity mismatch")
    if identity.classification is RuntimeClassification.CANDIDATE_VALIDATION:
        mode = RuntimeMode.CANDIDATE_VALIDATION
    elif identity.classification is RuntimeClassification.FORMAL:
        mode = RuntimeMode.FORMAL_READONLY
    else:
        raise ValueError("preflight marker classification is not authorized")
    # The shared CLI builder verifies OCI evidence; FORMAL_READONLY additionally
    # calls verify_execution there because RuntimeContext itself forbids a request.
    args.expected_runtime_id = identity.runtime_id
    args.expected_marker_sha256 = identity.marker_sha256
    args.approved_commit = None
    args.approved_tree = None
    return initialize_execution_identity(args, mode=mode)


def initialize_strict_page(args, snapshot_root: Path, cache: Path) -> None:
    """Initialize the actual page without a write handler or date override."""
    page_script = (
        "from import_profit_intraday_runtime_page import render_import_profit_intraday_runtime_page\n"
        f"render_import_profit_intraday_runtime_page({str(args.soybean_runtime_root)!r}, "
        f"result_root={str(args.result_root)!r}, snapshot_root={str(snapshot_root)!r}, "
        f"config_path={str(CONFIG_PATH)!r}, page_mode='STRICT_RUNTIME', environment='FORMAL', "
        f"preview_historical_cnf_path={str(cache)!r}, allow_cnf_save=False)\n"
    )
    app = AppTest.from_string(page_script, default_timeout=40).run(timeout=40)
    if app.exception or app.error or app.warning:
        raise ValueError("STRICT_RUNTIME page initialization reported an error or warning")


def load_formal_preflight_snapshot(snapshot_root: Path):
    """Read existing sealed sessions; a valid empty store needs no capture."""
    if not snapshot_root.is_dir():
        raise ValueError("snapshot store is unavailable")
    releases = snapshot_root / "releases"
    if releases.is_symlink() or (releases.exists() and not releases.is_dir()):
        raise ValueError("snapshot releases directory is invalid")
    snapshots = []
    for session in (MarketSession.AM, MarketSession.PM):
        if any(releases.glob(f"????-??-??-{session.value}")):
            # Do not catch NotFound here: an existing incomplete release is
            # invalid, whereas an absent session is normal before first capture.
            snapshots.append(load_latest_intraday_snapshot(
                snapshot_root, session, expected_environment="FORMAL"))
    return max(snapshots, key=lambda item: (item.business_date, item.session.value), default=None)


def readonly_preflight(args) -> dict[str, object]:
    """Read actual mounted consumers without capture, writes, secrets or network."""
    context = initialize_preflight_identity(args)
    snapshot_root = Path(args.snapshot_root).resolve(strict=True)
    if context.runtime_root not in snapshot_root.parents:
        raise ValueError("snapshot root escapes the identity runtime")
    domestic_path = resolve_domestic_spread_path(context.runtime_root / "01_data")
    domestic = load_domestic_spread_database(domestic_path)
    if domestic.empty:
        raise ValueError("Domestic Spread runtime is empty")
    soybean = resolve_current_runtime_release(Path(args.soybean_runtime_root))
    # This validates the sealed release, its manual CNF identity and the actual
    # dataset consumer before initializing the page; warnings cannot mask it.
    load_runtime_release_dataset(Path(args.soybean_runtime_root))
    cache = Path(args.historical_cnf_cache)
    if not cache.is_file() or cache.is_symlink():
        raise ValueError("historical CNF cache is unavailable")
    if context.mode is RuntimeMode.CANDIDATE_VALIDATION:
        assert_runtime_write(context, Path(args.capture_snapshot_root))
    else:
        capture_root = Path(args.capture_snapshot_root).resolve(strict=True)
        if not os.path.samefile(capture_root, snapshot_root):
            raise ValueError("production capture and consumer snapshots must share one directory")
        capture_stat, consumer_stat = os.stat(capture_root), os.stat(snapshot_root)
        if (capture_stat.st_dev, capture_stat.st_ino) != (consumer_stat.st_dev, consumer_stat.st_ino):
            raise ValueError("production snapshot aliases have different device/inode")
    if context.mode is RuntimeMode.CANDIDATE_VALIDATION:
        snapshot = load_intraday_snapshot(snapshot_root, date(2026, 8, 31), MarketSession.PM,
                                          expected_environment="TEST_ISOLATED_NON_PRODUCTION")
    else:
        snapshot = load_formal_preflight_snapshot(snapshot_root)
    initialize_strict_page(args, snapshot_root, cache)
    return {
        "schema_version": "spread-runtime-preflight/1",
        "status": "PASS",
        "mode": context.mode.value,
        "runtime_id": context.identity.runtime_id,
        "domestic_spread_rows": len(domestic),
        "soybean_release": soybean.release_id,
        "snapshot_status": "AVAILABLE" if snapshot is not None else "NOT_YET_AVAILABLE",
        "snapshot_release": snapshot.release_id if snapshot is not None else None,
        "snapshot_business_date": snapshot.business_date.isoformat() if snapshot is not None else None,
        "capture_executed": False,
        "secret_accessed": False,
        "network_accessed": False,
    }


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        import json
        print(json.dumps(readonly_preflight(args), ensure_ascii=False, sort_keys=True))
        return 0
    except Exception as exc:
        import json
        print(json.dumps({"schema_version": "spread-runtime-preflight/1", "status": "FAILED",
                          "error_type": type(exc).__name__}, ensure_ascii=False, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
