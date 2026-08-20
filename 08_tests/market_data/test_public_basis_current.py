from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pyarrow as pa
import pytest

from agri_research_agent.market_data import public_basis_current as reader
from agri_research_agent.shared.file_identity import identify_file
from agri_research_agent.summary_engine.basis import build_basis_summary


HISTORICAL_COMMODITIES = (
    "一豆", "24度", "三菜", "豆粕", "菜粕", "一葵", "一级玉米油", "葵粕",
)


def _formal_rows() -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for index, commodity in enumerate(HISTORICAL_COMMODITIES):
        cash_only = commodity == "一葵"
        rows.append({
            "date": date(2026, 5, 20 + index),
            "business_date": date(2026, 5, 20 + index),
            "commodity": commodity,
            "region": f"历史地区{index}",
            "quote_type": "一口价" if cash_only else "基差报价",
            "delivery_month": "现货",
            "futures_contract": None if cash_only else "2609",
            "cash_price": Decimal("8000") + index,
            "futures_price": None if cash_only else Decimal("7900") + index,
            "basis": None if cash_only else Decimal("100"),
            "source_sheet": f"历史页{index}",
            "series_id": f"market.basis.domestic.china.historical.fixture_{index}",
            "segment": "SEALED_HISTORICAL",
            "provider": "Historical Domestic Basis Excel",
            "source_series_id": f"history:{index}",
            "source_locator": f"sealed:{index}",
            "currency": "CNY",
            "unit": "CNY/metric_tonne",
        })
    for index in range(21):
        basis = (Decimal("-10"), Decimal("0"), Decimal("10"))[index % 3]
        rows.append({
            "date": date(2026, 8, 19),
            "business_date": date(2026, 8, 19),
            "commodity": f"现货品种{index}",
            "region": f"现货地区{index}",
            "quote_type": "基差报价",
            "delivery_month": "现货",
            "futures_contract": "2609",
            "cash_price": None,
            "futures_price": None,
            "basis": basis,
            "source_sheet": f"basis_price:source_{index}",
            "series_id": f"market.basis.domestic.china.fixture_{index}",
            "segment": "LIVE_LUTOU",
            "provider": "Lutou",
            "source_series_id": f"live:{index}",
            "source_locator": f"lutou:fixture:{index}",
            "currency": "CNY",
            "unit": "CNY/metric_tonne",
        })
    return pd.DataFrame(rows)


def _manifest(rows: pd.DataFrame, release_id: str = "formal-basis-release") -> dict[str, object]:
    return {
        "release_id": release_id,
        "schema_version": "lutou-domestic-basis-current/3",
        "source": "sealed_history_plus_lutou",
        "scope": "formal-domestic-basis",
        "quality_status": "PASS",
        "cutover_date": "2026-06-01",
        "row_count": len(rows),
        "series_count": 29,
        "historical_row_count": 8,
        "live_row_count": 21,
        "min_date": "2026-05-20",
        "max_date": "2026-08-19",
        "source_max_date": "2026-08-19",
        "formal_contract_parity": {
            "quality_status": "PASS",
            "baseline_sha256": reader.FORMAL_BASELINE_SHA256,
            "baseline_rows": 8,
            "common_rows": 8,
            "baseline_only_rows": 0,
            "public_only_nonextension_rows": 0,
            "field_differences": {field: 0 for field in reader._CONSUMER_FIELDS},
        },
        "quality": {
            "stable_key_duplicate_count": 0,
            "legacy_contract_collision_count": 0,
        },
    }


def _install_current(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, rows: pd.DataFrame,
    *, release_id: str = "formal-basis-release",
    manifest_overrides: dict[str, object] | None = None,
) -> Path:
    monkeypatch.setattr(reader, "_EXPECTED_HISTORICAL_ROWS", 8)
    monkeypatch.setattr(reader, "_EXPECTED_BASELINE_ROWS", 8)
    directory = tmp_path / "releases" / release_id
    directory.mkdir(parents=True)
    manifest = _manifest(rows, release_id)
    manifest.update(manifest_overrides or {})
    manifest_path = directory / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    manifest_sha256 = identify_file(manifest_path).sha256
    (tmp_path / "current.json").write_text(json.dumps({
        "schema_version": 1,
        "release_id": release_id,
        "manifest_sha256": manifest_sha256,
    }), encoding="utf-8")
    current = SimpleNamespace(
        release_id=release_id,
        directory=directory,
        manifest=manifest,
        observations=pa.Table.from_pandas(rows, preserve_index=False),
    )
    monkeypatch.setattr(reader, "load_domestic_basis_current", lambda _root: current)
    reader.clear_public_basis_current_cache()
    return tmp_path


