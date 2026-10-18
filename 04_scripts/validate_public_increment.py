"""Offline, read-only semantic verification of a sparse public increment."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "03_src"))
from agri_research_agent.pipelines.public_data_increment import validate_increment


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--increment", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(validate_increment(args.increment, args.baseline), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
