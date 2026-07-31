from __future__ import annotations

from datetime import date, datetime, time

import akshare
import pandas as pd
import pytest

from agri_research_agent.import_profit import dce_daily


TARGET = date(2026, 7, 28)
CAPTURED = datetime(2026, 7, 28, 9, 0, 30, tzinfo=dce_daily.CAPTURE_ZONE)


def spot_frame(
    symbols: list[object] | None = None,
    prices: list[object] | None = None,
    times: list[object] | None = None,
    **extra: list[object],
) -> pd.DataFrame:
    symbols = symbols or ["豆粕2701", "豆油2701"]
    prices = prices or [3010.0, 8010.0]
    times = times or ["09:00:25", 90026]
    return pd.DataFrame(
        {
            "symbol": symbols,
            "time": times,
            "current_price": prices,
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
    assert record.price_type == "morning_open_snapshot"
    assert record.source_function == "futures_zh_spot"
    assert record.capture_timezone == "Asia/Shanghai"
    assert record.source_quote_date is None
    assert record.source_quote_time == time(9, 0, 25)


def test_return_order_changes_and_extra_contracts_do_not_change_matching() -> None:
    result = dce_daily.fetch_dce_morning_open_snapshot(
        ["M2701", "Y2701"],
        TARGET,
        fetcher=lambda symbol: spot_frame(
            symbols=["A9999", "Y2701", "M2701"],
            prices=[1.0, 8001.0, 3001.0],
            times=["09:00:25"] * 3,
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
            symbols=["M2701"], prices=[3000.0], times=["09:00:25"]
        ),
        captured_at=CAPTURED,
    )
    duplicate = dce_daily.fetch_dce_morning_open_snapshot(
        ["M2701"],
        TARGET,
        fetcher=lambda symbol: spot_frame(
            symbols=["M2701", "豆粕2701"],
            prices=[3000.0, 3001.0],
            times=["09:00:25", "09:00:26"],
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
            times=["09:00:25"],
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
            symbols=["M2701"], prices=[bad], times=["09:00:25"]
        ),
        captured_at=CAPTURED,
    )
    assert result.contract_results[0].quality_status == "invalid_current_price"
    assert not result.is_usable


@pytest.mark.parametrize(
    "payload",
    [
        pd.DataFrame({"symbol": ["M2701"], "time": ["09:00:25"], "close": [3010]}),
        pd.DataFrame({"symbol": ["M2701"], "time": ["09:00:25"], "settle": [3010]}),
        pd.DataFrame({"symbol": ["M2701"], "current_price": [3010]}),
        pd.DataFrame({"time": ["09:00:25"], "current_price": [3010]}),
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
            times=["09:00:25"],
            date=["2026-07-28"],
        ),
        captured_at=CAPTURED,
    )
    assert result.is_usable
    assert result.records[0].source_quote_date == TARGET


def test_stale_quote_date_is_rejected() -> None:
    result = dce_daily.fetch_dce_morning_open_snapshot(
        ["M2701"],
        TARGET,
        fetcher=lambda symbol: spot_frame(
            symbols=["M2701"],
            prices=[3010.0],
            times=["09:00:25"],
            date=["2026-07-27"],
        ),
        captured_at=CAPTURED,
    )
    assert result.contract_results[0].quality_status == "stale_quote_date"


def test_no_quote_date_is_allowed_only_with_explicit_current_context() -> None:
    result = dce_daily.fetch_dce_morning_open_snapshot(
        ["M2701"], TARGET, fetcher=lambda symbol: spot_frame(
            symbols=["M2701"], prices=[3010.0], times=["09:00:25"]
        ), captured_at=CAPTURED
    )
    assert result.is_usable
    assert result.records[0].source_quote_date is None


@pytest.mark.parametrize(
    "quote_time",
    ["09:00:00", "09:01:00", "09:02:59", time(9, 2, 59, 999999)],
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
        ("08:59:59", "quote_time_before_window"),
        ("09:03:00", "quote_time_outside_allowed_range"),
        ("15:00:00", "quote_time_outside_allowed_range"),
        ("21:00:00", "quote_time_outside_allowed_range"),
    ],
)
def test_quote_time_outside_morning_window_is_rejected(
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
            datetime(2026, 7, 28, 8, 59, 59, tzinfo=dce_daily.CAPTURE_ZONE),
            "capture_window_not_started",
        ),
        (
            datetime(2026, 7, 28, 9, 0, 0, tzinfo=dce_daily.CAPTURE_ZONE),
            "valid",
        ),
        (
            datetime(2026, 7, 28, 9, 1, 0, tzinfo=dce_daily.CAPTURE_ZONE),
            "valid",
        ),
        (
            datetime(
                2026,
                7,
                28,
                9,
                2,
                59,
                999999,
                tzinfo=dce_daily.CAPTURE_ZONE,
            ),
            "valid",
        ),
        (
            datetime(2026, 7, 28, 9, 3, 0, tzinfo=dce_daily.CAPTURE_ZONE),
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
            symbols=["M2701"], prices=[3010.0], times=["09:00:25"]
        ),
        captured_at=datetime(
            2026, 7, 28, 20, 0, tzinfo=dce_daily.CAPTURE_ZONE
        ),
        enforce_capture_window=False,
    )
    assert result.capture_gate_status == "capture_window_closed"
    assert result.is_usable


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
