from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import agri_research_agent.pipelines.soybean_intraday as intraday_pipeline

from agri_research_agent.import_profit.cnf_store import (
    CnfQuoteRecord,
    CnfStoreSnapshot,
    load_cnf_store,
)
from agri_research_agent.import_profit.config import load_soybean_config
from agri_research_agent.import_profit.contract_override import select_soybean_contracts
from agri_research_agent.import_profit.intraday import (
    SoybeanIntradayCnfMismatchError,
    SoybeanIntradaySnapshotMissingError,
    calculate_soybean_intraday_profit,
    load_soybean_intraday_market_inputs,
    require_shared_cnf_identity,
    required_intraday_contracts_for_date,
    select_soybean_intraday_market_inputs,
)
from agri_research_agent.import_profit.intraday_store import (
    IntradayProfitSealStatus,
    SoybeanIntradayResultBatch,
    read_intraday_profit_rows,
    seal_intraday_profit_batch,
)
from agri_research_agent.import_profit.models import BusinessKey
from agri_research_agent.market_data.contracts import ContractId, Exchange
from agri_research_agent.market_data.intraday import (
    FreshnessStatus,
    InstrumentAvailabilityStatus,
    IntradayQuote,
    IntradaySnapshot,
    IntradayUnavailableInstrument,
    MarketSession,
    seal_intraday_snapshot as _seal_public_snapshot,
)
from agri_research_agent.shared.runtime_context import RuntimeContext, RuntimeMode
from agri_research_agent.pipelines.soybean_intraday import (
    save_manual_cnf_and_materialize_am,
    save_soybean_intraday_manual_cnf,
)


ROOT = Path(__file__).resolve().parents[1]
CONFIG = load_soybean_config(ROOT / "02_configs" / "import_profit_soybean.yaml")
DAY = date(2026, 8, 28)
CN = ZoneInfo("Asia/Shanghai")


def seal_fixture_snapshot(root: Path, snapshot: IntradaySnapshot):
    """Use the public v2 sealer with a real, pytest-isolated runtime marker."""
    root.mkdir(parents=True, exist_ok=True)
    marker = root / '.market-data-runtime.json'
    if not marker.exists():
        marker.write_text(json.dumps({
            'schema_version': 1, 'runtime_id': 'soybean-consumer-fixture',
            'classification': 'fixture', 'module_id': 'shared-intraday',
            'created_at': snapshot.captured_at.isoformat(),
        }), encoding='utf-8')
    context = RuntimeContext(RuntimeMode.FIXTURE, 'shared-intraday', root)
    return _seal_public_snapshot(root, snapshot, context=context)


def key(origin: str = "brazil", year: int = 2026, month: int = 12) -> BusinessKey:
    return BusinessKey(DAY, "soybean", origin, year, month, CONFIG.origin_codes, CONFIG.commodity)


def public_snapshot(session: MarketSession, business_key: BusinessKey, *, delta: float = 0) -> IntradaySnapshot:
    captured = datetime(
        2026, 8, 28,
        9 if session is MarketSession.AM else 15,
        15,
        tzinfo=CN,
    )
    selection = select_soybean_contracts(CONFIG, business_key)
    c = selection.cbot.effective_contract
    m = selection.soymeal.effective_contract
    y = selection.soyoil.effective_contract
    values = (
        (str(ContractId(Exchange.CBOT, "SOYBEAN", c.contract_year, c.contract_month)), f"{c.contract_year%100:02d}{c.contract_month:02d}", "CBOT", "SOYBEAN", 1200 + delta, "USD", "US_CENTS_PER_BUSHEL", "market.foreign_futures_live"),
        ("FX:USD/CNH:SPOT", "", "OTC", "USD/CNH", 7.2 + delta / 10000, "CNH", "CNH_PER_USD", "market.exchange_rate_live"),
        (str(ContractId(Exchange.DCE, "SOYMEAL", m.contract_year, m.contract_month)), m.code, "DCE", "SOYMEAL", 3200 + delta, "CNY", "CNY_PER_METRIC_TONNE", "market.futures_live"),
        (str(ContractId(Exchange.DCE, "SOYOIL", y.contract_year, y.contract_month)), y.code, "DCE", "SOYOIL", 8000 + delta, "CNY", "CNY_PER_METRIC_TONNE", "market.futures_live"),
    )
    quotes = tuple(
        IntradayQuote(
            DAY, session, captured, instrument, contract, exchange, product, price,
            "MID" if instrument.startswith("FX:") else "LAST", currency, unit,
            "TANKAN", table, captured, DAY if instrument.startswith("FX:") else None,
            FreshnessStatus.FRESH, {"source": "fixture"}, captured,
        )
        for instrument, contract, exchange, product, price, currency, unit, table in values
    )
    return IntradaySnapshot(DAY, session, captured, quotes, "fixture", "fixture",
                            environment="TEST_ISOLATED_NON_PRODUCTION")


