"""Create an isolated formal Current candidate from the read-only workbook."""
from __future__ import annotations

from datetime import date
import json
from pathlib import Path
import shutil

import pyarrow as pa
import pyarrow.parquet as pq

from agri_research_agent.data_sources.nutstore_basis import (
    PRODUCTS, assert_external_output, read_nutstore_basis, sha256,
)
from agri_research_agent.pipelines.lutou_domestic_basis import (
    FORMAL_CURRENT_SCHEMA, FORMAL_STABLE_KEY, DomesticBasisPipelineError,
    _business_sha, load_domestic_basis_current, load_historical_basis_seed,
)
from agri_research_agent.shared.atomic_storage import atomic_write_json
from agri_research_agent.shared.immutable_candidate import seal_immutable_candidate

CURRENT_SCHEMA = "domestic-basis-current/4"


def prepare_nutstore_basis_package(
    *, baseline_package: str | Path, output_root: str | Path, source: str | Path,
) -> dict:
    """Stage a complete reviewable delivery package, preserving other datasets."""
    from agri_research_agent.market_data.public_basis_current import load_public_basis_current
    from agri_research_agent.pipelines.public_data_delivery import (
        build_production_package, validate_production_package,
    )
    baseline = validate_production_package(baseline_package)
    output = assert_external_output(output_root)
    if output.exists():
        raise FileExistsError("Nutstore package candidate output already exists")
    baseline_path = baseline.directory.resolve()
    if output.is_relative_to(baseline_path) or baseline_path.is_relative_to(output):
        raise ValueError("Nutstore package output must be isolated from its baseline")

    def build(directory: Path) -> dict:
        public = directory / "public-market-data"
        public.mkdir()
        for dataset in baseline.manifest["current_identities"]:
            if dataset != "lutou-domestic-basis":
                shutil.copytree(baseline.directory / "data/public-market-data" / dataset, public / dataset)
        result = build_nutstore_basis_candidate(
            baseline_root=baseline.directory / "data/public-market-data/lutou-domestic-basis",
            output_root=public / "lutou-domestic-basis", source=source,
        )
        if result["status"] != "CANDIDATE_READY":
            raise ValueError("Nutstore source has no dates after the formal baseline")
        load_public_basis_current(public / "lutou-domestic-basis")
        dates = {**baseline.manifest["source_max_dates"],
                 "nutstore.domestic_basis": result["source_max_date"]}
        package = build_production_package(
            public_current_root=public, packages_root=directory / "packages",
            source_max_dates=dates, required_datasets=list(baseline.manifest["current_identities"]),
            delivery_artifacts={
                name: baseline.directory / "data" / item["package_path"]
                for name, item in baseline.manifest["delivery_artifacts"].items()
            },
        )
        def unrelated(manifest):
            return {item["path"]: item for item in manifest["files"]
                    if not item["path"].startswith("public-market-data/lutou-domestic-basis/")}
        if unrelated(baseline.manifest) != unrelated(package.manifest):
            raise ValueError("Nutstore candidate altered unrelated production datasets")
        validate_production_package(package.directory)
        report = {**result, "baseline_package_id": baseline.package_id,
                  "package_id": package.package_id, "unrelated_datasets": "UNCHANGED"}
        report.pop("candidate_root")
        (directory / "candidate-report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        return report

    directory, report = seal_immutable_candidate(output.parent, output.name, build)
    return {**report, "package_root": str(directory / "packages" / report["package_id"])}


def validate_nutstore_current(root: Path, directory: Path, manifest: dict, table: pa.Table) -> None:
    """Verify preservation using actual parent rows, not just a claimed row count."""
    def require(condition: bool, message: str) -> None:
        if not condition:
            raise DomesticBasisPipelineError(message)

    require(table.schema == FORMAL_CURRENT_SCHEMA, "Nutstore Current schema differs")
    baseline_path = directory / "baseline.parquet"
    require(sha256(baseline_path) == manifest.get("baseline_data_sha256"),
            "Nutstore preserved baseline identity differs")
    baseline = pq.read_table(baseline_path)
    require(baseline.schema == FORMAL_CURRENT_SCHEMA and
            _business_sha(baseline) == manifest.get("baseline_business_sha256"),
            "Nutstore preserved baseline schema or business identity differs")
    cutoff = date.fromisoformat(manifest["append_after"])
    rows = table.to_pylist()
    preserved = pa.Table.from_pylist(
        [row for row in rows if row["business_date"] <= cutoff], schema=FORMAL_CURRENT_SCHEMA,
    )
    ordering = [(field, "ascending") for field in FORMAL_STABLE_KEY]
    require(preserved.sort_by(ordering).equals(baseline.sort_by(ordering)),
            "Nutstore candidate changed or dropped previous formal observations")
    additions = [row for row in rows if row["business_date"] > cutoff]
    require(bool(additions) and all(row["segment"] == "LIVE_NUTSTORE" for row in additions),
            "Nutstore candidate has no verified extension")
    seed = load_historical_basis_seed(root)
    historical = pa.Table.from_pylist(
        [row for row in rows if row["segment"] == "SEALED_HISTORICAL"], schema=FORMAL_CURRENT_SCHEMA,
    )
    require(seed is not None and historical.sort_by(ordering).equals(seed.observations.sort_by(ordering)),
            "Nutstore Current changed sealed historical observations")
    require(manifest.get("historical_seed_business_sha256") == _business_sha(historical),
            "Nutstore historical business identity differs")
    require(manifest.get("source") == "preserved_current_plus_nutstore" and
            manifest.get("scope") == "formal-domestic-basis" and
            manifest.get("quality_status") == "PASS", "Nutstore Current manifest differs")
    keys = [tuple(row[field] for field in FORMAL_STABLE_KEY) for row in rows]
    page_keys = [(row["date"], row["commodity"], row["region"], row["quote_type"],
                  row["delivery_month"], row["futures_contract"]) for row in rows]
    require(len(set(keys)) == len(rows) and len(set(page_keys)) == len(rows),
            "Nutstore Current stable key or page contract collision")
    require(len(rows) == manifest.get("row_count") and
            len({row["series_id"] for row in rows}) == manifest.get("series_count"),
            "Nutstore Current counts differ")
    report = manifest["nutstore_source"]
    require(report.get("source_write_policy") == "entire_123_tree_forbidden" and
            report.get("source_max_date") == manifest.get("source_max_date"),
            "Nutstore source identity differs")
    nutstore = [row for row in rows if row["segment"] == "LIVE_NUTSTORE"]
    for row in nutstore:
        is_basis = row["quote_type"] == "基差报价"
        require(row["provider"] == "Nutstore" and row["currency"] == "CNY" and
                row["unit"] == "CNY/metric_tonne" and row["futures_price"] is None and
                row["date"] == row["business_date"] and row["region"] == row["location"] and
                row["commodity"] == row["consumer_product"],
                "Nutstore quote provenance or aliases differ")
        require((is_basis and row["basis"] == row["value"] and row["cash_price"] is None and
                 row["futures_contract"] is not None) or
                (row["quote_type"] == "一口价" and row["cash_price"] == row["value"] and
                 row["basis"] is None and row["futures_contract"] is None),
                "Nutstore cash/basis null semantics differ")
    require(all(row["snapshot_identity"] == report["source_sha256"] for row in additions),
            "Nutstore source SHA differs from appended observations")
    require(max(row["business_date"] for row in additions).isoformat() == report["source_max_date"],
            "Nutstore latest source date has no usable observations")
    require(min(row["business_date"] for row in rows).isoformat() == manifest.get("min_date") and
            max(row["business_date"] for row in rows).isoformat() == manifest.get("max_date"),
            "Nutstore date range differs")


def build_nutstore_basis_candidate(
    *, baseline_root: str | Path, output_root: str | Path, source: str | Path,
) -> dict:
    """output_root is an empty candidate dataset, never the active dataset."""
    baseline_root = Path(baseline_root).resolve()
    output = assert_external_output(output_root)
    if output == baseline_root or output.is_relative_to(baseline_root) or baseline_root.is_relative_to(output):
        raise ValueError("Nutstore output must be isolated from the baseline")
    if output.exists():
        raise FileExistsError("Nutstore candidate output already exists")
    baseline = load_domestic_basis_current(baseline_root)
    if baseline is None or baseline.observations.schema != FORMAL_CURRENT_SCHEMA:
        raise ValueError("A verified formal basis Current baseline is required")
    cutoff = date.fromisoformat(baseline.manifest["max_date"])
    snapshot = read_nutstore_basis(source, after=cutoff)
    if not snapshot.rows:
        return {"status": "NO_CHANGE", **snapshot.report}
    additions = pa.Table.from_pylist(snapshot.rows, schema=FORMAL_CURRENT_SCHEMA)
    if {row["commodity"] for row in snapshot.rows if row["quote_type"] == "基差报价"} != {
        item[0] for item in PRODUCTS.values()
    }:
        raise ValueError("Nutstore extension lacks one or more of the five basis products")
    combined = pa.concat_tables([baseline.observations, additions]).sort_by(
        [(field, "ascending") for field in FORMAL_STABLE_KEY],
    )
    seed = load_historical_basis_seed(baseline_root)
    if seed is None:
        raise ValueError("Verified sealed basis history is required")
    release_id = f"nutstore-basis-{_business_sha(combined)[:24]}"

    def build(dataset: Path) -> dict:
        shutil.copytree(baseline_root / "historical-seeds", dataset / "historical-seeds")
        shutil.copy2(baseline_root / "historical-seed.json", dataset / "historical-seed.json")

        def build_release(directory: Path) -> dict:
            pq.write_table(combined, directory / "observations.parquet")
            pq.write_table(baseline.observations, directory / "baseline.parquet")
            manifest = {
                "schema_version": CURRENT_SCHEMA, "release_id": release_id,
                "source": "preserved_current_plus_nutstore", "scope": "formal-domestic-basis",
                "quality_status": "PASS", "cutover_date": "2026-06-01",
                "append_after": cutoff.isoformat(), "row_count": combined.num_rows,
                "series_count": len(set(combined["series_id"].to_pylist())),
                "historical_row_count": seed.observations.num_rows,
                "live_row_count": combined.num_rows - seed.observations.num_rows,
                "min_date": min(combined["business_date"].to_pylist()).isoformat(),
                "max_date": max(combined["business_date"].to_pylist()).isoformat(),
                "source_max_date": snapshot.report["source_max_date"],
                "data_sha256": sha256(directory / "observations.parquet"),
                "business_content_sha256": _business_sha(combined),
                "baseline_data_sha256": sha256(directory / "baseline.parquet"),
                "baseline_business_sha256": _business_sha(baseline.observations),
                "baseline_release_id": baseline.release_id,
                "baseline_manifest_sha256": sha256(baseline.directory / "manifest.json"),
                "historical_seed_business_sha256": _business_sha(seed.observations),
                "formal_contract_parity": baseline.manifest["formal_contract_parity"],
                "nutstore_source": snapshot.report,
                "quality": {"stable_key_duplicate_count": 0, "legacy_contract_collision_count": 0},
                "files": {
                    filename: {"sha256": sha256(directory / filename),
                               "size_bytes": (directory / filename).stat().st_size}
                    for filename in ("observations.parquet", "baseline.parquet")
                },
            }
            validate_nutstore_current(dataset, directory, manifest, combined)
            (directory / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8",
            )
            return manifest

        directory, manifest = seal_immutable_candidate(dataset / "releases", release_id, build_release)
        atomic_write_json(dataset / "current.json", {
            "schema_version": 1, "release_id": release_id,
            "manifest_sha256": sha256(directory / "manifest.json"),
        })
        load_domestic_basis_current(dataset)
        return manifest

    # Seal the entire candidate dataset only after all record/reader checks pass.
    directory, manifest = seal_immutable_candidate(output.parent, output.name, build)
    return {"status": "CANDIDATE_READY", "candidate_root": str(directory),
            "release_id": manifest["release_id"], **snapshot.report}
