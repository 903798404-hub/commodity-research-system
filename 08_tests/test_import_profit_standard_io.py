from dataclasses import FrozenInstanceError
from datetime import date, datetime, time
import hashlib

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from agri_research_agent.import_profit.standard_io import (
    CBOT_SCHEMA,
    DCE_HISTORICAL_SCHEMA,
    DCE_INCREMENTAL_SCHEMA,
    LEGACY_DCE_HISTORICAL_SCHEMA,
    LEGACY_DCE_INCREMENTAL_SCHEMA,
    LEGACY_QUOTE_DATE_DCE_HISTORICAL_SCHEMA,
    LEGACY_QUOTE_DATE_DCE_INCREMENTAL_SCHEMA,
    FX_SCHEMA,
    StandardDataError,
    StandardDuplicateKeyError,
    StandardFileError,
    StandardSchemaError,
    load_cbot_parquet,
    load_dce_parquet,
    load_fx_parquet,
)


DAY = date(2026, 7, 28)


def cbot_row(**overrides):
    row = {
        "market_date": DAY,
        "contract_year": 2027,
        "contract_month": 1,
        "price_cents_per_bushel": 1200.0,
        "lead_months": 6,
        "exchange_quality_status": "standard_window",
        "is_usable": True,
        "eligible_for_import_profit": True,
        "source_table": "us_cbot_soybean",
        "source_column": "F202701",
        "source_statement_index": 1,
        "source_snapshot_sha256": "CBOT-SHA",
    }
    row.update(overrides)
    return row


def fx_row(**overrides):
    row = {
        "market_date": DAY,
        "tenor_months": 5,
        "fx_value": 7.2,
        "unit": "cnh_per_usd",
        "source_table": "fx_curve",
        "source_column": "Fwd_5M",
        "source_statement_index": 2,
        "source_snapshot_sha256": "FX-SHA",
        "quality_status": "valid",
        "is_usable": True,
    }
    row.update(overrides)
    return row


def dce_row(code="M2701", **overrides):
    symbol = code[0]
    row = {
        "business_date": DAY,
        "instrument": {"M": "soymeal", "Y": "soyoil"}[symbol],
        "commodity": "soybean",
        "exchange": "DCE",
        "contract_code": code,
        "contract_year": 2000 + int(code[1:3]),
        "contract_month": int(code[3:5]),
        "price_cny_per_tonne": 3200.0 if symbol == "M" else 8000.0,
        "price_type": "post_close_current_price",
        "source": "akshare",
        "source_function": "futures_zh_spot",
        "source_quote_date": DAY,
        "source_quote_time": time(15, 1),
        "captured_at": datetime.fromisoformat("2026-07-28T15:10:00+08:00"),
        "capture_timezone": "Asia/Shanghai",
        "quality_status": "valid",
        "is_usable": True,
        "contract_identity_status": "source_confirmed_exact",
        "source_contract_code": code,
        "source_delivery_month": int(code[3:5]),
        "quote_date_evidence_status": "source_confirmed",
    }
    row.update(overrides)
    return row


def write(path, schema, rows):
    pq.write_table(pa.Table.from_pylist(rows, schema=schema), path)


def test_normal_cbot_identity_range_sha_and_frozen_records(tmp_path) -> None:
    path = tmp_path / "cbot.parquet"
    rows = [
        cbot_row(market_date=date(2026, 7, 27)),
        cbot_row(contract_month=3, lead_months=8, source_column="F202703"),
    ]
    write(path, CBOT_SCHEMA, rows)
    loaded = load_cbot_parquet(path)
    expected_sha = hashlib.sha256(path.read_bytes()).hexdigest().upper()

    assert loaded.identity.filename == "cbot.parquet"
    assert loaded.identity.sha256 == expected_sha
    assert loaded.identity.earliest_date == date(2026, 7, 27)
    assert loaded.identity.latest_date == DAY
    assert loaded.identity.record_count == 2
    assert loaded.identity.duplicate_key_count == 0
    assert loaded.identity.as_manifest_dict()["filename"] == "cbot.parquet"
    assert str(tmp_path) not in str(loaded.identity.as_manifest_dict())
    assert isinstance(loaded.records, tuple)
    with pytest.raises(FrozenInstanceError):
        loaded.records[0].price_cents_per_bushel = 1


