"""Controlled Lutou Weather mapping for the current formal consumers."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import yaml

from agri_research_agent.weather.crop_weather import load_weather_config

from .live import LutouClient, LutouQuery

LUTOU_WEATHER_SCHEMA = "天气2.0"
MAPPING_VERSION = "lutou-public-weather-current/1"
EXPECTED_NON_SOIL_SERIES = 686


class WeatherLiveError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class MetricContract:
    metric: str
    source_unit: str
    canonical_unit: str
    interval: str
    aggregation: str
    transformation: str
    metadata_source_type: str


@dataclass(frozen=True, slots=True)
class WeatherSeries:
    series_id: str
    provider_dataset_id: str
    provider_series_id: str
    crop: str
    country: str
    region: str
    region_label: str
    region_type: str
    metric: str
    data_family: str
    model: str
    source_table: str
    source_column: str
    date_column: str
    source_unit: str
    unit: str
    interval: str
    aggregation: str
    transformation_id: str


@dataclass(frozen=True, slots=True)
class WeatherTable:
    source_table: str
    date_column: str
    data_family: str
    model: str
    series: tuple[WeatherSeries, ...]
    query: LutouQuery
    min_date: date
    max_date: date
    source_row_count: int | None


@dataclass(frozen=True, slots=True)
class WeatherSourceCatalog:
    schema_version: str
    source_schema: str
    observation_lookback_days: int
    forecast_horizon_days: int
    tables: tuple[WeatherTable, ...]
    series: tuple[WeatherSeries, ...]
    schema_inventory: tuple[Mapping[str, object], ...]
    audited_inventory_table_count: int
    inventory_audited_at: str
    consumer_soil_table_count: int
    relation_audit_performed: bool
    derived_contracts: tuple[Mapping[str, object], ...]
    canada_region_policy: Mapping[str, object]

    def safe_manifest_fields(self) -> dict[str, object]:
        table_rows = []
        for item in self.tables:
            table_rows.append(
                {
                    "source_locator": (
                        f"database:lutou/schema:{self.source_schema}/relation:"
                        f"{item.source_table}"
                    ),
                    "source_table": item.source_table,
                    "date_column": item.date_column,
                    "data_family": item.data_family,
                    "model": item.model,
                    "min_date": item.min_date.isoformat(),
                    "max_date": item.max_date.isoformat(),
                    "source_row_count": item.source_row_count,
                    "series_count": len(item.series),
                    "query": item.query.safe_manifest_fields(),
                    "bounds_status": "SEALED_AUDIT_EVIDENCE",
                }
            )
        inventory_scan_performed = bool(self.schema_inventory)
        inventory_table_count = (
            len(self.schema_inventory)
            if inventory_scan_performed
            else self.audited_inventory_table_count
        )
        return {
            "schema_version": self.schema_version,
            "source_schema": self.source_schema,
            "inventory_mode": (
                "LIVE_SCHEMA_AUDIT"
                if inventory_scan_performed
                else "SEALED_AUDIT_EVIDENCE"
            ),
            "inventory_scan_performed": inventory_scan_performed,
            "relation_validation_mode": (
                "LIVE_RELATION_AUDIT"
                if self.relation_audit_performed
                else "SEALED_MAPPING_EVIDENCE"
            ),
            "inventory_audited_at": self.inventory_audited_at,
            "inventory_table_count": inventory_table_count,
            "consumer_table_count": len(self.tables)
            + self.consumer_soil_table_count,
            "out_of_scope_table_count": inventory_table_count
            - len(self.tables)
            - self.consumer_soil_table_count,
            "consumer_non_soil_series_count": len(self.series),
            "mapped_non_soil_series_count": len(self.series),
            "unresolved_non_soil_series_count": 0,
            "derived_contracts": [dict(item) for item in self.derived_contracts],
            "canada_region_policy": dict(self.canada_region_policy),
            "tables": table_rows,
        }


@dataclass(frozen=True, slots=True)
class _TableSpec:
    metric: str
    data_family: str
    model: str
    column_suffix: str = ""


def load_weather_source_catalog(
    client: LutouClient,
    policy_path: str | Path,
    *,
    audit_inventory: bool = False,
    audit_relations: bool = False,
) -> WeatherSourceCatalog:
    path = Path(policy_path)
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != "lutou-public-weather-source-policy/1":
        raise WeatherLiveError("Weather source policy schema is invalid")
    if payload.get("source_schema") != LUTOU_WEATHER_SCHEMA:
        raise WeatherLiveError("Weather source schema is invalid")
    metric_contracts = _metric_contracts(payload)
    bounds_audit = payload.get("source_bounds_audit")
    if not isinstance(bounds_audit, dict):
        raise WeatherLiveError("Weather source bounds audit is missing")
    if bounds_audit.get("status") != "LIVE_DB_VERIFIED":
        raise WeatherLiveError("Weather source bounds audit is not verified")
    historical_starts = bounds_audit.get("historical_start_by_table_prefix")
    if not isinstance(historical_starts, dict):
        raise WeatherLiveError("Weather historical source bounds are missing")
    observation_max = date.fromisoformat(str(bounds_audit["observation_max_date"]))
    forecast_min = date.fromisoformat(
        str(bounds_audit["forecast_valid_date_min"])
    )
    forecast_max = date.fromisoformat(
        str(bounds_audit["forecast_valid_date_max"])
    )
    forecast_horizon_days = int(payload["forecast_horizon_days"])
    if forecast_horizon_days <= 0:
        raise WeatherLiveError("Weather forecast horizon is invalid")
    inventory_audit = payload.get("source_inventory_audit")
    if not isinstance(inventory_audit, dict):
        raise WeatherLiveError("Weather source inventory audit is missing")
    if inventory_audit.get("status") != "LIVE_DB_VERIFIED":
        raise WeatherLiveError("Weather source inventory audit is not verified")
    audited_inventory_table_count = int(inventory_audit["table_count"])
    consumer_soil_table_count = int(
        inventory_audit["consumer_soil_table_count"]
    )
    if audited_inventory_table_count < 90 + consumer_soil_table_count:
        raise WeatherLiveError("Weather source inventory audit count is invalid")
    config_root = path.parent
    inventory_rows = (
        client.inspect_schema_inventory(LUTOU_WEATHER_SCHEMA)
        if audit_inventory
        else ()
    )
    inventory_by_table = {str(item["TABLE_NAME"]): item for item in inventory_rows}
    tables: list[WeatherTable] = []
    all_series: list[WeatherSeries] = []
    seen_tables: set[str] = set()
    for route in payload.get("routes", []):
        config_path = config_root / str(route["config"])
        config = load_weather_config(config_path)
        prefix = str(
            route.get("source_table_prefix") or config.get("source_table_prefix") or ""
        )
        if not prefix:
            raise WeatherLiveError("Weather route source prefix is missing")
        for table_name in _table_specs(
            prefix, minimum_temperature=bool(route["minimum_temperature"])
        ):
            if table_name in seen_tables:
                # India crop routes intentionally share physical tables but use
                # different explicit source columns. They are resolved together below.
                continue
            seen_tables.add(table_name)
            route_users = _route_users(payload, config_root, table_name)
            actual_columns = None
            if audit_relations:
                relation = client.inspect_relation(LUTOU_WEATHER_SCHEMA, table_name)
                actual_columns = [str(item["COLUMN_NAME"]) for item in relation]
            table_series: list[WeatherSeries] = []
            table_family: str | None = None
            table_model: str | None = None
            date_column: str | None = None
            for user_route, user_config, user_prefix, user_spec in route_users:
                expected, region_columns = _expected_columns(user_config, user_spec)
                actual = (
                    _resolve_actual_columns(expected, actual_columns, user_prefix)
                    if actual_columns is not None
                    else _sealed_actual_columns(expected, user_prefix, user_route)
                )
                if date_column is None:
                    date_column = actual[0]
                elif date_column != actual[0]:
                    raise WeatherLiveError(
                        "Weather table date identity is inconsistent"
                    )
                if table_family is None:
                    table_family = user_spec.data_family
                    table_model = user_spec.model
                elif (table_family, table_model) != (
                    user_spec.data_family,
                    user_spec.model,
                ):
                    raise WeatherLiveError(
                        "Weather table semantic family is inconsistent"
                    )
                display = {
                    str(item["key"]): item
                    for item in user_config["regions"]
                    if isinstance(item, dict)
                }
                for region, expected_column, actual_column in zip(
                    region_columns,
                    expected[1 : 1 + len(region_columns)],
                    actual[1 : 1 + len(region_columns)],
                    strict=True,
                ):
                    region_key = str(region["key"])
                    if region_key not in display:
                        continue
                    metric = metric_contracts[user_spec.metric]
                    series = _series_contract(
                        config=user_config,
                        region=display[region_key],
                        spec=user_spec,
                        metric=metric,
                        table=table_name,
                        column=actual_column,
                        date_column=actual[0],
                    )
                    table_series.append(series)
            if (
                not table_series
                or date_column is None
                or table_family is None
                or table_model is None
            ):
                raise WeatherLiveError("Weather consumer table mapping is empty")
            unique = {
                (item.series_id, item.provider_series_id) for item in table_series
            }
            if len(unique) != len(table_series):
                raise WeatherLiveError(
                    "Weather table mapping contains duplicate identity"
                )
            query = LutouQuery(
                schema=LUTOU_WEATHER_SCHEMA,
                table=table_name,
                date_column=date_column,
                value_columns=tuple(
                    sorted({item.source_column for item in table_series})
                ),
                version=MAPPING_VERSION,
                max_plan_rows=25_000,
            )
            if table_family == "observation":
                if prefix not in historical_starts:
                    raise WeatherLiveError(
                        "Weather historical source start is not sealed"
                    )
                minimum = date.fromisoformat(str(historical_starts[prefix]))
                maximum = observation_max
            else:
                minimum, maximum = forecast_min, forecast_max
            source_rows = inventory_by_table.get(table_name, {}).get("TABLE_ROWS")
            tables.append(
                WeatherTable(
                    source_table=table_name,
                    date_column=date_column,
                    data_family=table_family,
                    model=table_model,
                    series=tuple(sorted(table_series, key=lambda item: item.series_id)),
                    query=query,
                    min_date=minimum,
                    max_date=maximum,
                    source_row_count=int(source_rows)
                    if source_rows is not None
                    else None,
                )
            )
            all_series.extend(table_series)
    inventory = inventory_rows
    if len(tables) != 90:
        raise WeatherLiveError("approved non-soil Weather scope must contain 90 tables")
    if len(all_series) != EXPECTED_NON_SOIL_SERIES:
        raise WeatherLiveError(
            "approved non-soil Weather scope must contain "
            f"{EXPECTED_NON_SOIL_SERIES} Series"
        )
    identities = [item.series_id for item in all_series]
    if len(identities) != len(set(identities)):
        raise WeatherLiveError("Weather Series identities are duplicated")
    return WeatherSourceCatalog(
        schema_version=MAPPING_VERSION,
        source_schema=LUTOU_WEATHER_SCHEMA,
        observation_lookback_days=int(payload["observation_lookback_days"]),
        forecast_horizon_days=forecast_horizon_days,
        tables=tuple(sorted(tables, key=lambda item: item.source_table)),
        series=tuple(sorted(all_series, key=lambda item: item.series_id)),
        schema_inventory=inventory,
        audited_inventory_table_count=audited_inventory_table_count,
        inventory_audited_at=str(inventory_audit["audited_at"]),
        consumer_soil_table_count=consumer_soil_table_count,
        relation_audit_performed=audit_relations,
        derived_contracts=tuple(payload.get("derived_contracts", [])),
        canada_region_policy=dict(payload.get("canada_region_policy", {})),
    )


def _metric_contracts(payload: Mapping[str, object]) -> dict[str, MetricContract]:
    raw = payload.get("metric_contracts")
    if not isinstance(raw, dict):
        raise WeatherLiveError("Weather metric contracts are missing")
    output = {}
    for metric, value in raw.items():
        if not isinstance(value, dict):
            raise WeatherLiveError("Weather metric contract is invalid")
        output[str(metric)] = MetricContract(
            metric=str(metric),
            source_unit=str(value["source_unit"]),
            canonical_unit=str(value["canonical_unit"]),
            interval=str(value["interval"]),
            aggregation=str(value["aggregation"]),
            transformation=str(value["transformation"]),
            metadata_source_type=str(value["metadata_source_type"]),
        )
    if set(output) != {"precipitation", "temperature_max", "temperature_min"}:
        raise WeatherLiveError("Weather metric contract coverage is invalid")
    return output


def _table_specs(prefix: str, *, minimum_temperature: bool) -> dict[str, _TableSpec]:
    specs = {
        f"{prefix}_降雨": _TableSpec("precipitation", "observation", "OBSERVED"),
        f"{prefix}_降雨_预测_ec": _TableSpec(
            "precipitation", "forecast", "ECMWF", "_precip_ec"
        ),
        f"{prefix}_降雨_预测_gfs": _TableSpec(
            "precipitation", "forecast", "GFS", "_precip_gfs"
        ),
        f"{prefix}_最高气温": _TableSpec("temperature_max", "observation", "OBSERVED"),
        f"{prefix}_最高气温_预测_ec": _TableSpec(
            "temperature_max", "forecast", "ECMWF", "_hightemp_ec"
        ),
        f"{prefix}_最高气温_预测_gfs": _TableSpec(
            "temperature_max", "forecast", "GFS", "_hightemp_gfs"
        ),
    }
    if minimum_temperature:
        specs.update(
            {
                f"{prefix}_最低气温": _TableSpec(
                    "temperature_min", "observation", "OBSERVED"
                ),
                f"{prefix}_最低气温_预测_ec": _TableSpec(
                    "temperature_min", "forecast", "ECMWF", "_lowtemp_ec"
                ),
                f"{prefix}_最低气温_预测_gfs": _TableSpec(
                    "temperature_min", "forecast", "GFS", "_lowtemp_gfs"
                ),
            }
        )
    return specs


def _route_users(
    payload: Mapping[str, object], config_root: Path, table_name: str
) -> list[tuple[Mapping[str, object], dict[str, object], str, _TableSpec]]:
    output = []
    routes = payload.get("routes")
    if not isinstance(routes, list):
        raise WeatherLiveError("Weather routes are invalid")
    for route in routes:
        if not isinstance(route, dict):
            raise WeatherLiveError("Weather route is invalid")
        config = load_weather_config(config_root / str(route["config"]))
        prefix = str(
            route.get("source_table_prefix") or config.get("source_table_prefix") or ""
        )
        specs = _table_specs(
            prefix, minimum_temperature=bool(route["minimum_temperature"])
        )
        if table_name in specs:
            output.append((route, config, prefix, specs[table_name]))
    return output


def _expected_columns(
    config: Mapping[str, object], spec: _TableSpec
) -> tuple[list[str], list[Mapping[str, object]]]:
    raw_regions = config.get("source_regions", config["regions"])
    if not isinstance(raw_regions, list) or not all(
        isinstance(item, dict) for item in raw_regions
    ):
        raise WeatherLiveError("Weather source regions are invalid")
    missing = config.get("allowed_missing_forecast_regions", {})
    omitted: set[str] = set()
    if spec.data_family == "forecast":
        if not isinstance(missing, dict):
            raise WeatherLiveError("Weather missing-forecast policy is invalid")
        by_metric = missing.get(spec.metric, {})
        if not isinstance(by_metric, dict):
            raise WeatherLiveError("Weather missing-forecast metric policy is invalid")
        omitted = {str(item) for item in by_metric.get(spec.model, [])}
    regions = [item for item in raw_regions if str(item.get("key", "")) not in omitted]
    columns = ["日期"]
    prefix = str(config.get("source_table_prefix", ""))
    for region in regions:
        column = str(
            region.get(f"source_column_{spec.metric}", region.get("source_column", ""))
        )
        if not column:
            column = f"{prefix}_{region['display_name']}"
        columns.append(f"{column}{spec.column_suffix}")
    extras_by_metric = config.get("source_extra_columns_by_metric", {})
    extras = []
    if spec.data_family == "observation" and isinstance(extras_by_metric, dict):
        extras = [str(item) for item in extras_by_metric.get(spec.metric, [])]
    columns.extend(extras)
    return columns, regions


def _resolve_actual_columns(
    expected: list[str], actual: list[str], prefix: str
) -> list[str]:
    if actual == expected:
        return actual
    expanded = [
        expected[0],
        *(f"{prefix}_{column.lstrip('_')}" for column in expected[1:]),
    ]
    if actual == expanded:
        return actual
    raise WeatherLiveError("Weather live columns do not match the controlled mapping")


def _sealed_actual_columns(
    expected: list[str], prefix: str, route: Mapping[str, object]
) -> list[str]:
    style = str(route.get("sealed_column_style", "exact"))
    if style == "exact":
        return expected
    if style == "table_prefix_expanded":
        return [
            expected[0],
            *(f"{prefix}_{column.lstrip('_')}" for column in expected[1:]),
        ]
    raise WeatherLiveError("Weather sealed column style is invalid")


def _series_contract(
    *,
    config: Mapping[str, object],
    region: Mapping[str, object],
    spec: _TableSpec,
    metric: MetricContract,
    table: str,
    column: str,
    date_column: str,
) -> WeatherSeries:
    crop = str(config["crop"])
    country = str(config["country"])
    region_id = str(region["key"])
    model_slug = spec.model.lower()
    series_id = (
        f"weather.{spec.metric}.{crop}.{country.lower()}.{region_id}."
        f"{spec.data_family}.{model_slug}"
    )
    provider_series_id = f"lutou:{LUTOU_WEATHER_SCHEMA}:{table}:{column}"
    provider_dataset_id = f"lutou:{LUTOU_WEATHER_SCHEMA}:{table}"
    region_type = (
        "source_provided_weighted_region"
        if country == "CAN" and region_id.endswith("_weighted")
        else "consumer_display_region"
    )
    return WeatherSeries(
        series_id=series_id,
        provider_dataset_id=provider_dataset_id,
        provider_series_id=provider_series_id,
        crop=crop,
        country=country,
        region=region_id,
        region_label=str(region.get("display_name", region_id)),
        region_type=region_type,
        metric=spec.metric,
        data_family=spec.data_family,
        model=spec.model,
        source_table=table,
        source_column=column,
        date_column=date_column,
        source_unit=metric.source_unit,
        unit=metric.canonical_unit,
        interval=metric.interval,
        aggregation=metric.aggregation,
        transformation_id=f"weather.{metric.transformation}/1",
    )


def forecast_run_id(table: WeatherTable, rows: tuple[Mapping[str, object], ...]) -> str:
    payload = [{"source_table": table.source_table, "model": table.model}]
    for row in rows:
        payload.append(
            {
                "valid_date": str(row[table.date_column])[:10],
                "values": {
                    column: row.get(column) for column in table.query.value_columns
                },
            }
        )
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode(
            "utf-8"
        )
    ).hexdigest()
    return f"source-content-sha256:{digest}"


def build_soil_consumer_bindings(
    policy_path: str | Path,
    soil_series: Iterable[object],
) -> tuple[dict[str, object], ...]:
    """Bind preserved native Soil identities to explicit consumer contexts."""
    path = Path(policy_path)
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    native = {
        (str(item.source_table), str(item.source_column)): item for item in soil_series
    }
    bindings = []
    covered: set[str] = set()
    for route in payload["routes"]:
        config = load_weather_config(path.parent / str(route["config"]))
        prefix = str(
            route.get("source_table_prefix") or config.get("source_table_prefix")
        )
        table = f"{prefix}_土壤墒情"
        display = {str(item["key"]) for item in config["regions"]}
        for region in config.get("source_regions", config["regions"]):
            region_id = str(region["key"])
            column = str(
                region.get(
                    "source_column_soil_moisture",
                    region.get("source_column", ""),
                )
            )
            if not column:
                column = f"{prefix}_{region['display_name']}"
            item = native.get((table, column)) or native.get(
                (table, f"{prefix}_{column.lstrip('_')}")
            )
            if item is None:
                raise WeatherLiveError(
                    "Soil consumer binding is missing a native Series"
                )
            covered.add(str(item.series_id))
            bindings.append(
                {
                    "series_id": str(item.series_id),
                    "provider_series_id": str(item.provider_series_id),
                    "crop": str(config["crop"]),
                    "country": str(config["country"]),
                    "region": region_id,
                    "region_label": str(region.get("display_name", region_id)),
                    "consumer_status": (
                        "MAPPED_DISPLAY"
                        if region_id in display
                        else "SOURCE_ONLY_LINEAGE"
                    ),
                    "metric": "soil_moisture",
                    "soil_depth": "0-100cm",
                    "unit": "%",
                }
            )
    expected = {str(item.series_id) for item in soil_series}
    if covered != expected:
        raise WeatherLiveError("Soil consumer binding coverage is incomplete")
    return tuple(
        sorted(
            bindings,
            key=lambda item: (
                str(item["series_id"]),
                str(item["crop"]),
                str(item["region"]),
            ),
        )
    )


__all__ = [
    "EXPECTED_NON_SOIL_SERIES",
    "LUTOU_WEATHER_SCHEMA",
    "MAPPING_VERSION",
    "MetricContract",
    "WeatherLiveError",
    "WeatherSeries",
    "WeatherSourceCatalog",
    "WeatherTable",
    "build_soil_consumer_bindings",
    "forecast_run_id",
    "load_weather_source_catalog",
]
