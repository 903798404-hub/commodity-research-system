from datetime import date
from decimal import Decimal
import json
from types import SimpleNamespace

import pyarrow as pa
import pytest
from openpyxl import Workbook

from agri_research_agent.data_sources import nutstore_basis as source
from agri_research_agent.market_data import public_basis_current as reader
from agri_research_agent.pipelines import lutou_domestic_basis as existing
from agri_research_agent.pipelines import nutstore_domestic_basis as pipeline


def row(**changes):
    return {"日期": date(2026, 9, 29), "品种": "豆粕", "地区": "华东",
            "报价类别": "现货基差", "期货合约": "2701", "基差": 0, **changes}


def workbook(monkeypatch, tmp_path, records):
    path = tmp_path / "123/国内基差及一口价/基差数据.xlsx"
    path.parent.mkdir(parents=True)
    book = Workbook()
    book.active.title = source.SOURCE_SHEET
    book.active.append(source.COLUMNS)
    for record in records:
        book.active.append([record.get(field) for field in source.COLUMNS])
    book.save(path)
    book.close()
    monkeypatch.setattr(source, "PROTECTED_ROOT", tmp_path / "123")
    monkeypatch.setattr(source, "SOURCE_PATH", path)
    return path


def baseline(monkeypatch, tmp_path):
    baseline_root = tmp_path / "baseline"
    directory = baseline_root / "releases/original"
    directory.mkdir(parents=True)
    historical = {field.name: None for field in existing.FORMAL_CURRENT_SCHEMA}
    historical.update({
        "schema_version": "public-domestic-basis-current/3", "segment": "SEALED_HISTORICAL",
        "series_id": "historical", "provider_dataset_id": "historical",
        "provider_series_id": "historical", "source_series_id": "historical",
        "provider": "Historical Domestic Basis Excel", "business_date": date(2026, 5, 29),
        "date": date(2026, 5, 29), "commodity": "豆粕", "region": "华东",
        "product": "soybean_meal", "consumer_product": "豆粕", "location": "华东",
        "region_id": "east", "quote_type": "基差报价", "canonical_quote_type": "现货基差",
        "delivery_month": "现货", "futures_contract": "2609", "value": Decimal("100"),
        "cash_price": Decimal("3100"), "futures_price": Decimal("3000"), "basis": Decimal("100"),
        "currency": "CNY", "unit": "CNY/metric_tonne", "source_sheet": "history",
        "source_locator": "history", "source_row_count": 1, "far_contract_row_count": 0,
        "source_group_sha256": "a" * 64, "aggregation_method": "none",
        "query_identity": "history", "snapshot_identity": "history",
        "mapping_version": "history", "evidence_type": "history", "live_status": "SEALED",
    })
    historical_cash = dict(historical, series_id="historical_cash", quote_type="一口价",
                           futures_contract=None, basis=None, futures_price=None)
    hist = pa.Table.from_pylist([historical, historical_cash], schema=existing.FORMAL_CURRENT_SCHEMA)
    live = dict(historical, segment="LIVE_LUTOU", provider="Lutou", series_id="live",
                date=date(2026, 9, 3), business_date=date(2026, 9, 3),
                cash_price=None, futures_price=None)
    table = pa.Table.from_pylist([historical, historical_cash, live], schema=existing.FORMAL_CURRENT_SCHEMA)
    parity = {
        "quality_status": "PASS", "baseline_sha256": existing.FORMAL_BASELINE_SHA256,
        "baseline_rows": 3, "common_rows": 3, "baseline_only_rows": 0,
        "public_only_nonextension_rows": 0,
        "field_differences": {field: 0 for field in reader._CONSUMER_FIELDS},
    }
    manifest = {"max_date": "2026-09-03", "formal_contract_parity": parity}
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (baseline_root / "historical-seeds").mkdir()
    (baseline_root / "historical-seed.json").write_text("{}", encoding="utf-8")
    seed = SimpleNamespace(observations=hist)
    current = SimpleNamespace(release_id="original", directory=directory,
                              observations=table, manifest=manifest)
    original_loader = existing.load_domestic_basis_current
    monkeypatch.setattr(pipeline, "load_domestic_basis_current",
                        lambda root: current if root == baseline_root else original_loader(root))
    monkeypatch.setattr(pipeline, "load_historical_basis_seed", lambda _: seed)
    monkeypatch.setattr(reader, "_EXPECTED_HISTORICAL_ROWS", 2)
    monkeypatch.setattr(reader, "_EXPECTED_BASELINE_ROWS", 3)
    monkeypatch.setattr(reader, "_EXPECTED_HISTORICAL_COMMODITIES", {"豆粕"})
    return baseline_root, current