@pytest.mark.parametrize("mode", ["missing", "reordered"])
def test_cbot_schema_is_exact_and_ordered(tmp_path, mode: str) -> None:
    path = tmp_path / "bad.parquet"
    fields = list(CBOT_SCHEMA)
    fields = fields[:-1] if mode == "missing" else [fields[1], fields[0], *fields[2:]]
    schema = pa.schema(fields)
    values = cbot_row()
    write(path, schema, [{field.name: values[field.name] for field in schema}])
    with pytest.raises(StandardSchemaError):
        load_cbot_parquet(path)


def test_cbot_duplicate_unsorted_and_quality_contracts_fail(tmp_path) -> None:
    duplicate = tmp_path / "duplicate.parquet"
    write(duplicate, CBOT_SCHEMA, [cbot_row(), cbot_row()])
    with pytest.raises(StandardDuplicateKeyError):
        load_cbot_parquet(duplicate)

    unsorted = tmp_path / "unsorted.parquet"
    write(
        unsorted,
        CBOT_SCHEMA,
        [
            cbot_row(
                contract_month=3,
                lead_months=8,
                source_column="F202703",
            ),
            cbot_row(),
        ],
    )
    with pytest.raises(StandardDataError, match="not sorted"):
        load_cbot_parquet(unsorted)

    contradictory = tmp_path / "contradictory.parquet"
    write(
        contradictory,
        CBOT_SCHEMA,
        [cbot_row(is_usable=False, eligible_for_import_profit=True)],
    )
    with pytest.raises(StandardDataError, match="requires"):
        load_cbot_parquet(contradictory)

    nonpositive = tmp_path / "nonpositive.parquet"
    write(
        nonpositive,
        CBOT_SCHEMA,
        [
            cbot_row(
                price_cents_per_bushel=0.0,
                exchange_quality_status="zero_price",
                is_usable=False,
                eligible_for_import_profit=False,
            )
        ],
    )
    with pytest.raises(StandardDataError, match="invalid CBOT"):
        load_cbot_parquet(nonpositive)


def test_normal_fx_and_invalid_tenor_duplicate_or_value(tmp_path) -> None:
    good = tmp_path / "fx.parquet"
    write(good, FX_SCHEMA, [fx_row(tenor_months=0), fx_row()])
    loaded = load_fx_parquet(good)
    assert tuple(point.tenor_months for point in loaded.records) == (0, 5)

    invalid_tenor = tmp_path / "invalid-tenor.parquet"
    write(invalid_tenor, FX_SCHEMA, [fx_row(tenor_months=13)])
    with pytest.raises(StandardDataError, match="invalid FX"):
        load_fx_parquet(invalid_tenor)

    duplicate = tmp_path / "duplicate-fx.parquet"
    write(duplicate, FX_SCHEMA, [fx_row(), fx_row()])
    with pytest.raises(StandardDuplicateKeyError):
        load_fx_parquet(duplicate)

    nonpositive = tmp_path / "zero-fx.parquet"
    write(
        nonpositive,
        FX_SCHEMA,
        [fx_row(fx_value=0.0, quality_status="zero_fx", is_usable=False)],
    )
    with pytest.raises(StandardDataError, match="valid and usable"):
        load_fx_parquet(nonpositive)


def test_dce_incremental_preserves_unusable_and_rejects_duplicate(tmp_path) -> None:
    good = tmp_path / "dce.parquet"
    write(
        good,
        DCE_INCREMENTAL_SCHEMA,
        [dce_row(is_usable=False), dce_row("Y2701")],
    )
    loaded = load_dce_parquet(good)
    assert loaded.records[0].is_usable is False
    assert loaded.records[1].contract_code == "Y2701"
    assert loaded.records[1].contract_identity_status == "source_confirmed_exact"
    assert loaded.records[1].source_contract_code == "Y2701"

    duplicate = tmp_path / "duplicate-dce.parquet"
    write(duplicate, DCE_INCREMENTAL_SCHEMA, [dce_row(), dce_row()])
    with pytest.raises(StandardDuplicateKeyError):
        load_dce_parquet(duplicate)


