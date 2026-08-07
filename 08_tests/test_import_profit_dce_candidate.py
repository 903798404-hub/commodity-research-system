from __future__ import annotations

from collections import Counter
from datetime import date, datetime
import hashlib
import json
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq
import pytest
import yaml

from agri_research_agent.import_profit import dce_daily
from agri_research_agent.import_profit.standard_io import load_dce_parquet
from import_profit import build_dce_daily_candidate as candidate
from import_profit import probe_dce_daily_source as probe


CONFIG = candidate.REPOSITORY_ROOT / "02_configs" / "import_profit_soybean.yaml"
TARGET = date(2026, 7, 28)
FIXED_TIME = datetime(2026, 7, 28, 8, 30, 30, tzinfo=dce_daily.CAPTURE_ZONE)
OUTSIDE_TIME = datetime(2026, 7, 28, 20, 0, tzinfo=dce_daily.CAPTURE_ZONE)
SHIPMENTS = [
    "2026-08",
    "2026-09",
    "2026-10",
    "2026-11",
    "2026-12",
    "2027-01",
    "2027-02",
    "2027-03",
    "2027-04",
    "2027-05",
    "2027-06",
    "2027-07",
]
EXPECTED_CONTRACTS = ["M2701", "M2705", "M2709", "Y2701", "Y2705", "Y2709"]


def good_batch(batch_symbol: str) -> pd.DataFrame:
    symbols = batch_symbol.split(",")
    return pd.DataFrame(
        {
            "symbol": symbols,
            "time": ["23:00:00"] * len(symbols),
            "current_price": [
                3000.0 + index if symbol.startswith("M") else 8000.0 + index
                for index, symbol in enumerate(symbols)
            ],
            "close": [9999.0] * len(symbols),
            "settle": [8888.0] * len(symbols),
        }
    )


def fixed_clock() -> datetime:
    return FIXED_TIME


@pytest.fixture(autouse=True)
def stable_trade_calendar(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        dce_daily,
        "default_trade_calendar_fetcher",
        lambda: pd.DataFrame(
            {"trade_date": [date(2026, 7, 27), TARGET]}
        ),
    )


def build_good(tmp_path: Path, name: str = "candidate"):
    output = tmp_path / name
    result = candidate.build_dce_daily_candidate(
        CONFIG,
        TARGET,
        SHIPMENTS,
        output,
        fetcher=good_batch,
        clock=fixed_clock,
        akshare_version="test-version",
    )
    return output, result


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def test_config_locks_night_session_close_incremental_source() -> None:
    config = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    source = config["source_policy"]["dce"]["incremental_source"]
    assert source["provider"] == "akshare"
    assert source["function"] == "futures_zh_spot"
    assert source["source_field"] == "current_price"
    assert source["price_type"] == "night_session_close"
    assert source["scheduled_time"] == "08:30:00"
    assert source["capture_timezone"] == "Asia/Shanghai"
    assert source["capture_window"] == {
        "start": "08:30:00",
        "end_exclusive": "08:33:00",
    }
    assert source["require_same_business_date"] is True
    assert source["require_complete_contract_set"] is False
    assert source["allow_previous_date_fallback"] is False
    assert source["allow_post_close_fallback"] is False
    assert source["allow_historical_close_fallback"] is False
    assert source["allow_adjacent_contract_fallback"] is False
    assert source["allow_other_price_field_fallback"] is False


def test_multiple_shipment_periods_use_existing_mapping_and_deduplicate() -> None:
    resolved = candidate.resolve_required_contracts(CONFIG, SHIPMENTS)
    assert resolved["required_contracts"] == EXPECTED_CONTRACTS
    assert resolved["mapping_identity"] == "import_profit_soybean:schema_version=1"
    assert resolved["contract_mappings"][0] == {
        "shipment_period": "2026-08",
        "soymeal_contract": "M2701",
        "soyoil_contract": "Y2701",
    }
    assert resolved["contract_mappings"][-1] == {
        "shipment_period": "2027-07",
        "soymeal_contract": "M2709",
        "soyoil_contract": "Y2709",
    }


def test_december_and_next_january_mapping() -> None:
    resolved = candidate.resolve_required_contracts(CONFIG, ["2026-12", "2027-01"])
    assert resolved["contract_mappings"] == [
        {
            "shipment_period": "2026-12",
            "soymeal_contract": "M2701",
            "soyoil_contract": "Y2701",
        },
        {
            "shipment_period": "2027-01",
            "soymeal_contract": "M2705",
            "soyoil_contract": "Y2705",
        },
    ]