def cnf_store(business_key: BusinessKey, identity: str = "a" * 64) -> CnfStoreSnapshot:
    record = CnfQuoteRecord(business_key, 150.0, "manual_ui", datetime.now(timezone.utc), "batch")
    return CnfStoreSnapshot((record,), True, 1, identity)


def mixed_snapshot(
    session: MarketSession,
    available_key: BusinessKey,
    unavailable_key: BusinessKey,
) -> IntradaySnapshot:
    available = public_snapshot(session, available_key)
    future = public_snapshot(session, unavailable_key)
    future_selection = select_soybean_contracts(CONFIG, unavailable_key)
    unavailable_codes = {
        future_selection.soymeal.effective_contract.code,
        future_selection.soyoil.effective_contract.code,
    }
    by_key = {quote.key: quote for quote in available.quotes}
    by_key.update(
        {
            quote.key: quote
            for quote in future.quotes
            if quote.contract_code not in unavailable_codes
        }
    )
    evidence = tuple(
        IntradayUnavailableInstrument(
            instrument_id=str(
                ContractId(
                    Exchange.DCE,
                    product,
                    contract.contract_year,
                    contract.contract_month,
                )
            ),
            contract_code=contract.code,
            exchange="DCE",
            product=product,
            status=InstrumentAvailabilityStatus.CONTRACT_NOT_AVAILABLE,
            reason="REQUESTED_EXACT_CONTRACT_ABSENT_FROM_LIVE_SOURCE",
            provenance={"source_table": "market.futures_live", "listing_status": "UNVERIFIED"},
        )
        for product, contract in (
            ("SOYMEAL", future_selection.soymeal.effective_contract),
            ("SOYOIL", future_selection.soyoil.effective_contract),
        )
    )
    return IntradaySnapshot(
        DAY,
        session,
        available.captured_at,
        tuple(by_key.values()),
        "fixture",
        "fixture",
        environment="TEST_ISOLATED_NON_PRODUCTION",
        unavailable_instruments=evidence,
    )


def calculated(session: MarketSession, business_key: BusinessKey, *, delta: float = 0, cnf_identity: str = "a" * 64):
    snapshot = public_snapshot(session, business_key, delta=delta)
    inputs = select_soybean_intraday_market_inputs(snapshot, business_key=business_key, config=CONFIG)
    return calculate_soybean_intraday_profit(
        inputs, cnf_store=cnf_store(business_key, cnf_identity), config=CONFIG,
        calculated_at=datetime.now(timezone.utc),
    )


def test_dynamic_contract_union_uses_existing_mapping_and_exact_codes() -> None:
    cbot, meal, oil = required_intraday_contracts_for_date(DAY, CONFIG)
    assert cbot and meal and oil
    assert all(len(code) == 4 and code.isdigit() for code in cbot)
    assert all(code.startswith("M") and len(code) == 5 for code in meal)
    assert all(code.startswith("Y") and len(code) == 5 for code in oil)


def test_other_day_cnf_cannot_materialize_target_day(tmp_path, monkeypatch):
    from datetime import timedelta
    from agri_research_agent.import_profit.intraday import SoybeanIntradayCnfError
    path = tmp_path / 'cnf.parquet'
    values = {(origin, month): 150.0 if origin == 'brazil' and month == 1 else None
              for origin in CONFIG.origin_codes for month in range(1, 13)}
    save_soybean_intraday_manual_cnf(cnf_store_path=path, business_date=DAY,
        values=values, config=CONFIG, updated_at=datetime.now(timezone.utc))
    before = path.read_bytes()
    monkeypatch.setattr(intraday_pipeline, 'load_soybean_intraday_market_inputs',
                        lambda *args, **kwargs: pytest.fail('CNF gate must precede market reads'))
    with pytest.raises(SoybeanIntradayCnfError, match='target business date'):
        intraday_pipeline.materialize_soybean_intraday_profit(snapshot_root=str(tmp_path / 'snapshots'),
            result_root=str(tmp_path / 'results'), cnf_store_path=str(path),
            business_date=DAY + timedelta(days=3), session=MarketSession.AM, config=CONFIG,
            calculated_at=datetime.now(timezone.utc))
    assert path.read_bytes() == before and not (tmp_path / 'results').exists()


