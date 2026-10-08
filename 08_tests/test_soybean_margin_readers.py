from datetime import date
import hashlib
import json
from pathlib import Path
import sys

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from agri_research_agent.pipelines.tankan_goal_a import (
    load_current, load_current_files, TankanGoalAError,
)
from agri_research_agent.soybean_margin import public_inputs as readers
from agri_research_agent.soybean_margin.public_inputs import apply_public_inputs


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "05_apps"))
import soybean_margin_page as page


def identity(path):
    return {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "size_bytes": path.stat().st_size}


def seal(root, market, fx):
    directory = root / "public-market-data/tankan/releases/view-test"
    directory.mkdir(parents=True, exist_ok=True)
    pq.write_table(market, directory / "market.parquet", row_group_size=64)
    pq.write_table(fx, directory / "fx.parquet")
    manifest = dict(schema_version="tankan-goal-a-current/1", release_id="view-test",
                    source="tankan", promoted_at="2026-08-18T00:00:00Z",
                    canonical_manifest_sha256="a" * 64, market={}, fx={},
                    quality_status="PASS",
                    files={name: identity(directory / name)
                           for name in ("market.parquet", "fx.parquet")})
    manifest_path = directory / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    (directory.parent.parent / "current.json").write_text(json.dumps(
        dict(schema_version=1, release_id="view-test",
             manifest_sha256=identity(manifest_path)["sha256"])), encoding="utf-8")
    return directory


@pytest.fixture
def published(tmp_path):
    fixtures = ROOT / "08_tests/fixtures/spread_runtime"
    market = pq.read_table(fixtures / "soybean_public_market.parquet")
    fx = pq.read_table(fixtures / "soybean_public_fx.parquet")
    day = date(2026, 8, 17)
    domestic = pd.DataFrame([
        dict(date=day, status="success", season="2026/2027",
             leg1_instrument="M", leg1_month=1, leg1_price=3000.,
             leg2_instrument="Y", leg2_month=1, leg2_price=8000.,
             leg1_contract="M2701", leg2_contract="Y2701", unused="x" * 4096),
        dict(date=day, status="success", season="2026/2027",
             leg1_instrument="A", leg1_month=1, leg1_price=10.,
             leg2_instrument="C", leg2_month=1, leg2_price=11.,
             leg1_contract="A2701", leg2_contract="C2701", unused="y" * 4096),
    ])
    domestic_path = tmp_path / "domestic.parquet"
    domestic.to_parquet(domestic_path, index=False)
    seal(tmp_path, market, fx)
    return tmp_path, domestic_path, market, fx, domestic


def test_metadata_load_authenticates_without_reading_full_tables(published, monkeypatch):
    root, _, _, _, _ = published
    def forbidden(*args, **kwargs):
        raise AssertionError("full Arrow table must not be materialized")
    monkeypatch.setattr(pq, "read_table", forbidden)
    current = load_current_files(root / "public-market-data/tankan")
    assert current.release_id == "view-test"
    assert set(current.manifest["files"]) == {"market.parquet", "fx.parquet"}


def test_projected_page_read_is_equal_to_existing_exact_contract_calculation(published):
    root, domestic_path, market, fx, domestic = published
    day = date(2026, 8, 17)
    history = pd.DataFrame([dict(business_date=day, origin="brazil", shipment_year=2026,
                                shipment_month=9, cnf_cents_per_bushel=0.)])
    selected_market, selected_fx, selected_domestic, release = readers.read_public_tables(root, domestic_path)
    old_data, old_latest = apply_public_inputs(history, market.to_pandas(), fx.to_pandas(), domestic, day)
    new_data, new_latest = apply_public_inputs(history, selected_market, selected_fx, selected_domestic, day)
    pd.testing.assert_frame_equal(old_data, new_data)
    assert old_latest == new_latest
    assert release == "view-test"
    assert list(selected_market.columns) == list(readers.MARKET_COLUMNS)
    assert list(selected_fx.columns) == list(readers.FX_COLUMNS)
    assert selected_market["product"].eq("SOYBEAN").all()
    assert len(selected_domestic) == 1 and "unused" not in selected_domestic
    complete = load_current(root / "public-market-data/tankan")
    assert complete.market.equals(market)
    assert complete.fx.equals(fx)