@pytest.mark.parametrize("value", ["2026-00", "2026-13", "26-01", "2026-1", "x"])
def test_invalid_shipment_period_is_rejected(value: str) -> None:
    with pytest.raises(candidate.DceCandidateBuildError):
        candidate.resolve_required_contracts(CONFIG, [value])


def test_successful_candidate_uses_one_batch_and_fixed_schema(tmp_path: Path) -> None:
    calls: list[str] = []

    def fetch(batch: str) -> pd.DataFrame:
        calls.append(batch)
        return good_batch(batch)

    output = tmp_path / "success"
    result = candidate.build_dce_daily_candidate(
        CONFIG,
        TARGET,
        SHIPMENTS,
        output,
        fetcher=fetch,
        clock=fixed_clock,
        akshare_version="test-version",
    )
    assert calls == [",".join(EXPECTED_CONTRACTS)]
    assert sorted(path.name for path in output.iterdir()) == [
        "dce_night_session_close_prices.parquet",
        "manifest.json",
        "quality_report.json",
    ]
    table = pq.read_table(output / candidate.PARQUET_FILENAME)
    pandas_frame = pd.read_parquet(output / candidate.PARQUET_FILENAME)
    assert table.schema == candidate.DCE_NIGHT_SESSION_CLOSE_SCHEMA
    assert table.num_rows == 6
    assert list(pandas_frame["contract_code"]) == EXPECTED_CONTRACTS
    assert set(pandas_frame["business_date"]) == {TARGET}
    assert set(pandas_frame["price_type"]) == {"night_session_close"}
    assert set(pandas_frame["source_function"]) == {"futures_zh_spot"}
    assert pandas_frame["price_cny_per_tonne"].gt(0).all()
    assert result["manifest"]["candidate_status"] == "success"
    loaded = load_dce_parquet(output / candidate.PARQUET_FILENAME)
    assert len(loaded.records) == 6
    assert {record.price_type for record in loaded.records} == {
        "night_session_close"
    }


def test_manifest_is_safe_complete_and_hash_matches(tmp_path: Path) -> None:
    output, _ = build_good(tmp_path)
    manifest = json.loads((output / candidate.MANIFEST_FILENAME).read_text("utf-8"))
    serialized = json.dumps(manifest, ensure_ascii=False).casefold()
    assert "c:\\" not in serialized
    assert "/users/" not in serialized
    assert "proxy" not in serialized
    assert manifest["schema_version"] == 2
    assert manifest["adapter_version"] == "4"
    assert manifest["business_date"] == "2026-07-28"
    assert manifest["required_contracts"] == EXPECTED_CONTRACTS
    assert manifest["required_contract_count"] == 6
    assert manifest["successful_contract_count"] == 6
    assert manifest["missing_contracts"] == []
    assert manifest["mapping_identity"] == "import_profit_soybean:schema_version=1"
    assert manifest["source_function"] == "futures_zh_spot"
    assert manifest["price_field"] == "current_price"
    assert manifest["price_type"] == "night_session_close"
    assert manifest["snapshot_batch_id"].startswith("dce-night-close-20260728-")
    assert manifest["capture_timezone"] == "Asia/Shanghai"
    parquet = output / candidate.PARQUET_FILENAME
    assert manifest["output_size"] == parquet.stat().st_size
    assert manifest["output_sha256"] == sha256(parquet)


def test_quality_report_is_bounded_and_contains_request_and_contract_evidence(
    tmp_path: Path,
) -> None:
    output, _ = build_good(tmp_path)
    report = json.loads((output / candidate.QUALITY_FILENAME).read_text("utf-8"))
    assert report["candidate_status"] == "success"
    assert report["capture_window_status"] == "valid"
    assert report["request_mode"] == "batch"
    assert report["snapshot_batch_id"].startswith("dce-night-close-20260728-")
    assert len(report["requests"]) == 1
    assert report["requests"][0]["request_symbol"] == ",".join(EXPECTED_CONTRACTS)
    assert len(report["contract_results"]) == 6
    for item in report["contract_results"]:
        assert item["request_status"] == "valid_time_only"
        assert item["source_quote_time"] == "23:00:00"
        assert item["current_price"] > 0
        assert "rows" not in item


