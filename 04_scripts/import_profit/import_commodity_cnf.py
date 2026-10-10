"""Codex-assisted local CNF entry from an explicit dated USD/tonne quote file."""
from pathlib import Path
import argparse
import json
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "03_src"))

from agri_research_agent.commodity_import_margin.manual import import_manual_cnf
from agri_research_agent.commodity_import_margin.runtime import database_path, authorize_write


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quotes-file", type=Path, required=True)
    parser.add_argument("--expected-version", type=int, required=True)
    args = parser.parse_args()
    try:
        database = database_path()
        authorize_write(database)
        version = import_manual_cnf(database, args.quotes_file, expected_version=args.expected_version,
                                    authorize=authorize_write, refresh_current=True)
        print(json.dumps(dict(version=version, entry_method="manual_codex")))
        return 0
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error) as exc:
        print(json.dumps(dict(error=type(exc).__name__), ensure_ascii=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
