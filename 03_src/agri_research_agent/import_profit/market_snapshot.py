"""Pure in-memory market matching for soybean import-profit calculations."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from enum import StrEnum
from math import isfinite
from numbers import Real
from types import MappingProxyType
from typing import Iterable, Mapping, TypeVar

from .business_days import require_business_weekday
from .cnf_store import CnfQuoteRecord, business_key_tuple
from .config import SoybeanImportProfitConfig
from .contract_mapping import map_soybean_contracts
from .fx import calculate_tenor_months, select_fx
from .models import (
    BusinessKey,
    CbotContract,
    FxCurve,
    FxCurvePoint,
    FxSelection,
    FxSelectionStatus,
    MappedContracts,
    MissingReason,
)
from .soybean import SoybeanCalculationInput


CBOT_KEY_FIELDS = ("market_date", "contract_year", "contract_month")
FX_KEY_FIELDS = ("market_date", "tenor_months")
DCE_KEY_FIELDS = ("business_date", "contract_code")
MARKET_MISSING_REASON_ORDER = (
    MissingReason.MISSING_CNF,
    MissingReason.MISSING_CBOT,
    MissingReason.MISSING_FX,
    MissingReason.MISSING_SOYMEAL,
    MissingReason.MISSING_SOYOIL,
)
_DCE_CONTRACT_CODE = re.compile(r"^[MY][0-9]{4}$")

CbotKey = tuple[date, int, int]
FxKey = tuple[date, int]
DceKey = tuple[date, str]
CnfKey = tuple[date, str, str, int, int]


class MarketSnapshotError(ValueError):
    """Base error for market snapshot construction."""


class MarketSnapshotValidationError(MarketSnapshotError):
    """Raised for structurally invalid snapshot inputs."""


class MarketSnapshotDuplicateKeyError(MarketSnapshotValidationError):
    """Raised when a source collection contains a duplicate canonical key."""


class SnapshotStatus(StrEnum):
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"


@dataclass(frozen=True, slots=True)
class HistoricalCnfMarketPoint:
    """Read-only historical CNF input without weakening the manual CNF store."""

    business_key: BusinessKey
    cnf_cents_per_bushel: float | None
    source: str
    updated_at: datetime
    batch_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.business_key, BusinessKey):
            raise MarketSnapshotValidationError(
                "historical CNF business_key must be a BusinessKey"
            )
        if self.cnf_cents_per_bushel is not None:
            object.__setattr__(
                self,
                "cnf_cents_per_bushel",
                _finite_number(
                    self.cnf_cents_per_bushel,
                    "historical CNF cnf_cents_per_bushel",
                    positive=False,
                ),
            )
        if self.source != "historical_excel":
            raise MarketSnapshotValidationError(
                "historical CNF source must remain historical_excel"
            )
        if not isinstance(self.updated_at, datetime) or self.updated_at.tzinfo is None:
            raise MarketSnapshotValidationError(
                "historical CNF updated_at must be timezone-aware"
            )
        object.__setattr__(
            self,
            "updated_at",
            self.updated_at.astimezone(timezone.utc),
        )
        _require_non_empty(self.batch_id, "historical CNF batch_id")

    @property
    def key(self) -> CnfKey:
        return business_key_tuple(self.business_key)


@dataclass(frozen=True, slots=True)
class CbotPricePoint:
    market_date: date
    contract_year: int
    contract_month: int
    price_cents_per_bushel: float
    exchange_quality_status: str
    is_usable: bool
    eligible_for_import_profit: bool
    source: str
    source_table: str
    source_column: str
    source_snapshot_sha256: str
    source_statement_index: int | None = None

    def __post_init__(self) -> None:
        _require_real_date(self.market_date, "market_date")
        try:
            CbotContract(self.contract_year, self.contract_month)
        except ValueError as exc:
            raise MarketSnapshotValidationError(str(exc)) from exc
        object.__setattr__(
            self,
            "price_cents_per_bushel",
            _finite_number(
                self.price_cents_per_bushel,
                "price_cents_per_bushel",
                positive=True,
            ),
        )
        _require_non_empty(self.exchange_quality_status, "exchange_quality_status")
        _require_bool(self.is_usable, "is_usable")
        _require_bool(
            self.eligible_for_import_profit,
            "eligible_for_import_profit",
        )
        if self.eligible_for_import_profit and not self.is_usable:
            raise MarketSnapshotValidationError(
                "eligible_for_import_profit requires is_usable=true"
            )
        _require_non_empty(self.source, "source")
        _require_non_empty(self.source_table, "source_table")
        _require_non_empty(self.source_column, "source_column")
        _require_non_empty(
            self.source_snapshot_sha256,
            "source_snapshot_sha256",
        )
        if self.source_statement_index is not None and (
            isinstance(self.source_statement_index, bool)
            or not isinstance(self.source_statement_index, int)
            or self.source_statement_index < 0
        ):
            raise MarketSnapshotValidationError(
                "source_statement_index must be a non-negative integer or None"
            )

    @property
    def key(self) -> CbotKey:
        return (self.market_date, self.contract_year, self.contract_month)


@dataclass(frozen=True, slots=True)
class FxPricePoint:
    market_date: date
    tenor_months: int
    fx_value: float
    source: str
    source_table: str
    source_column: str
    source_snapshot_sha256: str

    def __post_init__(self) -> None:
        try:
            validated = FxCurvePoint(
                market_date=self.market_date,
                tenor_months=self.tenor_months,
                value=self.fx_value,
                source=self.source,
                source_identity=self.source_snapshot_sha256,
            )
        except ValueError as exc:
            raise MarketSnapshotValidationError(str(exc)) from exc
        object.__setattr__(self, "fx_value", validated.value)
        _require_non_empty(self.source_table, "source_table")
        _require_non_empty(self.source_column, "source_column")
        _require_non_empty(
            self.source_snapshot_sha256,
            "source_snapshot_sha256",
        )

    @property
    def key(self) -> FxKey:
        return (self.market_date, self.tenor_months)

    def as_curve_point(self) -> FxCurvePoint:
        return FxCurvePoint(
            market_date=self.market_date,
            tenor_months=self.tenor_months,
            value=self.fx_value,
            source=self.source,
            source_identity=self.source_snapshot_sha256,
        )


@dataclass(frozen=True, slots=True)
class DcePricePoint:
    business_date: date
    contract_code: str
    price_cny_per_tonne: float
    price_type: str
    source: str
    source_function: str
    is_usable: bool
    source_quote_date: date | None = None
    source_quote_time: time | None = None
    source_snapshot_sha256: str | None = None

    def __post_init__(self) -> None:
        _require_real_date(self.business_date, "business_date")
        if (
            not isinstance(self.contract_code, str)
            or _DCE_CONTRACT_CODE.fullmatch(self.contract_code) is None
        ):
            raise MarketSnapshotValidationError(
                "contract_code must be a complete uppercase M/Y contract code"
            )
        object.__setattr__(
            self,
            "price_cny_per_tonne",
            _finite_number(
                self.price_cny_per_tonne,
                "price_cny_per_tonne",
                positive=True,
            ),
        )
        _require_non_empty(self.price_type, "price_type")
        _require_non_empty(self.source, "source")
        _require_non_empty(self.source_function, "source_function")
        _require_bool(self.is_usable, "is_usable")
        if self.source_quote_date is not None:
            _require_real_date(self.source_quote_date, "source_quote_date")
        if self.source_quote_time is not None and type(self.source_quote_time) is not time:
            raise MarketSnapshotValidationError(
                "source_quote_time must be a real time or None"
            )
        if self.source_snapshot_sha256 is not None:
            _require_non_empty(
                self.source_snapshot_sha256,
                "source_snapshot_sha256",
            )

    @property
    def key(self) -> DceKey:
        return (self.business_date, self.contract_code)


@dataclass(frozen=True, slots=True)
class FxPointProvenance:
    tenor_months: int
    source: str
    source_table: str
    source_column: str
    source_snapshot_sha256: str


@dataclass(frozen=True, slots=True)
class SoybeanMarketSnapshot:
    business_key: BusinessKey
    mapped_contracts: MappedContracts
    parameter_version: str
    cnf_cents_per_bushel: float | None
    cnf_source: str | None
    cbot_price_cents_per_bushel: float | None
    cbot_exchange_quality_status: str | None
    cbot_source: str | None
    cbot_source_table: str | None
    cbot_source_column: str | None
    cbot_source_snapshot_sha256: str | None
    fx_target_tenor: int
    fx_value: float | None
    fx_is_interpolated: bool
    fx_lower_tenor: int | None
    fx_upper_tenor: int | None
    fx_selection_status: FxSelectionStatus
    fx_source: str | None
    fx_source_snapshot_sha256: str | None
    fx_point_provenance: tuple[FxPointProvenance, ...]
    soymeal_price_cny_per_tonne: float | None
    soymeal_price_type: str | None
    soymeal_source: str | None
    soymeal_source_function: str | None
    soyoil_price_cny_per_tonne: float | None
    soyoil_price_type: str | None
    soyoil_source: str | None
    soyoil_source_function: str | None
    snapshot_status: SnapshotStatus
    missing_reasons: tuple[MissingReason, ...]

    @property
    def mapping_identity(self) -> str:
        return self.mapped_contracts.mapping_identity

    @property
    def cbot_contract_year(self) -> int:
        return self.mapped_contracts.cbot.contract_year

    @property
    def cbot_contract_month(self) -> int:
        return self.mapped_contracts.cbot.contract_month

    @property
    def soymeal_contract_code(self) -> str:
        return self.mapped_contracts.soymeal.code

    @property
    def soyoil_contract_code(self) -> str:
        return self.mapped_contracts.soyoil.code


@dataclass(frozen=True, slots=True)
class MarketDataIndex:
    cnf_by_key: Mapping[CnfKey, CnfQuoteRecord | HistoricalCnfMarketPoint]
    cbot_by_key: Mapping[CbotKey, CbotPricePoint]
    fx_by_key: Mapping[FxKey, FxPricePoint]
    dce_by_key: Mapping[DceKey, DcePricePoint]


def index_market_data(
    *,
    cnf_records: Iterable[CnfQuoteRecord | HistoricalCnfMarketPoint],
    cbot_records: Iterable[CbotPricePoint],
    fx_records: Iterable[FxPricePoint],
    dce_records: Iterable[DcePricePoint],
) -> MarketDataIndex:
    """Validate every record and build immutable canonical-key indexes."""

    return MarketDataIndex(
        cnf_by_key=_cnf_index(cnf_records),
        cbot_by_key=_unique_index(
            cbot_records,
            CbotPricePoint,
            lambda record: record.key,
            "CBOT",
        ),
        fx_by_key=_unique_index(
            fx_records,
            FxPricePoint,
            lambda record: record.key,
            "FX",
        ),
        dce_by_key=_unique_index(
            dce_records,
            DcePricePoint,
            lambda record: record.key,
            "DCE",
        ),
    )


def build_soybean_market_snapshot(
    business_key: BusinessKey,
    *,
    config: SoybeanImportProfitConfig,
    cnf_records: Iterable[CnfQuoteRecord | HistoricalCnfMarketPoint],
    cbot_records: Iterable[CbotPricePoint],
    fx_records: Iterable[FxPricePoint],
    dce_records: Iterable[DcePricePoint],
) -> SoybeanMarketSnapshot:
    """Build one deterministic snapshot from caller-owned in-memory records."""

    index = index_market_data(
        cnf_records=cnf_records,
        cbot_records=cbot_records,
        fx_records=fx_records,
        dce_records=dce_records,
    )
    return build_soybean_market_snapshot_from_index(
        business_key,
        config=config,
        market_index=index,
    )


def build_soybean_market_snapshot_from_index(
    business_key: BusinessKey,
    *,
    config: SoybeanImportProfitConfig,
    market_index: MarketDataIndex,
) -> SoybeanMarketSnapshot:
    """Build one snapshot while reusing already validated source indexes."""

    _validate_business_key_for_config(business_key, config)
    require_business_weekday(business_key.business_date)
    if not isinstance(market_index, MarketDataIndex):
        raise MarketSnapshotValidationError(
            "market_index must be a validated MarketDataIndex"
        )

    mapped = map_soybean_contracts(
        config,
        business_key.shipment_year,
        business_key.shipment_month,
    )
    cnf_record = market_index.cnf_by_key.get(business_key_tuple(business_key))
    cnf_value = (
        None if cnf_record is None else cnf_record.cnf_cents_per_bushel
    )

    cbot_record = market_index.cbot_by_key.get(
        (
            business_key.business_date,
            mapped.cbot.contract_year,
            mapped.cbot.contract_month,
        )
    )
    selected_cbot = (
        cbot_record
        if cbot_record is not None
        and cbot_record.is_usable
        and cbot_record.eligible_for_import_profit
        else None
    )

    target_tenor = calculate_tenor_months(
        business_key.business_date,
        business_key.shipment_year,
        business_key.shipment_month,
        config.fx_policy,
    )
    same_day_fx = tuple(
        point
        for (market_date, _), point in market_index.fx_by_key.items()
        if market_date == business_key.business_date
    )
    fx_curve = FxCurve(
        business_key.business_date,
        tuple(point.as_curve_point() for point in same_day_fx),
    )
    fx_selection = select_fx(
        business_key.business_date,
        target_tenor,
        fx_curve,
        config.fx_policy,
    )
    fx_provenance = _fx_provenance(fx_selection, market_index.fx_by_key, business_key.business_date)
    fx_source = _shared_value(fx_provenance, "source")
    fx_identity = _shared_value(fx_provenance, "source_snapshot_sha256")

    soymeal_record = market_index.dce_by_key.get(
        (business_key.business_date, mapped.soymeal.code)
    )
    selected_soymeal = (
        soymeal_record
        if soymeal_record is not None and soymeal_record.is_usable
        else None
    )
    soyoil_record = market_index.dce_by_key.get(
        (business_key.business_date, mapped.soyoil.code)
    )
    selected_soyoil = (
        soyoil_record
        if soyoil_record is not None and soyoil_record.is_usable
        else None
    )

    missing_reasons = tuple(
        reason
        for value, reason in (
            (cnf_value, MissingReason.MISSING_CNF),
            (
                None if selected_cbot is None else selected_cbot.price_cents_per_bushel,
                MissingReason.MISSING_CBOT,
            ),
            (fx_selection.fx_value, MissingReason.MISSING_FX),
            (
                None
                if selected_soymeal is None
                else selected_soymeal.price_cny_per_tonne,
                MissingReason.MISSING_SOYMEAL,
            ),
            (
                None
                if selected_soyoil is None
                else selected_soyoil.price_cny_per_tonne,
                MissingReason.MISSING_SOYOIL,
            ),
        )
        if value is None
    )
    status = (
        SnapshotStatus.COMPLETE
        if not missing_reasons
        else SnapshotStatus.INCOMPLETE
    )
    return SoybeanMarketSnapshot(
        business_key=business_key,
        mapped_contracts=mapped,
        parameter_version=str(config.schema_version),
        cnf_cents_per_bushel=cnf_value,
        cnf_source=None if cnf_record is None else cnf_record.source,
        cbot_price_cents_per_bushel=(
            None
            if selected_cbot is None
            else selected_cbot.price_cents_per_bushel
        ),
        cbot_exchange_quality_status=(
            None
            if selected_cbot is None
            else selected_cbot.exchange_quality_status
        ),
        cbot_source=None if selected_cbot is None else selected_cbot.source,
        cbot_source_table=(
            None if selected_cbot is None else selected_cbot.source_table
        ),
        cbot_source_column=(
            None if selected_cbot is None else selected_cbot.source_column
        ),
        cbot_source_snapshot_sha256=(
            None
            if selected_cbot is None
            else selected_cbot.source_snapshot_sha256
        ),
        fx_target_tenor=fx_selection.target_tenor,
        fx_value=fx_selection.fx_value,
        fx_is_interpolated=fx_selection.is_interpolated,
        fx_lower_tenor=fx_selection.lower_tenor,
        fx_upper_tenor=fx_selection.upper_tenor,
        fx_selection_status=fx_selection.selection_status,
        fx_source=fx_source,
        fx_source_snapshot_sha256=fx_identity,
        fx_point_provenance=fx_provenance,
        soymeal_price_cny_per_tonne=(
            None
            if selected_soymeal is None
            else selected_soymeal.price_cny_per_tonne
        ),
        soymeal_price_type=(
            None if selected_soymeal is None else selected_soymeal.price_type
        ),
        soymeal_source=(
            None if selected_soymeal is None else selected_soymeal.source
        ),
        soymeal_source_function=(
            None
            if selected_soymeal is None
            else selected_soymeal.source_function
        ),
        soyoil_price_cny_per_tonne=(
            None
            if selected_soyoil is None
            else selected_soyoil.price_cny_per_tonne
        ),
        soyoil_price_type=(
            None if selected_soyoil is None else selected_soyoil.price_type
        ),
        soyoil_source=(
            None if selected_soyoil is None else selected_soyoil.source
        ),
        soyoil_source_function=(
            None
            if selected_soyoil is None
            else selected_soyoil.source_function
        ),
        snapshot_status=status,
        missing_reasons=missing_reasons,
    )


def snapshot_to_calculation_input(
    snapshot: SoybeanMarketSnapshot,
    *,
    config: SoybeanImportProfitConfig,
) -> SoybeanCalculationInput:
    """Convert a complete or incomplete snapshot without filling missing data."""

    if not isinstance(snapshot, SoybeanMarketSnapshot):
        raise MarketSnapshotValidationError(
            "snapshot must be a SoybeanMarketSnapshot"
        )
    if not isinstance(config, SoybeanImportProfitConfig):
        raise MarketSnapshotValidationError(
            "config must be a SoybeanImportProfitConfig"
        )
    if (
        snapshot.parameter_version != str(config.schema_version)
        or snapshot.mapping_identity != config.contract_mapping_identity
    ):
        raise MarketSnapshotValidationError(
            "snapshot configuration identity does not match config"
        )
    return SoybeanCalculationInput(
        business_key=snapshot.business_key,
        cnf_cents_per_bushel=snapshot.cnf_cents_per_bushel,
        cbot_contract=snapshot.mapped_contracts.cbot,
        cbot_daily_price_cents_per_bushel=snapshot.cbot_price_cents_per_bushel,
        fx_value=snapshot.fx_value,
        soymeal_contract=snapshot.mapped_contracts.soymeal,
        soymeal_price_cny_per_tonne=snapshot.soymeal_price_cny_per_tonne,
        soyoil_contract=snapshot.mapped_contracts.soyoil,
        soyoil_price_cny_per_tonne=snapshot.soyoil_price_cny_per_tonne,
        resolved_parameters=config.resolve_parameters(snapshot.business_key.origin),
        mapping_identity=snapshot.mapping_identity,
    )


RecordT = TypeVar("RecordT")
KeyT = TypeVar("KeyT")


def _cnf_index(
    records: Iterable[CnfQuoteRecord | HistoricalCnfMarketPoint],
) -> Mapping[CnfKey, CnfQuoteRecord | HistoricalCnfMarketPoint]:
    indexed: dict[CnfKey, CnfQuoteRecord | HistoricalCnfMarketPoint] = {}
    for record in records:
        if not isinstance(record, (CnfQuoteRecord, HistoricalCnfMarketPoint)):
            raise MarketSnapshotValidationError(
                "CNF records must contain CnfQuoteRecord or "
                "HistoricalCnfMarketPoint objects"
            )
        key = business_key_tuple(record.business_key)
        if key in indexed:
            raise MarketSnapshotDuplicateKeyError(
                f"CNF records contain duplicate canonical key {key!r}"
            )
        indexed[key] = record
    return MappingProxyType(indexed)


def _unique_index(
    records: Iterable[RecordT],
    expected_type: type[RecordT],
    key_function,
    label: str,
) -> Mapping:
    indexed = {}
    for record in records:
        if not isinstance(record, expected_type):
            raise MarketSnapshotValidationError(
                f"{label} records must contain {expected_type.__name__} objects"
            )
        key = key_function(record)
        if key in indexed:
            raise MarketSnapshotDuplicateKeyError(
                f"{label} records contain duplicate canonical key {key!r}"
            )
        indexed[key] = record
    return MappingProxyType(indexed)


def _validate_business_key_for_config(
    business_key: BusinessKey,
    config: SoybeanImportProfitConfig,
) -> None:
    if not isinstance(business_key, BusinessKey):
        raise MarketSnapshotValidationError(
            "business_key must be a validated BusinessKey"
        )
    if not isinstance(config, SoybeanImportProfitConfig):
        raise MarketSnapshotValidationError(
            "config must be a SoybeanImportProfitConfig"
        )
    if (
        business_key.commodity != config.commodity
        or business_key.origin not in config.origin_codes
    ):
        raise MarketSnapshotValidationError(
            "business_key is inconsistent with configuration"
        )


def _fx_provenance(
    selection: FxSelection,
    fx_by_key: Mapping[FxKey, FxPricePoint],
    business_date: date,
) -> tuple[FxPointProvenance, ...]:
    if selection.fx_value is None:
        return ()
    tenors = (
        (selection.target_tenor,)
        if not selection.is_interpolated
        else (selection.lower_tenor, selection.upper_tenor)
    )
    result = []
    for tenor in tenors:
        if tenor is None:
            raise MarketSnapshotValidationError(
                "successful FX selection is missing provenance tenor"
            )
        point = fx_by_key[(business_date, tenor)]
        result.append(
            FxPointProvenance(
                tenor_months=point.tenor_months,
                source=point.source,
                source_table=point.source_table,
                source_column=point.source_column,
                source_snapshot_sha256=point.source_snapshot_sha256,
            )
        )
    return tuple(result)


def _shared_value(
    provenance: tuple[FxPointProvenance, ...],
    field_name: str,
) -> str | None:
    values = {getattr(item, field_name) for item in provenance}
    return next(iter(values)) if len(values) == 1 else None


def _require_real_date(value: object, field_name: str) -> None:
    if type(value) is not date:
        raise MarketSnapshotValidationError(
            f"{field_name} must be a real date"
        )


def _finite_number(value: object, field_name: str, *, positive: bool) -> float:
    if isinstance(value, (bool, str, bytes)) or not isinstance(value, Real):
        raise MarketSnapshotValidationError(
            f"{field_name} must be a finite number"
        )
    result = float(value)
    if not isfinite(result) or (positive and result <= 0):
        qualifier = "positive and finite" if positive else "finite"
        raise MarketSnapshotValidationError(
            f"{field_name} must be {qualifier}"
        )
    return result


def _require_non_empty(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise MarketSnapshotValidationError(
            f"{field_name} must be non-empty"
        )


def _require_bool(value: object, field_name: str) -> None:
    if type(value) is not bool:
        raise MarketSnapshotValidationError(f"{field_name} must be boolean")
