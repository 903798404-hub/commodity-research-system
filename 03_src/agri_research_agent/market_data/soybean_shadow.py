"""Canonical P0 shadow inputs for Soybean Import Crush.

This module consumes already-adapted Public Market Data candidates.  It does not
query Tankan and it never authorizes a formal source cutover.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Iterable

import pyarrow as pa

from .contracts import ContractId, Exchange, parse_standard_instrument


CBOT_CURRENT_PROVIDER_SERIES_ID = "tankan.ffpr.cbot.soybean.en"
CBOT_LEGACY_PROVIDER_SERIES_ID = "tankan.ffpr.cbot.soybean.zh"
CBOT_SHADOW_SERIES_ID = "market.quote.cbot.soybean.delivery.close.unknown"
FX_SPOT_PROVIDER_SERIES_ID = "tankan:market.exchange_rate:spot"
FX_SPOT_SHADOW_SERIES_ID = "fx.usd.cnh.spot"

CBOT_SHADOW_SCHEMA = pa.schema(
    [
        pa.field("schema_version", pa.string(), nullable=False),
        pa.field("series_id", pa.string(), nullable=False),
        pa.field("business_date", pa.date32(), nullable=False),
        pa.field("exchange", pa.string(), nullable=False),
        pa.field("commodity", pa.string(), nullable=False),
        pa.field("contract_year", pa.int16(), nullable=False),
        pa.field("contract_month", pa.int8(), nullable=False),
        pa.field("price", pa.float64(), nullable=True),
        pa.field("price_type", pa.string(), nullable=False),
        pa.field("session", pa.string(), nullable=False),
        pa.field("currency", pa.string(), nullable=False),
        pa.field("unit", pa.string(), nullable=False),
        pa.field("provider_dataset_id", pa.string(), nullable=False),
        pa.field("provider_series_id", pa.string(), nullable=False),
        pa.field("provider_role", pa.string(), nullable=False),
        pa.field("source_updated_at", pa.timestamp("us", tz="Asia/Shanghai"), nullable=True),
        pa.field("captured_at", pa.timestamp("us", tz="UTC"), nullable=False),
        pa.field("source_snapshot_sha256", pa.string(), nullable=False),
        pa.field("source_row_sha256", pa.string(), nullable=False),
        pa.field("quality_status", pa.string(), nullable=False),
        pa.field("is_usable", pa.bool_(), nullable=False),
        pa.field("promotion_authorized", pa.bool_(), nullable=False),
    ]
)

FX_SPOT_SHADOW_SCHEMA = pa.schema(
    [
        pa.field("schema_version", pa.string(), nullable=False),
        pa.field("series_id", pa.string(), nullable=False),
        pa.field("quote_date", pa.date32(), nullable=False),
        pa.field("base_currency", pa.string(), nullable=False),
        pa.field("quote_currency", pa.string(), nullable=False),
        pa.field("tenor", pa.string(), nullable=False),
        pa.field("tenor_months", pa.int8(), nullable=False),
        pa.field("rate", pa.float64(), nullable=True),
        pa.field("rate_type", pa.string(), nullable=False),
        pa.field("rate_unit", pa.string(), nullable=False),
        pa.field("provider_dataset_id", pa.string(), nullable=False),
        pa.field("provider_series_id", pa.string(), nullable=False),
        pa.field("source_updated_at", pa.timestamp("us", tz="Asia/Shanghai"), nullable=True),
        pa.field("captured_at", pa.timestamp("us", tz="UTC"), nullable=False),
        pa.field("source_snapshot_sha256", pa.string(), nullable=False),
        pa.field("source_row_sha256", pa.string(), nullable=False),
        pa.field("quality_status", pa.string(), nullable=False),
        pa.field("is_usable", pa.bool_(), nullable=False),
        pa.field("promotion_authorized", pa.bool_(), nullable=False),
    ]
)

_YYMM = re.compile(r"^[0-9]{4}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MARKET_CANDIDATE_FIELDS = (
    "schema_version",
    "dataset_id",
    "series_id_candidate",
    "provider_dataset_id",
    "provider_series_id",
    "source_series_id",
    "origin_system",
    "acquisition_channel",
    "source_locator",
    "business_date",
    "exchange",
    "product",
    "contract_code",
    "instrument_id",
    "price",
    "price_type",
    "session",
    "currency",
    "price_unit",
    "source_updated_at",
    "captured_at",
    "snapshot_sha256",
    "source_row_sha256",
    "quality_status",
    "is_usable",
)
_FX_CANDIDATE_FIELDS = (
    "schema_version",
    "dataset_id",
    "series_id_candidate",
    "provider_dataset_id",
    "provider_series_id",
    "origin_system",
    "acquisition_channel",
    "source_locator",
    "quote_date",
    "value_date",
    "maturity_date",
    "base_currency",
    "quote_currency",
    "tenor",
    "tenor_months",
    "rate",
    "rate_type",
    "rate_unit",
    "source_column",
    "source_updated_at",
    "captured_at",
    "snapshot_sha256",
    "source_row_sha256",
    "quality_status",
    "is_usable",
)


class SoybeanShadowError(ValueError):
    pass


def _aware(value: datetime | None, field_name: str, *, optional: bool = False) -> None:
    if value is None and optional:
        return
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise SoybeanShadowError(f"{field_name} must be timezone-aware")


def _sha(value: str, field_name: str) -> None:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise SoybeanShadowError(f"{field_name} must be a lowercase SHA-256")


def _value(value: float | None, *, usable: bool, field_name: str) -> float | None:
    if value is None:
        if usable:
            raise SoybeanShadowError(f"usable {field_name} cannot be missing")
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SoybeanShadowError(f"{field_name} must be numeric or missing")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        if usable:
            raise SoybeanShadowError(f"usable {field_name} must be positive and finite")
    return number


@dataclass(frozen=True, slots=True)
class CbotSoybeanShadowQuote:
    business_date: date
    contract: ContractId
    price: float | None
    provider_dataset_id: str
    provider_series_id: str
    source_updated_at: datetime | None
    captured_at: datetime
    source_snapshot_sha256: str
    source_row_sha256: str
    quality_status: str
    is_usable: bool

    def __post_init__(self) -> None:
        if type(self.business_date) is not date:
            raise SoybeanShadowError("business_date must be an exact date")
        if self.contract.exchange is not Exchange.CBOT or self.contract.product != "SOYBEAN":
            raise SoybeanShadowError("CBOT shadow requires an exact CBOT Soybean contract")
        if type(self.is_usable) is not bool:
            raise SoybeanShadowError("is_usable must be boolean")
        object.__setattr__(
            self,
            "price",
            _value(self.price, usable=self.is_usable, field_name="price"),
        )
        _aware(self.source_updated_at, "source_updated_at", optional=True)
        _aware(self.captured_at, "captured_at")
        _sha(self.source_snapshot_sha256, "source_snapshot_sha256")
        _sha(self.source_row_sha256, "source_row_sha256")
        for field_name in ("provider_dataset_id", "provider_series_id", "quality_status"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise SoybeanShadowError(f"{field_name} must be non-empty")
        if self.is_usable != (self.quality_status == "valid"):
            raise SoybeanShadowError("CBOT usability and quality status disagree")

    @property
    def key(self) -> tuple[date, int, int]:
        return (self.business_date, self.contract.year, self.contract.month)


@dataclass(frozen=True, slots=True)
class FxSpotShadowQuote:
    quote_date: date
    rate: float | None
    provider_dataset_id: str
    provider_series_id: str
    source_updated_at: datetime | None
    captured_at: datetime
    source_snapshot_sha256: str
    source_row_sha256: str
    quality_status: str
    is_usable: bool

    def __post_init__(self) -> None:
        if type(self.quote_date) is not date:
            raise SoybeanShadowError("quote_date must be an exact date")
        if type(self.is_usable) is not bool:
            raise SoybeanShadowError("is_usable must be boolean")
        object.__setattr__(
            self,
            "rate",
            _value(self.rate, usable=self.is_usable, field_name="rate"),
        )
        _aware(self.source_updated_at, "source_updated_at", optional=True)
        _aware(self.captured_at, "captured_at")
        _sha(self.source_snapshot_sha256, "source_snapshot_sha256")
        _sha(self.source_row_sha256, "source_row_sha256")
        for field_name in ("provider_dataset_id", "provider_series_id", "quality_status"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise SoybeanShadowError(f"{field_name} must be non-empty")
        if self.is_usable != (self.quality_status == "valid"):
            raise SoybeanShadowError("FX usability and quality status disagree")


@dataclass(frozen=True, slots=True)
class CbotShadowResult:
    table: pa.Table
    source_policy: dict[str, object]

    def current_quotes(self) -> tuple[CbotSoybeanShadowQuote, ...]:
        return tuple(
            _cbot_quote(row)
            for row in self.table.to_pylist()
            if row["provider_role"] == "current_candidate"
        )


@dataclass(frozen=True, slots=True)
class FxSpotShadowResult:
    table: pa.Table
    source_policy: dict[str, object]

    def quotes(self) -> tuple[FxSpotShadowQuote, ...]:
        return tuple(_fx_quote(row) for row in self.table.to_pylist())


@dataclass(frozen=True, slots=True)
class CbotReferenceQuote:
    business_date: date
    contract_year: int
    contract_month: int
    price: float
    price_type: str
    source_date: date

    def __post_init__(self) -> None:
        if type(self.business_date) is not date or type(self.source_date) is not date:
            raise SoybeanShadowError("reference dates must be exact dates")
        ContractId(Exchange.CBOT, "SOYBEAN", self.contract_year, self.contract_month)
        if (
            isinstance(self.price, bool)
            or not isinstance(self.price, (int, float))
            or not math.isfinite(float(self.price))
            or self.price <= 0
        ):
            raise SoybeanShadowError("reference price must be positive and finite")
        if not isinstance(self.price_type, str) or not self.price_type.strip():
            raise SoybeanShadowError("reference price_type must be non-empty")

    @property
    def key(self) -> tuple[date, int, int]:
        return (self.business_date, self.contract_year, self.contract_month)


@dataclass(frozen=True, slots=True)
class CbotShadowComparison:
    business_date: date
    contract_year: int
    contract_month: int
    tankan_close: float | None
    existing_price: float | None
    absolute_difference: float | None
    relative_difference: float | None
    availability: str
    source_date: date | None
    semantic_status: str


def build_cbot_soybean_shadow(candidates: pa.Table) -> CbotShadowResult:
    if tuple(candidates.schema.names) != _MARKET_CANDIDATE_FIELDS:
        raise SoybeanShadowError("market candidate schema is invalid")
    output: list[dict[str, object]] = []
    seen: set[tuple[str, date, int, int]] = set()
    for row in candidates.to_pylist():
        provider_id = row["provider_series_id"]
        if provider_id not in {
            CBOT_CURRENT_PROVIDER_SERIES_ID,
            CBOT_LEGACY_PROVIDER_SERIES_ID,
        }:
            continue
        if row["exchange"] != "CBOT" or row["product"] != "SOYBEAN":
            raise SoybeanShadowError("soybean provider identity disagrees with market identity")
        if (
            row["schema_version"] != "tankan-market-candidate/1"
            or row["dataset_id"] != "tankan.market.foreign_futures_price_raw"
            or row["provider_dataset_id"]
            != "tankan:market.foreign_futures_price_raw"
            or row["source_series_id"] != provider_id
            or row["origin_system"] != "tankan"
            or row["acquisition_channel"] != "direct_database"
            or row["price_type"] != "CLOSE"
            or row["session"] != "UNKNOWN"
            or row["currency"] != "USD"
            or row["price_unit"] != "US_cents/bushel"
        ):
            raise SoybeanShadowError("CBOT shadow semantic identity is inconsistent")
        contract = _exact_cbot_contract(row["contract_code"], row["instrument_id"])
        role = (
            "current_candidate"
            if provider_id == CBOT_CURRENT_PROVIDER_SERIES_ID
            else "legacy_comparison"
        )
        key = (role, row["business_date"], contract.year, contract.month)
        if key in seen:
            raise SoybeanShadowError("CBOT shadow key is duplicated")
        seen.add(key)
        output.append(
            {
                "schema_version": "soybean-cbot-shadow/1",
                "series_id": CBOT_SHADOW_SERIES_ID,
                "business_date": row["business_date"],
                "exchange": "CBOT",
                "commodity": "SOYBEAN",
                "contract_year": contract.year,
                "contract_month": contract.month,
                "price": row["price"],
                "price_type": "close",
                "session": "unknown",
                "currency": "USD",
                "unit": "US_cents/bushel",
                "provider_dataset_id": row["provider_dataset_id"],
                "provider_series_id": provider_id,
                "provider_role": role,
                "source_updated_at": row["source_updated_at"],
                "captured_at": row["captured_at"],
                "source_snapshot_sha256": row["snapshot_sha256"],
                "source_row_sha256": row["source_row_sha256"],
                "quality_status": row["quality_status"],
                "is_usable": row["is_usable"],
                "promotion_authorized": False,
            }
        )
        _cbot_quote(output[-1])
    output.sort(
        key=lambda item: (
            item["business_date"],
            item["contract_year"],
            item["contract_month"],
            item["provider_role"],
        )
    )
    current = {
        (row["business_date"], row["contract_year"], row["contract_month"]): row
        for row in output
        if row["provider_role"] == "current_candidate"
    }
    legacy = {
        (row["business_date"], row["contract_year"], row["contract_month"]): row
        for row in output
        if row["provider_role"] == "legacy_comparison"
    }
    overlap = sorted(current.keys() & legacy.keys())
    equal = sum(current[key]["price"] == legacy[key]["price"] for key in overlap)
    return CbotShadowResult(
        table=pa.Table.from_pylist(output, schema=CBOT_SHADOW_SCHEMA),
        source_policy={
            "schema_version": 1,
            "candidate_only": True,
            "current_provider_series_id": CBOT_CURRENT_PROVIDER_SERIES_ID,
            "legacy_provider_series_id": CBOT_LEGACY_PROVIDER_SERIES_ID,
            "current_candidate_row_count": len(current),
            "legacy_comparison_row_count": len(legacy),
            "overlap_key_count": len(overlap),
            "overlap_equal_key_count": equal,
            "overlap_different_key_count": len(overlap) - equal,
            "provider_merge_policy": "separate_no_average",
            "cutover_authorized": False,
        },
    )


def build_usd_cnh_spot_shadow(candidates: pa.Table) -> FxSpotShadowResult:
    if tuple(candidates.schema.names) != _FX_CANDIDATE_FIELDS:
        raise SoybeanShadowError("FX candidate schema is invalid")
    output: list[dict[str, object]] = []
    seen: set[date] = set()
    blocked_forward_rows = 0
    for row in candidates.to_pylist():
        if (
            row["schema_version"] != "tankan-fx-candidate/1"
            or row["dataset_id"] != "tankan.market.exchange_rate"
            or row["provider_dataset_id"] != "tankan:market.exchange_rate"
            or row["origin_system"] != "tankan"
            or row["acquisition_channel"] != "direct_database"
            or row["base_currency"] != "USD"
            or row["quote_currency"] != "CNH"
            or row["rate_unit"] != "CNH_per_USD"
            or row["rate_type"] != "unspecified"
        ):
            raise SoybeanShadowError("FX candidate identity is inconsistent")
        if row["tenor_months"] != 0 or row["tenor"] != "SPOT":
            month = row["tenor_months"]
            if (
                not isinstance(month, int)
                or not 1 <= month <= 12
                or row["tenor"] != f"{month}M"
                or row["source_column"] != f"fx_{month}m"
            ):
                raise SoybeanShadowError("FX Forward source identity is inconsistent")
            blocked_forward_rows += 1
            continue
        if (
            row["source_column"] != "spot"
            or row["provider_series_id"] != FX_SPOT_PROVIDER_SERIES_ID
        ):
            raise SoybeanShadowError("FX Spot identity is inconsistent")
        if row["quote_date"] in seen:
            raise SoybeanShadowError("FX Spot shadow key is duplicated")
        seen.add(row["quote_date"])
        output.append(
            {
                "schema_version": "soybean-fx-spot-shadow/1",
                "series_id": FX_SPOT_SHADOW_SERIES_ID,
                "quote_date": row["quote_date"],
                "base_currency": "USD",
                "quote_currency": "CNH",
                "tenor": "SPOT",
                "tenor_months": 0,
                "rate": row["rate"],
                "rate_type": "spot",
                "rate_unit": "CNH_per_USD",
                "provider_dataset_id": row["provider_dataset_id"],
                "provider_series_id": row["provider_series_id"],
                "source_updated_at": row["source_updated_at"],
                "captured_at": row["captured_at"],
                "source_snapshot_sha256": row["snapshot_sha256"],
                "source_row_sha256": row["source_row_sha256"],
                "quality_status": row["quality_status"],
                "is_usable": row["is_usable"],
                "promotion_authorized": False,
            }
        )
        _fx_quote(output[-1])
    output.sort(key=lambda item: item["quote_date"])
    return FxSpotShadowResult(
        table=pa.Table.from_pylist(output, schema=FX_SPOT_SHADOW_SCHEMA),
        source_policy={
            "schema_version": 1,
            "candidate_only": True,
            "spot_canonical_shadow": True,
            "forward_canonical_promotion": False,
            "blocked_forward_row_count": blocked_forward_rows,
            "cutover_authorized": False,
        },
    )


def compare_cbot_reference(
    shadow: CbotShadowResult,
    references: Iterable[CbotReferenceQuote],
) -> tuple[CbotShadowComparison, ...]:
    current = {quote.key: quote for quote in shadow.current_quotes() if quote.is_usable}
    reference_by_key: dict[tuple[date, int, int], CbotReferenceQuote] = {}
    for reference in references:
        if reference.key in reference_by_key:
            raise SoybeanShadowError("reference CBOT key is duplicated")
        reference_by_key[reference.key] = reference
    rows: list[CbotShadowComparison] = []
    for key in sorted(current.keys() | reference_by_key.keys()):
        tankan = current.get(key)
        existing = reference_by_key.get(key)
        both = tankan is not None and existing is not None
        absolute = (
            abs(float(tankan.price) - existing.price)
            if both and tankan.price is not None
            else None
        )
        relative = (
            absolute / abs(existing.price)
            if absolute is not None and existing.price
            else None
        )
        rows.append(
            CbotShadowComparison(
                business_date=key[0],
                contract_year=key[1],
                contract_month=key[2],
                tankan_close=None if tankan is None else tankan.price,
                existing_price=None if existing is None else existing.price,
                absolute_difference=absolute,
                relative_difference=relative,
                availability=("both" if both else "tankan_only" if tankan else "existing_only"),
                source_date=None if existing is None else existing.source_date,
                semantic_status=(
                    "SEMANTIC_DIFFERENCE"
                    if both and existing.price_type.casefold() != "close"
                    else "ALIGNED_PRICE_TYPE"
                    if both
                    else "NOT_COMPARABLE"
                ),
            )
        )
    return tuple(rows)


def _exact_cbot_contract(code: object, instrument_id: object) -> ContractId:
    if not isinstance(code, str) or _YYMM.fullmatch(code) is None:
        raise SoybeanShadowError("CBOT shadow requires an exact contract YYMM code")
    month = int(code[2:])
    if not 1 <= month <= 12 or not isinstance(instrument_id, str):
        raise SoybeanShadowError("CBOT shadow requires an exact contract identity")
    try:
        instrument = parse_standard_instrument(instrument_id)
    except ValueError as exc:
        raise SoybeanShadowError("CBOT shadow requires an exact contract identity") from exc
    expected = ContractId(Exchange.CBOT, "SOYBEAN", 2000 + int(code[:2]), month)
    if instrument != expected:
        raise SoybeanShadowError("CBOT exact contract code and identity disagree")
    return expected


def _cbot_quote(row: dict[str, object]) -> CbotSoybeanShadowQuote:
    return CbotSoybeanShadowQuote(
        business_date=row["business_date"],
        contract=ContractId(
            Exchange.CBOT,
            "SOYBEAN",
            row["contract_year"],
            row["contract_month"],
        ),
        price=row["price"],
        provider_dataset_id=row["provider_dataset_id"],
        provider_series_id=row["provider_series_id"],
        source_updated_at=row["source_updated_at"],
        captured_at=row["captured_at"],
        source_snapshot_sha256=row["source_snapshot_sha256"],
        source_row_sha256=row["source_row_sha256"],
        quality_status=row["quality_status"],
        is_usable=row["is_usable"],
    )


def _fx_quote(row: dict[str, object]) -> FxSpotShadowQuote:
    return FxSpotShadowQuote(
        quote_date=row["quote_date"],
        rate=row["rate"],
        provider_dataset_id=row["provider_dataset_id"],
        provider_series_id=row["provider_series_id"],
        source_updated_at=row["source_updated_at"],
        captured_at=row["captured_at"],
        source_snapshot_sha256=row["source_snapshot_sha256"],
        source_row_sha256=row["source_row_sha256"],
        quality_status=row["quality_status"],
        is_usable=row["is_usable"],
    )


__all__ = [
    "CBOT_SHADOW_SCHEMA",
    "FX_SPOT_SHADOW_SCHEMA",
    "CbotReferenceQuote",
    "CbotShadowComparison",
    "CbotShadowResult",
    "CbotSoybeanShadowQuote",
    "FxSpotShadowQuote",
    "FxSpotShadowResult",
    "SoybeanShadowError",
    "build_cbot_soybean_shadow",
    "build_usd_cnh_spot_shadow",
    "compare_cbot_reference",
]
