"""Capture current API quotes or backfill only a retained day's CBOT minute."""
from __future__ import annotations

import argparse
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import sys

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "03_src"))

from agri_research_agent.soybean_margin.api_inputs import publish, read_day, read_days, encoded, archive_evidence
from agri_research_agent.soybean_margin.api_sources import credential, capture, backfill, manual_cbot, SourceError, SHANGHAI
from agri_research_agent.shared.runtime_context import establish_application_service_context, assert_runtime_write


def writer():
    deployed = bool(os.getenv("MARKET_DATA_GIT_HEAD") or os.getenv("MARKET_DATA_EXECUTION_GRANT") or (REPO / "RELEASE.json").exists())
    if deployed:
        root = Path("/runtime/capture-snapshots/soybean-api")
        context = establish_application_service_context(service_id="spread-dashboard",
                    module_id="shared-intraday", runtime_root=Path("/runtime"))
    else:
        if os.getenv("SOYBEAN_MARGIN_LOCAL_PREVIEW") != "1" or not os.getenv("LOCALAPPDATA"):
            raise SourceError("local_preview_or_runtime_identity_required")
        base = Path(os.environ["LOCALAPPDATA"]).resolve(strict=True)
        root = (base / "market-data-runtime" / "soybean-api").resolve()
        if not root.is_relative_to(base):
            raise SourceError("local_preview_root_outside_app_data")
        context = None
    root = root.absolute()

    def authorize(path):
        path = Path(path).absolute()
        if not path.is_relative_to(root) or path != path.resolve():
            raise SourceError("api_write_path_invalid")
        if context is not None:
            assert_runtime_write(context, path)
    authorize(root)
    return root, authorize


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--secret-file", type=Path)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--backfill", type=date.fromisoformat, metavar="YYYY-MM-DD")
    group.add_argument("--backfill-pending", action="store_true", help="Retry up to 8 pending days, without fetching historical FX/DCE from current quotes")
    group.add_argument("--manual-cbot-date", type=date.fromisoformat, metavar="YYYY-MM-DD", help="Import user-confirmed contract prices from stdin into a retained day's snapshot")
    parser.add_argument("--evidence-file", type=Path, help="Original user-provided PNG/JPEG screenshot")
    parser.add_argument("--quoted-at", help="Optional actual quotation timestamp with timezone")
    args = parser.parse_args()
    try:
        root, authorize = writer()
        expected = {}
        if args.manual_cbot_date:
            if args.evidence_file is None:
                raise SourceError("manual_cbot_evidence_required")
            snapshot = read_day(root, args.manual_cbot_date)
            if snapshot is None:
                raise SourceError("retained_day_required_for_manual_cbot")
            expected[snapshot["business_date"]] = hashlib.sha256(encoded(snapshot)).hexdigest()
            raw = sys.stdin.read(16385)
            if len(raw.encode("utf-8")) > 16384:
                raise SourceError("manual_cbot_input_too_large")
            evidence = archive_evidence(root, args.evidence_file, authorize=authorize)
            snapshots = [manual_cbot(snapshot, json.loads(raw), evidence, quoted_at=args.quoted_at)]
        elif args.backfill:
            key = credential(args.secret_file)
            snapshot = read_day(root, args.backfill)
            if snapshot is None:
                raise SourceError("retained_day_required_for_cbot_backfill")
            expected[snapshot["business_date"]] = hashlib.sha256(encoded(snapshot)).hexdigest()
            snapshots = [backfill(snapshot, key)]
        elif args.backfill_pending:
            key = credential(args.secret_file)
            from datetime import datetime
            today = datetime.now(SHANGHAI).date()
            pending = [value for day, value in read_days(root, today).items()
                       if day < today and any(v is None for v in value["cbot"].values())]
            snapshots = []
            for value in pending[-8:]:
                try:
                    expected[value["business_date"]] = hashlib.sha256(encoded(value)).hexdigest()
                    snapshots.append(backfill(value, key))
                except SourceError as exc:
                    print(json.dumps(dict(business_date=value["business_date"], error=str(exc))))
        else:
            key = credential(args.secret_file)
            value = capture(key)
            previous = read_day(root, date.fromisoformat(value["business_date"]))
            if previous is not None:
                expected[value["business_date"]] = hashlib.sha256(encoded(previous)).hexdigest()
                manual = previous["sources"]["cbot"].get("manual")
                if manual:
                    value = manual_cbot(value, manual["prices"], manual["evidence"], quoted_at=manual.get("quoted_at"))
                    value["sources"]["cbot"]["manual"]["received_at"] = manual["received_at"]
            snapshots = [value]
        for snapshot in snapshots:
            identity = publish(root, snapshot, authorize=authorize, expected_identity=expected.get(snapshot["business_date"]))
            print(json.dumps(dict(business_date=snapshot["business_date"], sha256=identity,
                    cbot_status=snapshot["sources"]["cbot"].get("status", "missing"),
                    errors=snapshot["errors"], store=str(root)), ensure_ascii=False))
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(json.dumps(dict(error=str(exc) if isinstance(exc, SourceError) else type(exc).__name__)))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