def test_dce_incremental_order_and_price_are_strict(tmp_path) -> None:
    reordered = tmp_path / "reordered.parquet"
    fields = list(DCE_INCREMENTAL_SCHEMA)
    schema = pa.schema([fields[1], fields[0], *fields[2:]])
    row = dce_row()
    write(reordered, schema, [{field.name: row[field.name] for field in schema}])
    with pytest.raises(StandardSchemaError):
        load_dce_parquet(reordered)

    invalid_price = tmp_path / "invalid-price.parquet"
    write(
        invalid_price,
        DCE_INCREMENTAL_SCHEMA,
        [dce_row(price_cny_per_tonne=0.0, is_usable=False)],
    )
    with pytest.raises(StandardDataError, match="invalid DCE"):
        load_dce_parquet(invalid_price)


def test_explicit_historical_dce_schema_requires_full_contract(tmp_path) -> None:
    path = tmp_path / "historical.parquet"
    rows = []
    for code in ("M2701", "Y2701"):
        row = dce_row(
            code,
            price_type="historical_daily_close",
            source="historical_vendor",
            source_function="daily_history",
        )
        row.pop("captured_at")
        row.pop("capture_timezone")
        row["source_snapshot_sha256"] = "HISTORY-SHA"
        rows.append(row)
    write(path, DCE_HISTORICAL_SCHEMA, rows)
    loaded = load_dce_parquet(path)
    assert loaded.identity.schema_version == "dce-explicit-history-v3"
    assert loaded.records[0].price_type == "historical_daily_close"

    month_only = tmp_path / "month-only.parquet"
    schema = pa.schema(
        [
            pa.field("business_date", pa.date32(), nullable=False),
            pa.field("delivery_month", pa.int8(), nullable=False),
            pa.field("price_cny_per_tonne", pa.float64(), nullable=False),
        ]
    )
    write(
        month_only,
        schema,
        [{"business_date": DAY, "delivery_month": 1, "price_cny_per_tonne": 3200.0}],
    )
    with pytest.raises(StandardSchemaError):
        load_dce_parquet(month_only)


@pytest.mark.parametrize(
    ("schema", "historical", "identity_unknown"),
    [
        (LEGACY_DCE_INCREMENTAL_SCHEMA, False, True),
        (LEGACY_DCE_HISTORICAL_SCHEMA, True, True),
        (LEGACY_QUOTE_DATE_DCE_INCREMENTAL_SCHEMA, False, False),
        (LEGACY_QUOTE_DATE_DCE_HISTORICAL_SCHEMA, True, False),
    ],
)
def test_legacy_dce_standard_input_is_explicitly_unknown(
    tmp_path, schema, historical, identity_unknown
) -> None:
    row = dce_row()
    if historical:
        row.pop("captured_at")
        row.pop("capture_timezone")
        row["source_snapshot_sha256"] = "HISTORY-SHA"
    legacy_row = {name: row[name] for name in schema.names}
    path = tmp_path / ("historical.parquet" if historical else "daily.parquet")
    write(path, schema, [legacy_row])

    record = load_dce_parquet(path).records[0]
    assert record.contract_identity_status == (
        "legacy_unknown" if identity_unknown else "source_confirmed_exact"
    )
    assert record.source_contract_code == (
        None if identity_unknown else "M2701"
    )
    assert record.source_delivery_month == (None if identity_unknown else 1)
    assert record.quote_date_evidence_status == "legacy_unknown"
    assert record.source_quote_date is None
    assert record.source_quote_time is None


def test_missing_directory_and_corrupt_parquet_fail_explicitly(tmp_path) -> None:
    with pytest.raises(StandardFileError, match="does not exist"):
        load_cbot_parquet(tmp_path / "missing.parquet")
    with pytest.raises(StandardFileError, match="not a regular file"):
        load_cbot_parquet(tmp_path)
    corrupt = tmp_path / "corrupt.parquet"
    corrupt.write_bytes(b"not parquet")
    with pytest.raises(StandardFileError, match="failed to read"):
        load_fx_parquet(corrupt)
