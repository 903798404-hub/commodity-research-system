"""OCI runtime boundary for explicit Shared Intraday AM/PM capture."""
from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import json
from pathlib import Path
import signal
import sys


SOURCE_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SOURCE_ROOT / "03_src"))

from agri_research_agent.data_sources.tankan.client import (  # noqa: E402
    TankanClient,
    TankanConnectionSettings,
)
from agri_research_agent.data_sources.tankan.intraday_adapter import ExactRequest  # noqa: E402
from agri_research_agent.market_data.calendars import DeterministicBusinessDayPolicy  # noqa: E402
from agri_research_agent.market_data.intraday import MarketSession, _read_json, canonical_json  # noqa: E402
from agri_research_agent.pipelines.public_intraday import CapturePolicy, capture_public_intraday  # noqa: E402
from agri_research_agent.shared.file_identity import identify_file  # noqa: E402
from agri_research_agent.shared.production_identity import OCIExecutionRequest, verify_execution  # noqa: E402
from agri_research_agent.shared.runtime_context import (  # noqa: E402
    RuntimeClassification,
    RuntimeContext,
    RuntimeMode,
    assert_runtime_write,
    load_runtime_identity,
)
from agri_research_agent.shared.runtime_manifest import parse_runtime_manifest  # noqa: E402


RUNTIME_ROOT = Path("/runtime")
INPUT_ROOT = RUNTIME_ROOT / "inputs"
SNAPSHOT_ROOT = RUNTIME_ROOT / "snapshots"
GRANT_PATH = Path("/run/market-data-grants/grant.json")
RELEASE_PATH = SOURCE_ROOT / "RELEASE.json"
MANIFEST_PATH = SOURCE_ROOT / "02_configs/runtime_contracts/public-intraday-runtime.json"
SECRET_PATH = Path("/run/secrets/tankan.env")
MODULE_ID = "shared-intraday"
PROJECT_ID = "public-intraday-runtime"
SERVICE_ID = "public-intraday-runtime"


def execution_request() -> OCIExecutionRequest:
    return OCIExecutionRequest(
        grant_path=GRANT_PATH,
        release_path=RELEASE_PATH,
        runtime_manifest_path=MANIFEST_PATH,
        runtime_root=RUNTIME_ROOT,
        runtime_marker_path=RUNTIME_ROOT / ".market-data-runtime.json",
    )


def _runtime_context(*, write: bool) -> RuntimeContext:
    identity = load_runtime_identity(RUNTIME_ROOT)
    if write:
        if identity.classification is not RuntimeClassification.FORMAL:
            raise ValueError("capture requires a formal runtime")
        return RuntimeContext(
            RuntimeMode.PRODUCTION_WRITE,
            MODULE_ID,
            RUNTIME_ROOT,
            formal_identity=identity,
            expected_runtime_id=identity.runtime_id,
            execution_request=execution_request(),
        )
    if identity.classification is RuntimeClassification.CANDIDATE_VALIDATION:
        return RuntimeContext(
            RuntimeMode.CANDIDATE_VALIDATION,
            MODULE_ID,
            RUNTIME_ROOT,
            expected_runtime_id=identity.runtime_id,
            execution_request=execution_request(),
        )
    if identity.classification is RuntimeClassification.FORMAL:
        context = RuntimeContext(
            RuntimeMode.FORMAL_READONLY,
            MODULE_ID,
            RUNTIME_ROOT,
            formal_identity=identity,
        )
        verify_execution(
            execution_request(),
            expected_role="production",
            module_id=MODULE_ID,
            runtime_id=identity.runtime_id,
            runtime_root=RUNTIME_ROOT,
            marker_sha256=identity.marker_sha256,
        )
        return context
    raise ValueError("runtime classification cannot initialize this service")


