from __future__ import annotations

from datetime import date, datetime, time

import akshare
import pandas as pd
import pytest

from agri_research_agent.import_profit import dce_daily


TARGET = date(2026, 7, 28)
PREVIOUS_TRADING_DATE = date(2026, 7, 27)
CAPTURED = datetime(2026, 7, 28, 8, 30, 30, tzinfo=dce_daily.CAPTURE_ZONE)


@pytest.fixture(autouse=True)
def stable_trade_calendar(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        dce_daily,
        "default_trade_calendar_fetcher",
        lambda: pd.DataFrame(
            {"trade_date": [PREVIOUS_TRADING_DATE, TARGET]}
        ),
    )


def spot_frame(
    symbols: list[object] | None = None,
    prices: list[object] | None = None,
    times: list[object] | None = None,
    **extra: list[object],
) -> pd.DataFrame:
    symbols = symbols or ["豆粕2701", "豆油2701"]
    prices = prices or [3010.0, 8010.0]
    times = times or ["23:00:00", 230000]
    dates = extra.pop("date", [PREVIOUS_TRADING_DATE] * len(symbols))
    return pd.DataFrame(
        {
            "symbol": symbols,
            "time": times,
            "current_price": prices,
            "date": dates,
            **extra,
        }
    )


@pytest.mark.parametrize(
    ("raw", "code", "instrument", "year", "month", "display"),
    [
        ("M2701", "M2701", "soymeal", 2027, 1, "豆粕2701"),
        ("Y2705", "Y2705", "soyoil", 2027, 5, "豆油2705"),
        ("m2709", "M2709", "soymeal", 2027, 9, "豆粕2709"),
        ("y0001", "Y0001", "soyoil", 2000, 1, "豆油0001"),
        ("M9909", "M9909", "soymeal", 2099, 9, "豆粕9909"),
    ],
)
def test_full_contract_normalization(
    raw: str,
    code: str,
    instrument: str,
    year: int,
    month: int,
    display: str,
) -> None:
    actual = dce_daily.normalize_full_contract_code(raw)
    assert (
        actual.code,
        actual.instrument,
        actual.contract_year,
        actual.contract_month,
        actual.display_symbol,
    ) == (code, instrument, year, month, display)


@pytest.mark.parametrize(
    "raw",
    [
        "A2701",
        "M270",
        "M27010",
        "M0",
        "Y0",
        "MMAIN",
        "M27",
        "M2712",
        "M2703",
        "M2701.DCE",
        " M2701",
        "M2701 ",
        "",
    ],
)
def test_invalid_contract_codes_are_rejected(raw: str) -> None:
    with pytest.raises(dce_daily.DceSpotContractError):
        dce_daily.normalize_full_contract_code(raw)


def test_multi_contract_snapshot_produces_strict_records() -> None:
    result = dce_daily.fetch_dce_morning_open_snapshot(
        ["M2701", "Y2701"],
        TARGET,
        fetcher=lambda symbol: spot_frame(
            close=[9999.0, 9999.0],
            settle=[8888.0, 8888.0],
        ),
        captured_at=CAPTURED,
    )
    assert result.request_symbol == "M2701,Y2701"
    assert result.is_usable
    assert [item.contract.code for item in result.contract_results] == [
        "M2701",
        "Y2701",
    ]
    assert [record.price_cny_per_tonne for record in result.records] == [
        3010.0,
        8010.0,
    ]
    record = result.records[0]
    assert tuple(record.as_dict()) == dce_daily.STANDARDIZED_RECORD_FIELDS
    assert record.price_type == "night_session_close"
    assert record.source_function == "futures_zh_spot"
    assert record.capture_timezone == "Asia/Shanghai"
    assert record.source_quote_date == PREVIOUS_TRADING_DATE
    assert record.source_quote_time == time(23, 0)
    assert record.quality_status == "valid"
    assert record.quote_date_evidence_status == "source_confirmed"
    assert result.attempt_status == "success"
    assert result.previous_trading_date == PREVIOUS_TRADING_DATE