@pytest.mark.parametrize('rejected_month', [9, 10, 11, 12])
@pytest.mark.parametrize('rejected_value', [None, 0.0, 150.0])
def test_historical_excel_formal_save_rejected_without_partial_write(
    tmp_path, rejected_month, rejected_value,
):
    from hashlib import sha256
    from agri_research_agent.import_profit.cnf_store import (
        CnfQuoteUpdate, CnfStoreValidationError, upsert_cnf_quotes,
    )

    path = tmp_path / 'cnf.parquet'
    stamp = datetime.now(timezone.utc)
    # A real isolated store includes number, zero and NULL, in unsorted input order.
    upsert_cnf_quotes(path, tuple(
        CnfQuoteUpdate(key(month=month), value, 'manual_ui', stamp, 'fixture')
        for month, value in ((12, 150.0), (10, None), (11, 0.0))
    ), allowed_origins=CONFIG.origin_codes, expected_store_sha256=None)
    before = load_cnf_store(path, allowed_origins=CONFIG.origin_codes)
    before_bytes = path.read_bytes()
    before_files = {p.relative_to(tmp_path): p.read_bytes()
                    for p in tmp_path.rglob('*') if p.is_file()}
    assert before.record_count == 3
    assert before.store_sha256 == sha256(before_bytes).hexdigest().upper()

    def updates():
        # Even an earlier valid update must not be partially applied.
        yield CnfQuoteUpdate(key(month=12), 999.0, 'manual_ui', stamp, 'rejected')
        yield CnfQuoteUpdate(key(month=rejected_month), rejected_value,
                             'historical_excel', stamp, 'rejected')

    with pytest.raises(CnfStoreValidationError, match='^source must be manual_ui$') as error:
        upsert_cnf_quotes(path, updates(), allowed_origins=CONFIG.origin_codes,
                         expected_store_sha256=before.store_sha256)
    assert type(error.value) is CnfStoreValidationError
    assert error.value.status == 'validation_failed'
    after = load_cnf_store(path, allowed_origins=CONFIG.origin_codes)
    assert after == before
    assert after.records == before.records  # Includes provenance and record ordering.
    assert path.read_bytes() == before_bytes
    assert after.store_sha256 == before.store_sha256 == sha256(path.read_bytes()).hexdigest().upper()
    assert {p.relative_to(tmp_path): p.read_bytes()
            for p in tmp_path.rglob('*') if p.is_file()} == before_files
    assert {r.business_key.shipment_month: r.cnf_cents_per_bushel
            for r in after.records} == {10: None, 11: 0.0, 12: 150.0}
    assert all(r.source == 'manual_ui' and r.batch_id == 'fixture' for r in after.records)


@pytest.mark.parametrize('field,value', [
    ('immutable', False), ('status', 'PREVIEW'), ('environment', 'FINAL_UI_PREVIEW'),
    ('business_date', '2026-08-27'), ('session', 'AM'), ('release_id', 'other'),
    ('schema_version', 'unapproved'),
])
def test_result_reader_rejects_tampered_sealed_identity(tmp_path, field, value):
    from agri_research_agent.import_profit.intraday_store import (
        load_intraday_profit_batch, IntradayProfitStoreValidationError)
    result = calculated(MarketSession.PM, key())
    batch = SoybeanIntradayResultBatch(DAY, MarketSession.PM,
        result.market_snapshot_release_id, result.market_snapshot_sha256,
        result.market_captured_at, result.cnf_identity, result.calculated_at, (result,))
    sealed = seal_intraday_profit_batch(tmp_path, batch)
    manifest_path = sealed.release_dir / 'manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    manifest[field] = value
    manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
    with pytest.raises(IntradayProfitStoreValidationError):
        load_intraday_profit_batch(tmp_path, DAY, MarketSession.PM)


