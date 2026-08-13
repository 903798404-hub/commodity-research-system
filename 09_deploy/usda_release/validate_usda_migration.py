#!/usr/bin/env python3
"""Validate legacy and immutable USDA Runtime HTTP data availability."""

from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any
from urllib.request import urlopen


RELEASE_ID_RE = re.compile(r"^usda-[0-9]{4}-(?:0[1-9]|1[0-2])-[0-9a-f]{16}$")


def get_json(url: str) -> Any:
    with urlopen(url, timeout=10) as response:
        if int(response.status) != 200:
            raise RuntimeError(f"HTTP {response.status}: {url}")
        return json.loads(response.read().decode("utf-8"))


def validate(base_url: str) -> dict[str, Any]:
    base = f"{base_url.rstrip('/')}/usda/data"
    current = get_json(f"{base}/current.json")
    release_id = str(current.get("release_id", ""))
    if not RELEASE_ID_RE.fullmatch(release_id):
        raise RuntimeError("current.json contains an invalid release_id")
    legacy_index = get_json(f"{base}/index.json")
    release_index = get_json(f"{base}/releases/{release_id}/data/index.json")
    catalogs = {
        "legacy": [str(item.get("file", "")) for item in legacy_index.get("matrices", [])],
        "release": [str(item.get("file", "")) for item in release_index.get("matrices", [])],
    }
    report = get_json(f"{base}/report_version.json")
    previous = str(report.get("previousReportMonth", ""))
    fixed = ["report_version.json", "presentation_changes.json", "soybean_oil_US.json", f"snapshots/usda_psd/{previous}/index.json"]
    required_business_paths = {"matrix/4243000_MY.json", "matrix/4243000_ID.json", "matrix/4232000_AR.json"}
    counts: dict[str, int] = {}
    for kind, paths in catalogs.items():
        if not paths or len(paths) != len(set(paths)):
            raise RuntimeError(f"{kind} matrix catalog is missing or duplicated")
        if not required_business_paths.issubset(paths):
            raise RuntimeError(f"{kind} matrix catalog lacks fixed migration regressions")
        prefix = f"{base}/" if kind == "legacy" else f"{base}/releases/{release_id}/data/"
        for relative in [*fixed, *paths]:
            with urlopen(prefix + relative, timeout=10) as response:
                if int(response.status) != 200:
                    raise RuntimeError(f"{kind} HTTP {response.status}: {relative}")
        counts[kind] = len(paths)
    return {"status": "passed", "release_id": release_id, "report_month": report.get("currentReportMonth"), "previous_report_month": previous, "matrix_http_200": counts}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(validate(args.base_url), sort_keys=True))
        return 0
    except Exception as exc:
        print(f"USDA migration validation failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