@pytest.mark.parametrize(
    ("when", "status"),
    [
        (
            datetime(2026, 7, 28, 8, 29, 59, tzinfo=dce_daily.CAPTURE_ZONE),
            "capture_window_not_started",
        ),
        (
            datetime(2026, 7, 28, 8, 33, 0, tzinfo=dce_daily.CAPTURE_ZONE),
            "capture_window_closed",
        ),
        (
            datetime(2026, 7, 29, 8, 30, tzinfo=dce_daily.CAPTURE_ZONE),
            "non_current_business_date",
        ),
    ],
)
def test_time_or_date_gate_fails_before_network(
    tmp_path: Path, when: datetime, status: str
) -> None:
    calls: list[str] = []
    with pytest.raises(candidate.DceCandidateBuildError) as caught:
        candidate.build_dce_daily_candidate(
            CONFIG,
            TARGET,
            ["2026-08"],
            tmp_path / status,
            fetcher=lambda batch: calls.append(batch) or good_batch(batch),
            clock=lambda: when,
        )
    assert str(caught.value) == status
    assert calls == []
    assert not (tmp_path / status).exists()


def test_partial_missing_contract_writes_incomplete_candidate(tmp_path: Path) -> None:
    output = tmp_path / "partial"

    def fetch(batch: str) -> pd.DataFrame:
        frame = good_batch(batch)
        return frame[frame["symbol"] != "Y2705"].reset_index(drop=True)

    result = candidate.build_dce_daily_candidate(
        CONFIG,
        TARGET,
        SHIPMENTS,
        output,
        fetcher=fetch,
        clock=fixed_clock,
        akshare_version="test-version",
    )
    assert result["manifest"]["candidate_status"] == "passed_with_incomplete"
    assert result["manifest"]["missing_contracts"] == ["Y2705"]
    assert result["quality_report"]["fatal_issues"] == []
    assert result["quality_report"]["warnings"] == [
        {"code": "required_contracts_incomplete", "contracts": ["Y2705"]}
    ]


def test_quote_at_end_boundary_fails_without_candidate_files(tmp_path: Path) -> None:
    output = tmp_path / "quote-after-window"

    def fetch(batch: str) -> pd.DataFrame:
        frame = good_batch(batch)
        frame["time"] = "23:01:00"
        return frame

    with pytest.raises(candidate.DceCandidateBuildError) as caught:
        candidate.build_dce_daily_candidate(
            CONFIG,
            TARGET,
            SHIPMENTS,
            output,
            fetcher=fetch,
            clock=fixed_clock,
            akshare_version="test-version",
        )
    assert not output.exists() or list(output.iterdir()) == []
    report = caught.value.quality_report
    assert report is not None
    assert report["candidate_status"] == "failed"
    assert {
        item["request_status"] for item in report["contract_results"]
    } == {"quote_time_outside_allowed_range"}


def test_previous_date_quotes_fail_without_candidate_files(tmp_path: Path) -> None:
    output = tmp_path / "previous-date"

    def fetch(batch: str) -> pd.DataFrame:
        frame = good_batch(batch)
        frame["date"] = "2026-07-27"
        return frame

    with pytest.raises(candidate.DceCandidateBuildError) as caught:
        candidate.build_dce_daily_candidate(
            CONFIG,
            TARGET,
            SHIPMENTS,
            output,
            fetcher=fetch,
            clock=fixed_clock,
            akshare_version="test-version",
        )
    assert not output.exists() or list(output.iterdir()) == []
    report = caught.value.quality_report
    assert report is not None
    assert report["candidate_status"] == "failed"
    assert {
        item["request_status"] for item in report["contract_results"]
    } == {"stale_quote_date"}


def test_retry_requests_one_complete_contract_batch(tmp_path: Path) -> None:
    calls: list[str] = []
    sleeps: list[float] = []

    def fetch(batch: str) -> pd.DataFrame:
        calls.append(batch)
        frame = good_batch(batch)
        if len(calls) == 1:
            frame = frame[frame["symbol"] != "Y2709"].reset_index(drop=True)
        return frame

    _, result = (
        tmp_path / "retry",
        candidate.build_dce_daily_candidate(
            CONFIG,
            TARGET,
            SHIPMENTS,
            tmp_path / "retry",
            attempts=2,
            retry_seconds=2.5,
            fetcher=fetch,
            sleeper=sleeps.append,
            clock=fixed_clock,
            akshare_version="test-version",
        ),
    )
    assert calls == [
        ",".join(EXPECTED_CONTRACTS),
        ",".join(EXPECTED_CONTRACTS),
    ]
    assert sleeps == [2.5]
    counts = {
        item["contract_code"]: item["attempt_count"]
        for item in result["quality_report"]["contract_results"]
    }
    assert all(counts[code] == 2 for code in EXPECTED_CONTRACTS)