def test_return_order_changes_and_extra_contracts_do_not_change_matching() -> None:
    result = dce_daily.fetch_dce_morning_open_snapshot(
        ["M2701", "Y2701"],
        TARGET,
        fetcher=lambda symbol: spot_frame(
            symbols=["A9999", "Y2701", "M2701"],
            prices=[1.0, 8001.0, 3001.0],
            times=["23:00:00"] * 3,
        ),
        captured_at=CAPTURED,
    )
    assert result.is_usable
    assert [item.current_price for item in result.contract_results] == [
        3001.0,
        8001.0,
    ]


def test_source_frame_is_not_modified() -> None:
    source = spot_frame()
    before = source.copy(deep=True)
    result = dce_daily.fetch_dce_morning_open_snapshot(
        ["M2701", "Y2701"],
        TARGET,
        fetcher=lambda symbol: source,
        captured_at=CAPTURED,
    )
    assert result.is_usable
    pd.testing.assert_frame_equal(source, before)


def test_internal_and_chinese_symbols_are_both_supported() -> None:
    result = dce_daily.fetch_dce_morning_open_snapshot(
        ["M2701", "Y2701"],
        TARGET,
        fetcher=lambda symbol: spot_frame(symbols=["M2701", "豆油2701"]),
        captured_at=CAPTURED,
    )
    assert result.is_usable


def test_missing_and_duplicate_contracts_are_distinct() -> None:
    missing = dce_daily.fetch_dce_morning_open_snapshot(
        ["M2701", "Y2701"],
        TARGET,
        fetcher=lambda symbol: spot_frame(
            symbols=["M2701"], prices=[3000.0], times=["23:00:00"]
        ),
        captured_at=CAPTURED,
    )
    duplicate = dce_daily.fetch_dce_morning_open_snapshot(
        ["M2701"],
        TARGET,
        fetcher=lambda symbol: spot_frame(
            symbols=["M2701", "豆粕2701"],
            prices=[3000.0, 3001.0],
            times=["23:00:00", "23:00:01"],
        ),
        captured_at=CAPTURED,
    )
    assert missing.contract_results[1].quality_status == "missing_contract"
    assert duplicate.contract_results[0].quality_status == "duplicate_contract"


def test_adjacent_contract_never_substitutes_for_target() -> None:
    result = dce_daily.fetch_dce_morning_open_snapshot(
        ["M2701"],
        TARGET,
        fetcher=lambda symbol: spot_frame(
            symbols=["M2705"],
            prices=[3000.0],
            times=["23:00:00"],
        ),
        captured_at=CAPTURED,
    )
    assert result.contract_results[0].quality_status == "missing_contract"
    assert not result.is_usable


@pytest.mark.parametrize("bad", [0, -1, float("nan"), float("inf"), "3010"])
def test_invalid_current_price_is_rejected(bad: object) -> None:
    result = dce_daily.fetch_dce_morning_open_snapshot(
        ["M2701"],
        TARGET,
        fetcher=lambda symbol: spot_frame(
            symbols=["M2701"], prices=[bad], times=["23:00:00"]
        ),
        captured_at=CAPTURED,
    )
    assert result.contract_results[0].quality_status == "invalid_current_price"
    assert not result.is_usable


@pytest.mark.parametrize(
    "payload",
    [
        pd.DataFrame({"symbol": ["M2701"], "time": ["23:00:00"], "close": [3010]}),
        pd.DataFrame({"symbol": ["M2701"], "time": ["23:00:00"], "settle": [3010]}),
        pd.DataFrame({"symbol": ["M2701"], "current_price": [3010]}),
        pd.DataFrame({"time": ["23:00:00"], "current_price": [3010]}),
        pd.DataFrame(),
    ],
)
def test_missing_required_fields_never_fall_back(payload: pd.DataFrame) -> None:
    result = dce_daily.fetch_dce_morning_open_snapshot(
        ["M2701"],
        TARGET,
        fetcher=lambda symbol: payload,
        captured_at=CAPTURED,
    )
    assert result.contract_results[0].quality_status == "source_schema_error"