def test_candidate_retains_all_history_and_is_readable_by_actual_page(monkeypatch, tmp_path):
    original, current = baseline(monkeypatch, tmp_path)
    path = workbook(monkeypatch, tmp_path, [
        row(品种=product, 基差=index - 2)
        for index, product in enumerate(source.PRODUCTS)
    ] + [row(报价类别="一口价", 现货价=3300, 基差=None, 期货合约=None)])
    before = source.sha256(path)
    output = tmp_path / "candidate/lutou-domestic-basis"
    result = pipeline.build_nutstore_basis_candidate(
        baseline_root=original, output_root=output, source=path,
    )
    assert result["candidate_rows"] == 6
    snapshot = reader.load_public_basis_current(output)
    assert snapshot.identity.row_count == 9 and snapshot.identity.series_count == 9
    assert snapshot.identity.max_date == date(2026, 9, 29)
    actual = existing.load_domestic_basis_current(output)
    with pytest.raises(existing.DomesticBasisPipelineError, match="retired"):
        existing._extract_live_canonical(actual)
    preserved = pa.Table.from_pylist(
        [item for item in actual.observations.to_pylist() if item["business_date"] <= date(2026, 9, 3)],
        schema=existing.FORMAL_CURRENT_SCHEMA,
    )
    assert existing._business_sha(preserved) == existing._business_sha(current.observations)
    assert source.sha256(path) == before
    pointer = (output / "current.json").read_bytes()
    with pytest.raises(FileExistsError):
        pipeline.build_nutstore_basis_candidate(baseline_root=original, output_root=output, source=path)
    assert (output / "current.json").read_bytes() == pointer

    # A reader rejects alterations to old observations even with recomputed outer hashes.
    rows = actual.observations.to_pylist()
    rows[0]["basis"] += 1
    damaged = pa.Table.from_pylist(rows, schema=existing.FORMAL_CURRENT_SCHEMA)
    with pytest.raises(existing.DomesticBasisPipelineError, match="changed or dropped"):
        pipeline.validate_nutstore_current(output, actual.directory, dict(actual.manifest), damaged)


def test_retired_lutou_provider_preserves_nutstore_and_other_provider(monkeypatch, tmp_path):
    from agri_research_agent.pipelines import public_data_delivery as delivery
    from agri_research_agent.pipelines.public_data_providers import DomesticBasisRefreshAdapter
    from agri_research_agent.pipelines.public_data_refresh import (
        CurrentIdentity, OverallStatus, RefreshResult, run_unified_refresh,
    )
    from agri_research_agent.shared.runtime_context import RuntimeContext, RuntimeMode
    original, _current = baseline(monkeypatch, tmp_path)
    path = workbook(monkeypatch, tmp_path, [row(品种=product) for product in source.PRODUCTS])
    public = tmp_path / "public-market-data"
    output = public / "lutou-domestic-basis"
    pipeline.build_nutstore_basis_candidate(baseline_root=original, output_root=output, source=path)
    (tmp_path / ".market-data-runtime.json").write_text(json.dumps({
        "schema_version": 1, "runtime_id": "nutstore-retirement-fixture",
        "classification": "isolated-dev", "module_id": "international-spread",
        "created_at": "2026-09-29T00:00:00Z",
    }), encoding="utf-8")
    runtime = RuntimeContext(RuntimeMode.ISOLATED_DEV, "international-spread", tmp_path)
    adapter = DomesticBasisRefreshAdapter(
        None, runtime, "retired", tmp_path / "no-old-catalog.yaml",
        connector=lambda *_: pytest.fail("Retired source must not connect"),
    )
    assert adapter.current_identity().source_max_dates == {"domestic_basis": "2026-09-03"}
    pointer = (output / "current.json").read_bytes()
    calls = []
    class OtherProvider:
        name = "tankan"
        def current_identity(self):
            return CurrentIdentity("unchanged", "a" * 64, {"market": "2026-09-29"})
        def preflight(self):
            calls.append("other-preflight")
            return {}
        def refresh(self):
            calls.append("other-refresh")
            return RefreshResult(False, {"market": "2026-09-29"}, {"market": "NO_CHANGE"})
    result = run_unified_refresh(runtime=runtime, run_id="retired-source-test",
                                 adapters=[adapter, OtherProvider()])
    assert result.overall_status is OverallStatus.SUCCESS_WITH_UNAVAILABLE_SOURCE
    assert calls == ["other-preflight", "other-refresh"]
    assert (output / "current.json").read_bytes() == pointer
    dates = delivery._with_current_source_dates(public, ["lutou-domestic-basis"],
                                                {"lutou_domestic_basis.domestic_basis": "2026-09-03"})
    assert dates == {"lutou_domestic_basis.domestic_basis": "2026-09-03",
                     "nutstore.domestic_basis": "2026-09-29"}
    with pytest.raises(delivery.DeliveryError, match="source date differs"):
        delivery._with_current_source_dates(public, ["lutou-domestic-basis"],
                                             {"nutstore.domestic_basis": "2026-10-09"})


def test_failure_does_not_create_candidate_or_modify_baseline(monkeypatch, tmp_path):
    original, current = baseline(monkeypatch, tmp_path)
    path = workbook(monkeypatch, tmp_path, [row()])
    before = (current.directory / "manifest.json").read_bytes()
    output = tmp_path / "candidate"
    with pytest.raises(ValueError, match="five basis products"):
        pipeline.build_nutstore_basis_candidate(baseline_root=original, output_root=output, source=path)
    assert not output.exists()
    assert (current.directory / "manifest.json").read_bytes() == before
