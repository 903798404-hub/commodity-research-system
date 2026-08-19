"""Approved Lutou soil-moisture acquisition for current Weather consumers."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from .live import LutouClient, LutouPlanProof, LutouQuery


LUTOU_WEATHER_SCHEMA = "天气2.0"
MAPPING_VERSION = "lutou-soil-moisture-0-100cm/1"


class SoilMoistureLiveError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SoilMoistureSeries:
    dataset_id: str
    series_id: str
    provider_dataset_id: str
    provider_series_id: str
    source_table: str
    date_column: str
    source_column: str
    earliest_date: date


@dataclass(frozen=True, slots=True)
class SoilMoistureRecord:
    contract: SoilMoistureSeries
    business_date: date
    raw_value_text: str
    source_value: Decimal | None
    value_percent: Decimal | None
    is_numeric: bool
    extracted_at: datetime
    query_sha256: str
    source_row_sha256: str


@dataclass(frozen=True, slots=True)
class SoilMoistureExtraction:
    records: tuple[SoilMoistureRecord, ...]
    series: tuple[SoilMoistureSeries, ...]
    plans: tuple[LutouPlanProof, ...]
    queries: tuple[LutouQuery, ...]
    table_row_counts: Mapping[str, int]


def load_soil_moisture_series(catalog_path: str | Path) -> tuple[SoilMoistureSeries, ...]:
    payload = json.loads(Path(catalog_path).read_text(encoding="utf-8"))
    output: list[SoilMoistureSeries] = []
    for dataset in payload["datasets"]:
        table = str(dataset.get("source_native_name", ""))
        if (
            dataset.get("origin_system") != "lutou"
            or dataset.get("asset_class") != "soil_moisture"
            or table.endswith("_raw")
        ):
            continue
        source_columns = dataset.get("source_columns")
        if not isinstance(source_columns, list) or not source_columns:
            raise SoilMoistureLiveError("soil-moisture date identity is missing")
        date_column = str(source_columns[0]["name"])
        for item in dataset["provider_series_candidates"]:
            provider_series_id = str(item["provider_series_id_candidate"])
            suffix = hashlib.sha256(provider_series_id.encode("utf-8")).hexdigest()[:16]
            output.append(
                SoilMoistureSeries(
                    dataset_id=str(dataset["dataset_id_candidate"]),
                    series_id=f"weather.soil_moisture.0_100cm.native-{suffix}",
                    provider_dataset_id=str(dataset["provider_dataset_id_candidate"]),
                    provider_series_id=provider_series_id,
                    source_table=table,
                    date_column=date_column,
                    source_column=str(item["source_column"]),
                    earliest_date=date.fromisoformat(str(dataset["date_min"])),
                )
            )
    if len(output) != 94:
        raise SoilMoistureLiveError("approved soil-moisture scope must contain 94 Series")
    identities = [(item.provider_series_id, item.series_id) for item in output]
    if len(identities) != len(set(identities)):
        raise SoilMoistureLiveError("soil-moisture Series identities are duplicated")
    return tuple(sorted(output, key=lambda item: item.provider_series_id))


def extract_soil_moisture_live(
    client: LutouClient,
    series: tuple[SoilMoistureSeries, ...],
    *,
    start: date,
    end: date,
) -> SoilMoistureExtraction:
    by_table: dict[str, list[SoilMoistureSeries]] = defaultdict(list)
    for item in series:
        by_table[item.source_table].append(item)
    records: list[SoilMoistureRecord] = []
    plans: list[LutouPlanProof] = []
    queries: list[LutouQuery] = []
    counts: dict[str, int] = {}
    for table in sorted(by_table):
        contracts = sorted(by_table[table], key=lambda item: item.source_column)
        date_columns = {item.date_column for item in contracts}
        if len(date_columns) != 1:
            raise SoilMoistureLiveError("soil-moisture table has inconsistent date identity")
        query = LutouQuery(
            schema=LUTOU_WEATHER_SCHEMA,
            table=table,
            date_column=next(iter(date_columns)),
            value_columns=tuple(item.source_column for item in contracts),
            version=MAPPING_VERSION,
            max_plan_rows=20_000,
        )
        client.inspect_query(query)
        plan, batches = client.plan_stream(query, start, end)
        plans.append(plan)
        queries.append(query)
        row_count = 0
        for batch in batches:
            row_count += len(batch.rows)
            for row in batch.rows:
                business_date = _date(row.get(query.date_column))
                for contract in contracts:
                    raw_value = row.get(contract.source_column)
                    if raw_value is None or (
                        isinstance(raw_value, str) and not raw_value.strip()
                    ):
                        continue
                    raw_value_text = str(raw_value)
                    source_value = _decimal(raw_value)
                    records.append(
                        SoilMoistureRecord(
                            contract=contract,
                            business_date=business_date,
                            raw_value_text=raw_value_text,
                            source_value=source_value,
                            value_percent=(
                                source_value * Decimal("100")
                                if source_value is not None
                                else None
                            ),
                            is_numeric=source_value is not None,
                            extracted_at=batch.extracted_at,
                            query_sha256=query.sha256,
                            source_row_sha256=_row_hash(
                                contract, business_date, raw_value_text
                            ),
                        )
                    )
        counts[table] = row_count
    observed = {item.contract.series_id for item in records}
    if observed != {item.series_id for item in series}:
        raise SoilMoistureLiveError("live soil-moisture coverage is incomplete")
    return SoilMoistureExtraction(
        tuple(records),
        series,
        tuple(plans),
        tuple(queries),
        MappingProxyType(dict(sorted(counts.items()))),
    )


def _date(value: object) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError as exc:
        raise SoilMoistureLiveError("soil-moisture date is invalid") from exc


def _decimal(value: object) -> Decimal | None:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not result.is_finite():
        return None
    return result


def _row_hash(contract: SoilMoistureSeries, day: date, raw_value_text: str) -> str:
    payload = json.dumps(
        {
            "provider_series_id": contract.provider_series_id,
            "business_date": day.isoformat(),
            "raw_value_text": raw_value_text,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