def test_legacy_result_environment_requires_verified_snapshot(tmp_path):
    from agri_research_agent.import_profit.intraday_store import (
        load_intraday_profit_batch, IntradayProfitStoreValidationError)
    snapshot = public_snapshot(MarketSession.PM, key())
    seal_fixture_snapshot(tmp_path / 'snapshots', snapshot)
    result = calculated(MarketSession.PM, key())
    batch = SoybeanIntradayResultBatch(DAY, MarketSession.PM,
        result.market_snapshot_release_id, result.market_snapshot_sha256,
        result.market_captured_at, result.cnf_identity, result.calculated_at, (result,))
    sealed = seal_intraday_profit_batch(tmp_path / 'results', batch)
    manifest_path = sealed.release_dir / 'manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    manifest.pop('environment')
    manifest.pop('status')
    manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
    before = manifest_path.read_bytes()
    with pytest.raises(IntradayProfitStoreValidationError, match='environment'):
        load_intraday_profit_batch(tmp_path / 'results', DAY, 'PM', expected_environment='FORMAL')
    resolved = load_intraday_profit_batch(tmp_path / 'results', DAY, 'PM',
        expected_environment='TEST_ISOLATED_NON_PRODUCTION', snapshot_root=tmp_path / 'snapshots')
    assert resolved.environment == 'TEST_ISOLATED_NON_PRODUCTION'
    assert manifest_path.read_bytes() == before


def test_manual_intraday_cnf_save_is_explicit_partial_and_manual_ui(
    tmp_path: Path,
) -> None:
    path = tmp_path / "manual-cnf" / "manual_cnf_quotes.parquet"
    values = {
        (origin, month): (
            150.0 if origin == "brazil" and month == 1 else None
        )
        for origin in CONFIG.origin_codes
        for month in range(1, 13)
    }
    receipt = save_soybean_intraday_manual_cnf(
        cnf_store_path=path,
        business_date=DAY,
        values=values,
        config=CONFIG,
        updated_at=datetime(2026, 8, 28, 1, 30, tzinfo=timezone.utc),
    )
    stored = load_cnf_store(path, allowed_origins=CONFIG.origin_codes)
    assert receipt.cnf_record_count == 1
    assert receipt.cnf_sha256 == stored.store_sha256
    assert receipt.coverage_by_origin["brazil"] == (1,)
    assert all(record.source == "manual_ui" for record in stored.records)
    assert not any(
        record.business_key.origin != "brazil"
        or record.business_key.shipment_month != 1
        for record in stored.records
    )


def test_manual_intraday_cnf_all_null_does_not_create_store(tmp_path: Path) -> None:
    path = tmp_path / "manual-cnf" / "manual_cnf_quotes.parquet"
    values = {
        (origin, month): None
        for origin in CONFIG.origin_codes
        for month in range(1, 13)
    }
    with pytest.raises(ValueError, match="no manual CNF change"):
        save_soybean_intraday_manual_cnf(
            cnf_store_path=path,
            business_date=DAY,
            values=values,
            config=CONFIG,
            updated_at=datetime(2026, 8, 28, 1, 30, tzinfo=timezone.utc),
        )
    assert not path.exists()


