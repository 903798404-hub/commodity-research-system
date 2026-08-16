"""Tankan-specific mapping from source rows to public market candidates."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pyarrow as pa
import yaml

from agri_research_agent.market_data.contracts import ContractId, Exchange
from agri_research_agent.market_data.quotes import (
    Currency,
    PriceType,
    PriceUnit,
    TradingSession,
)
from agri_research_agent.research_data import ProviderSeriesId

from .queries import MARKET_WINDOW_QUERY


MARKET_RAW_SCHEMA = pa.schema(
    [
        pa.field("trade_date", pa.date32(), nullable=False),
        pa.field("exchange", pa.string(), nullable=False),
        pa.field("product_name", pa.string(), nullable=False),
        pa.field("contract", pa.string(), nullable=False),
        pa.field("close_price", pa.float64(), nullable=True),
        pa.field("updated_at", pa.timestamp("us"), nullable=True),
    ]
)

MARKET_CANDIDATE_SCHEMA = pa.schema(
    [
        pa.field("schema_version", pa.string(), nullable=False),
        pa.field("dataset_id", pa.string(), nullable=False),
        pa.field("series_id_candidate", pa.string(), nullable=True),
        pa.field("provider_dataset_id", pa.string(), nullable=False),
        pa.field("provider_series_id", pa.string(), nullable=False),
        pa.field("source_series_id", pa.string(), nullable=False),
        pa.field("origin_system", pa.string(), nullable=False),
        pa.field("acquisition_channel", pa.string(), nullable=False),
        pa.field("source_locator", pa.string(), nullable=False),
        pa.field("business_date", pa.date32(), nullable=False),
        pa.field("exchange", pa.string(), nullable=False),
        pa.field("product", pa.string(), nullable=False),
        pa.field("contract_code", pa.string(), nullable=False),
        pa.field("instrument_id", pa.string(), nullable=True),
        pa.field("price", pa.float64(), nullable=True),
        pa.field("price_type", pa.string(), nullable=False),
        pa.field("session", pa.string(), nullable=False),
        pa.field("currency", pa.string(), nullable=False),
        pa.field("price_unit", pa.string(), nullable=False),
        pa.field("source_updated_at", pa.timestamp("us", tz="Asia/Shanghai"), nullable=True),
        pa.field("captured_at", pa.timestamp("us", tz="UTC"), nullable=False),
        pa.field("snapshot_sha256", pa.string(), nullable=False),
        pa.field("source_row_sha256", pa.string(), nullable=False),
        pa.field("quality_status", pa.string(), nullable=False),
        pa.field("is_usable", pa.bool_(), nullable=False),
    ]
)

MARKET_SCHEMA_VERSION = "tankan-market-candidate/1"
_CONTRACT_PATTERN = re.compile(r"^[0-9]{4}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class MarketPriceAdapterError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class MarketSeriesMapping:
    source_exchange: str
    source_product_name: str
    source_series_id: str
    exchange: Exchange
    product: str
    currency: Currency
    price_unit: PriceUnit
    quantity_unit: str
    contract_size: float | None
    contract_size_unit: str | None

    @property
    def provider_series_id(self) -> ProviderSeriesId:
        """Compatibility migration: source_series_id is provider identity evidence."""

        return ProviderSeriesId(self.source_series_id)


@dataclass(frozen=True, slots=True)
class MarketAdapterConfig:
    max_forward_years: int
    required_products: frozenset[str]
    mappings: dict[tuple[str, str], MarketSeriesMapping]


@dataclass(frozen=True, slots=True)
class MarketAdapterResult:
    table: pa.Table
    quality_report: dict[str, object]
    collision_report: dict[str, object]


def load_market_config(path: str | Path) -> MarketAdapterConfig:
    payload = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "max_forward_years",
        "required_products",
        "series",
    }:
        raise MarketPriceAdapterError("market config fields are invalid")
    if payload["schema_version"] != 1:
        raise MarketPriceAdapterError("unsupported market config version")
    mappings: dict[tuple[str, str], MarketSeriesMapping] = {}
    provider_ids: set[str] = set()
    expected = {
        "source_exchange",
        "source_product_name",
        "source_series_id",
        "exchange",
        "product",
        "currency",
        "price_unit",
        "quantity_unit",
        "contract_size",
        "contract_size_unit",
    }
    for item in payload["series"]:
        if not isinstance(item, dict) or set(item) != expected:
            raise MarketPriceAdapterError("market series mapping fields are invalid")
        mapping = MarketSeriesMapping(
            source_exchange=str(item["source_exchange"]),
            source_product_name=str(item["source_product_name"]),
            source_series_id=str(item["source_series_id"]),
            exchange=Exchange(str(item["exchange"])),
            product=str(item["product"]).strip().upper(),
            currency=Currency(str(item["currency"])),
            price_unit=PriceUnit(str(item["price_unit"])),
            quantity_unit=str(item["quantity_unit"]),
            contract_size=(
                None if item["contract_size"] is None else float(item["contract_size"])
            ),
            contract_size_unit=(
                None
                if item["contract_size_unit"] is None
                else str(item["contract_size_unit"])
            ),
        )
        provider_id = str(mapping.provider_series_id)
        key = (mapping.source_exchange, mapping.source_product_name)
        if key in mappings or provider_id in provider_ids:
            raise MarketPriceAdapterError("market series mapping is duplicated")
        mappings[key] = mapping
        provider_ids.add(provider_id)
    config = MarketAdapterConfig(
        max_forward_years=int(payload["max_forward_years"]),
        required_products=frozenset(
            str(item).strip().upper() for item in payload["required_products"]
        ),
        mappings=mappings,
    )
    if config.max_forward_years <= 0 or not config.required_products:
        raise MarketPriceAdapterError("market config bounds are invalid")
    return config


def adapt_market_price(
    raw: pa.Table,
    config: MarketAdapterConfig,
    *,
    snapshot_sha256: str,
    captured_at: datetime,
    previous_latest_date: date | None = None,
) -> MarketAdapterResult:
    if raw.schema != MARKET_RAW_SCHEMA:
        raise MarketPriceAdapterError("market raw schema does not match the exact contract")
    if _SHA256.fullmatch(snapshot_sha256) is None:
        raise MarketPriceAdapterError("snapshot_sha256 is invalid")
    if captured_at.tzinfo is None or captured_at.utcoffset() is None:
        raise MarketPriceAdapterError("captured_at must be timezone-aware")
    provider = MARKET_WINDOW_QUERY.provider
    source_timezone = ZoneInfo("Asia/Shanghai")
    records: list[dict[str, object]] = []
    statuses: Counter[str] = Counter()
    products: set[str] = set()
    provider_keys: set[tuple[object, ...]] = set()
    logical: dict[tuple[object, ...], list[tuple[str, float | None]]] = defaultdict(list)

    for source_row in raw.to_pylist():
        mapping = config.mappings.get(
            (source_row["exchange"], source_row["product_name"])
        )
        if mapping is None:
            raise MarketPriceAdapterError("unmapped source market series")
        products.add(mapping.product)
        quality: list[str] = []
        code = source_row["contract"]
        instrument: ContractId | None = None
        if not isinstance(code, str) or _CONTRACT_PATTERN.fullmatch(code) is None:
            quality.append("malformed_contract")
        else:
            year = 2000 + int(code[:2])
            month = int(code[2:])
            if not 1 <= month <= 12:
                quality.append("invalid_contract_month")
            else:
                instrument = ContractId(mapping.exchange, mapping.product, year, month)
                if year < source_row["trade_date"].year:
                    quality.append("contract_before_trade_year")
                if year > source_row["trade_date"].year + config.max_forward_years:
                    quality.append("contract_too_far_forward")
        value = source_row["close_price"]
        if value is None:
            quality.append("missing_price")
        elif not math.isfinite(float(value)):
            quality.append("nonfinite_price")
        elif float(value) <= 0:
            quality.append("nonpositive_price")
        updated = source_row["updated_at"]
        if updated is not None and updated.tzinfo is None:
            updated = updated.replace(tzinfo=source_timezone)
        status = "valid" if not quality else "|".join(sorted(set(quality)))
        for item in quality or ["valid"]:
            statuses[item] += 1
        provider_series_id = str(mapping.provider_series_id)
        record = {
            "schema_version": MARKET_SCHEMA_VERSION,
            "dataset_id": str(provider.dataset.dataset_id),
            "series_id_candidate": None,
            "provider_dataset_id": str(provider.provider_dataset_id),
            "provider_series_id": provider_series_id,
            "source_series_id": mapping.source_series_id,
            "origin_system": str(provider.dataset.origin_system),
            "acquisition_channel": provider.acquisition_channel.value,
            "source_locator": str(provider.source_locator),
            "business_date": source_row["trade_date"],
            "exchange": mapping.exchange.value,
            "product": mapping.product,
            "contract_code": code,
            "instrument_id": None if instrument is None else str(instrument),
            "price": None if value is None else float(value),
            "price_type": PriceType.CLOSE.value,
            "session": TradingSession.UNKNOWN.value,
            "currency": mapping.currency.value,
            "price_unit": mapping.price_unit.value,
            "source_updated_at": updated,
            "captured_at": captured_at.astimezone(timezone.utc),
            "snapshot_sha256": snapshot_sha256,
            "source_row_sha256": _row_sha(source_row),
            "quality_status": status,
            "is_usable": status == "valid",
        }
        provider_key = (provider_series_id, record["business_date"], code)
        if provider_key in provider_keys:
            raise MarketPriceAdapterError("provider candidate key is duplicated")
        provider_keys.add(provider_key)
        logical_key = (
            record["exchange"],
            record["product"],
            record["instrument_id"],
            record["business_date"],
            record["price_type"],
            record["session"],
        )
        logical[logical_key].append((provider_series_id, record["price"]))
        records.append(record)

    missing_products = sorted(config.required_products - products)
    if missing_products and raw.num_rows:
        raise MarketPriceAdapterError(f"required products are missing: {missing_products}")
    latest = max((item["business_date"] for item in records), default=None)
    if previous_latest_date is not None and (
        latest is None or latest < previous_latest_date
    ):
        raise MarketPriceAdapterError("market latest date regressed")
    collisions = [values for values in logical.values() if len(values) > 1]
    differing = sum(
        1
        for values in collisions
        if len({price for _, price in values}) > 1
    )
    collision_samples = []
    for key, values in logical.items():
        if len(values) <= 1:
            continue
        collision_samples.append(
            {
                "logical_key": [
                    item.isoformat() if isinstance(item, date) else item for item in key
                ],
                "observations": [
                    {
                        "provider_series_id": provider_id,
                        "price": (
                            {"nonfinite_float": repr(price)}
                            if isinstance(price, float) and not math.isfinite(price)
                            else price
                        ),
                    }
                    for provider_id, price in values
                ],
                "prices_differ": len({price for _, price in values}) > 1,
            }
        )
    collision_report = {
        "candidate_only": True,
        "promotion_authorized": False,
        "logical_collision_key_count": len(collisions),
        "different_price_key_count": differing,
        "samples": collision_samples[:20],
        "collision_status": (
            "BLOCKED_DIFFERING_PROVIDER_VALUES"
            if differing
            else "NO_DIFFERING_PROVIDER_VALUES"
        ),
    }
    quality_report = {
        "schema_version": 1,
        "candidate_only": True,
        "promotion_authorized": False,
        "dataset_id": str(provider.dataset.dataset_id),
        "row_count": len(records),
        "usable_row_count": sum(bool(item["is_usable"]) for item in records),
        "unusable_row_count": sum(not bool(item["is_usable"]) for item in records),
        "quality_status_counts": dict(sorted(statuses.items())),
        "source_min_date": (
            min(item["business_date"] for item in records).isoformat()
            if records
            else None
        ),
        "source_max_date": latest.isoformat() if latest else None,
        "canonical_series_status": "candidate_only",
        "empty_input_status": "no_data" if not records else None,
    }
    return MarketAdapterResult(
        table=pa.Table.from_pylist(records, schema=MARKET_CANDIDATE_SCHEMA),
        quality_report=quality_report,
        collision_report=collision_report,
    )


def _row_sha(row: dict[str, object]) -> str:
    payload = {}
    for key, value in row.items():
        if isinstance(value, (date, datetime)):
            payload[key] = value.isoformat()
        elif isinstance(value, float) and not math.isfinite(value):
            payload[key] = {"nonfinite_float": repr(value)}
        else:
            payload[key] = value
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