def test_matching_quote_date_is_preserved() -> None:
    result = dce_daily.fetch_dce_morning_open_snapshot(
        ["M2701"],
        TARGET,
        fetcher=lambda symbol: spot_frame(
            symbols=["M2701"],
            prices=[3010.0],
            times=["23:00:00"],
            date=[PREVIOUS_TRADING_DATE],
        ),
        captured_at=CAPTURED,
    )
    assert result.is_usable
    assert result.records[0].source_quote_date == PREVIOUS_TRADING_DATE
    assert result.records[0].quote_date_evidence_status == "source_confirmed"


def test_stale_quote_date_is_rejected() -> None:
    result = dce_daily.fetch_dce_morning_open_snapshot(
        ["M2701"],
        TARGET,
        fetcher=lambda symbol: spot_frame(
            symbols=["M2701"],
            prices=[3010.0],
            times=["23:00:00"],
            date=[TARGET],
        ),
        captured_at=CAPTURED,
    )
    assert result.contract_results[0].quality_status == "stale_quote_date"
    assert result.contract_results[0].quote_date_evidence_status == "date_mismatch"
    assert result.contract_results[0].record is not None
    assert not result.contract_results[0].record.is_usable


def test_time_only_quote_is_retained_but_not_formally_usable() -> None:
    result = dce_daily.fetch_dce_morning_open_snapshot(
        ["M2701"], TARGET, fetcher=lambda symbol: spot_frame(
            symbols=["M2701"], prices=[3010.0], times=["23:00:00"], date=[None]
        ), captured_at=CAPTURED
    )
    assert not result.is_usable
    assert result.attempt_status == "failed"
    assert result.records[0].source_quote_date is None
    assert result.records[0].source_quote_time == time(23, 0)
    assert result.records[0].price_cny_per_tonne == 3010.0
    assert result.records[0].contract_identity_status == "source_confirmed_exact"
    assert result.records[0].quote_date_evidence_status == (
        "time_only_unconfirmed"
    )
    assert result.contract_results[0].quality_status == (
        "quote_date_unconfirmed"
    )


@pytest.mark.parametrize(
    "quote_time",
    ["22:59:00", "23:00:00", "23:00:59", time(23, 0, 59, 999999)],
)
def test_allowed_quote_time_range_is_end_exclusive(quote_time: object) -> None:
    result = dce_daily.fetch_dce_morning_open_snapshot(
        ["M2701"],
        TARGET,
        fetcher=lambda symbol: spot_frame(
            symbols=["M2701"], prices=[3010.0], times=[quote_time]
        ),
        captured_at=CAPTURED,
    )
    assert result.is_usable


@pytest.mark.parametrize(
    ("quote_time", "status"),
    [
        ("22:58:59", "quote_time_before_window"),
        ("23:01:00", "quote_time_outside_allowed_range"),
        ("15:00:00", "quote_time_before_window"),
        ("21:00:00", "quote_time_before_window"),
    ],
)
def test_quote_time_outside_night_close_window_is_rejected(
    quote_time: str, status: str
) -> None:
    result = dce_daily.fetch_dce_morning_open_snapshot(
        ["M2701"],
        TARGET,
        fetcher=lambda symbol: spot_frame(
            symbols=["M2701"], prices=[3010.0], times=[quote_time]
        ),
        captured_at=CAPTURED,
    )
    assert result.contract_results[0].quality_status == status


@pytest.mark.parametrize("quote_time", [None, "", "invalid", "25:00:00"])
def test_missing_or_invalid_time_is_schema_error(quote_time: object) -> None:
    result = dce_daily.fetch_dce_morning_open_snapshot(
        ["M2701"],
        TARGET,
        fetcher=lambda symbol: spot_frame(
            symbols=["M2701"], prices=[3010.0], times=[quote_time]
        ),
        captured_at=CAPTURED,
    )
    assert result.contract_results[0].quality_status == "source_schema_error"


