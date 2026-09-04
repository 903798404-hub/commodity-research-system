"""Strict adapter for AkShare DCE night-session-close full-contract snapshots."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, time
from math import isfinite
from numbers import Real
import os
import re
from time import perf_counter
from typing import Any, Callable, Collection, Mapping, Sequence
from zoneinfo import ZoneInfo

import pandas as pd


ADAPTER_VERSION = "4"
SOURCE = "akshare"
SOURCE_FUNCTION = "futures_zh_spot"
PRICE_FIELD = "current_price"
PRICE_TYPE = "night_session_close"
CAPTURE_TIMEZONE = "Asia/Shanghai"
CAPTURE_ZONE = ZoneInfo(CAPTURE_TIMEZONE)
SCHEDULED_TIME = time(8, 30, 0)
CAPTURE_START = SCHEDULED_TIME
CAPTURE_END_EXCLUSIVE = time(8, 33, 0)
QUOTE_TIME_START = time(22, 59, 0)
QUOTE_TIME_END_EXCLUSIVE = time(23, 1, 0)
FULL_CONTRACT_PATTERN = re.compile(r"^([MYmy])(\d{2})(01|05|09)$")
STANDARDIZED_RECORD_FIELDS = (
    "business_date",
    "instrument",
    "commodity",
    "exchange",
    "contract_code",
    "contract_year",
    "contract_month",
    "contract_identity_status",
    "source_contract_code",
    "source_delivery_month",
    "price_cny_per_tonne",
    "price_type",
    "source",
    "source_function",
    "source_quote_date",
    "source_quote_time",
    "quote_date_evidence_status",
    "captured_at",
    "capture_timezone",
    "quality_status",
    "is_usable",
)

SpotFetcher = Callable[[str], pd.DataFrame]
TradeCalendarFetcher = Callable[[], pd.DataFrame]


class DceSpotContractError(ValueError):
    """Raised when an internal full-contract code is invalid."""


@dataclass(frozen=True, slots=True)
class DceFullContract:
    code: str
    instrument: str
    contract_year: int
    contract_month: int

    @property
    def display_symbol(self) -> str:
        prefix = {"soymeal": "豆粕", "soyoil": "豆油"}[self.instrument]
        return f"{prefix}{self.contract_year % 100:02d}{self.contract_month:02d}"


@dataclass(frozen=True, slots=True)
class DceNightSessionCloseRecord:
    business_date: date
    instrument: str
    commodity: str
    exchange: str
    contract_code: str
    contract_year: int
    contract_month: int
    contract_identity_status: str
    source_contract_code: str
    source_delivery_month: int
    price_cny_per_tonne: float
    price_type: str
    source: str
    source_function: str
    source_quote_date: date | None
    source_quote_time: time
    quote_date_evidence_status: str
    captured_at: datetime
    capture_timezone: str
    quality_status: str
    is_usable: bool

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class DceSpotContractResult:
    contract: DceFullContract
    quality_status: str
    is_usable: bool
    record: DceNightSessionCloseRecord | None
    matched_source_symbol: str | None
    source_quote_date: date | None
    source_quote_time: time | None
    quote_date_evidence_status: str | None
    current_price: float | None
    error_type: str | None
    error_message: str | None

    def bounded_quality_dict(self) -> dict[str, Any]:
        return {
            "contract_code": self.contract.code,
            "request_status": self.quality_status,
            "is_usable": self.is_usable,
            "matched_source_symbol": self.matched_source_symbol,
            "source_quote_date": _date_text(self.source_quote_date),
            "source_quote_time": _time_text(self.source_quote_time),
            "quote_date_evidence_status": self.quote_date_evidence_status,
            "current_price": self.current_price,
            "error_type": self.error_type,
            "error_message": self.error_message,
        }


@dataclass(frozen=True, slots=True)
class DceSpotBatchResult:
    requested_contracts: tuple[str, ...]
    request_symbol: str
    elapsed_seconds: float
    returned_fields: tuple[str, ...]
    source_row_count: int
    captured_at: datetime
    capture_gate_status: str
    contract_results: tuple[DceSpotContractResult, ...]
    previous_trading_date: date | None = None
    trade_calendar_source: str | None = None
    source_error_type: str | None = None
    source_error_message: str | None = None

    @property
    def records(self) -> tuple[DceNightSessionCloseRecord, ...]:
        return tuple(
            item.record for item in self.contract_results if item.record is not None
        )

    @property
    def is_usable(self) -> bool:
        return bool(self.contract_results) and all(
            item.is_usable for item in self.contract_results
        )

    @property
    def attempt_status(self) -> str:
        available = sum(item.is_usable for item in self.contract_results)
        if available == len(self.contract_results) and available:
            return "success"
        if available:
            return "passed_with_incomplete"
        return "failed"


# Compatibility aliases for callers that imported the pre-production 09:00 names.
DceMorningOpenRecord = DceNightSessionCloseRecord


def normalize_full_contract_code(value: str) -> DceFullContract:
    if not isinstance(value, str):
        raise DceSpotContractError("contract code must be a string")
    if value != value.strip():
        raise DceSpotContractError("contract code must not contain surrounding whitespace")
    match = FULL_CONTRACT_PATTERN.fullmatch(value)
    if match is None:
        raise DceSpotContractError(
            "contract code must be MYYMM or YYYMM with month 01, 05, or 09"
        )
    symbol, year_text, month_text = match.groups()
    symbol = symbol.upper()
    return DceFullContract(
        code=f"{symbol}{year_text}{month_text}",
        instrument={"M": "soymeal", "Y": "soyoil"}[symbol],
        contract_year=2000 + int(year_text),
        contract_month=int(month_text),
    )


def default_spot_fetcher(batch_symbol: str) -> pd.DataFrame:
    """Call the same public AkShare boundary used by the existing spread updater."""
    import akshare as ak

    return ak.futures_zh_spot(symbol=batch_symbol, market="CF", adjust="0")


def default_trade_calendar_fetcher() -> pd.DataFrame:
    """Load the closest available China trading-day calendar from AkShare."""
    import akshare as ak

    return ak.tool_trade_date_hist_sina()


def capture_gate_status(business_date: date, captured_at: datetime) -> str:
    target = _require_date(business_date)
    local = _as_shanghai(captured_at)
    if target != local.date():
        return "non_current_business_date"
    local_time = local.timetz().replace(tzinfo=None)
    if local_time < CAPTURE_START:
        return "capture_window_not_started"
    if local_time >= CAPTURE_END_EXCLUSIVE:
        return "capture_window_closed"
    return "valid"


def fetch_dce_night_session_close_snapshot(
    contract_codes: Sequence[str],
    business_date: date,
    *,
    fetcher: SpotFetcher = default_spot_fetcher,
    trade_calendar_fetcher: TradeCalendarFetcher | None = None,
    captured_at: datetime | None = None,
    enforce_capture_window: bool = True,
) -> DceSpotBatchResult:
    """Freeze the latest completed M/Y night-session prices at the 08:30 gate."""
    target = _require_date(business_date)
    timestamp = _as_shanghai(captured_at or datetime.now(CAPTURE_ZONE))
    contracts = _normalize_unique_contracts(contract_codes)
    request_symbol = ",".join(contract.code for contract in contracts)
    gate = capture_gate_status(target, timestamp)
    if enforce_capture_window and gate != "valid":
        return DceSpotBatchResult(
            requested_contracts=tuple(item.code for item in contracts),
            request_symbol=request_symbol,
            elapsed_seconds=0.0,
            returned_fields=(),
            source_row_count=0,
            captured_at=timestamp,
            capture_gate_status=gate,
            contract_results=tuple(
                _failure(item, gate, error_message=gate) for item in contracts
            ),
        )

    try:
        calendar_frame = (trade_calendar_fetcher or default_trade_calendar_fetcher)()
        trading_dates = _trade_dates(calendar_frame)
    except Exception as exc:
        message = _bounded_safe_message(exc)
        return DceSpotBatchResult(
            requested_contracts=tuple(item.code for item in contracts),
            request_symbol=request_symbol,
            elapsed_seconds=0.0,
            returned_fields=(),
            source_row_count=0,
            captured_at=timestamp,
            capture_gate_status="trade_calendar_error",
            contract_results=tuple(
                _failure(
                    item,
                    "trade_calendar_error",
                    error_type=type(exc).__name__,
                    error_message=message,
                )
                for item in contracts
            ),
            trade_calendar_source="akshare.tool_trade_date_hist_sina",
            source_error_type=type(exc).__name__,
            source_error_message=message,
        )
    if target not in trading_dates:
        return DceSpotBatchResult(
            requested_contracts=tuple(item.code for item in contracts),
            request_symbol=request_symbol,
            elapsed_seconds=0.0,
            returned_fields=(),
            source_row_count=0,
            captured_at=timestamp,
            capture_gate_status="non_trading_business_date",
            contract_results=tuple(
                _failure(
                    item,
                    "non_trading_business_date",
                    error_message="business_date is absent from the trading calendar",
                )
                for item in contracts
            ),
            trade_calendar_source="akshare.tool_trade_date_hist_sina",
        )
    previous_trading_date = max((item for item in trading_dates if item < target), default=None)
    if previous_trading_date is None:
        return DceSpotBatchResult(
            requested_contracts=tuple(item.code for item in contracts),
            request_symbol=request_symbol,
            elapsed_seconds=0.0,
            returned_fields=(),
            source_row_count=0,
            captured_at=timestamp,
            capture_gate_status=gate,
            contract_results=tuple(
                _failure(
                    item,
                    "missing_previous_trading_date",
                    error_message=(
                        "trade calendar does not contain the expected night-session date"
                    ),
                )
                for item in contracts
            ),
            trade_calendar_source="akshare.tool_trade_date_hist_sina",
        )

    started = perf_counter()
    try:
        frame = fetcher(request_symbol)
    except Exception as exc:
        elapsed = perf_counter() - started
        message = _bounded_safe_message(exc)
        return DceSpotBatchResult(
            requested_contracts=tuple(item.code for item in contracts),
            request_symbol=request_symbol,
            elapsed_seconds=elapsed,
            returned_fields=(),
            source_row_count=0,
            captured_at=timestamp,
            capture_gate_status=gate,
            contract_results=tuple(
                _failure(
                    item,
                    "source_error",
                    error_type=type(exc).__name__,
                    error_message=message,
                )
                for item in contracts
            ),
            previous_trading_date=previous_trading_date,
            trade_calendar_source="akshare.tool_trade_date_hist_sina",
            source_error_type=type(exc).__name__,
            source_error_message=message,
        )
    elapsed = perf_counter() - started
    if not isinstance(frame, pd.DataFrame):
        return _schema_failure_batch(
            contracts, request_symbol, timestamp, gate, elapsed, (), 0,
            "fetcher result must be a pandas DataFrame",
        )
    fields = tuple(str(column).strip() for column in frame.columns)
    normalized = frame.copy()
    normalized.columns = list(fields)
    required = {"symbol", "time", "current_price"}
    if not required.issubset(fields) or len(fields) != len(set(fields)):
        return _schema_failure_batch(
            contracts, request_symbol, timestamp, gate, elapsed, fields, len(frame),
            "source requires unambiguous symbol, time, and current_price fields",
        )

    results = tuple(
        _adapt_contract(
            normalized,
            contract,
            target,
            previous_trading_date,
            timestamp,
        )
        for contract in contracts
    )
    return DceSpotBatchResult(
        requested_contracts=tuple(item.code for item in contracts),
        request_symbol=request_symbol,
        elapsed_seconds=elapsed,
        returned_fields=fields,
        source_row_count=len(frame),
        captured_at=timestamp,
        capture_gate_status=gate,
        contract_results=results,
        previous_trading_date=previous_trading_date,
        trade_calendar_source="akshare.tool_trade_date_hist_sina",
    )


# Import compatibility only; all formal callers use the night-session name.
fetch_dce_morning_open_snapshot = fetch_dce_night_session_close_snapshot


def _adapt_contract(
    frame: pd.DataFrame,
    contract: DceFullContract,
    business_date: date,
    expected_night_session_date: date,
    captured_at: datetime,
) -> DceSpotContractResult:
    normalized_symbols = frame["symbol"].map(_normalize_source_symbol)
    matches = frame[normalized_symbols == contract.code]
    if matches.empty:
        return _failure(contract, "missing_contract", error_message="contract not found")
    if len(matches) > 1:
        return _failure(
            contract,
            "duplicate_contract",
            error_message="contract occurs more than once",
        )
    row = matches.iloc[0]
    source_symbol = str(row["symbol"]).strip()
    quote_time = _parse_quote_time(row["time"])
    if quote_time is None:
        return _failure(
            contract,
            "source_schema_error",
            matched_source_symbol=source_symbol,
            error_message="time field is missing or invalid",
        )
    if quote_time < QUOTE_TIME_START:
        return _failure(
            contract,
            "quote_time_before_window",
            matched_source_symbol=source_symbol,
            source_quote_time=quote_time,
            error_message="quote time precedes the night-session close window",
        )
    if quote_time >= QUOTE_TIME_END_EXCLUSIVE:
        return _failure(
            contract,
            "quote_time_outside_allowed_range",
            matched_source_symbol=source_symbol,
            source_quote_time=quote_time,
            error_message="quote time is outside the night-session close window",
        )
    quote_date: date | None = None
    quote_date_evidence_status = "time_only_unconfirmed"
    quality_status = "quote_date_unconfirmed"
    usable = False
    if "date" in frame.columns and not pd.isna(row["date"]):
        quote_date = _parse_quote_date(row["date"])
        if quote_date is None:
            return _failure(
                contract,
                "source_schema_error",
                matched_source_symbol=source_symbol,
                source_quote_time=quote_time,
                quote_date_evidence_status=None,
                error_message="date field is invalid",
            )
        if quote_date != expected_night_session_date:
            quote_date_evidence_status = "date_mismatch"
            quality_status = "stale_quote_date"
        else:
            quote_date_evidence_status = "source_confirmed"
            quality_status = "valid"
            usable = True
    price = row["current_price"]
    if not _valid_price(price):
        return _failure(
            contract,
            "invalid_current_price",
            matched_source_symbol=source_symbol,
            source_quote_date=quote_date,
            source_quote_time=quote_time,
            error_message="current_price must be a finite positive numeric value",
        )
    numeric_price = float(price)
    record = DceNightSessionCloseRecord(
        business_date=business_date,
        instrument=contract.instrument,
        commodity="soybean",
        exchange="DCE",
        contract_code=contract.code,
        contract_year=contract.contract_year,
        contract_month=contract.contract_month,
        contract_identity_status="source_confirmed_exact",
        source_contract_code=contract.code,
        source_delivery_month=contract.contract_month,
        price_cny_per_tonne=numeric_price,
        price_type=PRICE_TYPE,
        source=SOURCE,
        source_function=SOURCE_FUNCTION,
        source_quote_date=quote_date,
        source_quote_time=quote_time,
        quote_date_evidence_status=quote_date_evidence_status,
        captured_at=captured_at,
        capture_timezone=CAPTURE_TIMEZONE,
        quality_status=quality_status,
        is_usable=usable,
    )
    return DceSpotContractResult(
        contract=contract,
        quality_status=record.quality_status,
        is_usable=usable,
        record=record,
        matched_source_symbol=source_symbol,
        source_quote_date=quote_date,
        source_quote_time=quote_time,
        quote_date_evidence_status=quote_date_evidence_status,
        current_price=numeric_price,
        error_type=None,
        error_message=None,
    )


def _normalize_unique_contracts(values: Sequence[str]) -> tuple[DceFullContract, ...]:
    if not values:
        raise DceSpotContractError("at least one contract code is required")
    contracts = [normalize_full_contract_code(value) for value in values]
    codes = [item.code for item in contracts]
    if len(codes) != len(set(codes)):
        raise DceSpotContractError("requested contract codes must be unique")
    return tuple(contracts)


def _normalize_source_symbol(value: object) -> str | None:
    if pd.isna(value):
        return None
    text = str(value).strip().upper()
    if FULL_CONTRACT_PATTERN.fullmatch(text):
        return normalize_full_contract_code(text).code
    for chinese, prefix in (("豆粕", "M"), ("豆油", "Y")):
        if text.startswith(chinese):
            candidate = prefix + text[len(chinese):]
            if FULL_CONTRACT_PATTERN.fullmatch(candidate):
                return normalize_full_contract_code(candidate).code
    return None


def _parse_quote_time(value: object) -> time | None:
    if pd.isna(value) or isinstance(value, bool):
        return None
    if isinstance(value, time):
        return value.replace(tzinfo=None)
    if isinstance(value, Real):
        text = str(int(value)).zfill(6)
    else:
        text = str(value).strip()
        if re.fullmatch(r"\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?", text):
            try:
                return time.fromisoformat(text)
            except ValueError:
                return None
        text = text.replace(":", "")
    if not re.fullmatch(r"\d{6}", text):
        return None
    try:
        return time(int(text[:2]), int(text[2:4]), int(text[4:6]))
    except ValueError:
        return None


def _parse_quote_date(value: object) -> date | None:
    try:
        parsed = pd.to_datetime(value, errors="raise")
    except (TypeError, ValueError):
        return None
    if pd.isna(parsed):
        return None
    return parsed.date()


def _trade_dates(frame: pd.DataFrame) -> frozenset[date]:
    if not isinstance(frame, pd.DataFrame) or "trade_date" not in frame.columns:
        raise ValueError("trade calendar requires a trade_date column")
    parsed: set[date] = set()
    for value in frame["trade_date"]:
        item = _parse_quote_date(value)
        if item is None:
            raise ValueError("trade calendar contains an invalid trade_date")
        parsed.add(item)
    if not parsed:
        raise ValueError("trade calendar is empty")
    return frozenset(parsed)


def _valid_price(value: object) -> bool:
    return (
        not isinstance(value, (bool, str, bytes))
        and isinstance(value, Real)
        and isfinite(float(value))
        and float(value) > 0
    )


def _failure(
    contract: DceFullContract,
    status: str,
    *,
    matched_source_symbol: str | None = None,
    source_quote_date: date | None = None,
    source_quote_time: time | None = None,
    quote_date_evidence_status: str | None = None,
    error_type: str | None = None,
    error_message: str | None = None,
) -> DceSpotContractResult:
    return DceSpotContractResult(
        contract=contract,
        quality_status=status,
        is_usable=False,
        record=None,
        matched_source_symbol=matched_source_symbol,
        source_quote_date=source_quote_date,
        source_quote_time=source_quote_time,
        quote_date_evidence_status=quote_date_evidence_status,
        current_price=None,
        error_type=error_type,
        error_message=error_message,
    )


def _schema_failure_batch(
    contracts: tuple[DceFullContract, ...],
    request_symbol: str,
    captured_at: datetime,
    gate: str,
    elapsed: float,
    fields: tuple[str, ...],
    rows: int,
    message: str,
) -> DceSpotBatchResult:
    return DceSpotBatchResult(
        requested_contracts=tuple(item.code for item in contracts),
        request_symbol=request_symbol,
        elapsed_seconds=elapsed,
        returned_fields=fields,
        source_row_count=rows,
        captured_at=captured_at,
        capture_gate_status=gate,
        contract_results=tuple(
            _failure(
                item,
                "source_schema_error",
                error_type="SourceSchemaError",
                error_message=message,
            )
            for item in contracts
        ),
        source_error_type="SourceSchemaError",
        source_error_message=message,
    )


def _as_shanghai(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("captured_at must be a timezone-aware datetime")
    return value.astimezone(CAPTURE_ZONE)


def _require_date(value: date) -> date:
    if type(value) is not date:
        raise TypeError("business_date must be a real date")
    return value


def _bounded_safe_message(exc: BaseException) -> str:
    message = str(exc).replace("\r", " ").replace("\n", " ")
    message = re.sub(
        r"(?i)(?:https?|socks5h?)://[^\s,;]+",
        "[redacted_network_endpoint]",
        message,
    )
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
        value = os.environ.get(name)
        if value:
            message = message.replace(value, "[redacted_proxy_value]")
    return message[:500]


def _date_text(value: date | None) -> str | None:
    return value.isoformat() if value is not None else None


def _time_text(value: time | None) -> str | None:
    return value.isoformat() if value is not None else None


def records_as_dicts(
    records: Sequence[DceNightSessionCloseRecord],
) -> list[Mapping[str, Any]]:
    return [record.as_dict() for record in records]
