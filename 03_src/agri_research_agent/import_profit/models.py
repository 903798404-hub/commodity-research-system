"""Immutable business models for imported commodity profit calculations."""

from __future__ import annotations

from dataclasses import InitVar, dataclass, field
from datetime import date
from enum import StrEnum
from math import isfinite
from typing import Collection


MIN_BUSINESS_YEAR = 1900
MAX_BUSINESS_YEAR = 2199
MIN_DCE_CONTRACT_YEAR = 2000
MAX_DCE_CONTRACT_YEAR = 2099


class MissingReason(StrEnum):
    MISSING_CNF = "missing_cnf"
    MISSING_CBOT = "missing_cbot"
    MISSING_FX = "missing_fx"
    MISSING_SOYMEAL = "missing_soymeal"
    MISSING_SOYOIL = "missing_soyoil"
    MISSING_OVERRIDE_CBOT = "override_cbot_contract_price_missing"
    MISSING_OVERRIDE_SOYMEAL = "override_soymeal_contract_price_missing"
    MISSING_OVERRIDE_SOYOIL = "override_soyoil_contract_price_missing"
    INVALID_BUSINESS_KEY = "invalid_business_key"
    INVALID_CONTRACT_MAPPING = "invalid_contract_mapping"
    INVALID_PARAMETER = "invalid_parameter"
    INVALID_PRICE = "invalid_price"
    FX_TENOR_OUT_OF_RANGE = "fx_tenor_out_of_range"
    FX_INTERPOLATION_UNAVAILABLE = "fx_interpolation_unavailable"


class CalculationStatus(StrEnum):
    SUCCESS = "success"
    INCOMPLETE = "incomplete"


class FxSelectionStatus(StrEnum):
    DIRECT = "direct"
    INTERPOLATED = "interpolated"
    INTERPOLATION_UNAVAILABLE = "interpolation_unavailable"
    DATE_MISMATCH = "date_mismatch"


class ImportProfitError(ValueError):
    """Base error carrying a stable business reason."""

    def __init__(self, message: str, reason: MissingReason) -> None:
        super().__init__(message)
        self.reason = reason


class BusinessKeyError(ImportProfitError):
    pass


class ContractMappingError(ImportProfitError):
    pass


class InvalidParameterError(ImportProfitError):
    pass


class InvalidPriceError(ImportProfitError):
    pass


class FxTenorError(ImportProfitError):
    pass