@pytest.mark.parametrize(
    ("captured_at", "status"),
    [
        (
            datetime(2026, 7, 28, 8, 29, 59, tzinfo=dce_daily.CAPTURE_ZONE),
            "capture_window_not_started",
        ),
        (
            datetime(2026, 7, 28, 8, 30, 0, tzinfo=dce_daily.CAPTURE_ZONE),
            "valid",
        ),
        (
            datetime(2026, 7, 28, 8, 31, 0, tzinfo=dce_daily.CAPTURE_ZONE),
            "valid",
        ),
        (
            datetime(
                2026,
                7,
                28,
                8,
                32,
                59,
                999999,
                tzinfo=dce_daily.CAPTURE_ZONE,
            ),
            "valid",
        ),
        (
            datetime(2026, 7, 28, 8, 33, 0, tzinfo=dce_daily.CAPTURE_ZONE),
            "capture_window_closed",
        ),
    ],
)
def test_capture_window_boundaries(captured_at: datetime, status: str) -> None:
    assert dce_daily.capture_gate_status(TARGET, captured_at) == status


def test_non_current_business_date_is_rejected_before_network() -> None:
    calls: list[str] = []
    result = dce_daily.fetch_dce_morning_open_snapshot(
        ["M2701"],
        date(2026, 7, 27),
        fetcher=lambda symbol: calls.append(symbol) or spot_frame(),
        captured_at=CAPTURED,
    )
    assert result.capture_gate_status == "non_current_business_date"
    assert result.contract_results[0].quality_status == "non_current_business_date"
    assert calls == []


def test_probe_mode_can_inspect_source_outside_capture_window() -> None:
    result = dce_daily.fetch_dce_morning_open_snapshot(
        ["M2701"],
        TARGET,
        fetcher=lambda symbol: spot_frame(
            symbols=["M2701"], prices=[3010.0], times=["23:00:00"]
        ),
        captured_at=datetime(
            2026, 7, 28, 20, 0, tzinfo=dce_daily.CAPTURE_ZONE
        ),
        enforce_capture_window=False,
    )
    assert result.capture_gate_status == "capture_window_closed"
    assert result.is_usable


def test_monday_uses_calendar_previous_trading_date_without_sunday_guess() -> None:
    monday = date(2026, 8, 3)
    result = dce_daily.fetch_dce_night_session_close_snapshot(
        ["M2701"],
        monday,
        fetcher=lambda symbol: spot_frame(
            symbols=["M2701"],
            prices=[3010.0],
            times=["23:00:00"],
            date=[date(2026, 7, 31)],
        ),
        trade_calendar_fetcher=lambda: pd.DataFrame(
            {"trade_date": [date(2026, 7, 31), monday]}
        ),
        captured_at=datetime(
            2026, 8, 3, 8, 30, 30, tzinfo=dce_daily.CAPTURE_ZONE
        ),
    )
    assert result.attempt_status == "success"
    assert result.previous_trading_date == date(2026, 7, 31)
    assert result.records[0].business_date == monday
    assert result.records[0].source_quote_date == date(2026, 7, 31)


def test_post_holiday_first_trading_day_uses_calendar_previous_trading_date() -> None:
    post_holiday = date(2026, 10, 9)
    previous_trading_date = date(2026, 9, 30)
    result = dce_daily.fetch_dce_night_session_close_snapshot(
        ["M2701"],
        post_holiday,
        fetcher=lambda symbol: spot_frame(
            symbols=["M2701"],
            prices=[3010.0],
            times=["23:00:00"],
            date=[previous_trading_date],
        ),
        trade_calendar_fetcher=lambda: pd.DataFrame(
            {"trade_date": [previous_trading_date, post_holiday]}
        ),
        captured_at=datetime(
            2026, 10, 9, 8, 30, 30, tzinfo=dce_daily.CAPTURE_ZONE
        ),
    )
    assert result.attempt_status == "success"
    assert result.previous_trading_date == previous_trading_date
    assert result.records[0].business_date == post_holiday
    assert result.records[0].source_quote_date == previous_trading_date


def test_post_holiday_calendar_previous_day_is_not_night_session_date() -> None:
    post_holiday = date(2026, 10, 9)
    result = dce_daily.fetch_dce_night_session_close_snapshot(
        ["M2701"],
        post_holiday,
        fetcher=lambda symbol: spot_frame(
            symbols=["M2701"],
            prices=[3010.0],
            times=["23:00:00"],
            date=[date(2026, 10, 8)],
        ),
        trade_calendar_fetcher=lambda: pd.DataFrame(
            {"trade_date": [date(2026, 9, 30), post_holiday]}
        ),
        captured_at=datetime(
            2026, 10, 9, 8, 30, 30, tzinfo=dce_daily.CAPTURE_ZONE
        ),
    )
    assert result.attempt_status == "failed"
    assert result.contract_results[0].quality_status == "stale_quote_date"
    assert result.contract_results[0].quote_date_evidence_status == "date_mismatch"


