"""Explicit independent intraday CLI; never calls Public refresh or DAILY."""
import argparse
from datetime import date, datetime, timezone
import os
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"03_src"))

from agri_research_agent.data_sources.tankan.client import TankanClient, TankanConnectionSettings
from agri_research_agent.data_sources.tankan.intraday_adapter import ExactRequest
from agri_research_agent.market_data.calendars import DeterministicBusinessDayPolicy
from agri_research_agent.market_data.intraday import MarketSession, canonical_json, _read_json
from agri_research_agent.pipelines.public_intraday import CapturePolicy, capture_public_intraday
from agri_research_agent.shared.file_identity import identify_file
from agri_research_agent.shared.production_identity import (GitExecutionRequest, OCIExecutionRequest,
                                                            verify_execution)
from agri_research_agent.shared.runtime_context import (RuntimeClassification, RuntimeContext,
                                                        RuntimeMode, assert_runtime_write,
                                                        load_runtime_identity)


MODULE_ID = "shared-intraday"
OCI_RUNTIME_ROOT = Path("/runtime")
OCI_GRANT_PATH = Path("/run/market-data-grants/grant.json")
OCI_RELEASE_PATH = ROOT / "RELEASE.json"
OCI_MANIFEST_PATH = ROOT / "02_configs/runtime_contracts/spread-production-runtime.json"
OCI_CAPTURE_SNAPSHOT_ROOT = Path("/runtime/capture-snapshots")
OCI_READONLY_SNAPSHOT_ROOT = Path("/runtime/import-profit/snapshots")


def parser():
    result=argparse.ArgumentParser(description=__doc__)
    result.add_argument("--business-date",type=date.fromisoformat,required=True)
    result.add_argument("--session",choices=("AM","PM"),required=True)
    result.add_argument("--request-file",type=Path,required=True)
    result.add_argument("--calendar-file",type=Path,required=True)
    result.add_argument("--runtime-root",type=Path,required=True)
    result.add_argument("--runtime-mode",choices=("FIXTURE","ISOLATED_DEV","PRODUCTION_WRITE"),required=True)
    result.add_argument("--expected-runtime-id",required=True)
    result.add_argument("--expected-marker-sha256",required=True)
    result.add_argument("--secret-file",type=Path,required=True)
    result.add_argument("--snapshot-root",type=Path,
                        help="Capture destination; legacy modes default to RUNTIME_ROOT/snapshots")
    result.add_argument("--identity-kind",choices=("git_worktree","oci_container"))
    result.add_argument("--approved-commit")
    result.add_argument("--approved-tree")
    return result


def oci_execution_request(runtime_root: Path) -> OCIExecutionRequest:
    """Build the fixed in-image OCI request; authorization is verified on use."""
    root = Path(runtime_root).resolve(strict=True)
    if root != OCI_RUNTIME_ROOT:
        raise ValueError("OCI capture requires the declared /runtime identity root")
    return OCIExecutionRequest(
        grant_path=OCI_GRANT_PATH,
        release_path=OCI_RELEASE_PATH,
        runtime_manifest_path=OCI_MANIFEST_PATH,
        runtime_root=root,
        runtime_marker_path=root / ".market-data-runtime.json",
    )


def initialize_execution_identity(args, *, mode: RuntimeMode | None = None) -> RuntimeContext:
    """Initialize an explicit execution request before a protected runtime action.

    Legacy fixture and isolated-development callers intentionally retain their
    existing marker-only behavior.  Production writes never infer authority
    from the repository root or a marker: they require an identity kind and
    its complete request material.
    """
    selected = mode or RuntimeMode(args.runtime_mode)
    root = Path(args.runtime_root)
    identity = load_runtime_identity(root)
    if (identity.runtime_id, identity.marker_sha256) != (
            args.expected_runtime_id, args.expected_marker_sha256):
        raise ValueError("Runtime identity mismatch")
    if selected in {RuntimeMode.FIXTURE, RuntimeMode.ISOLATED_DEV}:
        if (getattr(args, "identity_kind", None) is not None
                or getattr(args, "approved_commit", None) or getattr(args, "approved_tree", None)):
            raise ValueError("execution identity is only valid for production runtime modes")
        return RuntimeContext(selected, MODULE_ID, root)
    kind = getattr(args, "identity_kind", None)
    if selected in {RuntimeMode.FORMAL_READONLY, RuntimeMode.CANDIDATE_VALIDATION} and kind != "oci_container":
        raise ValueError("readonly and candidate preflight require explicit OCI identity")
    if kind == "git_worktree":
        if selected is not RuntimeMode.PRODUCTION_WRITE:
            raise ValueError("Git execution only authorizes production writes")
        if not args.approved_commit or not args.approved_tree:
            raise ValueError("Git execution requires approved commit and tree")
        request = GitExecutionRequest(ROOT, args.approved_commit, args.approved_tree)
    elif kind == "oci_container":
        if args.approved_commit or args.approved_tree:
            raise ValueError("OCI execution identity is supplied only by its signed grant")
        if (selected is RuntimeMode.PRODUCTION_WRITE
                and identity.classification is not RuntimeClassification.FORMAL):
            raise ValueError("OCI capture requires a formal runtime marker")
        request = oci_execution_request(root)
    else:
        raise ValueError("production capture requires an explicit identity kind")
    if selected is RuntimeMode.CANDIDATE_VALIDATION:
        return RuntimeContext(selected, MODULE_ID, root, expected_runtime_id=identity.runtime_id,
                              execution_request=request)
    if selected is RuntimeMode.FORMAL_READONLY:
        verify_execution(request, expected_role="production", module_id=MODULE_ID,
                         runtime_id=identity.runtime_id, runtime_root=root,
                         marker_sha256=identity.marker_sha256)
        return RuntimeContext(selected, MODULE_ID, root, formal_identity=identity)
    if selected is not RuntimeMode.PRODUCTION_WRITE:
        raise ValueError("unsupported runtime mode")
    return RuntimeContext(
        RuntimeMode.PRODUCTION_WRITE,
        MODULE_ID,
        root,
        formal_identity=identity,
        expected_runtime_id=identity.runtime_id,
        execution_request=request,
    )


