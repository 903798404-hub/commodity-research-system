"""Lightweight Tankan-only producer for immutable Public AM/PM snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from time import monotonic, sleep
from typing import Callable, Iterable, Mapping, Protocol
from zoneinfo import ZoneInfo

from agri_research_agent.data_sources.tankan import TankanClient
from agri_research_agent.data_sources.tankan.queries import (
    CBOT_SOYBEAN_LIVE_QUERY,
    DCE_SOYMEAL_LIVE_QUERY,
    DCE_SOYOIL_LIVE_QUERY,
    USD_CNH_SPOT_LIVE_QUERY,
)
from agri_research_agent.market_data.calendars import CalendarBusinessDayPolicy
from agri_research_agent.market_data.contracts import ContractId, Exchange
from agri_research_agent.market_data.intraday import (
    FreshnessStatus,
    InstrumentAvailabilityStatus,
    IntradayQuote,
    IntradaySealResult,
    IntradaySnapshot,
    IntradaySnapshotValidationError,
    IntradayUnavailableInstrument,
    MarketSession,
    seal_intraday_snapshot,
)


BEIJING = ZoneInfo("Asia/Shanghai")
AM_WINDOW_START = time(8, 0)
AM_WINDOW_END = time(12, 0)
PM_WINDOW_START = time(15, 0)
PM_WINDOW_END = time(21, 0)


class PublicIntradayCaptureError(IntradaySnapshotValidationError):
    pass


class PublicIntradayNotScheduledError(PublicIntradayCaptureError):
    status = "NOT_SCHEDULED"


class PublicIntradaySourceIncompleteError(PublicIntradayCaptureError):
    status = "SOURCE_INCOMPLETE"


class PublicIntradaySourceStaleError(PublicIntradayCaptureError):
    status = "SOURCE_STALE"


class TankanClientContextFactory(Protocol):
    def __call__(self) -> TankanClient: ...


@dataclass(frozen=True, slots=True)
class RequiredIntradayContracts:
    cbot: tuple[str, ...]
    soymeal: tuple[str, ...]
    soyoil: tuple[str, ...]

    def __post_init__(self) -> None:
        _validate_contract_set(self.cbot, "", "CBOT")
        _validate_contract_set(self.soymeal, "M", "soymeal")
        _validate_contract_set(self.soyoil, "Y", "soyoil")


@dataclass(frozen=True, slots=True)
class CaptureAttempt:
    snapshot: IntradaySnapshot
    elapsed_seconds: float
    source_row_count: int


@dataclass(frozen=True, slots=True)
class CaptureJobResult:
    seal: IntradaySealResult
    attempts: int
    elapsed_seconds: float
    source_row_count: int


def capture_public_intraday_once(
    client: TankanClient,
    *,
    business_date: date,
    session: MarketSession,
    captured_at: datetime,
    required: RequiredIntradayContracts,
    business_day_policy: CalendarBusinessDayPolicy,
    environment: str = "FORMAL",
) -> CaptureAttempt:
    """Read only the required contracts and construct a readiness-gated snapshot."""

    started = monotonic()
    local_capture = _local(captured_at)
    if local_capture.date() != business_date:
        raise PublicIntradayCaptureError("captured_at date does not match business_date")
    decision = business_day_policy.decide(business_date)
    if decision.business_date != business_date or not decision.is_business_day:
        raise PublicIntradayNotScheduledError("business calendar rejected the capture date")
    if environment not in {"FORMAL", "TEST_ISOLATED_NON_PRODUCTION"}:
        raise PublicIntradayCaptureError("unsupported capture environment")
    _require_session_window(session, local_capture.time())

    cbot_rows = _read_rows(client.stream_live(CBOT_SOYBEAN_LIVE_QUERY, required.cbot))
    meal_rows = _read_rows(
        client.stream_live(DCE_SOYMEAL_LIVE_QUERY, tuple(code[1:] for code in required.soymeal))
    )
    oil_rows = _read_rows(
        client.stream_live(DCE_SOYOIL_LIVE_QUERY, tuple(code[1:] for code in required.soyoil))
    )
    fx_rows = _read_rows(client.stream_live(USD_CNH_SPOT_LIVE_QUERY))
    missing_cbot = _missing_exact_rows(cbot_rows, set(required.cbot), "contract", "CBOT")
    missing_meal = _missing_exact_rows(
        meal_rows, {code[1:] for code in required.soymeal}, "contract", "DCE M"
    )
    missing_oil = _missing_exact_rows(
        oil_rows, {code[1:] for code in required.soyoil}, "contract", "DCE Y"
    )
    if len(fx_rows) != 1 or str(fx_rows[0].get("tenor", "")).lower() != "spot":
        raise PublicIntradaySourceIncompleteError("USD/CNH SPOT row is missing or duplicated")

    quotes: list[IntradayQuote] = []
    unavailable: list[IntradayUnavailableInstrument] = []
    query_identity = CBOT_SOYBEAN_LIVE_QUERY.identity()
    for row in cbot_rows:
        code = str(row["contract"])
        _require_not_expired(code, business_date, "CBOT")
        updated = _source_time(row.get("update_time"))
        _require_current_business_date(updated, business_date, "CBOT")
        year, month = _year_month(code)
        quotes.append(
            IntradayQuote(
                business_date=business_date,
                session=session,
                captured_at=local_capture,
                instrument_id=str(ContractId(Exchange.CBOT, "SOYBEAN", year, month)),
                contract_code=code,
                exchange="CBOT",
                product="SOYBEAN",
                price=_price(row.get("last"), f"CBOT {code}"),
                quote_type="LAST",
                currency="USD",
                unit="US_CENTS_PER_BUSHEL",
                source_system="TANKAN",
                source_table="market.foreign_futures_live",
                source_updated_at=updated,
                source_trade_date=None,
                freshness_status=FreshnessStatus.FRESH,
                provenance={
                    "query": query_identity,
                    "source_exchange": row.get("exchange"),
                    "source_product_name": row.get("product_name"),
                    "ric": row.get("ric"),
                    "source_trade_date_status": "NOT_PROVIDED_BY_LIVE_TABLE",
                },
            )
        )
    unavailable.extend(
        _unavailable_contract(
            code,
            exchange="CBOT",
            product="SOYBEAN",
            instrument_id=str(ContractId(Exchange.CBOT, "SOYBEAN", *_year_month(code))),
            source_table="market.foreign_futures_live",
            query_identity=query_identity,
        )
        for code in sorted(missing_cbot)
    )
    quotes.extend(
        _dce_quotes(
            meal_rows,
            required.soymeal,
            business_date,
            session,
            local_capture,
            product="SOYMEAL",
            symbol="M",
            query_identity=DCE_SOYMEAL_LIVE_QUERY.identity(),
        )
    )
    unavailable.extend(
        _unavailable_contract(
            f"M{code}",
            exchange="DCE",
            product="SOYMEAL",
            instrument_id=str(ContractId(Exchange.DCE, "SOYMEAL", *_year_month(code))),
            source_table="market.futures_live",
            query_identity=DCE_SOYMEAL_LIVE_QUERY.identity(),
        )
        for code in sorted(missing_meal)
    )
    unavailable.extend(
        _unavailable_contract(
            f"Y{code}",
            exchange="DCE",
            product="SOYOIL",
            instrument_id=str(ContractId(Exchange.DCE, "SOYOIL", *_year_month(code))),
            source_table="market.futures_live",
            query_identity=DCE_SOYOIL_LIVE_QUERY.identity(),
        )
        for code in sorted(missing_oil)
    )
    quotes.extend(
        _dce_quotes(
            oil_rows,
            required.soyoil,
            business_date,
            session,
            local_capture,
            product="SOYOIL",
            symbol="Y",
            query_identity=DCE_SOYOIL_LIVE_QUERY.identity(),
        )
    )
    fx = fx_rows[0]
    fx_updated = _source_time(fx.get("update_time"))
    _require_current_business_date(fx_updated, business_date, "FX")
    value_date = fx.get("value_date")
    if type(value_date) is not date or value_date != business_date:
        raise PublicIntradaySourceStaleError("USD/CNH value_date does not match business_date")
    quotes.append(
        IntradayQuote(
            business_date=business_date,
            session=session,
            captured_at=local_capture,
            instrument_id="FX:USD/CNH:SPOT",
            contract_code="",
            exchange="OTC",
            product="USD/CNH",
            price=_price(fx.get("mid"), "USD/CNH MID"),
            quote_type="MID",
            currency="CNH",
            unit="CNH_PER_USD",
            source_system="TANKAN",
            source_table="market.exchange_rate_live",
            source_updated_at=fx_updated,
            source_trade_date=value_date,
            freshness_status=FreshnessStatus.FRESH,
            provenance={
                "query": USD_CNH_SPOT_LIVE_QUERY.identity(),
                "tenor": "spot",
                "bid": _price(fx.get("bid"), "USD/CNH BID"),
                "ask": _price(fx.get("ask"), "USD/CNH ASK"),
                "mid": _price(fx.get("mid"), "USD/CNH MID"),
            },
        )
    )
    snapshot = IntradaySnapshot(
        business_date=business_date,
        session=session,
        captured_at=local_capture,
        quotes=tuple(quotes),
        calendar_policy=decision.policy_name,
        calendar_source_identity=decision.source_identity,
        environment=environment,
        unavailable_instruments=tuple(unavailable),
    )
    return CaptureAttempt(snapshot, monotonic() - started, len(cbot_rows) + len(meal_rows) + len(oil_rows) + 1)


def run_public_intraday_capture_job(
    client_factory: TankanClientContextFactory,
    *,
    store_root: str,
    business_date: date,
    session: MarketSession,
    required: RequiredIntradayContracts,
    business_day_policy: CalendarBusinessDayPolicy,
    clock: Callable[[], datetime],
    retry_interval_seconds: float = 30.0,
    max_attempts: int = 21,
    environment: str = "FORMAL",
    sleeper: Callable[[float], None] = sleep,
) -> CaptureJobResult:
    """Run a finite readiness loop; each attempt owns a short-lived connection."""

    if max_attempts < 1 or retry_interval_seconds < 0:
        raise ValueError("retry policy is invalid")
    started = monotonic()
    last_error: PublicIntradayCaptureError | None = None
    for attempt_number in range(1, max_attempts + 1):
        captured_at = clock()
        try:
            with client_factory() as client:
                attempt = capture_public_intraday_once(
                    client,
                    business_date=business_date,
                    session=session,
                    captured_at=captured_at,
                    required=required,
                    business_day_policy=business_day_policy,
                    environment=environment,
                )
            sealed = seal_intraday_snapshot(store_root, attempt.snapshot)
            return CaptureJobResult(
                sealed,
                attempt_number,
                monotonic() - started,
                attempt.source_row_count,
            )
        except (PublicIntradaySourceIncompleteError, PublicIntradaySourceStaleError) as exc:
            last_error = exc
            if attempt_number == max_attempts or _past_deadline(session, _local(captured_at).time(), environment):
                break
            sleeper(retry_interval_seconds)
    assert last_error is not None
    raise last_error


def _dce_quotes(
    rows: tuple[Mapping[str, object], ...],
    required_codes: tuple[str, ...],
    business_date: date,
    session: MarketSession,
    captured_at: datetime,
    *,
    product: str,
    symbol: str,
    query_identity: Mapping[str, str],
) -> list[IntradayQuote]:
    required_by_suffix = {code[1:]: code for code in required_codes}
    output: list[IntradayQuote] = []
    for row in rows:
        suffix = str(row["contract"])
        code = required_by_suffix[suffix]
        _require_not_expired(suffix, business_date, f"DCE {symbol}")
        updated = _source_time(row.get("update_time"))
        _require_current_business_date(updated, business_date, f"DCE {symbol}")
        year, month = _year_month(suffix)
        output.append(
            IntradayQuote(
                business_date=business_date,
                session=session,
                captured_at=captured_at,
                instrument_id=str(ContractId(Exchange.DCE, product, year, month)),
                contract_code=code,
                exchange="DCE",
                product=product,
                price=_price(row.get("last"), f"DCE {code}"),
                quote_type="LAST",
                currency="CNY",
                unit="CNY_PER_METRIC_TONNE",
                source_system="TANKAN",
                source_table="market.futures_live",
                source_updated_at=updated,
                source_trade_date=None,
                freshness_status=FreshnessStatus.FRESH,
                provenance={
                    "query": dict(query_identity),
                    "source_product_name": row.get("product_name"),
                    "source_contract": suffix,
                    "bid": row.get("bid"),
                    "ask": row.get("ask"),
                    "volume": row.get("volume"),
                    "open_interest": row.get("open_interest"),
                    "source_trade_date_status": "NOT_PROVIDED_BY_LIVE_TABLE",
                },
            )
        )
    return output


def _read_rows(batches: Iterable[object]) -> tuple[Mapping[str, object], ...]:
    rows: list[Mapping[str, object]] = []
    for batch in batches:
        rows.extend(batch.rows)
    return tuple(rows)


def _validate_contract_set(values: tuple[str, ...], prefix: str, label: str) -> None:
    if not isinstance(values, tuple) or not values or len(values) != len(set(values)):
        raise PublicIntradayCaptureError(f"{label} contracts must be a non-empty unique tuple")
    for value in values:
        suffix = value[len(prefix):] if prefix and value.startswith(prefix) else value
        if prefix and not value.startswith(prefix):
            raise PublicIntradayCaptureError(f"{label} contract prefix is invalid")
        if len(suffix) != 4 or not suffix.isascii() or not suffix.isdigit():
            raise PublicIntradayCaptureError(f"{label} contracts must be exact YYMM codes")
        _year_month(suffix)


def _missing_exact_rows(
    rows: tuple[Mapping[str, object], ...],
    expected: set[str],
    field: str,
    label: str,
) -> set[str]:
    observed = [str(row.get(field, "")) for row in rows]
    if len(observed) != len(set(observed)):
        raise PublicIntradaySourceIncompleteError(f"{label} exact-contract identity is duplicated")
    unexpected = set(observed) - expected
    if unexpected:
        raise PublicIntradaySourceIncompleteError(f"{label} returned unexpected exact contracts")
    return expected - set(observed)


def _unavailable_contract(
    contract_code: str,
    *,
    exchange: str,
    product: str,
    instrument_id: str,
    source_table: str,
    query_identity: Mapping[str, str],
) -> IntradayUnavailableInstrument:
    return IntradayUnavailableInstrument(
        instrument_id=instrument_id,
        contract_code=contract_code,
        exchange=exchange,
        product=product,
        status=InstrumentAvailabilityStatus.CONTRACT_NOT_AVAILABLE,
        reason="REQUESTED_EXACT_CONTRACT_ABSENT_FROM_LIVE_SOURCE",
        provenance={
            "query": dict(query_identity),
            "source_system": "TANKAN",
            "source_table": source_table,
            "listing_status": "UNVERIFIED",
        },
    )


def _source_time(value: object) -> datetime:
    if not isinstance(value, datetime):
        raise PublicIntradaySourceIncompleteError("source update_time is missing")
    return value.replace(tzinfo=BEIJING) if value.tzinfo is None else value.astimezone(BEIJING)


def _require_current_business_date(
    source_updated_at: datetime,
    business_date: date,
    label: str,
) -> None:
    if source_updated_at.date() != business_date:
        raise PublicIntradaySourceStaleError(f"{label} source date is not the business date")


def _require_not_expired(code: str, business_date: date, label: str) -> None:
    year, month = _year_month(code)
    if (year, month) < (business_date.year, business_date.month):
        raise PublicIntradaySourceStaleError(f"{label} requested contract is expired")


def _year_month(code: str) -> tuple[int, int]:
    year, month = 2000 + int(code[:2]), int(code[2:])
    if not 1 <= month <= 12:
        raise PublicIntradayCaptureError("contract month is invalid")
    return year, month


def _price(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PublicIntradaySourceIncompleteError(f"{label} price is missing")
    result = float(value)
    if not result > 0 or result == float("inf"):
        raise PublicIntradaySourceIncompleteError(f"{label} price is invalid")
    return result


def _local(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise PublicIntradayCaptureError("captured_at must be timezone-aware")
    return value.astimezone(BEIJING)


def _require_session_window(session: MarketSession, value: time) -> None:
    start, end = (
        (AM_WINDOW_START, AM_WINDOW_END)
        if session is MarketSession.AM
        else (PM_WINDOW_START, PM_WINDOW_END)
    )
    if not start <= value < end:
        raise PublicIntradayCaptureError(f"{session.value} capture is outside its manual window")


def _past_deadline(session: MarketSession, value: time, environment: str) -> bool:
    del environment
    return value >= (AM_WINDOW_END if session is MarketSession.AM else PM_WINDOW_END)


__all__ = [
    "AM_WINDOW_END", "AM_WINDOW_START", "CaptureAttempt", "CaptureJobResult",
    "PM_WINDOW_END", "PM_WINDOW_START", "PublicIntradayCaptureError",
    "PublicIntradayNotScheduledError", "PublicIntradaySourceIncompleteError",
    "PublicIntradaySourceStaleError", "RequiredIntradayContracts",
    "capture_public_intraday_once", "run_public_intraday_capture_job",
]