def test_manual_cnf_to_sealed_am_closure_preserves_snapshot(tmp_path: Path) -> None:
    captured = datetime(2026, 8, 28, 9, 30, tzinfo=CN)
    cbot, meal, oil = required_intraday_contracts_for_date(DAY, CONFIG)
    quote_specs = []
    for code in cbot:
        year, month = 2000 + int(code[:2]), int(code[2:])
        quote_specs.append(
            (
                str(ContractId(Exchange.CBOT, "SOYBEAN", year, month)),
                code, "CBOT", "SOYBEAN", 1200.0,
                "USD", "US_CENTS_PER_BUSHEL", "market.foreign_futures_live",
            )
        )
    for code in meal:
        year, month = 2000 + int(code[1:3]), int(code[3:])
        quote_specs.append(
            (
                str(ContractId(Exchange.DCE, "SOYMEAL", year, month)),
                code, "DCE", "SOYMEAL", 3200.0,
                "CNY", "CNY_PER_METRIC_TONNE", "market.futures_live",
            )
        )
    for code in oil:
        year, month = 2000 + int(code[1:3]), int(code[3:])
        quote_specs.append(
            (
                str(ContractId(Exchange.DCE, "SOYOIL", year, month)),
                code, "DCE", "SOYOIL", 8000.0,
                "CNY", "CNY_PER_METRIC_TONNE", "market.futures_live",
            )
        )
    quote_specs.append(
        (
            "FX:USD/CNH:SPOT", "", "OTC", "USD/CNH", 7.2,
            "CNH", "CNH_PER_USD", "market.exchange_rate_live",
        )
    )
    snapshot = IntradaySnapshot(
        DAY,
        MarketSession.AM,
        captured,
        tuple(
            IntradayQuote(
                DAY, MarketSession.AM, captured, instrument, contract,
                exchange, product, price,
                "MID" if instrument.startswith("FX:") else "LAST",
                currency, unit, "TANKAN", table, captured, None,
                FreshnessStatus.FRESH, {"source": "fixture"}, captured,
            )
            for (
                instrument, contract, exchange, product, price,
                currency, unit, table,
            ) in quote_specs
        ),
        "fixture",
        "fixture",
        environment="TEST_ISOLATED_NON_PRODUCTION",
    )
    snapshot_root = tmp_path / "snapshots"
    result_root = tmp_path / "results"
    cnf_path = tmp_path / "manual-cnf" / "manual_cnf_quotes.parquet"
    seal_fixture_snapshot(snapshot_root, snapshot)
    values = {
        (origin, month): (
            150.0 if origin == "brazil" and month == 1 else None
        )
        for origin in CONFIG.origin_codes
        for month in range(1, 13)
    }
    receipt = save_manual_cnf_and_materialize_am(
        snapshot_root=snapshot_root,
        result_root=result_root,
        cnf_store_path=cnf_path,
        business_date=DAY,
        values=values,
        config=CONFIG,
        saved_at=datetime(2026, 8, 28, 1, 31, tzinfo=timezone.utc),
    )
    assert receipt.snapshot_immutability_pass
    assert receipt.am_materialization_status == "MATERIALIZED"
    assert receipt.materialize is not None
    assert receipt.snapshot_content_sha256_before == snapshot.content_sha256
    assert receipt.snapshot_content_sha256_after == snapshot.content_sha256
    assert receipt.am_record_count == 48
    assert receipt.calculable_periods == ("2027-01",)
    assert len(receipt.unavailable_periods) == 47


def test_am_preflight_failure_does_not_partially_write_formal_cnf(tmp_path: Path) -> None:
    """Keep the baseline node: invalid input never partially rewrites an existing CNF store."""
    cnf_path = tmp_path / "formal" / "manual_cnf_quotes.parquet"
    values = {(origin, month): None for origin in CONFIG.origin_codes for month in range(1, 13)}
    values["brazil", 1] = 150.0
    save_soybean_intraday_manual_cnf(
        cnf_store_path=cnf_path, business_date=DAY, values=values,
        config=CONFIG, updated_at=datetime(2026, 8, 28, 1, 30, tzinfo=timezone.utc),
    )
    before = cnf_path.read_bytes()
    invalid = dict(values)
    invalid["brazil", 1] = float("nan")
    with pytest.raises(ValueError, match="finite numbers"):
        save_manual_cnf_and_materialize_am(
            snapshot_root=tmp_path / "snapshots", result_root=tmp_path / "results",
            cnf_store_path=cnf_path, business_date=DAY, values=invalid, config=CONFIG,
        )
    assert cnf_path.read_bytes() == before
    assert not (tmp_path / "results").exists()