def main(argv=None,*,client_factory=None,clock=None):
    args=parser().parse_args(argv)
    try:
        mode=RuntimeMode(args.runtime_mode)
        context=initialize_execution_identity(args, mode=mode)
        snapshot_root = args.snapshot_root or (context.runtime_root / "snapshots")
        if mode is RuntimeMode.PRODUCTION_WRITE and args.identity_kind == "oci_container" and args.snapshot_root is None:
            raise ValueError("OCI capture requires an explicit declared snapshot root")
        store=assert_runtime_write(context,snapshot_root)
        if mode is RuntimeMode.PRODUCTION_WRITE and args.identity_kind == "oci_container":
            if args.secret_file != Path("/run/secrets/tankan.env"):
                raise ValueError("OCI capture requires the declared Tankan secret file")
            if store != OCI_CAPTURE_SNAPSHOT_ROOT:
                raise ValueError("OCI capture must use the declared capture snapshot alias")
            readonly = OCI_READONLY_SNAPSHOT_ROOT.resolve(strict=True)
            try:
                if not os.path.samefile(store, readonly):
                    raise ValueError("production capture and consumer snapshots must share one directory")
                if os.stat(store).st_dev != os.stat(readonly).st_dev or os.stat(store).st_ino != os.stat(readonly).st_ino:
                    raise ValueError("production snapshot alias device/inode mismatch")
            except OSError as exc:
                raise ValueError("production snapshot aliases are unavailable") from exc
        request_identity=identify_file(args.request_file)
        calendar_identity=identify_file(args.calendar_file)
        request=_read_json(args.request_file)
        calendar=_read_json(args.calendar_file)
        if request["business_date"]!=args.business_date.isoformat() or request["session"]!=args.session:
            raise ValueError("Request date/session mismatch")
        requested=tuple(ExactRequest(**row) for row in request["requested"])
        policy=CapturePolicy(args.business_date,MarketSession(args.session),
                             {key:datetime.fromisoformat(value) for key,value in request["source_not_before"].items()},
                             request_identity.sha256)
        day_policy=DeterministicBusinessDayPolicy(frozenset(date.fromisoformat(d) for d in calendar["business_days"]),
                                                  "explicit-calendar-file/1",calendar_identity.sha256)
        if identify_file(args.request_file)!=request_identity or identify_file(args.calendar_file)!=calendar_identity:
            raise ValueError("Input identity changed during read")
        factory=client_factory or (lambda: TankanClient(TankanConnectionSettings.from_secret_file(args.secret_file)))
        with factory() as client:
            result=capture_public_intraday(client=client,requested=requested,context=context,store_root=store,
                                           policy=policy,business_day_policy=day_policy,
                                           clock=clock or (lambda:datetime.now(timezone.utc)))
        print(canonical_json(result.evidence).decode("utf-8"))
        return 0
    except Exception as exc:
        print(canonical_json({"schema_version":"public-intraday-evidence/1","status":"FAILED",
                              "business_date":args.business_date,"session":args.session,
                              "error_type":type(exc).__name__,"reason":getattr(exc,"status","ERROR"),
                              "immutable_conflict":getattr(exc,"status","")=="IMMUTABLE_CONFLICT"}).decode("utf-8"))
        return 1


if __name__=="__main__":
    raise SystemExit(main())