def readonly_preflight() -> dict[str, object]:
    """Verify identity and fixed wiring without secrets, network or writes."""
    manifest = parse_runtime_manifest(json.loads(MANIFEST_PATH.read_text(encoding="utf-8")))
    if (manifest.project_id, manifest.module_id, manifest.service_id) != (
        PROJECT_ID,
        MODULE_ID,
        SERVICE_ID,
    ):
        raise ValueError("runtime manifest identity mismatch")
    context = _runtime_context(write=False)
    expected = {INPUT_ROOT: False, SNAPSHOT_ROOT: True}
    for path, writable in expected.items():
        resolved = path.resolve(strict=True)
        if RUNTIME_ROOT.resolve(strict=True) not in resolved.parents:
            raise ValueError("runtime path escaped identity root")
        if not resolved.is_dir():
            raise ValueError("runtime path is not a directory")
        if writable and context.mode is RuntimeMode.FORMAL_READONLY:
            continue
    return {
        "schema_version": "public-intraday-runtime-preflight/1",
        "status": "PASS",
        "mode": context.mode.value,
        "runtime_id": context.identity.runtime_id,
        "module_id": context.module_id,
        "secret_accessed": False,
        "capture_executed": False,
    }


def _runtime_input(value: Path, label: str) -> Path:
    if value.is_symlink() or not value.is_file():
        raise ValueError(f"{label} must be a regular non-symlink file")
    resolved = value.resolve(strict=True)
    root = INPUT_ROOT.resolve(strict=True)
    if root not in resolved.parents:
        raise ValueError(f"{label} is outside the runtime input root")
    return resolved


def capture(args: argparse.Namespace, *, client_factory=None, clock=None) -> dict[str, object]:
    context = _runtime_context(write=True)
    store = assert_runtime_write(context, SNAPSHOT_ROOT)
    request_path = _runtime_input(args.request_file, "request file")
    calendar_path = _runtime_input(args.calendar_file, "calendar file")
    request_identity = identify_file(request_path)
    calendar_identity = identify_file(calendar_path)
    request = _read_json(request_path)
    calendar = _read_json(calendar_path)
    if request["business_date"] != args.business_date.isoformat() or request["session"] != args.session:
        raise ValueError("request date/session mismatch")
    requested = tuple(ExactRequest(**row) for row in request["requested"])
    policy = CapturePolicy(
        args.business_date,
        MarketSession(args.session),
        {key: datetime.fromisoformat(value) for key, value in request["source_not_before"].items()},
        request_identity.sha256,
    )
    day_policy = DeterministicBusinessDayPolicy(
        frozenset(date.fromisoformat(value) for value in calendar["business_days"]),
        "explicit-calendar-file/1",
        calendar_identity.sha256,
    )
    if identify_file(request_path) != request_identity or identify_file(calendar_path) != calendar_identity:
        raise ValueError("input identity changed during read")
    factory = client_factory or (
        lambda: TankanClient(TankanConnectionSettings.from_secret_file(SECRET_PATH))
    )
    with factory() as client:
        result = capture_public_intraday(
            client=client,
            requested=requested,
            context=context,
            store_root=store,
            policy=policy,
            business_day_policy=day_policy,
            clock=clock or (lambda: datetime.now(timezone.utc)),
        )
    return json.loads(canonical_json(result.evidence))


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    subparsers = result.add_subparsers(dest="action", required=True)
    subparsers.add_parser("serve", allow_abbrev=False)
    subparsers.add_parser("readonly-preflight", allow_abbrev=False)
    capture_parser = subparsers.add_parser("capture", allow_abbrev=False)
    capture_parser.add_argument("--business-date", type=date.fromisoformat, required=True)
    capture_parser.add_argument("--session", choices=("AM", "PM"), required=True)
    capture_parser.add_argument("--request-file", type=Path, required=True)
    capture_parser.add_argument("--calendar-file", type=Path, required=True)
    return result


def _serve() -> int:
    print(canonical_json(readonly_preflight()).decode("utf-8"), flush=True)
    signal.signal(signal.SIGTERM, lambda *_: raise_exit())
    while True:
        signal.pause()


def raise_exit() -> None:
    raise SystemExit(0)


def main(argv=None, *, client_factory=None, clock=None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.action == "serve":
            return _serve()
        result = readonly_preflight() if args.action == "readonly-preflight" else capture(
            args, client_factory=client_factory, clock=clock
        )
        print(canonical_json(result).decode("utf-8"))
        return 0
    except Exception as exc:
        print(canonical_json({
            "schema_version": "public-intraday-runtime-error/1",
            "status": "FAILED",
            "error_type": type(exc).__name__,
        }).decode("utf-8"))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
