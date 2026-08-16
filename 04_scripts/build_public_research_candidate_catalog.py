"""Build the Gate A Public Research Data candidate asset catalog.

This is an offline audit utility. It reads previously generated, secret-free audit
inventories and never connects to a database or rewrites source snapshots.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any


CATALOG_SCHEMA_VERSION = "0.1-candidate"
WEATHER_EXISTING_ACL_TABLES = {
    "美国_降雨",
    "美国_降雨_预测_ec",
    "美国_降雨_预测_gfs",
    "美国_最高气温",
    "美国_最高气温_预测_ec",
    "美国_最高气温_预测_gfs",
    "美国_最低气温",
    "美国_最低气温_预测_ec",
    "美国_最低气温_预测_gfs",
    "美国_土壤墒情",
}
TANKAN_OPERATIONAL_OR_UNRESOLVED_TABLES = {
    "domestic_contract_rule",
    "futures_live",
    "futures_warehouse",
    "ric_mapping",
    "scheduler_job",
    "sync_log",
    "sync_queue",
}


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _ascii_slug(value: str) -> str:
    if value.isascii():
        normalized = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
        return normalized
    return f"native-{_sha256_text(value)[:12]}"


def _schema_fingerprint(columns: list[dict[str, Any]]) -> str:
    schema = [{"name": item["name"], "type": item.get("type")} for item in columns]
    payload = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
    return f"sha256:{_sha256_text(payload)}"


def _tankan_contract(schema: str, table: str, category: str) -> str:
    if table in TANKAN_OPERATIONAL_OR_UNRESOLVED_TABLES or table == "feedback":
        return "unclassified"
    if table in {"exchange_rate", "exchange_rate_live"}:
        return "fx_quote_or_curve_point_candidate"
    if table in {"foreign_futures_live", "foreign_futures_price", "foreign_futures_price_raw"}:
        return "market_quote_candidate"
    if table == "basis_fob":
        return "basis_observation_candidate"
    if table == "foreign_position":
        return "fundamental_or_typed_observation_candidate"
    if schema in {"balance", "trade", "pig", "public"}:
        return "fundamental_or_typed_observation_candidate"
    if category == "UNCLASSIFIED":
        return "unclassified"
    return "domain_specific_candidate"


def _tankan_domain(schema: str, table: str, category: str) -> str:
    if table in TANKAN_OPERATIONAL_OR_UNRESOLVED_TABLES or table == "feedback":
        return "unclassified"
    if table.startswith("exchange_rate"):
        return "fx"
    if table == "basis_fob":
        return "basis"
    if table == "foreign_position":
        return "position"
    if "futures" in table:
        return "external_futures"
    if table.endswith("_param") or table == "india_import_profit":
        return "import_cost_or_profit"
    if schema == "balance":
        return "supply_demand_balance"
    if schema == "trade":
        return "trade"
    if schema == "pig":
        return "livestock"
    if schema == "public" and "crop" in table:
        return "crop_progress_or_area"
    if table == "mpob_balance":
        return "palm_balance"
    if category == "UNCLASSIFIED":
        return "unclassified"
    return "market_or_time_series"


def _potential_consumers(domain: str) -> list[str]:
    if domain in {"external_futures", "external_or_domestic_futures", "fx"}:
        return ["international_spread", "import_profit"]
    if domain in {"basis", "freight_cnf_or_basis", "import_cost_or_profit"}:
        return ["international_spread", "import_profit"]
    if domain == "biodiesel_and_energy":
        return ["international_spread", "biodiesel_research"]
    if domain == "international_physical_or_oilseed_price":
        return ["international_spread", "import_profit"]
    if domain == "weather":
        return ["weather"]
    if domain in {
        "crop_progress_or_area", "livestock", "palm_balance", "position",
        "supply_demand_balance", "trade",
    }:
        return ["research_workbench_or_future_typed_consumer"]
    return []


def _is_measurement_column(name: str, data_type: str) -> bool:
    lowered = name.casefold()
    excluded = {
        "id", "year", "month", "day", "date", "trade_date", "report_date",
        "business_date", "updated_at", "created_at", "imported_at", "update_time",
    }
    if lowered in excluded or lowered.endswith("_id"):
        return False
    return data_type.casefold() in {
        "smallint", "integer", "bigint", "numeric", "decimal", "real",
        "double precision", "float", "float4", "float8",
    }


def _build_tankan(profile: dict[str, Any]) -> list[dict[str, Any]]:
    column_map: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for item in profile["columns"]:
        key = (item["schema_name"], item["relation_name"])
        column_map.setdefault(key, []).append(
            {
                "name": item["column_name"],
                "type": item["data_type"],
                "nullable": not item["not_null"],
                "comment": item.get("comment"),
            }
        )
    classification_map = {
        (item["schema_name"], item["relation_name"]): item
        for item in profile["classifications"]
    }
    high_value_map = {
        (item["schema_name"], item["relation_name"]): item
        for item in profile["high_value_assets"]
    }
    identity_map = {
        (item["schema_name"], item["relation_name"]): item
        for item in profile["identity_candidates"]
    }
    datasets: list[dict[str, Any]] = []
    for relation in profile["relations"]:
        schema = relation["schema_name"]
        table = relation["relation_name"]
        key = (schema, table)
        classification = classification_map[key]
        columns = column_map[key]
        high_value = high_value_map.get(key, {})
        provider_series = []
        for column in columns:
            if _is_measurement_column(column["name"], column["type"]):
                provider_series.append(
                    {
                        "provider_series_id_candidate": f"tankan:{schema}.{table}:{column['name']}",
                        "source_column": column["name"],
                        "series_id_candidate": None,
                        "canonicalization_status": "requires_dimension_and_unit_mapping",
                    }
                )
        date_range = high_value.get("date_range") or {}
        datasets.append(
            {
                "dataset_id_candidate": f"tankan.{schema}.{table}",
                "identity_status": "candidate",
                "origin_system": "tankan",
                "acquisition_channel": "direct_database",
                "source_locator": f"database:quanyong/schema:{schema}/relation:{table}",
                "provider_dataset_id_candidate": f"tankan:{schema}.{table}",
                "source_object_type": relation["relation_type"],
                "source_native_name": f"{schema}.{table}",
                "asset_class": classification["category"],
                "domain": _tankan_domain(schema, table, classification["category"]),
                "canonical_contract_candidate": _tankan_contract(
                    schema, table, classification["category"]
                ),
                "canonicalization_status": "cataloged_only",
                "estimated_row_count": relation["estimated_rows"],
                "size_bytes": relation["total_bytes"],
                "date_min": date_range.get("min"),
                "date_max": date_range.get("max"),
                "frequency": "unverified",
                "update_status": "database_read_only_audit_current_at_capture",
                "unit": "requires_mapping",
                "currency": "requires_mapping",
                "market": "requires_mapping",
                "region": "requires_mapping",
                "basis": table == "basis_fob",
                "tenor": "source_dimension" if table.startswith("exchange_rate") else None,
                "schema_fingerprint": _schema_fingerprint(columns),
                "schema_version": "source-schema-candidate-1",
                "source_columns": columns,
                "provider_series_candidates": provider_series,
                "provider_series_count": len(provider_series),
                "provider_identity_evidence": identity_map.get(key),
                "quality_status": "requires_semantic_mapping",
                "quality_issues": [],
                "potential_consumers": _potential_consumers(
                    _tankan_domain(schema, table, classification["category"])
                ),
            }
        )
    return datasets


def _weather_contract(table: dict[str, Any]) -> str:
    if table["table"] in WEATHER_EXISTING_ACL_TABLES:
        return "existing_weather_contract_via_acl"
    return "weather_source_observation_candidate"


def _build_weather(inventory: dict[str, Any]) -> list[dict[str, Any]]:
    datasets: list[dict[str, Any]] = []
    for table in inventory["tables"]:
        native_name = table["table"]
        slug = _ascii_slug(native_name)
        columns = [
            {"name": item["name"], "type": item["type"], "comment": item.get("comment")}
            for item in table["columns"]
        ]
        provider_series = [
            {
                "provider_series_id_candidate": f"lutou:weather:{native_name}:{name}",
                "source_column": name,
                "series_id_candidate": None,
                "canonicalization_status": "blocked_on_unit_and_geography_mapping",
            }
            for name in table["series_names_candidate"]
        ]
        issues = []
        if table["comment_count"] == 0:
            issues.append("source_has_no_column_comments_or_units")
        if table["raw"] and table["date_min"] == "1940-01-01":
            issues.append("1940_date_axis_is_seasonal_baseline_not_observation_year")
        datasets.append(
            {
                "dataset_id_candidate": f"lutou.weather.{slug}",
                "identity_status": "candidate",
                "origin_system": "lutou",
                "acquisition_channel": "manual_snapshot",
                "source_locator": f"snapshot:01_data/manual/weather/source_dumps/天气2.0.sql#table={native_name}",
                "provider_dataset_id_candidate": f"lutou:weather:{native_name}",
                "source_object_type": "snapshot_table",
                "source_native_name": native_name,
                "asset_class": table["classification"],
                "domain": "weather",
                "canonical_contract_candidate": _weather_contract(table),
                "canonicalization_status": (
                    "existing_acl" if native_name in WEATHER_EXISTING_ACL_TABLES else "cataloged_only"
                ),
                "row_count": table["insert_count"],
                "size_bytes": None,
                "date_min": table["date_min"],
                "date_max": table["date_max"],
                "frequency": "daily_date_axis",
                "update_status": "offline_snapshot_at_capture",
                "unit": "unclassified",
                "currency": None,
                "market": None,
                "region": table["country_prefix"],
                "basis": False,
                "tenor": None,
                "weather_dimensions": {
                    "metric": table["metric"],
                    "model": table["model"],
                    "forecast": table["forecast"],
                    "raw": table["raw"],
                    "national": table["national"],
                    "family": table["family"],
                },
                "schema_fingerprint": _schema_fingerprint(columns),
                "schema_version": "source-schema-candidate-1",
                "source_columns": columns,
                "provider_series_candidates": provider_series,
                "provider_series_count": len(provider_series),
                "quality_status": "requires_semantic_mapping" if issues else "profiled",
                "quality_issues": issues,
                "potential_consumers": _potential_consumers("weather"),
            }
        )
    return datasets


def _oil_domain(name: str) -> str:
    lowered = name.casefold()
    if "汇率" in name:
        return "fx"
    if "生物柴油" in name:
        return "biodiesel_and_energy"
    if "basis" in lowered or "基差" in name:
        return "basis"
    if "cnf" in lowered or "运费" in name or "freight" in lowered:
        return "freight_cnf_or_basis"
    if any(token in lowered for token in ("cbot", "bmd", "ice", "euronext")) or "期货" in name:
        return "external_or_domestic_futures"
    if "持仓" in name:
        return "position"
    return "international_physical_or_oilseed_price"


def _oil_contract(domain: str) -> str:
    if domain == "fx":
        return "fx_quote_or_curve_point_candidate"
    if domain == "basis":
        return "existing_basis_contract_via_acl"
    if domain in {"biodiesel_and_energy", "international_physical_or_oilseed_price", "external_or_domestic_futures"}:
        return "market_quote_candidate_where_semantics_complete"
    if domain == "position":
        return "fundamental_or_typed_observation_candidate"
    return "domain_specific_candidate"


def _looks_numeric_type(value: str) -> bool:
    lowered = value.casefold()
    return any(token in lowered for token in ("int", "decimal", "numeric", "double", "float", "real"))


def _build_oils(inventory: dict[str, Any]) -> list[dict[str, Any]]:
    datasets: list[dict[str, Any]] = []
    for table in inventory["tables"]:
        native_name = table["name"]
        slug = _ascii_slug(native_name)
        columns = [
            {
                "name": item["name"],
                "type": item["type"],
                "null_count": item.get("null_count"),
                "non_null_count": item.get("non_null_count"),
            }
            for item in table["columns"]
        ]
        provider_series = []
        for item in table["columns"]:
            if _looks_numeric_type(item["type"]) and item["name"].casefold() not in {"id", "year", "month"}:
                provider_series.append(
                    {
                        "provider_series_id_candidate": f"lutou:oils:{native_name}:{item['name']}",
                        "source_column": item["name"],
                        "series_id_candidate": None,
                        "canonicalization_status": "requires_dimension_unit_currency_mapping",
                    }
                )
        domain = _oil_domain(native_name)
        issues = []
        if table["arity_mismatches"]:
            issues.append("row_arity_mismatch_detected")
        if table.get("date_invalid_or_unparsed_count", 0):
            issues.append(
                f"invalid_or_unparsed_business_dates:{table['date_invalid_or_unparsed_count']}"
            )
        if table.get("suspicious_sentinel_dates"):
            issues.append("suspicious_date_values_require_review")
        issues.append("wide_table_mixes_dimensions_units_or_price_semantics")
        datasets.append(
            {
                "dataset_id_candidate": f"lutou.oils.{slug}",
                "identity_status": "candidate",
                "origin_system": "lutou",
                "acquisition_channel": "manual_snapshot",
                "source_locator": f"snapshot:01_data/manual/榨利表/油脂油料价格.sql#table={native_name}",
                "provider_dataset_id_candidate": f"lutou:oils:{native_name}",
                "source_object_type": "snapshot_table",
                "source_native_name": native_name,
                "asset_class": "PROFILED_SOURCE_TABLE",
                "domain": domain,
                "canonical_contract_candidate": _oil_contract(domain),
                "canonicalization_status": "existing_acl" if native_name == "basis_price" else "cataloged_only",
                "row_count": table["row_count"],
                "size_bytes": None,
                "date_field_candidate": table.get("date_field_candidate"),
                "date_min": table.get("date_min"),
                "date_max": table.get("date_max"),
                "date_parse_count": table.get("date_parse_count"),
                "date_invalid_or_unparsed_count": table.get("date_invalid_or_unparsed_count"),
                "frequency": table.get("frequency_candidate", "unverified"),
                "update_status": "offline_snapshot_at_capture",
                "unit": "mixed_or_requires_mapping",
                "currency": "mixed_or_requires_mapping",
                "market": "source_columns_require_mapping",
                "region": "source_columns_or_table_name_require_mapping",
                "basis": domain == "basis",
                "tenor": "source_column" if domain == "fx" else None,
                "schema_fingerprint": _schema_fingerprint(columns),
                "schema_version": "source-schema-candidate-1",
                "source_columns": columns,
                "provider_series_candidates": provider_series,
                "provider_series_count": len(provider_series),
                "quality_status": "requires_semantic_mapping",
                "quality_issues": issues,
                "potential_consumers": _potential_consumers(domain),
            }
        )
    return datasets


def _catalog(tankan: dict[str, Any], weather: dict[str, Any], oils: dict[str, Any]) -> dict[str, Any]:
    datasets = _build_tankan(tankan) + _build_weather(weather) + _build_oils(oils)
    domains = Counter(item["domain"] for item in datasets)
    origins = Counter(item["origin_system"] for item in datasets)
    provider_series_count = sum(item["provider_series_count"] for item in datasets)
    return {
        "catalog_schema_version": CATALOG_SCHEMA_VERSION,
        "status": "HUMAN_GATE_A_PENDING",
        "generated_from_audit_timestamp_utc": tankan.get("finished_at_utc"),
        "scope": "Public Research Data Layer candidate inventory; no provider implementation",
        "identity_policy_candidate": {
            "origin_system": "Business source identity; unchanged by acquisition method.",
            "acquisition_channel": "Transport/acquisition method; manual_snapshot may later become direct_database.",
            "dataset_id": "Stable semantic dataset family identity; must not contain snapshot path, batch, or hash.",
            "series_id": "Provider-neutral canonical research concept; assigned only after dimensions, units and date semantics are verified.",
            "provider_dataset_id": "Provider-native dataset mapping key.",
            "provider_series_id": "Optional provider-native table/column/endpoint mapping key; not a canonical identity.",
            "source_series_id": "Existing Tankan mapping identity retained as provider_series_id-compatible evidence; not promoted to canonical series_id.",
            "source_locator": "Auditable physical locator; never part of canonical identity.",
            "provenance": "Capture, schema/mapping version, locator and immutable source evidence; distinct from identity.",
        },
        "series_id_templates_candidate": {
            "market_quote": "market.quote.{instrument}.{price_type}.{session}",
            "fx": "fx.{base_currency}.{quote_currency}.{tenor}.{side_or_fixing}",
            "weather": "weather.{metric}.{geography}.{model_or_observed}.{forecast_role}",
            "fundamental": "fundamental.{subject}.{measure}.{geography}.{frequency}",
            "basis": "basis.{commodity}.{origin}.{destination_or_market}.{contract_tenor}",
        },
        "existing_provider_identity_evidence": {
            "evidence_source_locator": "extraction-source:feat/international-spread/02_configs/tankan_market_source_policy.yaml",
            "migration_rule": "Existing source_series_id values are provider identity evidence and map compatibly to provider_series_id; they are not canonical series_id.",
            "mappings": [
                {
                    "canonical_concept_candidate": "market.quote.CBOT.SOYBEAN",
                    "existing_source_series_ids": ["tankan.ffpr.cbot.soybean.zh", "tankan.ffpr.cbot.soybean.en"],
                },
                {
                    "canonical_concept_candidate": "market.quote.CBOT.SOYBEAN_MEAL",
                    "existing_source_series_ids": ["tankan.ffpr.cbot.soybean_meal.zh", "tankan.ffpr.cbot.soybean_meal.en"],
                },
                {
                    "canonical_concept_candidate": "market.quote.CBOT.SOYBEAN_OIL",
                    "existing_source_series_ids": ["tankan.ffpr.cbot.soybean_oil.zh", "tankan.ffpr.cbot.soybean_oil.en"],
                },
                {
                    "canonical_concept_candidate": "market.quote.CBOT.CORN",
                    "existing_source_series_ids": ["tankan.ffpr.cbot.corn.zh", "tankan.ffpr.cbot.corn.en"],
                },
                {
                    "canonical_concept_candidate": "market.quote.BMD.PALM_OIL",
                    "existing_source_series_ids": ["tankan.ffpr.bmd.palm_oil.zh", "tankan.ffpr.bmd.palm_oil.en"],
                },
                {
                    "canonical_concept_candidate": "market.quote.ICE.CANOLA",
                    "existing_source_series_ids": ["tankan.ffpr.ice.canola.zh", "tankan.ffpr.ice.canola.en"],
                },
                {
                    "canonical_concept_candidate": "market.quote.EURONEXT.RAPESEED",
                    "existing_source_series_ids": ["tankan.ffpr.euronext.rapeseed.zh", "tankan.ffpr.euronext.rapeseed.en"],
                },
            ],
        },
        "audit_sources": [
            {
                "origin_system": "tankan",
                "acquisition_channel": "direct_database",
                "source_locator": "database:quanyong",
                "audit_generated_at_utc": tankan.get("generated_at_utc"),
                "read_only_proof": tankan.get("query_safety_summary"),
                "object_statistics": tankan.get("object_statistics"),
            },
            {
                "origin_system": "lutou",
                "acquisition_channel": "manual_snapshot",
                "source_locator": "snapshot:01_data/manual/weather/source_dumps/天气2.0.sql",
                "source_sha256": weather["source"].get("sha256"),
                "role": "source_asset",
            },
            {
                "origin_system": "lutou",
                "acquisition_channel": "manual_snapshot",
                "source_locator": "snapshot:01_data/manual/榨利表/油脂油料价格.sql",
                "source_sha256": oils["source"].get("sha256"),
                "role": "source_asset",
            },
            {
                "origin_system": "lutou",
                "acquisition_channel": "manual_snapshot",
                "source_locator": "snapshot:01_data/manual/榨利表/大豆-进口榨利.xlsx",
                "source_sha256": oils["excel_inventory"].get("sha256"),
                "role": "consumer_and_historical_definition_evidence_only",
                "catalog_rule": "Do not infer Lutou raw assets from calculated workbook outputs.",
            },
        ],
        "summary": {
            "dataset_count": len(datasets),
            "provider_series_candidate_count": provider_series_count,
            "datasets_by_origin": dict(sorted(origins.items())),
            "datasets_by_domain": dict(sorted(domains.items())),
            "unclassified_dataset_count": sum(
                item["domain"] == "unclassified" or item["canonical_contract_candidate"] == "unclassified"
                for item in datasets
            ),
        },
        "datasets": datasets,
        "canonical_contract_boundaries": {
            "MarketQuote": "Scalar instrument price with verified currency/unit/price type; excludes FX, positions, fundamentals, weather and mixed calculation tables.",
            "FX": "Separate FxQuote/FxCurvePoint candidate; tenor and bid/ask/mid semantics are not MarketQuote.",
            "Weather": "Preserve mature weather semantics; use an ACL at the public acquisition boundary.",
            "Fundamental": "Typed observations for balance, trade, crop, livestock and positions; design only when a real consumer exists.",
            "BasisFreightCnf": "Keep true business semantics; do not coerce all fields into MarketQuote.",
        },
        "known_unstandardized_assets": [
            "Lutou weather units and some geography/model semantics",
            "Lutou wide oil/energy tables with mixed units, currencies and price semantics",
            "Tankan relations classified UNCLASSIFIED by metadata/name evidence",
            "Tankan long-table canonical series dimensions and units",
            "Biodiesel product/grade/market/unit normalization",
            "Freight, CNF and basis semantic separation",
            "FAME/HVO/UCO/POME/PME/SAF assets not proven by current snapshots/audit",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tankan", required=True, type=Path)
    parser.add_argument("--weather", required=True, type=Path)
    parser.add_argument("--oils", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    catalog = _catalog(_load(args.tankan), _load(args.weather), _load(args.oils))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