def test_downstream_am_failure_keeps_persisted_cnf(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot_root = tmp_path / "snapshots"
    seal_fixture_snapshot(
        snapshot_root, public_snapshot(MarketSession.AM, key())
    )
    cnf_path = tmp_path / "formal" / "manual_cnf_quotes.parquet"
    values = {
        (origin, month): (
            150.0 if origin == "brazil" and month == 1 else None
        )
        for origin in CONFIG.origin_codes
        for month in range(1, 13)
    }

    def fail_materialization(**_kwargs):
        raise NameError("fixture materialize failure")

    monkeypatch.setattr(
        intraday_pipeline,
        "materialize_soybean_intraday_profit",
        fail_materialization,
    )
    receipt = save_manual_cnf_and_materialize_am(
        snapshot_root=snapshot_root,
        result_root=tmp_path / "results",
        cnf_store_path=cnf_path,
        business_date=DAY,
        values=values,
        config=CONFIG,
        saved_at=datetime(2026, 8, 28, 1, 31, tzinfo=timezone.utc),
    )
    assert receipt.am_materialization_status == "FAILED"
    assert "NameError" in receipt.am_diagnostic
    assert cnf_path.exists()
    saved = load_cnf_store(cnf_path, allowed_origins=CONFIG.origin_codes)
    assert saved.records[0].cnf_cents_per_bushel == 150
    assert not (tmp_path / "results").exists()


def test_expired_grant_blocks_am_seal_after_cnf_persistence(tmp_path: Path) -> None:
    from agri_research_agent.import_profit.historical_cnf_adapter import shipment_year_for

    snapshots = tmp_path / 'snapshots'
    quotes = {}
    for month in range(1, 13):
        snapshot = public_snapshot(
            MarketSession.AM,
            key(year=shipment_year_for(DAY, month), month=month),
        )
        quotes.update({quote.instrument_id: quote for quote in snapshot.quotes})
    seal_fixture_snapshot(snapshots, replace(snapshot, quotes=tuple(quotes.values())))
    cnf = tmp_path / 'operational' / 'manual_cnf_quotes.parquet'
    results = tmp_path / 'am-results'
    values = {(origin, month): None for origin in CONFIG.origin_codes for month in range(1, 13)}
    values['brazil', 12] = 150.0
    checks = []

    def expired_before_seal():
        checks.append('checked')
        raise RuntimeError('execution grant is not currently valid')

    receipt = save_manual_cnf_and_materialize_am(
        snapshot_root=snapshots, result_root=results, cnf_store_path=cnf,
        business_date=DAY, values=values, config=CONFIG,
        authorize_materialization=expired_before_seal,
    )
    assert checks == ['checked']
    assert receipt.am_materialization_status == 'FAILED'
    assert 'execution grant is not currently valid' in receipt.am_diagnostic
    assert load_cnf_store(cnf, allowed_origins=CONFIG.origin_codes).records[0].cnf_cents_per_bushel == 150.0
    assert not results.exists()


def test_cnf_persists_without_am_snapshot_and_reloads_exact_value(tmp_path: Path) -> None:
    cnf_path = tmp_path / "operational" / "cnf.parquet"
    results = tmp_path / "am-results"
    values = {(origin, month): None for origin in CONFIG.origin_codes for month in range(1, 13)}
    values["brazil", 1] = 0.0
    receipt = save_manual_cnf_and_materialize_am(
        snapshot_root=tmp_path / "missing-am-snapshot",
        result_root=results,
        cnf_store_path=cnf_path,
        business_date=DAY,
        values=values,
        config=CONFIG,
        saved_at=datetime(2026, 8, 28, 1, 31, tzinfo=timezone.utc),
    )
    assert receipt.am_materialization_status == "INPUT_INCOMPLETE"
    assert receipt.materialize is None and receipt.am_record_count == 0
    assert receipt.cnf.cnf_sha256 and cnf_path.is_file()
    saved = load_cnf_store(cnf_path, allowed_origins=CONFIG.origin_codes)
    assert saved.store_sha256 == receipt.cnf.cnf_sha256
    assert len(saved.records) == 1
    assert saved.records[0].business_key.shipment_month == 1
    assert saved.records[0].cnf_cents_per_bushel == 0.0
    assert not results.exists()


def test_missing_fx_keeps_cnf_without_fake_profit(tmp_path: Path) -> None:
    snapshot = public_snapshot(MarketSession.AM, key())
    snapshot = replace(snapshot, quotes=tuple(
        quote for quote in snapshot.quotes if not quote.instrument_id.startswith("FX:")
    ))
    snapshots = tmp_path / "snapshots"
    seal_fixture_snapshot(snapshots, snapshot)
    values = {(origin, month): None for origin in CONFIG.origin_codes for month in range(1, 13)}
    values["brazil", 12] = 150.0
    cnf_path = tmp_path / "operational" / "cnf.parquet"
    results = tmp_path / "am-results"
    receipt = save_manual_cnf_and_materialize_am(
        snapshot_root=snapshots, result_root=results, cnf_store_path=cnf_path,
        business_date=DAY, values=values, config=CONFIG,
    )
    assert receipt.am_materialization_status == "INPUT_INCOMPLETE"
    assert receipt.snapshot_immutability_pass is True
    assert cnf_path.is_file()
    assert not results.exists()


@pytest.mark.parametrize("invalid_values", ["missing-key", "nan"])
def test_invalid_cnf_does_not_claim_persistence(tmp_path: Path, invalid_values: str) -> None:
    values = {(origin, month): None for origin in CONFIG.origin_codes for month in range(1, 13)}
    if invalid_values == "missing-key":
        values.pop(("brazil", 1))
    else:
        values["brazil", 1] = float("nan")
    cnf_path = tmp_path / "operational" / "cnf.parquet"
    with pytest.raises(ValueError):
        save_manual_cnf_and_materialize_am(
            snapshot_root=tmp_path / "snapshots", result_root=tmp_path / "results",
            cnf_store_path=cnf_path, business_date=DAY, values=values, config=CONFIG,
        )
    assert not cnf_path.exists()


def test_cnf_write_error_does_not_claim_persistence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    values = {(origin, month): None for origin in CONFIG.origin_codes for month in range(1, 13)}
    values["brazil", 1] = 150.0
    cnf_path = tmp_path / "operational" / "cnf.parquet"
    def fail_write(*_args, **_kwargs):
        raise OSError("CNF operational write failed")
    monkeypatch.setattr(intraday_pipeline, "upsert_cnf_quotes", fail_write)
    with pytest.raises(OSError, match="CNF operational write failed"):
        save_manual_cnf_and_materialize_am(
            snapshot_root=tmp_path / "snapshots", result_root=tmp_path / "results",
            cnf_store_path=cnf_path, business_date=DAY, values=values, config=CONFIG,
        )
    assert not cnf_path.exists()


def test_acl_reads_am_and_pm_without_tankan_and_returns_provenance(tmp_path: Path) -> None:
    business_key = key()
    for session, delta in ((MarketSession.AM, 0), (MarketSession.PM, 10)):
        seal_fixture_snapshot(tmp_path, public_snapshot(session, business_key, delta=delta))
    am = load_soybean_intraday_market_inputs(
        str(tmp_path), business_key=business_key, session=MarketSession.AM, config=CONFIG
    )
    pm = load_soybean_intraday_market_inputs(
        str(tmp_path), business_key=business_key, session=MarketSession.PM, config=CONFIG
    )
    assert am.cbot.contract_code == pm.cbot.contract_code
    assert am.cbot.price != pm.cbot.price
    assert am.provenance["session"] == "AM"
    source = (ROOT / "03_src/agri_research_agent/import_profit/intraday.py").read_text(encoding="utf-8")
    assert "data_sources.tankan" not in source and "TankanClient" not in source
    with pytest.raises(SoybeanIntradaySnapshotMissingError):
        missing_key = BusinessKey(
            date(2026, 8, 27), "soybean", business_key.origin,
            business_key.shipment_year, business_key.shipment_month,
            CONFIG.origin_codes, CONFIG.commodity,
        )
        load_soybean_intraday_market_inputs(
            str(tmp_path), business_key=missing_key,
            session=MarketSession.AM, config=CONFIG,
        )


def test_am_pm_market_prices_may_differ_but_formula_and_cnf_are_shared() -> None:
    business_key = key()
    am = calculated(MarketSession.AM, business_key)
    pm = calculated(MarketSession.PM, business_key, delta=10)
    assert require_shared_cnf_identity((am, pm)) == "a" * 64
    assert am.market_snapshot_sha256 != pm.market_snapshot_sha256
    assert am.calculation.net_crush_margin_cny_per_tonne != pm.calculation.net_crush_margin_cny_per_tonne
    assert am.calculation.parameter_hash == pm.calculation.parameter_hash
    with pytest.raises(SoybeanIntradayCnfMismatchError):
        require_shared_cnf_identity((am, calculated(MarketSession.PM, business_key, cnf_identity="b" * 64)))


def test_result_store_keeps_am_pm_and_rejects_cnf_mismatch(tmp_path: Path) -> None:
    business_key = key()
    am = calculated(MarketSession.AM, business_key)
    pm = calculated(MarketSession.PM, business_key, delta=10)
    am_batch = SoybeanIntradayResultBatch(
        DAY, MarketSession.AM, am.market_snapshot_release_id, am.market_snapshot_sha256,
        am.market_captured_at, am.cnf_identity, am.calculated_at, (am,),
    )
    pm_batch = SoybeanIntradayResultBatch(
        DAY, MarketSession.PM, pm.market_snapshot_release_id, pm.market_snapshot_sha256,
        pm.market_captured_at, pm.cnf_identity, pm.calculated_at, (pm,),
    )
    assert seal_intraday_profit_batch(tmp_path, am_batch).status is IntradayProfitSealStatus.SEALED
    assert seal_intraday_profit_batch(tmp_path, pm_batch).status is IntradayProfitSealStatus.SEALED
    assert read_intraday_profit_rows(tmp_path, DAY, "AM")[0]["session"] == "AM"
    assert read_intraday_profit_rows(tmp_path, DAY, "PM")[0]["session"] == "PM"
    assert seal_intraday_profit_batch(tmp_path, am_batch).status is IntradayProfitSealStatus.NO_CHANGE

    mismatch = calculated(MarketSession.PM, business_key, delta=10, cnf_identity="b" * 64)
    mismatch_batch = SoybeanIntradayResultBatch(
        DAY, MarketSession.PM, mismatch.market_snapshot_release_id, mismatch.market_snapshot_sha256,
        mismatch.market_captured_at, mismatch.cnf_identity, mismatch.calculated_at, (mismatch,),
    )
    other = tmp_path / "mismatch"
    seal_intraday_profit_batch(other, am_batch)
    with pytest.raises(SoybeanIntradayCnfMismatchError):
        seal_intraday_profit_batch(other, mismatch_batch)


@pytest.mark.parametrize("session", [MarketSession.AM, MarketSession.PM])
def test_unavailable_far_contract_is_row_level_incomplete_without_substitution(
    tmp_path: Path,
    session: MarketSession,
) -> None:
    available_key = key(year=2026, month=12)
    unavailable_key = key(year=2027, month=9)
    snapshot = mixed_snapshot(session, available_key, unavailable_key)
    available_inputs = select_soybean_intraday_market_inputs(
        snapshot, business_key=available_key, config=CONFIG
    )
    unavailable_inputs = select_soybean_intraday_market_inputs(
        snapshot, business_key=unavailable_key, config=CONFIG
    )
    records = (
        CnfQuoteRecord(available_key, 150.0, "manual_ui", datetime.now(timezone.utc), "batch"),
        CnfQuoteRecord(unavailable_key, 151.0, "manual_ui", datetime.now(timezone.utc), "batch"),
    )
    cnf = CnfStoreSnapshot(records, True, 2, "a" * 64)
    calculated_at = datetime.now(timezone.utc)
    available_result = calculate_soybean_intraday_profit(
        available_inputs, cnf_store=cnf, config=CONFIG, calculated_at=calculated_at
    )
    unavailable_result = calculate_soybean_intraday_profit(
        unavailable_inputs, cnf_store=cnf, config=CONFIG, calculated_at=calculated_at
    )
    assert available_result.calculation.calculation_status.value == "success"
    assert available_result.calculation.net_crush_margin_cny_per_tonne is not None
    assert unavailable_result.availability_status == "CONTRACT_NOT_AVAILABLE"
    assert unavailable_result.market_inputs.unavailable_contracts == ("M2801", "Y2801")
    assert unavailable_result.calculation.calculation_status.value == "incomplete"
    assert unavailable_result.calculation.net_crush_margin_cny_per_tonne is None
    assert unavailable_result.cnf_cents_per_bushel == 151.0

    batch = SoybeanIntradayResultBatch(
        DAY,
        session,
        snapshot.release_id,
        snapshot.content_sha256,
        snapshot.captured_at,
        "a" * 64,
        calculated_at,
        (available_result, unavailable_result),
    )
    seal_intraday_profit_batch(tmp_path, batch)
    rows = read_intraday_profit_rows(tmp_path, DAY, session)
    by_period = {row["shipment_period"]: row for row in rows}
    assert by_period["2026-12"]["availability_status"] == "SUCCESS"
    assert by_period["2027-09"]["availability_status"] == "CONTRACT_NOT_AVAILABLE"
    assert by_period["2027-09"]["soymeal_contract"] == "M2801"
    assert by_period["2027-09"]["soyoil_contract"] == "Y2801"
    assert by_period["2027-09"]["soymeal_price_cny_per_tonne"] is None
    assert by_period["2027-09"]["net_crush_margin_cny_per_tonne"] is None
