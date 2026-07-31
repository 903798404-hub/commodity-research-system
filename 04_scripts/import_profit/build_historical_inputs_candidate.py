"""Build isolated historical CNF and DCE input candidates."""

from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Iterable, Sequence
import uuid

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = REPOSITORY_ROOT / "03_src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from agri_research_agent.import_profit.config import (  # noqa: E402
    SoybeanImportProfitConfig,
    load_soybean_config,
)
from agri_research_agent.import_profit.contract_mapping import (  # noqa: E402
    map_soybean_contracts,
)
from agri_research_agent.import_profit.historical_cnf_adapter import (  # noqa: E402
    ADAPTER_VERSION as CNF_ADAPTER_VERSION,
    HISTORICAL_CNF_SCHEMA,
    HistoricalCnfError,
    HistoricalCnfResult,
    adapt_historical_cnf_excel,
    inspect_excel_identity,
    records_as_dicts as cnf_records_as_dicts,
)
from agri_research_agent.import_profit.historical_dce_adapter import (  # noqa: E402
    ADAPTER_VERSION as DCE_ADAPTER_VERSION,
    HISTORICAL_DCE_CONTINUOUS_SCHEMA,
    HistoricalDceError,
    HistoricalDceResult,
    adapt_historical_dce_sql,
    records_as_dicts as dce_records_as_dicts,
    resolve_historical_dce_points,
)
from agri_research_agent.import_profit.market_snapshot import (  # noqa: E402
    DcePricePoint,
)
from agri_research_agent.import_profit.models import BusinessKey  # noqa: E402
from agri_research_agent.import_profit.reuters_adapter import (  # noqa: E402
    inspect_source_identity,
)
from agri_research_agent.import_profit.standard_io import (  # noqa: E402
    DCE_HISTORICAL_SCHEMA,
)


CNF_FILENAME = "historical_cnf_quotes.parquet"
DCE_FILENAME = "historical_dce_continuous.parquet"
RESOLVED_FILENAME = "historical_dce_resolved_sample.parquet"
MANIFEST_FILENAME = "manifest.json"
QUALITY_FILENAME = "quality_report.json"
SCHEMA_VERSION = "historical-input-candidate-v1"
ADAPTER_VERSION = f"{CNF_ADAPTER_VERSION}+{DCE_ADAPTER_VERSION}"
SAMPLE_KEY_PATTERN = re.compile(
    r"^(\d{4}-\d{2}-\d{2}),([^,]+),(\d{4})-(0[1-9]|1[0-2])$"
)


class HistoricalCandidateBuildError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        quality_report: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.quality_report = quality_report


def parse_sample_key(
    value: str, config: SoybeanImportProfitConfig
) -> BusinessKey:
    match = SAMPLE_KEY_PATTERN.fullmatch(value)
    if match is None:
        raise HistoricalCandidateBuildError(
            "sample-key must use business_date,origin,shipment_period"
        )
    try:
        business_date = date.fromisoformat(match.group(1))
    except ValueError as exc:
        raise HistoricalCandidateBuildError("sample-key business_date is invalid") from exc
    origin_text = match.group(2).strip()
    labels = {origin.label: origin.code for origin in config.origins}
    origin_codes = set(config.origin_codes)
    origin = labels.get(origin_text, origin_text)
    if origin not in origin_codes:
        raise HistoricalCandidateBuildError("sample-key origin is not configured")
    shipment_year = int(match.group(3))
    shipment_month = int(match.group(4))
    return BusinessKey(
        business_date=business_date,
        commodity=config.commodity,
        origin=origin,
        shipment_year=shipment_year,
        shipment_month=shipment_month,
        allowed_origins=config.origin_codes,
        expected_commodity=config.commodity,
        expected_shipment_period=f"{shipment_year:04d}-{shipment_month:02d}",
    )


