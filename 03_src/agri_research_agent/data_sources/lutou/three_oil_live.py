"""Source-preserving live adapter for the sealed Lutou Three-Oil V1 series."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Mapping

from agri_research_agent.research_data.three_oil_v1 import ThreeOilV1Catalog

from .live import LutouBatch, LutouClient, LutouPlanProof, LutouQuery


LUTOU_SCHEMA = "油脂油料价格"
MAPPING_VERSION = "lutou-three-oil-live/2"
OIL_WORLD_OFFICIAL_PRICE_SERIES = frozenset(
    {
        "Soybean oil,Dutch, fob ex-mill",
        "Sunoil, EU, fob N.W.Eur. ports",
        "Rape oil,Dutch, fob ex-mill",
    }
)


class ThreeOilLiveError(RuntimeError):
    """Raised when live source evidence cannot satisfy the sealed mapping."""


@dataclass(frozen=True, slots=True)
class LiveThreeOilRecord:
    dataset_id: str
    series_id: str
    provider_dataset_id: str
    provider_series_id: str
    provider: str
    metadata_source_type: str
    metadata_status: str
    source_quote_unit: str
    source_locator: str
    source_table: str
    source_column: str
    business_date: date
    raw_price: Decimal
    source_currency: str
    source_unit: str
    conversion_id: str | None
    extracted_at: datetime
    query_sha256: str
    source_row_sha256: str

    def source_record(self) -> Mapping[str, object]:
        return MappingProxyType(
            {
                "provider_series_id": self.provider_series_id,
                "business_date": self.business_date,
                "price": self.raw_price,
                "source_row_sha256": self.source_row_sha256,
            }
        )


@dataclass(frozen=True, slots=True)
class LiveThreeOilExtraction:
    records: tuple[LiveThreeOilRecord, ...]
    plans: tuple[LutouPlanProof, ...]
    queries: tuple[LutouQuery, ...]
    table_row_counts: Mapping[str, int]


def extract_three_oil_live(
    client: LutouClient,
    catalog: ThreeOilV1Catalog,
    *,
    start: date,
    end: date,
) -> LiveThreeOilExtraction:
    """Read only the 20 sealed source columns over an explicit date window."""

    if len(catalog.series) != 20:
        raise ThreeOilLiveError("Three-Oil V1 catalog must contain 20 source Series")
    by_table: dict[str, list[object]] = defaultdict(list)
    for contract in catalog.series:
        if contract.origin_system != "lutou":
            raise ThreeOilLiveError("sealed Three-Oil source is not Lutou")
        by_table[contract.source_native_table].append(contract)

    output: list[LiveThreeOilRecord] = []
    plans: list[LutouPlanProof] = []
    queries: list[LutouQuery] = []
    table_counts: dict[str, int] = {}
    for table in sorted(by_table):
        contracts = sorted(
            by_table[table], key=lambda item: item.source_native_series  # type: ignore[attr-defined]
        )
        query = LutouQuery(
            schema=LUTOU_SCHEMA,
            table=table,
            date_column="Date",
            value_columns=tuple(
                item.source_native_series for item in contracts  # type: ignore[attr-defined]
            ),
        )
        client.inspect_query(query)
        plan, batches = client.plan_stream(query, start, end)
        plans.append(plan)
        queries.append(query)
        table_count = 0
        for batch in batches:
            table_count += len(batch.rows)
            output.extend(_adapt_batch(batch, contracts))
        table_counts[table] = table_count

    if not output:
        raise ThreeOilLiveError("live Three-Oil extraction returned no observations")
    known = {item.series_id for item in catalog.series}
    observed = {item.series_id for item in output}
    if observed != known:
        missing = known - observed
        raise ThreeOilLiveError(
            f"live Three-Oil extraction has empty sealed Series: {len(missing)}"
        )
    return LiveThreeOilExtraction(
        records=tuple(output),
        plans=tuple(plans),
        queries=tuple(queries),
        table_row_counts=MappingProxyType(dict(sorted(table_counts.items()))),
    )


def _adapt_batch(
    batch: LutouBatch,
    contracts: list[object],
) -> tuple[LiveThreeOilRecord, ...]:
    output: list[LiveThreeOilRecord] = []
    for row in batch.rows:
        business_date = _date(row.get(batch.query.date_column))
        for contract in contracts:
            source_column = contract.source_native_series  # type: ignore[attr-defined]
            value = row.get(source_column)
            if value is None:
                continue
            raw_price = _price(value)
            provider_series_id = contract.provider_series_id  # type: ignore[attr-defined]
            metadata_source_type, metadata_status, source_quote_unit = (
                _metadata_provenance(contract)
            )
            source_locator = (
                f"database:lutou/schema:{batch.query.schema}/relation:{batch.query.table}"
            )
            output.append(
                LiveThreeOilRecord(
                    dataset_id=contract.dataset_id,  # type: ignore[attr-defined]
                    series_id=contract.series_id,  # type: ignore[attr-defined]
                    provider_dataset_id=f"lutou:oils:{batch.query.table}",
                    provider_series_id=provider_series_id,
                    provider=contract.provider,  # type: ignore[attr-defined]
                    metadata_source_type=metadata_source_type,
                    metadata_status=metadata_status,
                    source_quote_unit=source_quote_unit,
                    source_locator=source_locator,
                    source_table=batch.query.table,
                    source_column=source_column,
                    business_date=business_date,
                    raw_price=raw_price,
                    source_currency=contract.currency,  # type: ignore[attr-defined]
                    source_unit=contract.source_unit,  # type: ignore[attr-defined]
                    conversion_id=contract.conversion_id,  # type: ignore[attr-defined]
                    extracted_at=batch.extracted_at,
                    query_sha256=batch.query.sha256,
                    source_row_sha256=_row_hash(
                        batch.query.table,
                        source_column,
                        business_date,
                        raw_price,
                    ),
                )
            )
    return tuple(output)


def _metadata_provenance(contract: object) -> tuple[str, str, str]:
    table = contract.source_native_table  # type: ignore[attr-defined]
    column = contract.source_native_series  # type: ignore[attr-defined]
    currency = contract.currency  # type: ignore[attr-defined]
    unit = contract.source_unit  # type: ignore[attr-defined]
    if table == "oil_world_prices":
        if column not in OIL_WORLD_OFFICIAL_PRICE_SERIES:
            raise ThreeOilLiveError("Oil World price Series lacks official metadata proof")
        return "official_provider_website", "proven", "US-$/T"
    quote_units = {
        ("USD", "metric_tonne"): "USD/T",
        ("INR", "metric_tonne"): "INR/T",
        ("USD", "US_cents_per_lb"): "USC/LB",
        ("USD", "raw_basis_x100"): "raw_basis_x100",
        ("USD", "raw_credit_x100"): "raw_credit_x100",
        ("USD", "gallon"): "USD/GAL",
    }
    try:
        source_quote_unit = quote_units[(currency, unit)]
    except KeyError:
        raise ThreeOilLiveError("sealed Reuters Series lacks unit provenance") from None
    return "approved_source_mapping", "proven", source_quote_unit


def _date(value: object) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError) as exc:
        raise ThreeOilLiveError("Lutou live row has an invalid business date") from exc


def _price(value: object) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ThreeOilLiveError("Lutou live row has an invalid price") from exc
    if not result.is_finite():
        raise ThreeOilLiveError("Lutou live row has a non-finite price")
    return result


def _row_hash(
    table: str,
    column: str,
    business_date: date,
    raw_price: Decimal,
) -> str:
    payload = json.dumps(
        {
            "business_date": business_date.isoformat(),
            "raw_price": str(raw_price),
            "source_column": column,
            "source_table": table,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