def test_post_holiday_without_valid_night_quote_fails_closed() -> None:
    post_holiday = date(2026, 10, 9)
    result = dce_daily.fetch_dce_night_session_close_snapshot(
        ["M2701"],
        post_holiday,
        fetcher=lambda symbol: spot_frame(
            symbols=["M2701"],
            prices=[3010.0],
            times=["14:59:59"],
            date=[date(2026, 9, 30)],
        ),
        trade_calendar_fetcher=lambda: pd.DataFrame(
            {"trade_date": [date(2026, 9, 30), post_holiday]}
        ),
        captured_at=datetime(
            2026, 10, 9, 8, 30, 30, tzinfo=dce_daily.CAPTURE_ZONE
        ),
    )
    assert result.attempt_status == "failed"
    assert result.previous_trading_date == date(2026, 9, 30)
    assert result.contract_results[0].quality_status == (
        "quote_time_before_window"
    )


def test_partial_contract_set_is_passed_with_incomplete() -> None:
    result = dce_daily.fetch_dce_night_session_close_snapshot(
        ["M2701", "Y2701"],
        TARGET,
        fetcher=lambda symbol: spot_frame(
            symbols=["M2701"], prices=[3010.0], times=["23:00:00"]
        ),
        captured_at=CAPTURED,
    )
    assert result.attempt_status == "passed_with_incomplete"
    assert [record.contract_code for record in result.records] == ["M2701"]


def test_source_native_full_symbol_is_confirmed_exact_identity() -> None:
    result = dce_daily.fetch_dce_night_session_close_snapshot(
        ["M2701"],
        TARGET,
        fetcher=lambda symbol: spot_frame(
            symbols=["豆粕2701"], prices=[3010.0], times=["23:00:00"]
        ),
        captured_at=CAPTURED,
    )

    record = result.records[0]
    assert record.contract_identity_status == "source_confirmed_exact"
    assert record.source_contract_code == "M2701"
    assert record.source_delivery_month == 1


def test_fetcher_exception_is_structured_and_proxy_identity_is_redacted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "socks5://user:password@example.invalid:9999")

    def broken(symbol: str) -> pd.DataFrame:
        raise ConnectionError(
            "failed via socks5://user:password@example.invalid:9999\nretry"
        )

    result = dce_daily.fetch_dce_morning_open_snapshot(
        ["M2701"], TARGET, fetcher=broken, captured_at=CAPTURED
    )
    assert result.contract_results[0].quality_status == "source_error"
    assert "password" not in (result.source_error_message or "")
    assert "example.invalid" not in (result.source_error_message or "")
    assert "\n" not in (result.source_error_message or "")


def test_default_fetcher_calls_public_akshare_batch_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[str, str, str]] = []

    def public_function(*, symbol: str, market: str, adjust: str) -> pd.DataFrame:
        seen.append((symbol, market, adjust))
        return spot_frame()

    monkeypatch.setattr(akshare, "futures_zh_spot", public_function)
    result = dce_daily.fetch_dce_morning_open_snapshot(
        ["M2701", "Y2701"], TARGET, captured_at=CAPTURED
    )
    assert result.is_usable
    assert seen == [("M2701,Y2701", "CF", "0")]


def test_non_dataframe_and_naive_clock_are_rejected_or_structured() -> None:
    not_frame = dce_daily.fetch_dce_morning_open_snapshot(
        ["M2701"],
        TARGET,
        fetcher=lambda symbol: [],  # type: ignore[return-value]
        captured_at=CAPTURED,
    )
    assert not_frame.contract_results[0].quality_status == "source_schema_error"
    with pytest.raises(ValueError):
        dce_daily.fetch_dce_morning_open_snapshot(
            ["M2701"],
            TARGET,
            fetcher=lambda symbol: spot_frame(),
            captured_at=datetime(2026, 7, 28, 9, 0),
        )
