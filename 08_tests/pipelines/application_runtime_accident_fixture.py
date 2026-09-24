"""Disposable Public Current fixture for Linux runtime-readability tests.

The source rows are synthetic and never represent approved market observations.
Formal consumer identity fields are fixture stamps; the fixture tests runtime
readability through real parsers/readers, not source provenance approval.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
from datetime import date
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import yaml

from agri_research_agent.data_sources.lutou.domestic_basis import load_domestic_basis_catalog
from agri_research_agent.data_sources.lutou.weather_live import load_weather_source_catalog
from agri_research_agent.market_data import public_weather_current
from agri_research_agent.pipelines import lutou_domestic_basis, lutou_weather
from agri_research_agent.pipelines.public_data_delivery import build_production_package
from agri_research_agent.shared.file_identity import identify_file


ROOT = Path(__file__).resolve().parents[2]


def _test_fixture(name: str):
    path = ROOT / "08_tests" / "pipelines" / name
    spec = importlib.util.spec_from_file_location(f"accident_{path.stem}", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


def _file_identity(path: Path) -> dict[str, object]:
    identity = identify_file(path)
    return {"sha256": identity.sha256, "size_bytes": identity.size_bytes}


def _weather_current(work: Path, public: Path) -> None:
    source = _test_fixture("test_lutou_weather.py")
    weather_runtime = work / "weather-runtime"
    runtime = source._runtime(weather_runtime)
    source._seed_soil(runtime)
    baseline_root = runtime.runtime_root / "approved-normal-baselines"
    source._seed_normal_baselines(baseline_root)
    client = source.FakeWeatherClient()
    catalog = load_weather_source_catalog(
        client, ROOT / "02_configs" / "lutou_weather_current.yaml"
    )
    assert len(catalog.series) == 686
    lutou_weather.run_lutou_weather(
        client,
        runtime=runtime,
        run_id="weather-runtime-readability-fixture",
        as_of_date=date(2026, 8, 19),
        full_load=True,
        policy_path=ROOT / "02_configs" / "lutou_weather_current.yaml",
        baseline_root=baseline_root,
        source_catalog=catalog,
    )
    current = runtime.runtime_root / "public-market-data" / "lutou-weather"
    release = current / "releases" / "weather-runtime-readability-fixture"
    manifest_path = release / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["complete_series_count"] == 842
    # The synthetic offline source has a different source-content contract hash.
    # Stamp the approved reader contract solely for this parser/readability
    # fixture; do not claim source provenance from this artificial dataset.
    manifest["contract_sha256"] = public_weather_current._CURRENT_CONTRACT_SHA256
    _json(manifest_path, manifest)
    pointer = json.loads((current / "current.json").read_text(encoding="utf-8"))
    pointer["manifest_sha256"] = identify_file(manifest_path).sha256
    _json(current / "current.json", pointer)
    shutil.copytree(current, public / "lutou-weather")


def _basis_current(work: Path, public: Path) -> None:
    source = _test_fixture("test_lutou_domestic_basis_formal_current.py")
    pipeline = lutou_domestic_basis
    basis_runtime = work / "basis-runtime"
    basis_runtime.mkdir()
    runtime = source.runtime.__wrapped__(basis_runtime)
    config = yaml.safe_load((ROOT / "02_configs" / "lutou_domestic_basis.yaml").read_text(encoding="utf-8"))
    config["freshness_policy"] = {
        "policy_version": "test-only-not-business-approved",
        "threshold_approved": True,
        "freshness_threshold": 6,
        "stale_is_blocking": False,
    }
    mapping = work / "basis-mapping.yaml"
    mapping.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    catalog = load_domestic_basis_catalog(mapping)
    live = pipeline.run_domestic_basis_live(
        runtime=runtime,
        run_id="basis-live-readability-fixture",
        adapter=source.FakeLiveAdapter(source._live_records()),
        mapping_path=mapping,
        mode="full",
    ).current
    assert live is not None and live.observations.num_rows == 711

    historical, _ = pipeline.build_historical_seed_table(source._historical_source(), catalog)
    # Keep all 16,331 rows and their values while giving the synthetic historical
    # series disjoint fixture identities, as the strict 29-series reader expects.
    historical = pa.Table.from_pylist(
        [
            {**row, "series_id": f"{row['series_id']}.historical_fixture"}
            for row in historical.to_pylist()
        ],
        schema=pipeline.FORMAL_CURRENT_SCHEMA,
    )
    current = runtime.runtime_root / "public-market-data" / "lutou-domestic-basis"
    seed_directory = current / "historical-seeds" / pipeline.HISTORICAL_SEED_ID
    seed_directory.mkdir(parents=True)
    seed_data = seed_directory / "observations.parquet"
    pq.write_table(historical, seed_data)
    seed_manifest = {
        "seed_id": pipeline.HISTORICAL_SEED_ID,
        "sealed": True,
        "source_sha256": pipeline.HISTORICAL_SOURCE_SHA256,
        "quality_status": "PASS",
        "data_sha256": identify_file(seed_data).sha256,
        "business_content_sha256": pipeline._business_sha(historical),
        "files": {seed_data.name: _file_identity(seed_data)},
    }
    seed_manifest_path = seed_directory / "manifest.json"
    _json(seed_manifest_path, seed_manifest)
    _json(current / "historical-seed.json", {
        "schema_version": 1,
        "seed_id": pipeline.HISTORICAL_SEED_ID,
        "manifest_sha256": identify_file(seed_manifest_path).sha256,
    })
    seed = pipeline.load_historical_basis_seed(current)
    assert seed is not None
    combined, quality = pipeline.compose_formal_basis_current(seed, live.observations, catalog)
    assert combined.num_rows == 17_042 and quality["series_count"] == 29
    release_id = "basis-runtime-readability-fixture"
    release = current / "releases" / release_id
    release.mkdir()
    observations = release / "observations.parquet"
    pq.write_table(combined, observations)
    dates = combined["business_date"].to_pylist()
    live_dates = live.observations["business_date"].to_pylist()
    parity = {
        "quality_status": "PASS",
        "baseline_sha256": pipeline.FORMAL_BASELINE_SHA256,
        "baseline_rows": 17_042,
        "common_rows": 17_042,
        "baseline_only_rows": 0,
        "public_only_nonextension_rows": 0,
        "field_differences": {
            field: 0 for field in (
                "date", "commodity", "region", "quote_type", "delivery_month",
                "futures_contract", "cash_price", "futures_price", "basis", "source_sheet",
            )
        },
    }
    manifest = {
        "release_id": release_id,
        "schema_version": "lutou-domestic-basis-current/3",
        "source": "sealed_history_plus_lutou",
        "scope": "formal-domestic-basis",
        "quality_status": "PASS",
        "cutover_date": pipeline.FORMAL_CUTOVER_DATE.isoformat(),
        "series_count": 29,
        "historical_row_count": 16_331,
        "live_row_count": 711,
        "row_count": combined.num_rows,
        "source_max_date": max(live_dates).isoformat(),
        "min_date": min(dates).isoformat(),
        "max_date": max(dates).isoformat(),
        "formal_contract_parity": parity,
        "quality": quality,
        "historical_seed_manifest_sha256": identify_file(seed_manifest_path).sha256,
        "historical_seed_data_sha256": seed_manifest["data_sha256"],
        "historical_seed_business_sha256": seed_manifest["business_content_sha256"],
        "live_business_content_sha256": pipeline._business_sha(live.observations),
        "data_sha256": identify_file(observations).sha256,
        "business_content_sha256": pipeline._business_sha(combined),
        "files": {observations.name: _file_identity(observations)},
    }
    manifest_path = release / "manifest.json"
    _json(manifest_path, manifest)
    _json(current / "current.json", {
        "schema_version": 1,
        "release_id": release_id,
        "manifest_sha256": identify_file(manifest_path).sha256,
    })
    shutil.copytree(current, public / "lutou-domestic-basis")


def build_disposable_packages(work: Path) -> tuple[object, object]:
    """Build two distinct, immutable packages with real four-consumer reads."""

    required = _test_fixture("../../04_scripts/quality/required_lane_fixtures.py")
    delivery_test = _test_fixture("test_public_data_delivery.py")
    runtime_root = required._build_public_root(ROOT, work / "required")
    public = runtime_root / "public-market-data"
    _weather_current(work, public)
    _basis_current(work, public)
    old_artifact = delivery_test._domestic_spread(
        work / "artifact-old" / "historical_spread_database.parquet",
        value=10.0, updated_at="before",
    )
    new_artifact = delivery_test._domestic_spread(
        work / "artifact-new" / "historical_spread_database.parquet",
        value=11.0, updated_at="after",
    )
    source = runtime_root / "public-market-data"
    old = build_production_package(
        public_current_root=source,
        packages_root=work / "packages",
        source_max_dates={},
        delivery_artifacts={"domestic-spread": old_artifact},
    )
    new = build_production_package(
        public_current_root=source,
        packages_root=work / "packages",
        source_max_dates={},
        delivery_artifacts={"domestic-spread": new_artifact},
    )
    assert old.package_id != new.package_id
    return old, new
