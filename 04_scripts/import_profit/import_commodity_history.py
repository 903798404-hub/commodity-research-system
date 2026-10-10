"""Import audited, normalized workbook history into the isolated local store."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "03_src"))

from agri_research_agent.commodity_import_margin.history import MAX_BYTES, publish_history
from agri_research_agent.commodity_import_margin.store import authorize_local, local_database


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", required=True, type=Path)
    parser.add_argument("--source-file", required=True, type=Path)
    parser.add_argument("--expected-revision", default=None)
    args = parser.parse_args()
    if args.history.stat().st_size > MAX_BYTES:
        raise ValueError("历史文件超出64MiB上限")
    value = json.loads(args.history.read_text(encoding="utf-8", errors="strict"))
    revision = publish_history(local_database(), value, args.source_file,
        expected_revision=args.expected_revision, authorize=authorize_local)
    print(json.dumps({"commodity": value["commodity"], "revision": revision,
                      "records": len(value["rows"])}, ensure_ascii=False))


if __name__ == "__main__":
    main()