def test_retry_limit_exhaustion_keeps_valid_subset(tmp_path: Path) -> None:
    calls: Counter[str] = Counter()

    def fetch(batch: str) -> pd.DataFrame:
        calls[batch] += 1
        return good_batch("Y2701")

    result = candidate.build_dce_daily_candidate(
        CONFIG,
        TARGET,
        ["2026-08"],
        tmp_path / "exhausted",
        attempts=3,
        fetcher=fetch,
        sleeper=lambda seconds: None,
        clock=fixed_clock,
        akshare_version="test-version",
    )
    assert calls["M2701,Y2701"] == 3
    assert result["manifest"]["candidate_status"] == "passed_with_incomplete"
    assert result["manifest"]["missing_contracts"] == ["M2701"]


@pytest.mark.parametrize(
    ("attempts", "seconds"),
    [(0, 0), (6, 0), (1, -1), (1, 61)],
)
def test_retry_arguments_are_bounded(attempts: int, seconds: float) -> None:
    with pytest.raises(candidate.DceCandidateBuildError):
        candidate.build_dce_daily_candidate(
            CONFIG,
            TARGET,
            ["2026-08"],
            Path("unused"),
            attempts=attempts,
            retry_seconds=seconds,
            fetcher=good_batch,
            clock=fixed_clock,
        )


def test_atomic_replace_failure_cleans_all_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "write-failure"
    real_replace = candidate.os.replace
    calls = 0

    def fail_second(source: Path, destination: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated replace failure")
        real_replace(source, destination)

    monkeypatch.setattr(candidate.os, "replace", fail_second)
    with pytest.raises(OSError, match="simulated"):
        candidate.build_dce_daily_candidate(
            CONFIG,
            TARGET,
            SHIPMENTS,
            output,
            fetcher=good_batch,
            clock=fixed_clock,
            akshare_version="test-version",
        )
    assert output.exists()
    assert list(output.iterdir()) == []


def test_fixed_inputs_produce_stable_business_content(tmp_path: Path) -> None:
    first_output, first = build_good(tmp_path, "first")
    second_output, second = build_good(tmp_path, "second")
    ignored = {"output_size", "output_sha256", "output_files"}
    assert {
        key: value for key, value in first["manifest"].items() if key not in ignored
    } == {
        key: value for key, value in second["manifest"].items() if key not in ignored
    }
    assert pq.read_table(first_output / candidate.PARQUET_FILENAME).equals(
        pq.read_table(second_output / candidate.PARQUET_FILENAME)
    )


def test_output_inside_repository_and_nonempty_output_are_rejected(
    tmp_path: Path,
) -> None:
    with pytest.raises(candidate.DceCandidateBuildError, match="outside"):
        candidate.build_dce_daily_candidate(
            CONFIG,
            TARGET,
            ["2026-08"],
            candidate.REPOSITORY_ROOT / "forbidden",
            fetcher=good_batch,
            clock=fixed_clock,
        )
    nonempty = tmp_path / "nonempty"
    nonempty.mkdir()
    (nonempty / "keep.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(candidate.DceCandidateBuildError, match="empty"):
        candidate.build_dce_daily_candidate(
            CONFIG,
            TARGET,
            ["2026-08"],
            nonempty,
            fetcher=good_batch,
            clock=fixed_clock,
        )


def test_probe_separates_source_success_from_candidate_time_gate() -> None:
    payload = probe.run_probe(
        ["M2701", "Y2701"],
        clock=lambda: OUTSIDE_TIME,
        fetcher=good_batch,
    )
    assert payload["call_parameters"] == {
        "symbol": "M2701,Y2701",
        "market": "CF",
        "adjust": "0",
    }
    assert payload["source_probe_success"] is True
    assert payload["candidate_eligible_now"] is False
    assert payload["capture_window_status"] == "capture_window_closed"
    assert payload["matched_contracts"] == ["M2701", "Y2701"]
    assert payload["source_function"] == "futures_zh_spot"
    assert payload["price_type"] == "night_session_close"
    assert payload["target_contracts_complete"] is True
    assert payload["final_status"] == "failed"
    assert payload["request_started_at"] == OUTSIDE_TIME.isoformat()
    assert payload["request_completed_at"] == OUTSIDE_TIME.isoformat()