def require_finite_number(
    value: object,
    *,
    field_name: str,
    positive: bool,
    reason: MissingReason,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        error_type = InvalidPriceError if reason is MissingReason.INVALID_PRICE else InvalidParameterError
        raise error_type(f"{field_name} must be a finite number", reason)
    result = float(value)
    if not isfinite(result) or (positive and result <= 0):
        error_type = InvalidPriceError if reason is MissingReason.INVALID_PRICE else InvalidParameterError
        qualifier = "positive and finite" if positive else "finite"
        raise error_type(f"{field_name} must be {qualifier}", reason)
    return result


@dataclass(frozen=True, slots=True)
class BusinessKey:
    business_date: date
    commodity: str
    origin: str
    shipment_year: int
    shipment_month: int
    allowed_origins: InitVar[Collection[str]]
    expected_commodity: InitVar[str]
    expected_shipment_period: InitVar[str | None] = None

    def __post_init__(
        self,
        allowed_origins: Collection[str],
        expected_commodity: str,
        expected_shipment_period: str | None,
    ) -> None:
        if type(self.business_date) is not date:
            raise BusinessKeyError("business_date must be a real date", MissingReason.INVALID_BUSINESS_KEY)
        if not isinstance(self.commodity, str) or not self.commodity.strip():
            raise BusinessKeyError("commodity must be non-empty", MissingReason.INVALID_BUSINESS_KEY)
        if self.commodity != expected_commodity:
            raise BusinessKeyError(
                f"commodity must equal configured commodity {expected_commodity!r}",
                MissingReason.INVALID_BUSINESS_KEY,
            )
        if not isinstance(self.origin, str) or self.origin not in frozenset(allowed_origins):
            raise BusinessKeyError("origin is not allowed by configuration", MissingReason.INVALID_BUSINESS_KEY)
        if isinstance(self.shipment_year, bool) or not isinstance(self.shipment_year, int):
            raise BusinessKeyError("shipment_year must be an integer", MissingReason.INVALID_BUSINESS_KEY)
        if not MIN_BUSINESS_YEAR <= self.shipment_year <= MAX_BUSINESS_YEAR:
            raise BusinessKeyError(
                f"shipment_year must be between {MIN_BUSINESS_YEAR} and {MAX_BUSINESS_YEAR}",
                MissingReason.INVALID_BUSINESS_KEY,
            )
        if isinstance(self.shipment_month, bool) or not isinstance(self.shipment_month, int):
            raise BusinessKeyError("shipment_month must be an integer", MissingReason.INVALID_BUSINESS_KEY)
        if not 1 <= self.shipment_month <= 12:
            raise BusinessKeyError("shipment_month must be between 1 and 12", MissingReason.INVALID_BUSINESS_KEY)
        if expected_shipment_period is not None and expected_shipment_period != self.shipment_period:
            raise BusinessKeyError(
                "shipment_period does not match shipment_year and shipment_month",
                MissingReason.INVALID_BUSINESS_KEY,
            )

    @property
    def shipment_period(self) -> str:
        return f"{self.shipment_year:04d}-{self.shipment_month:02d}"


@dataclass(frozen=True, slots=True)
class CbotContract:
    contract_year: int
    contract_month: int
    market: str = field(default="CBOT", init=False)
    commodity: str = field(default="soybean", init=False)

    def __post_init__(self) -> None:
        _validate_contract_year_month(self.contract_year, self.contract_month, dce=False)

    @property
    def label(self) -> str:
        return f"{self.contract_year:04d}-{self.contract_month:02d}"


@dataclass(frozen=True, slots=True)
class DceContract:
    symbol: str
    commodity: str
    contract_year: int
    contract_month: int
    market: str = field(default="DCE", init=False)

    def __post_init__(self) -> None:
        if self.symbol not in {"M", "Y"}:
            raise ContractMappingError("DCE symbol must be M or Y", MissingReason.INVALID_CONTRACT_MAPPING)
        expected_commodity = {"M": "soymeal", "Y": "soyoil"}[self.symbol]
        if self.commodity != expected_commodity:
            raise ContractMappingError(
                f"{self.symbol} contract commodity must be {expected_commodity}",
                MissingReason.INVALID_CONTRACT_MAPPING,
            )
        _validate_contract_year_month(self.contract_year, self.contract_month, dce=True)

    @property
    def code(self) -> str:
        return f"{self.symbol}{self.contract_year % 100:02d}{self.contract_month:02d}"

    @classmethod
    def soymeal(cls, contract_year: int, contract_month: int) -> DceContract:
        return cls("M", "soymeal", contract_year, contract_month)

    @classmethod
    def soyoil(cls, contract_year: int, contract_month: int) -> DceContract:
        return cls("Y", "soyoil", contract_year, contract_month)


def _validate_contract_year_month(year: int, month: int, *, dce: bool) -> None:
    if isinstance(year, bool) or not isinstance(year, int):
        raise ContractMappingError("contract_year must be an integer", MissingReason.INVALID_CONTRACT_MAPPING)
    minimum = MIN_DCE_CONTRACT_YEAR if dce else MIN_BUSINESS_YEAR
    maximum = MAX_DCE_CONTRACT_YEAR if dce else MAX_BUSINESS_YEAR
    if not minimum <= year <= maximum:
        raise ContractMappingError(
            f"contract_year must be between {minimum} and {maximum}",
            MissingReason.INVALID_CONTRACT_MAPPING,
        )
    if isinstance(month, bool) or not isinstance(month, int) or not 1 <= month <= 12:
        raise ContractMappingError(
            "contract_month must be between 1 and 12",
            MissingReason.INVALID_CONTRACT_MAPPING,
        )


@dataclass(frozen=True, slots=True)
class MappedContracts:
    cbot: CbotContract
    soymeal: DceContract
    soyoil: DceContract
    mapping_identity: str
    mapping_hash: str


@dataclass(frozen=True, slots=True)
class FxCurvePoint:
    market_date: date
    tenor_months: int
    value: float
    source: str
    source_identity: str | None = None

    def __post_init__(self) -> None:
        if type(self.market_date) is not date:
            raise InvalidParameterError("market_date must be a real date", MissingReason.INVALID_PARAMETER)
        if isinstance(self.tenor_months, bool) or not isinstance(self.tenor_months, int):
            raise FxTenorError("tenor_months must be an integer", MissingReason.FX_TENOR_OUT_OF_RANGE)
        if not 0 <= self.tenor_months <= 12:
            raise FxTenorError("tenor_months must be between 0 and 12", MissingReason.FX_TENOR_OUT_OF_RANGE)
        object.__setattr__(
            self,
            "value",
            require_finite_number(
                self.value,
                field_name="FX value",
                positive=True,
                reason=MissingReason.INVALID_PRICE,
            ),
        )
        if not isinstance(self.source, str) or not self.source.strip():
            raise InvalidParameterError("FX source must be non-empty", MissingReason.INVALID_PARAMETER)


@dataclass(frozen=True, slots=True)
class FxCurve:
    market_date: date
    points: tuple[FxCurvePoint, ...]

    def __post_init__(self) -> None:
        if type(self.market_date) is not date:
            raise InvalidParameterError("market_date must be a real date", MissingReason.INVALID_PARAMETER)
        if any(point.market_date != self.market_date for point in self.points):
            raise InvalidParameterError(
                "all FX points must belong to the curve market_date",
                MissingReason.INVALID_PARAMETER,
            )
        tenors = [point.tenor_months for point in self.points]
        if len(tenors) != len(set(tenors)):
            raise InvalidParameterError("FX curve tenors must be unique", MissingReason.INVALID_PARAMETER)
        object.__setattr__(self, "points", tuple(sorted(self.points, key=lambda point: point.tenor_months)))


@dataclass(frozen=True, slots=True)
class FxSelection:
    fx_value: float | None
    target_tenor: int
    is_interpolated: bool
    lower_tenor: int | None
    upper_tenor: int | None
    selection_status: FxSelectionStatus
