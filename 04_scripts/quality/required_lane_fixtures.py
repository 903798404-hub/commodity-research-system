"""Build minimal deterministic data roots for hosted required tests.

The fixtures are synthetic and immutable. They exercise the production readers
and page contracts without consulting a developer machine, a server, or a live
Public Current pointer.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import sys


SPREAD_NAMES = (
    ("M 1-5", "M", 1, "M", 5),
    ("M 5-9", "M", 5, "M", 9),
    ("M 9-1", "M", 9, "M", 1),
    ("Y 1-5", "Y", 1, "Y", 5),
    ("Y 5-9", "Y", 5, "Y", 9),
    ("Y 9-1", "Y", 9, "Y", 1),
)
SEASON_STARTS = tuple(range(2021, 2027))
OBSERVATION_DAYS = ((1, 0), (8, 7))
PUBLIC_RELEASE_ID = "required-fixture-20260812"
PUBLIC_SOURCE_MAX_DATE = date(2026, 8, 12)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _spread_rows() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for spread_index, (name, leg1, month1, leg2, month2) in enumerate(SPREAD_NAMES):
        for season_start in SEASON_STARTS:
            for day, offset in OBSERVATION_DAYS:
                leg1_price = 3000 + spread_index * 100 + (season_start - 2020) * 10 + offset
                leg2_price = 2500 + spread_index * 80 + (season_start - 2020) * 8
                rows.append(
                    {
                        "date": datetime(season_start, 8, day),
                        "spread_group": name.split()[0],
                        "spread_name": name,
                        "leg1_instrument": leg1,
                        "leg1_month": month1,
                        "leg1_price": float(leg1_price),
                        "leg2_instrument": leg2,
                        "leg2_month": month2,
                        "leg2_price": float(leg2_price),
                        "spread_value": float(leg1_price - leg2_price),
                        "season": f"{season_start}/{season_start + 1}",
                        "calendar_offset": offset,
                        "month_day": f"08-{day:02d}",
                        "status": "success",
                        "error": "",
                        "updated_at": "2026-08-12T00:00:00+00:00",
                    }
                )
    return rows


def _build_spread_root(source_root: Path, output: Path) -> Path:
    import pandas as pd

    root = output / "spread-reference"
    data_dir = root / "01_data"
    config_dir = root / "02_configs"
    data_dir.mkdir(parents=True, exist_ok=True)
    config_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(_spread_rows()).to_parquet(
        data_dir / "historical_spread_database.parquet", index=False
    )
    shutil.copyfile(
        source_root / "02_configs" / "historical_spread_config.xlsx",
        config_dir / "historical_spread_config.xlsx",
    )
    return root


def _public_rows(catalog: object) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    captured_at = datetime(2026, 8, 12, 12, tzinfo=timezone.utc)
    for series_index, series in enumerate(catalog.series):
        for year in range(2021, 2027):
            # A common observation date keeps every derived spread computable,
            # while each series' catalog date preserves its real terminal-date
            # difference for freshness-status coverage.
            for day in (1, series.latest_date.day):
                rows.append(
                    {
                        "schema_version": "lutou-three-oil-canonical/2",
                        "dataset_id": series.dataset_id,
                        "series_id": series.series_id,
                        "provider_dataset_id": series.dataset_id,
                        "provider_series_id": series.provider_series_id,
                        "provider": series.provider,
                        "metadata_source_type": "repository_fixture",
                        "metadata_status": "proven",
                        "source_quote_unit": series.unit,
                        "origin_system": series.origin_system,
                        "acquisition_channel": "repository_fixture",
                        "source_locator": "fixture:required-lane/public-current",
                        "business_date": date(year, series.latest_date.month, day),
                        "value": Decimal(
                            1000 + series_index * 25 + (year - 2020) * 3 + day
                        ),
                        "currency": series.currency,
                        "unit": series.unit,
                        "product": series.product,
                        "product_grade": series.product_grade,
                        "country": series.country,
                        "region": series.region,
                        "location_native": series.location_native,
                        "quote_basis": series.quote_basis,
                        "tenor": series.tenor,
                        "price_type": series.price_type,
                        "source_duplicate_count": 0,
                        "source_group_sha256": hashlib.sha256(
                            series.series_id.encode("utf-8")
                        ).hexdigest(),
                        "source_policy_version": "required-lane-fixture/1",
                        "captured_at": captured_at,
                    }
                )
    return rows


def _build_public_root(source_root: Path, output: Path) -> Path:
    sys.path.insert(0, str(source_root / "03_src"))
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq

        from agri_research_agent.pipelines.lutou_goal_b import CANONICAL_SCHEMA
        from agri_research_agent.research_data.three_oil_v1 import load_three_oil_v1
    finally:
        sys.path.pop(0)

    catalog = load_three_oil_v1(
        source_root / "02_configs" / "international_three_oil_v1.sealed.json"
    )
    table = pa.Table.from_pylist(_public_rows(catalog), schema=CANONICAL_SCHEMA).sort_by(
        (("series_id", "ascending"), ("business_date", "ascending"))
    )
    root = output / "public-runtime"
    current_root = root / "public-market-data" / "lutou-three-oil"
    release = current_root / "releases" / PUBLIC_RELEASE_ID
    release.mkdir(parents=True, exist_ok=True)
    observations = release / "observations.parquet"
    pq.write_table(table, observations)
    dates = table["business_date"].to_pylist()
    manifest = {
        "schema_version": "lutou-goal-b-current/2",
        "release_id": PUBLIC_RELEASE_ID,
        "source": "repository-fixture",
        "scope": "three-oil-v1-sealed-series",
        "promoted_at": "2026-08-12T12:00:00+00:00",
        "canonical_manifest_sha256": "0" * 64,
        "source_max_date": PUBLIC_SOURCE_MAX_DATE.isoformat(),
        "row_count": table.num_rows,
        "series_count": len(catalog.series),
        "min_date": min(dates).isoformat(),
        "max_date": max(dates).isoformat(),
        "metadata_provenance": {
            "fixture": "required-lane-deterministic",
            "network_dependency": False,
        },
        "quality_status": "PASS",
        "files": {
            observations.name: {
                "sha256": _sha256(observations),
                "size_bytes": observations.stat().st_size,
            }
        },
    }
    manifest_path = release / "manifest.json"
    _write_json(manifest_path, manifest)
    _write_json(
        current_root / "current.json",
        {
            "schema_version": 1,
            "release_id": PUBLIC_RELEASE_ID,
            "manifest_sha256": _sha256(manifest_path),
        },
    )
    return root


def _make_read_only(root: Path) -> None:
    if os.name == "nt":
        return
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if path.is_file():
            path.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        elif path.is_dir():
            path.chmod(
                stat.S_IRUSR
                | stat.S_IXUSR
                | stat.S_IRGRP
                | stat.S_IXGRP
                | stat.S_IROTH
                | stat.S_IXOTH
            )
    root.chmod(
        stat.S_IRUSR
        | stat.S_IXUSR
        | stat.S_IRGRP
        | stat.S_IXGRP
        | stat.S_IROTH
        | stat.S_IXOTH
    )


def build(source_root: Path, output: Path) -> dict[str, object]:
    source_root = source_root.resolve(strict=True)
    output = output.resolve()
    if output.exists():
        raise ValueError("required fixture output must not already exist")
    output.mkdir(parents=True)
    spread_root = _build_spread_root(source_root, output)
    public_root = _build_public_root(source_root, output)
    evidence = {
        "schema_version": "required-lane-fixtures/1",
        "spread_reference_data_root": str(spread_root),
        "public_market_data_runtime_root": str(public_root),
        "spread_reference_required_files": [
            "01_data/historical_spread_database.parquet",
            "02_configs/historical_spread_config.xlsx",
        ],
        "public_runtime_required_files": [
            "public-market-data/lutou-three-oil/current.json",
            f"public-market-data/lutou-three-oil/releases/{PUBLIC_RELEASE_ID}/manifest.json",
            f"public-market-data/lutou-three-oil/releases/{PUBLIC_RELEASE_ID}/observations.parquet",
        ],
        "network_dependency": False,
        "production_data_dependency": False,
    }
    _write_json(output / "fixture-manifest.json", evidence)
    _make_read_only(output)
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    evidence = build(args.source_root, args.output)
    print(json.dumps(evidence, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