def test_29_series_reader_preserves_formal_segments_and_traceability(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    root = _install_current(monkeypatch, tmp_path, _formal_rows())
    snapshot = reader.load_public_basis_current(root)

    assert snapshot.identity.row_count == 29
    assert snapshot.identity.series_count == 29
    assert snapshot.identity.max_date == date(2026, 8, 19)
    assert set(snapshot.records["current_release_id"]) == {"formal-basis-release"}
    assert set(snapshot.records["current_manifest_sha256"]) == {
        snapshot.identity.manifest_sha256
    }
    assert set(snapshot.records["source_segment_identity"]) == {
        "SEALED_HISTORICAL", "LIVE_LUTOU",
    }
    historical = snapshot.records[snapshot.records["segment"].eq("SEALED_HISTORICAL")]
    live = snapshot.records[snapshot.records["segment"].eq("LIVE_LUTOU")]
    assert set(historical["commodity"]) == set(HISTORICAL_COMMODITIES)
    assert set(historical["quote_type"]) == {"基差报价", "一口价"}
    assert historical["cash_price"].notna().all()
    assert live[["cash_price", "futures_price"]].isna().all().all()
    assert set(live["basis"]) == {Decimal("-10"), Decimal("0"), Decimal("10")}


def test_reader_cache_is_bound_to_release_and_manifest_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    root = _install_current(monkeypatch, tmp_path, _formal_rows())
    first = reader.load_public_basis_current(root)
    before = reader._resolve_current_versioned.cache_info()
    second = reader.load_public_basis_current(root)
    after = reader._resolve_current_versioned.cache_info()
    assert first.identity == second.identity
    assert after.hits == before.hits + 1

    with pytest.raises(reader.PublicBasisCurrentError) as exc:
        reader.load_public_basis_current(root, expected_release_id="another-release")
    assert exc.value.code is reader.PublicBasisCurrentErrorCode.CURRENT_IDENTITY_MISMATCH


@pytest.mark.parametrize(
    ("column", "value", "code"),
    [
        ("currency", "USD", reader.PublicBasisCurrentErrorCode.CURRENCY_MISMATCH),
        ("unit", "USD/T", reader.PublicBasisCurrentErrorCode.UNIT_MISMATCH),
    ],
)
def test_reader_fails_closed_for_wrong_currency_or_unit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, column: str, value: str,
    code: reader.PublicBasisCurrentErrorCode,
) -> None:
    rows = _formal_rows()
    rows.loc[0, column] = value
    root = _install_current(monkeypatch, tmp_path, rows)
    with pytest.raises(reader.PublicBasisCurrentError) as exc:
        reader.load_public_basis_current(root)
    assert exc.value.code is code


def test_reader_fails_closed_for_missing_pointer(tmp_path: Path) -> None:
    with pytest.raises(reader.PublicBasisCurrentError) as exc:
        reader.load_public_basis_current(tmp_path)
    assert exc.value.code is reader.PublicBasisCurrentErrorCode.PUBLIC_CURRENT_UNAVAILABLE


def test_reader_fails_closed_for_invalid_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    root = _install_current(
        monkeypatch, tmp_path, _formal_rows(),
        manifest_overrides={"quality_status": "FAIL"},
    )
    with pytest.raises(reader.PublicBasisCurrentError) as exc:
        reader.load_public_basis_current(root)
    assert exc.value.code is reader.PublicBasisCurrentErrorCode.INVALID_CURRENT_MANIFEST


def test_reader_fails_closed_when_required_historical_commodity_is_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    rows = _formal_rows()
    rows.loc[rows["commodity"].eq("葵粕"), "commodity"] = "一豆"
    root = _install_current(monkeypatch, tmp_path, rows)
    with pytest.raises(reader.PublicBasisCurrentError) as exc:
        reader.load_public_basis_current(root)
    assert exc.value.code is reader.PublicBasisCurrentErrorCode.SERIES_NOT_FOUND


def test_historical_consumer_and_summary_parity_is_deterministic(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    source = _formal_rows()
    root = _install_current(monkeypatch, tmp_path, source)
    snapshot = reader.load_public_basis_current(root)
    fields = list(reader._CONSUMER_FIELDS)
    legacy = source[source["segment"].eq("SEALED_HISTORICAL")][fields].reset_index(drop=True)
    public = snapshot.records[
        snapshot.records["segment"].eq("SEALED_HISTORICAL")
    ][fields].copy()
    public["date"] = public["date"].dt.date
    pd.testing.assert_frame_equal(legacy, public.reset_index(drop=True), check_dtype=False)
    generated_at = datetime(2026, 8, 20, tzinfo=timezone.utc)
    old_summary = build_basis_summary(
        legacy, source_identity={"contract": "same"}, generated_at=generated_at
    )
    new_summary = build_basis_summary(
        public, source_identity={"contract": "same"}, generated_at=generated_at
    )
    assert old_summary.facts == new_summary.facts
    assert old_summary.detail_text == new_summary.detail_text
    assert old_summary.short_text == new_summary.short_text


def test_live_nulls_do_not_become_zero_or_derived_cash_prices(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    root = _install_current(monkeypatch, tmp_path, _formal_rows())
    snapshot = reader.load_public_basis_current(root)
    live = snapshot.records[snapshot.records["segment"].eq("LIVE_LUTOU")]
    summary = build_basis_summary(
        snapshot.records,
        source_identity=snapshot.source_identity,
        source_dataset="public_domestic_basis_current",
        generated_at=datetime(2026, 8, 20, tzinfo=timezone.utc),
    )
    assert live[["cash_price", "futures_price"]].isna().all().all()
    assert not live["cash_price"].fillna(0).ne(0).any()
    assert all(
        quote["cash_price"] is None and quote["futures_price"] is None
        for quote in summary.facts["quotes"]
    )
    assert summary.source_dataset == "public_domestic_basis_current"
    assert summary.source_date == "2026-08-19"


def test_formal_runtime_sources_have_no_legacy_or_live_fallback() -> None:
    root = Path(__file__).resolve().parents[2]
    sources = "\n".join(
        (root / relative).read_text(encoding="utf-8")
        for relative in (
            "05_apps/basis_page.py",
            "05_apps/research_overview_page.py",
            "05_apps/streamlit_app.py",
        )
    )
    assert "basis_quotes.parquet" not in sources
    assert "basis_quotes_sample.parquet" not in sources
    assert "basis_sql_loader" not in sources
    assert "LutouClient" not in sources
    assert "LutouDomesticBasisLiveAdapter" not in sources
