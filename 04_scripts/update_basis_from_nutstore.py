"""Build a local, isolated basis Current candidate; no production activation."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "03_src"))

from agri_research_agent.data_sources.nutstore_basis import SOURCE_PATH
from agri_research_agent.pipelines.nutstore_domestic_basis import (
    build_nutstore_basis_candidate, prepare_nutstore_basis_package,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    baseline = parser.add_mutually_exclusive_group(required=True)
    baseline.add_argument("--baseline-root", type=Path)
    baseline.add_argument("--baseline-package", type=Path)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--source", type=Path, default=SOURCE_PATH)
    args = parser.parse_args()
    if args.baseline_package is not None:
        result = prepare_nutstore_basis_package(
            baseline_package=args.baseline_package, output_root=args.output_root, source=args.source,
        )
    else:
        result = build_nutstore_basis_candidate(
            baseline_root=args.baseline_root, output_root=args.output_root, source=args.source,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
