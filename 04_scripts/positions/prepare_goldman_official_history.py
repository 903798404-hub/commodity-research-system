"""Build a source-backed Goldman history increment from the pinned positions archive."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "03_src"))

from agri_research_agent.positions import delivery
from agri_research_agent.sugar_positions.model import COFCO_PARTIAL_RECORDS, load_members
from agri_research_agent.sugar_positions.sources import parse_czce_goldman_capture


def prepare(baseline, project_root):
    builder = delivery.IncrementBuilder(baseline, project_root)
    snapshot = builder.snapshots["sugar"]
    _, files = delivery.archive_snapshots(baseline)
    member = next(m for m in load_members(project_root / "02_configs/sugar_positions.json")
                  if m["id"] == "goldman")
    complete_days = {row["report_date"] for row in snapshot["domestic"]
                     if row["scope"] == "SR" and row.get("record_type") not in COFCO_PARTIAL_RECORDS}
    rows, captures = [], []
    for key, source in sorted(snapshot["sources"].items()):
        if not key.startswith("czce_cofco_official_"):
            continue
        compact_day = key.removeprefix("czce_cofco_official_")
        day = datetime.strptime(compact_day, "%Y%m%d").date().isoformat()
        if day in complete_days:
            continue
        raw = files["sugar/" + source["raw_file"]]
        rows.extend(parse_czce_goldman_capture(raw, day, source["url"], source["retrieved_at"], member))
        captures.append(("czce_goldman_official_" + compact_day, raw, source["url"], "zip"))
    builder.publish(Path("sugar"), [], rows, captures, [])
    document, observations = builder.finish(project_root)
    return document, observations


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.output.absolute().is_relative_to(args.baseline.absolute().parent):
        parser.error("候选不能写入基线目录")
    baseline = delivery.read_archive(args.baseline)
    document, observations = prepare(baseline, ROOT)
    with args.output.open("xb") as stream:
        stream.write(delivery.canonical(document))
    print(json.dumps(observations, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