def test_large_unrelated_rows_and_provenance_never_enter_page_view(published, monkeypatch):
    root, domestic_path, market, fx, _ = published
    unrelated = market.slice(0, 1).to_pylist()[0]
    unrelated["source_locator"] = "x" * 8192
    enlarged = pa.concat_tables([market, pa.Table.from_pylist(
        [unrelated] * 4096, schema=market.schema)])
    seal(root, enlarged, fx)
    def forbidden(*args, **kwargs):
        raise AssertionError("consumer must not call read_table")
    monkeypatch.setattr(pq, "read_table", forbidden)
    selected, _, _, _ = readers.read_public_tables(root, domestic_path)
    assert len(selected) == 1
    assert "source_locator" not in selected
    assert selected.memory_usage(deep=True).sum() < 4096


@pytest.mark.parametrize("limit", ["MAX_VIEW_ROWS", "MAX_VIEW_BYTES"])
def test_page_read_limit_fails_instead_of_truncating(published, monkeypatch, limit):
    root, domestic_path, _, _, _ = published
    monkeypatch.setattr(readers, limit, 0)
    with pytest.raises(ValueError, match="bounded page read limit"):
        readers.read_public_tables(root, domestic_path)


@pytest.mark.parametrize("filename", ["market.parquet", "fx.parquet"])
def test_projection_does_not_bypass_whole_file_identity(published, filename):
    root, domestic_path, _, _, _ = published
    path = root / "public-market-data/tankan/releases/view-test" / filename
    path.write_bytes(path.read_bytes() + b"tampered")
    with pytest.raises(TankanGoalAError, match="file identity"):
        readers.read_public_tables(root, domestic_path)


def test_full_schema_is_checked_including_unselected_columns(published):
    root, domestic_path, market, fx, _ = published
    seal(root, market.drop(["source_locator"]), fx)
    with pytest.raises(TankanGoalAError, match="schema"):
        readers.read_public_tables(root, domestic_path)


def test_missing_current_and_domestic_fields_fail_closed(published, tmp_path):
    root, domestic_path, _, _, _ = published
    with pytest.raises(ValueError, match="not published"):
        readers.read_public_tables(tmp_path / "missing", domestic_path)
    pd.DataFrame({"date": [date(2026, 8, 17)]}).to_parquet(domestic_path)
    with pytest.raises(ValueError, match="fields are incomplete"):
        readers.read_public_tables(root, domestic_path)


def test_refresh_invalidates_one_version_not_other_cached_sessions(tmp_path, monkeypatch):
    page._public_tables.clear()
    calls = []
    def fake(root, path):
        calls.append(root)
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), "fixture"
    monkeypatch.setattr(page, "read_public_tables", fake)
    arguments = []
    for name in ("first", "second"):
        root = tmp_path / name
        pointer = root / "public-market-data/tankan/current.json"
        pointer.parent.mkdir(parents=True)
        pointer.write_text("{}", encoding="utf-8")
        domestic = root / "domestic.parquet"
        domestic.write_bytes(b"fixture")
        arguments.append((str(root), str(domestic), page.digest(pointer) + page.digest(domestic)))
    try:
        for args in arguments:
            page._public_tables(*args)
        page._public_tables.clear(*arguments[0])
        page._public_tables(*arguments[1])
        assert calls == [Path(args[0]) for args in arguments]
        page._public_tables(*arguments[0])
        assert len(calls) == 3
        assert not hasattr(page._public_rows, "clear")
    finally:
        page._public_tables.clear()


def test_public_identity_race_still_rejects_a_view(published, monkeypatch):
    root, domestic_path, _, _, _ = published
    page._public_tables.clear()
    try:
        with pytest.raises(ValueError, match="发生变化"):
            page._public_tables(str(root), str(domestic_path), "a" * 128)
    finally:
        page._public_tables.clear()