def build_historical_inputs_candidate(
    excel_path: str | Path,
    sql_path: str | Path,
    config_path: str | Path,
    output_dir: str | Path,
    *,
    sample_keys: Sequence[str] = (),
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    excel = Path(excel_path)
    sql = Path(sql_path)
    config_source = Path(config_path)
    destination = Path(output_dir)
    _validate_destination(destination)
    config = load_soybean_config(config_source)
    parsed_sample_keys = tuple(parse_sample_key(item, config) for item in sample_keys)

    excel_before = inspect_excel_identity(excel)
    sql_before = inspect_source_identity(sql)
    try:
        cnf = adapt_historical_cnf_excel(excel)
        dce = adapt_historical_dce_sql(sql)
    except (HistoricalCnfError, HistoricalDceError) as exc:
        source_name = "cnf" if isinstance(exc, HistoricalCnfError) else "dce"
        report = {
            "candidate_status": "failed",
            "fatal_issues": [
                {
                    "source": source_name,
                    "code": "adapter_quality_failure",
                    "count": 1,
                }
            ],
            "warnings": [],
            source_name: exc.quality_report,
        }
        raise HistoricalCandidateBuildError(
            f"{source_name} adapter quality checks failed",
            quality_report=report,
        ) from exc
    if cnf.source_identity != excel_before or dce.source_identity != sql_before:
        raise HistoricalCandidateBuildError(
            "source identity changed between preflight and adaptation"
        )

    resolved = resolve_historical_dce_points(
        parsed_sample_keys,
        config=config,
        continuous_points=dce.records,
    )
    sample_report = _sample_report(
        parsed_sample_keys, resolved, config, cnf, dce
    )
    timestamp = generated_at or datetime.now(timezone.utc)
    if timestamp.tzinfo is None:
        raise HistoricalCandidateBuildError("generated_at must be timezone-aware")
    generated_at_text = timestamp.astimezone(timezone.utc).isoformat()
    quality = _build_quality_report(cnf, dce, sample_report)

    existed_before = destination.exists()
    destination.mkdir(parents=True, exist_ok=True)
    finals = {
        CNF_FILENAME: destination / CNF_FILENAME,
        DCE_FILENAME: destination / DCE_FILENAME,
        QUALITY_FILENAME: destination / QUALITY_FILENAME,
        MANIFEST_FILENAME: destination / MANIFEST_FILENAME,
    }
    if parsed_sample_keys:
        finals[RESOLVED_FILENAME] = destination / RESOLVED_FILENAME
    if any(path.exists() for path in finals.values()):
        raise HistoricalCandidateBuildError("candidate output already exists")
    token = uuid.uuid4().hex
    temporaries = {
        name: destination / f".{name}.{token}.tmp" for name in finals
    }
    created_finals: list[Path] = []

    try:
        cnf_table = pa.Table.from_pylist(
            cnf_records_as_dicts(cnf.records), schema=HISTORICAL_CNF_SCHEMA
        )
        dce_table = pa.Table.from_pylist(
            dce_records_as_dicts(dce.records),
            schema=HISTORICAL_DCE_CONTINUOUS_SCHEMA,
        )
        pq.write_table(cnf_table, temporaries[CNF_FILENAME], compression="zstd")
        pq.write_table(dce_table, temporaries[DCE_FILENAME], compression="zstd")
        _fsync_file(temporaries[CNF_FILENAME])
        _fsync_file(temporaries[DCE_FILENAME])
        _verify_parquet(
            temporaries[CNF_FILENAME],
            HISTORICAL_CNF_SCHEMA,
            len(cnf.records),
            key_fields=(
                "business_date",
                "commodity",
                "origin",
                "shipment_year",
                "shipment_month",
            ),
        )
        _verify_parquet(
            temporaries[DCE_FILENAME],
            HISTORICAL_DCE_CONTINUOUS_SCHEMA,
            len(dce.records),
            key_fields=("business_date", "instrument", "delivery_month"),
        )
        if parsed_sample_keys:
            resolved_table = pa.Table.from_pylist(
                _resolved_as_dicts(resolved), schema=DCE_HISTORICAL_SCHEMA
            )
            pq.write_table(
                resolved_table, temporaries[RESOLVED_FILENAME], compression="zstd"
            )
            _fsync_file(temporaries[RESOLVED_FILENAME])
            _verify_parquet(
                temporaries[RESOLVED_FILENAME],
                DCE_HISTORICAL_SCHEMA,
                len(resolved),
                key_fields=("business_date", "contract_code"),
            )

        _write_json(temporaries[QUALITY_FILENAME], quality)
        output_identities = {
            CNF_FILENAME: _file_identity(
                temporaries[CNF_FILENAME],
                filename=CNF_FILENAME,
                schema=HISTORICAL_CNF_SCHEMA,
            ),
            DCE_FILENAME: _file_identity(
                temporaries[DCE_FILENAME],
                filename=DCE_FILENAME,
                schema=HISTORICAL_DCE_CONTINUOUS_SCHEMA,
            ),
            QUALITY_FILENAME: _file_identity(
                temporaries[QUALITY_FILENAME], filename=QUALITY_FILENAME
            ),
        }
        if parsed_sample_keys:
            output_identities[RESOLVED_FILENAME] = _file_identity(
                temporaries[RESOLVED_FILENAME],
                filename=RESOLVED_FILENAME,
                schema=DCE_HISTORICAL_SCHEMA,
            )
        manifest = _build_manifest(
            cnf,
            dce,
            parsed_sample_keys,
            sample_report,
            len(resolved),
            generated_at_text,
            output_identities,
        )
        _assert_safe_json(manifest)
        _assert_safe_json(quality)
        _write_json(temporaries[MANIFEST_FILENAME], manifest)

        if inspect_excel_identity(excel) != excel_before:
            raise HistoricalCandidateBuildError(
                "Excel source identity changed during candidate build"
            )
        if inspect_source_identity(sql) != sql_before:
            raise HistoricalCandidateBuildError(
                "SQL source identity changed during candidate build"
            )

        replace_order = [CNF_FILENAME, DCE_FILENAME]
        if parsed_sample_keys:
            replace_order.append(RESOLVED_FILENAME)
        replace_order.extend([QUALITY_FILENAME, MANIFEST_FILENAME])
        for name in replace_order:
            os.replace(temporaries[name], finals[name])
            created_finals.append(finals[name])

        _verify_parquet(
            finals[CNF_FILENAME],
            HISTORICAL_CNF_SCHEMA,
            len(cnf.records),
            key_fields=(
                "business_date",
                "commodity",
                "origin",
                "shipment_year",
                "shipment_month",
            ),
        )
        _verify_parquet(
            finals[DCE_FILENAME],
            HISTORICAL_DCE_CONTINUOUS_SCHEMA,
            len(dce.records),
            key_fields=("business_date", "instrument", "delivery_month"),
        )
        if parsed_sample_keys:
            _verify_parquet(
                finals[RESOLVED_FILENAME],
                DCE_HISTORICAL_SCHEMA,
                len(resolved),
                key_fields=("business_date", "contract_code"),
            )
        if json.loads(finals[QUALITY_FILENAME].read_text(encoding="utf-8")) != quality:
            raise HistoricalCandidateBuildError("quality report round-trip mismatch")
        if json.loads(finals[MANIFEST_FILENAME].read_text(encoding="utf-8")) != manifest:
            raise HistoricalCandidateBuildError("manifest round-trip mismatch")
        for name, expected in manifest["output_files"].items():
            actual = _file_identity(
                finals[name],
                schema=_schema_for_filename(name),
            )
            if actual != expected:
                raise HistoricalCandidateBuildError(
                    f"candidate file identity mismatch: {name}"
                )
    except Exception:
        for path in temporaries.values():
            if path.exists():
                path.unlink()
        for path in created_finals:
            if path.exists():
                path.unlink()
        if not existed_before and destination.exists() and not any(destination.iterdir()):
            destination.rmdir()
        raise

    final_identities = {
        name: _file_identity(path, schema=_schema_for_filename(name))
        for name, path in finals.items()
    }
    return {
        "output_dir": destination,
        "files": finals,
        "manifest": manifest,
        "quality_report": quality,
        "output_identities": final_identities,
        "resolved_records": resolved,
    }


def _sample_report(
    keys: tuple[BusinessKey, ...],
    resolved: tuple[DcePricePoint, ...],
    config: SoybeanImportProfitConfig,
    cnf: HistoricalCnfResult,
    dce: HistoricalDceResult,
) -> list[dict[str, Any]]:
    cnf_by_key = {record.key: record for record in cnf.records}
    resolved_by_key = {record.key: record for record in resolved}
    continuous_keys = {record.key for record in dce.records}
    result = []
    for key in keys:
        mapped = map_soybean_contracts(
            config, key.shipment_year, key.shipment_month
        )
        cnf_record = cnf_by_key.get(
            (
                key.business_date,
                key.commodity,
                key.origin,
                key.shipment_year,
                key.shipment_month,
            )
        )
        legs = []
        for instrument, contract in (
            ("soymeal", mapped.soymeal),
            ("soyoil", mapped.soyoil),
        ):
            point = resolved_by_key.get((key.business_date, contract.code))
            legs.append(
                {
                    "instrument": instrument,
                    "delivery_month": contract.contract_month,
                    "contract_code": contract.code,
                    "continuous_price_exists": (
                        key.business_date,
                        instrument,
                        contract.contract_month,
                    )
                    in continuous_keys,
                    "resolved": point is not None,
                    "price_cny_per_tonne": (
                        point.price_cny_per_tonne if point is not None else None
                    ),
                }
            )
        result.append(
            {
                "business_date": key.business_date.isoformat(),
                "origin": key.origin,
                "shipment_period": key.shipment_period,
                "cnf_key_exists": cnf_record is not None,
                "cnf_value_exists": (
                    cnf_record is not None
                    and cnf_record.cnf_cents_per_bushel is not None
                ),
                "soymeal_contract": mapped.soymeal.code,
                "soyoil_contract": mapped.soyoil.code,
                "dce_legs": legs,
            }
        )
    return result


def _build_quality_report(
    cnf: HistoricalCnfResult,
    dce: HistoricalDceResult,
    sample_report: list[dict[str, Any]],
) -> dict[str, Any]:
    warnings = [
        {"source": "cnf", **item} for item in cnf.quality_report["warnings"]
    ]
    warnings.extend(
        {"source": "dce", **item} for item in dce.quality_report["warnings"]
    )
    missing_sample_legs = sum(
        not leg["resolved"]
        for sample in sample_report
        for leg in sample["dce_legs"]
    )
    if missing_sample_legs:
        warnings.append(
            {"source": "sample", "code": "sample_dce_price_missing", "count": missing_sample_legs}
        )
    status = "passed_with_warnings" if warnings else "passed"
    return {
        "candidate_status": status,
        "fatal_issues": [],
        "warnings": warnings[:50],
        "cnf": cnf.quality_report,
        "dce": dce.quality_report,
        "sample_key_results": sample_report,
    }


def _build_manifest(
    cnf: HistoricalCnfResult,
    dce: HistoricalDceResult,
    sample_keys: tuple[BusinessKey, ...],
    sample_report: list[dict[str, Any]],
    resolved_count: int,
    generated_at: str,
    output_identities: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "adapter_version": ADAPTER_VERSION,
        "candidate_status": (
            "passed_with_warnings"
            if cnf.quality_report["warnings"]
            or dce.quality_report["warnings"]
            or any(
                not leg["resolved"]
                for sample in sample_report
                for leg in sample["dce_legs"]
            )
            else "passed"
        ),
        "synthetic_input": False,
        "source_excel_filename": cnf.source_identity.filename,
        "source_excel_size": cnf.source_identity.size,
        "source_excel_sha256": cnf.source_identity.sha256,
        "source_sql_filename": dce.source_identity.filename,
        "source_sql_size": dce.source_identity.size,
        "source_sql_sha256": dce.source_identity.sha256,
        "cnf": cnf.manifest_core,
        "historical_dce": dce.manifest_core,
        "sample_keys": [
            {
                "business_date": key.business_date.isoformat(),
                "origin": key.origin,
                "shipment_period": key.shipment_period,
            }
            for key in sample_keys
        ],
        "sample_key_results": sample_report,
        "resolved_sample_record_count": resolved_count,
        "generated_at": generated_at,
        "output_files": output_identities,
        "output_file_names": [
            *output_identities,
            MANIFEST_FILENAME,
        ],
        "historical_cnf_import_conversion": {
            "formal_store_source": "manual_ui",
            "candidate_source": "historical_excel",
            "rule": (
                "future explicit initialization must revalidate the stable key and "
                "value, translate source to the formal store's allowed source, and "
                "retain original provenance in its audited batch manifest"
            ),
        },
    }


def _resolved_as_dicts(
    records: Iterable[DcePricePoint],
) -> list[dict[str, Any]]:
    result = []
    for record in records:
        contract_year = 2000 + int(record.contract_code[1:3])
        contract_month = int(record.contract_code[3:5])
        instrument = {"M": "soymeal", "Y": "soyoil"}[record.contract_code[0]]
        result.append(
            {
                "business_date": record.business_date,
                "instrument": instrument,
                "commodity": "soybean",
                "exchange": "DCE",
                "contract_code": record.contract_code,
                "contract_year": contract_year,
                "contract_month": contract_month,
                "price_cny_per_tonne": record.price_cny_per_tonne,
                "price_type": record.price_type,
                "source": record.source,
                "source_function": record.source_function,
                "source_quote_date": record.source_quote_date,
                "source_quote_time": record.source_quote_time,
                "source_snapshot_sha256": record.source_snapshot_sha256,
                "quality_status": "valid",
                "is_usable": record.is_usable,
            }
        )
    return result


def _validate_destination(destination: Path) -> None:
    resolved = destination.resolve()
    if resolved == REPOSITORY_ROOT.resolve() or resolved.is_relative_to(
        REPOSITORY_ROOT.resolve()
    ):
        raise HistoricalCandidateBuildError("output-dir must be outside the repository")
    if destination.exists() and not destination.is_dir():
        raise HistoricalCandidateBuildError("output-dir exists and is not a directory")
    if destination.exists() and any(destination.iterdir()):
        raise HistoricalCandidateBuildError("output-dir must be empty")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    encoded = (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    with path.open("xb") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())
    if json.loads(path.read_text(encoding="utf-8")) != payload:
        raise HistoricalCandidateBuildError(f"JSON round-trip failed: {path.name}")


def _verify_parquet(
    path: Path,
    schema: pa.Schema,
    expected_rows: int,
    *,
    key_fields: tuple[str, ...],
) -> None:
    table = pq.read_table(path)
    if table.schema != schema:
        raise HistoricalCandidateBuildError(f"Parquet schema mismatch: {path.name}")
    if table.num_rows != expected_rows:
        raise HistoricalCandidateBuildError(f"Parquet row count mismatch: {path.name}")
    rows = table.to_pylist()
    keys = [tuple(row[field] for field in key_fields) for row in rows]
    if len(keys) != len(set(keys)):
        raise HistoricalCandidateBuildError(f"Parquet duplicate keys: {path.name}")
    if keys != sorted(keys):
        raise HistoricalCandidateBuildError(f"Parquet keys are not sorted: {path.name}")
    frame = pd.read_parquet(path)
    if list(frame.columns) != schema.names or len(frame) != expected_rows:
        raise HistoricalCandidateBuildError(f"pandas readback failed: {path.name}")


def _file_identity(
    path: Path,
    *,
    filename: str | None = None,
    schema: pa.Schema | None = None,
) -> dict[str, Any]:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    result: dict[str, Any] = {
        "filename": filename or path.name,
        "size": path.stat().st_size,
        "sha256": digest.hexdigest().upper(),
    }
    if schema is not None:
        result["schema_fingerprint"] = hashlib.sha256(
            schema.serialize().to_pybytes()
        ).hexdigest().upper()
        result["schema"] = str(schema)
    return result


def _schema_for_filename(filename: str) -> pa.Schema | None:
    return {
        CNF_FILENAME: HISTORICAL_CNF_SCHEMA,
        DCE_FILENAME: HISTORICAL_DCE_CONTINUOUS_SCHEMA,
        RESOLVED_FILENAME: DCE_HISTORICAL_SCHEMA,
    }.get(filename)


def _fsync_file(path: Path) -> None:
    with path.open("r+b") as stream:
        os.fsync(stream.fileno())


def _assert_safe_json(payload: dict[str, Any]) -> None:
    text = json.dumps(payload, ensure_ascii=False)
    if re.search(r"(?i)(?:[A-Z]:[\\/]|/home/|/Users/)", text):
        raise HistoricalCandidateBuildError("candidate JSON contains an absolute path")
    forbidden = ("Source Server", "Source Host", "username", "password")
    if any(marker in text for marker in forbidden):
        raise HistoricalCandidateBuildError(
            "candidate JSON contains forbidden connection identity"
        )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build isolated historical CNF and DCE input candidates."
    )
    parser.add_argument("--excel", required=True, type=Path)
    parser.add_argument("--sql", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument(
        "--sample-key",
        action="append",
        default=[],
        help="business_date,origin,shipment_period; may be repeated",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    result = build_historical_inputs_candidate(
        args.excel,
        args.sql,
        args.config,
        args.output_dir,
        sample_keys=args.sample_key,
    )
    print(
        json.dumps(
            {
                "candidate_status": result["manifest"]["candidate_status"],
                "output_dir": str(result["output_dir"]),
                "files": sorted(result["files"]),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
