from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
import json
import re
from types import SimpleNamespace
from pathlib import Path
import sys

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest


ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT / "03_src", ROOT / "05_apps"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from agri_research_agent.import_profit.cnf_store import (
    CnfQuoteUpdate,
    load_cnf_store,
    upsert_cnf_quotes,
)
from agri_research_agent.import_profit.intraday_store import (
    SoybeanIntradayResultBatch,
    seal_intraday_profit_batch,
)
from agri_research_agent.import_profit.intraday import (
    calculate_soybean_intraday_profit,
    select_soybean_intraday_market_inputs,
)
from agri_research_agent.market_data.intraday import MarketSession
from test_import_profit_intraday import CONFIG, DAY, calculated, key, mixed_snapshot, seal_fixture_snapshot


@pytest.mark.parametrize('day', [date(2026, 9, 7), date(2027, 1, 5)])
def test_strict_runtime_handler_binds_selected_business_date(tmp_path, monkeypatch, day):
    from types import SimpleNamespace
    import import_profit_intraday_runtime_page as runtime_page
    from agri_research_agent.shared.runtime_context import RuntimeContext, RuntimeMode
    runtime = tmp_path / 'runtime'
    runtime.mkdir()
    results = runtime / 'import-profit/operational/am-results'
    results.mkdir(parents=True)
    operational_cnf = runtime / 'import-profit/operational/cnf/manual_cnf_quotes.parquet'
    operational_cnf.parent.mkdir()
    marker = {'schema_version': 1, 'runtime_id': 'pm-page-fixture',
              'classification': 'fixture', 'module_id': 'soybean-pm',
              'created_at': '2026-09-07T00:00:00+00:00'}
    (runtime / '.market-data-runtime.json').write_text(json.dumps(marker), encoding='utf-8')
    context = RuntimeContext(RuntimeMode.FIXTURE, 'soybean-pm', runtime)
    historical = runtime / 'history.parquet'
    resolved = SimpleNamespace(manual_cnf_path=runtime / 'cnf.parquet',
        business_keys_path=runtime / 'keys.parquet', snapshots_path=runtime / 'old.parquet',
        results_path=historical, manifest={'output_files': {'history.parquet': {'sha256': 'a'*64}},
                                         'date_range': ['2020-01-01', '2026-08-28']})
    monkeypatch.setattr(runtime_page, 'resolve_current_runtime_release', lambda _: resolved)
    seen = {}
    monkeypatch.setattr(runtime_page, 'render_import_profit_intraday_page',
                        lambda paths, **kwargs: seen.update(paths=paths, **kwargs))
    monkeypatch.setattr(runtime_page, 'save_manual_cnf_and_materialize_am',
                        lambda **kwargs: seen.update(saved=kwargs))
    runtime_page.render_import_profit_intraday_runtime_page(runtime,
        result_root=results, snapshot_root=runtime / 'snapshots',
        config_path=ROOT / '02_configs/import_profit_soybean.yaml',
        environment='TEST_ISOLATED_NON_PRODUCTION',
        preview_historical_cnf_path=runtime / 'cnf-history.parquet',
        allow_cnf_save=True, business_date=day, write_context=context,
        intraday_cnf_store_path=operational_cnf, operational_result_root=results)
    assert seen['paths'].business_date == day
    assert seen['mode'].value == 'STRICT_RUNTIME'
    assert 'saved' not in seen  # Rendering never saves on behalf of the user.
    values = {(origin, month): None for origin in CONFIG.origin_codes for month in range(1, 13)}
    values['brazil', 1] = 0.0
    seen['save_cnf_handler'](values)
    assert seen['saved']['business_date'] == day
    assert seen['saved']['values'] is values
    assert not resolved.manual_cnf_path.exists()


def test_future_day_strict_editor_is_editable_without_inheriting_yesterday(tmp_path):
    results, cnf = _assets(tmp_path)
    script = _app_script(results, cnf)
    script = script.replace('config=config,',
        'config=config, save_cnf_handler=lambda values: st.session_state.update(saved_payload=values),')
    script = 'import streamlit as st\nfrom datetime import date\n' + script
    script = script.replace('business_date=date(2026, 8, 28)', 'business_date=date(2026, 9, 7)')
    app = AppTest.from_string(script, default_timeout=20).run()
    assert not app.exception
    button = next(button for button in app.button if button.label == '保存今日 CNF')
    assert not button.disabled
    button.click().run()
    assert not app.exception
    values = app.session_state['saved_payload']
    assert len(values) == 48 and all(value is None for value in values.values())


def test_existing_pm_entry_dispatches_without_shared_router_changes(monkeypatch):
    import import_profit_runtime_page as entry
    import import_profit_intraday_runtime_page as intraday_entry
    seen = {}
    monkeypatch.setenv('IMPORT_PROFIT_INTRADAY_RESULT_ROOT', 'explicit-results')
    monkeypatch.setattr(intraday_entry, 'render_configured_intraday_runtime_page',
                        lambda root, **kwargs: seen.update(root=root, **kwargs))
    monkeypatch.setattr(entry, 'resolve_current_runtime_release',
                        lambda _: pytest.fail('must not load legacy renderer'))
    entry.render_import_profit_runtime_page('explicit-runtime', config_path='config.yaml')
    assert seen == {'root': 'explicit-runtime', 'config_path': 'config.yaml', 'allow_save': True}


def test_missing_write_runtime_keeps_page_readonly(tmp_path, monkeypatch):
    import import_profit_intraday_runtime_page as entry
    seen = {}
    monkeypatch.setenv('IMPORT_PROFIT_INTRADAY_ALLOW_CNF_SAVE', '1')
    monkeypatch.delenv('IMPORT_PROFIT_INTRADAY_WRITE_RUNTIME_ROOT', raising=False)
    monkeypatch.setattr(entry, 'configured_operational_write',
                        lambda: (_ for _ in ()).throw(RuntimeError('grant missing')))
    monkeypatch.setattr(entry, 'render_import_profit_intraday_runtime_page',
                        lambda *args, **kwargs: seen.update(kwargs))
    entry.render_configured_intraday_runtime_page(tmp_path, config_path='config.yaml')
    assert seen['allow_cnf_save'] is False
    assert seen['write_context'] is None
    assert seen['write_unavailable'] is True


def test_phase_a_runtime_routes_save_to_lifecycle_without_materialization(
    tmp_path, monkeypatch
):
    import import_profit_intraday_runtime_page as runtime_page
    from agri_research_agent.shared.runtime_context import RuntimeContext, RuntimeMode

    runtime = tmp_path / 'runtime'
    runtime.mkdir()
    results = runtime / 'import-profit/operational/am-results'
    results.mkdir(parents=True)
    cnf = runtime / 'import-profit/operational/cnf/manual_cnf_quotes.parquet'
    cnf.parent.mkdir()
    lifecycle = runtime / 'import-profit/operational/lifecycle'
    (runtime / '.market-data-runtime.json').write_text(json.dumps({
        'schema_version': 1, 'runtime_id': 'phase-a-fixture',
        'classification': 'fixture', 'module_id': 'soybean-pm',
        'created_at': '2026-09-24T00:00:00+00:00',
    }), encoding='utf-8')
    context = RuntimeContext(RuntimeMode.FIXTURE, 'soybean-pm', runtime)
    historical = runtime / 'history.parquet'
    resolved = SimpleNamespace(
        manual_cnf_path=runtime / 'cnf.parquet',
        business_keys_path=runtime / 'keys.parquet',
        snapshots_path=runtime / 'old.parquet',
        results_path=historical,
        manifest={'output_files': {'history.parquet': {'sha256': 'a' * 64}},
                  'date_range': ['2020-01-01', '2026-08-28']},
    )
    seen = {}
    monkeypatch.setattr(runtime_page, 'resolve_current_runtime_release', lambda _: resolved)
    monkeypatch.setattr(runtime_page, 'render_import_profit_intraday_page',
                        lambda paths, **kwargs: seen.update(paths=paths, **kwargs))
    monkeypatch.setattr(runtime_page, 'save_manual_cnf_and_emit_lifecycle',
                        lambda **kwargs: seen.update(lifecycle_saved=kwargs))
    monkeypatch.setattr(runtime_page, 'save_manual_cnf_and_materialize_am',
                        lambda **kwargs: pytest.fail('Phase A UI must not materialize'))
    runtime_page.render_import_profit_intraday_runtime_page(
        runtime, result_root=results, snapshot_root=runtime / 'snapshots',
        config_path=ROOT / '02_configs/import_profit_soybean.yaml',
        environment='TEST_ISOLATED_NON_PRODUCTION',
        preview_historical_cnf_path=runtime / 'cnf-history.parquet',
        intraday_cnf_store_path=cnf, operational_result_root=results,
        lifecycle_state_root=lifecycle, allow_cnf_save=True,
        business_date=DAY, write_context=context,
    )
    values = {
        (origin, month): None
        for origin in CONFIG.origin_codes
        for month in range(1, 13)
    }
    seen['save_cnf_handler'](values)
    assert seen['lifecycle_saved']['lifecycle_state_root'] == lifecycle
    assert seen['lifecycle_saved']['values'] is values


def test_phase_a_readiness_view_defaults_and_reads_durable_state(tmp_path):
    from dataclasses import replace
    from import_profit_intraday_page import load_lifecycle_readiness_view
    from agri_research_agent.import_profit.lifecycle import (
        CnfState, MarketState, ProfitState,
    )
    from agri_research_agent.import_profit.lifecycle_store import SoybeanLifecycleStore

    root = tmp_path / 'lifecycle'
    view = load_lifecycle_readiness_view(root, DAY)
    assert view['AM'] == {
        'market': 'NOT_READY', 'cnf': 'NOT_SUBMITTED',
        'profit': 'WAITING_FOR_MARKET', 'blocking_reason': None,
        'market_snapshot_release_id': None, 'profit_release_id': None,
        'state_revision': 0,
    }
    store = SoybeanLifecycleStore(root)
    current = store.load(DAY, MarketSession.AM)
    store.save(
        replace(current, market_state=MarketState.SEALED,
                cnf_state=CnfState.SUBMITTED,
                profit_state=ProfitState.READY_TO_MATERIALIZE),
        expected_revision=0,
    )
    view = load_lifecycle_readiness_view(root, DAY)
    assert view['AM']['market'] == 'SEALED'
    assert view['AM']['cnf'] == 'SUBMITTED'
    assert view['AM']['profit'] == 'READY_TO_MATERIALIZE'


@pytest.mark.parametrize(
    ("label", "changes", "expected"),
    (
        (
            "WAITING_FOR_MARKET",
            {"market_state": "NOT_READY", "cnf_state": "SUBMITTED",
             "profit_state": "WAITING_FOR_MARKET", "blocking_reason": "MARKET_NOT_READY"},
            ("Market=NOT_READY", "CNF=SUBMITTED", "Profit=WAITING_FOR_MARKET"),
        ),
        (
            "WAITING_FOR_CNF",
            {"market_state": "SEALED", "cnf_state": "NOT_SUBMITTED",
             "profit_state": "WAITING_FOR_CNF", "blocking_reason": "CNF_NOT_SUBMITTED",
             "market_snapshot_release_id": "2026-08-28-AM"},
            ("Market=SEALED", "CNF=NOT_SUBMITTED", "Profit=WAITING_FOR_CNF"),
        ),
        (
            "READY_TO_MATERIALIZE",
            {"market_state": "SEALED", "cnf_state": "SUBMITTED",
             "profit_state": "READY_TO_MATERIALIZE", "blocking_reason": None,
             "market_snapshot_release_id": "2026-08-28-AM"},
            ("Market=SEALED", "CNF=SUBMITTED", "Profit=READY_TO_MATERIALIZE"),
        ),
        (
            "SEALED",
            {"market_state": "SEALED", "cnf_state": "SUBMITTED",
             "profit_state": "SEALED", "blocking_reason": None,
             "market_snapshot_release_id": "2026-08-28-AM",
             "profit_release_id": "2026-08-28-AM-profit-r1"},
            ("Profit=SEALED", "Snapshot=2026-08-28-AM",
             "Profit=2026-08-28-AM-profit-r1"),
        ),
        (
            "FAILED",
            {"market_state": "SEALED", "cnf_state": "SUBMITTED",
             "profit_state": "FAILED", "blocking_reason": "MATERIALIZATION_FAILED",
             "market_snapshot_release_id": "2026-08-28-AM"},
            ("Profit=FAILED", "MATERIALIZATION_FAILED"),
        ),
        (
            "MISSED_WINDOW",
            {"market_state": "MISSED_WINDOW", "cnf_state": "NOT_SUBMITTED",
             "profit_state": "WAITING_FOR_MARKET", "blocking_reason": "MISSED_WINDOW: AM"},
            ("Market=MISSED_WINDOW", "MISSED_WINDOW: AM"),
        ),
    ),
)
def test_phase_a_ui_readiness_renders_all_formal_states_without_side_effects(
    tmp_path, monkeypatch, label, changes, expected,
):
    import import_profit_intraday_page as page
    from agri_research_agent.import_profit.lifecycle import CnfState, MarketState, ProfitState
    from agri_research_agent.import_profit.lifecycle_store import SoybeanLifecycleStore

    root = tmp_path / "lifecycle"
    store = SoybeanLifecycleStore(root)
    current = store.load(DAY, MarketSession.AM)
    enum_changes = dict(changes)
    enum_changes["market_state"] = MarketState(enum_changes["market_state"])
    enum_changes["cnf_state"] = CnfState(enum_changes["cnf_state"])
    enum_changes["profit_state"] = ProfitState(enum_changes["profit_state"])
    saved = store.save(replace(current, **enum_changes), expected_revision=0)
    path = store.state_path(DAY, MarketSession.AM)
    before_bytes = path.read_bytes()
    before_revision = saved.state_revision
    before_transitions = saved.transitions
    rendered = []
    monkeypatch.setattr(page.st, "html", rendered.append)
    monkeypatch.setattr(page.st, "error", lambda message: pytest.fail(message))

    page._render_lifecycle_readiness(root, DAY)

    assert len(rendered) == 1, label
    for token in expected:
        assert token in rendered[0], (label, token, rendered[0])
    after = store.load(DAY, MarketSession.AM)
    assert path.read_bytes() == before_bytes
    assert after.state_revision == before_revision
    assert after.transitions == before_transitions


def _configured_page_fixture(tmp_path, monkeypatch):
    import shutil
    import import_profit_intraday_runtime_page as entry
    from import_profit_intraday_page import TANKAN_CNF_SOURCE
    from agri_research_agent.shared.runtime_context import RuntimeContext, RuntimeMode

    result_root, source_cnf = _assets(tmp_path)
    runtime = tmp_path / 'runtime'
    cnf = runtime / 'import-profit/operational/cnf/manual_cnf_quotes.parquet'
    cnf.parent.mkdir(parents=True)
    shutil.copyfile(source_cnf, cnf)
    am_results = runtime / 'import-profit/operational/am-results'
    am_results.mkdir(parents=True)
    (runtime / '.market-data-runtime.json').write_text(json.dumps({
        'schema_version': 1, 'runtime_id': 'read-write-page-fixture',
        'classification': 'fixture', 'module_id': 'soybean-pm',
        'created_at': '2026-08-28T00:00:00+00:00',
    }), encoding='utf-8')
    context = RuntimeContext(RuntimeMode.FIXTURE, 'soybean-pm', runtime)
    history = tmp_path / 'history'
    history.mkdir()
    cache = tmp_path / 'historical-cache.parquet'
    pd.DataFrame([{
        'trade_date': date(2026, 8, 27), 'origin': 'brazil', 'month': 12,
        'cnf': 125.0, 'source_identity': TANKAN_CNF_SOURCE,
    }]).to_parquet(cache)
    resolved = SimpleNamespace(
        manual_cnf_path=history / 'historical-cnf.parquet',
        business_keys_path=history / 'keys.parquet',
        snapshots_path=history / 'snapshots.parquet',
        results_path=history / 'profit.parquet',
        manifest={'output_files': {'profit.parquet': {'sha256': 'a' * 64}},
                  'date_range': ['2020-01-01', '2026-08-27']},
    )
    monkeypatch.setattr(entry, 'resolve_current_runtime_release', lambda _: resolved)
    settings = {
        'IMPORT_PROFIT_INTRADAY_ALLOW_CNF_SAVE': '1',
        'IMPORT_PROFIT_INTRADAY_BUSINESS_DATE': DAY.isoformat(),
        'IMPORT_PROFIT_INTRADAY_RESULT_ROOT': str(result_root),
        'IMPORT_PROFIT_INTRADAY_SNAPSHOT_ROOT': str(tmp_path / 'snapshots'),
        'IMPORT_PROFIT_INTRADAY_CNF_STORE_PATH': str(cnf),
        'IMPORT_PROFIT_INTRADAY_AM_RESULT_ROOT': str(am_results),
        'IMPORT_PROFIT_INTRADAY_ENVIRONMENT': 'TEST_ISOLATED_NON_PRODUCTION',
        'IMPORT_PROFIT_PREVIEW_HISTORICAL_CNF_PATH': str(cache),
    }
    for name, value in settings.items():
        monkeypatch.setenv(name, value)
    script = (
        'from import_profit_intraday_runtime_page import render_configured_intraday_runtime_page\n'
        f'render_configured_intraday_runtime_page({str(history)!r}, '
        f'config_path={str(ROOT / "02_configs/import_profit_soybean.yaml")!r})\n'
    )
    return entry, context, script


@pytest.mark.parametrize('write_state', ['valid', 'expired', 'missing', 'expires_during_render'])
def test_configured_page_renders_data_independent_of_write_grant(
    tmp_path, monkeypatch, write_state,
):
    entry, context, script = _configured_page_fixture(tmp_path, monkeypatch)
    if write_state in {'valid', 'expires_during_render'}:
        monkeypatch.setattr(entry, 'configured_operational_write', lambda: context)
        if write_state == 'expires_during_render':
            monkeypatch.setattr(entry, 'validate_operational_write',
                lambda *_args: (_ for _ in ()).throw(RuntimeError('grant expired')))
    else:
        monkeypatch.setattr(entry, 'configured_operational_write',
            lambda: (_ for _ in ()).throw(RuntimeError(f'grant {write_state}')))
    app = AppTest.from_string(script, default_timeout=20).run(timeout=20)
    assert not app.exception and not app.error
    html = '\n'.join(item.proto.body for item in app.get('html'))
    assert '大豆早间榨利' in html and '大豆下午榨利' in html
    assert '<span>AM — 尚未封存</span>' in html
    assert '<span>PM — 尚未封存</span>' in html
    assert html.count('2026-12') >= 2
    assert '150.0' in str(app.dataframe[0].value)
    button = next(item for item in app.button if item.label == '重试上午盘面榨利')
    assert button.disabled is (write_state != 'valid')
    assert bool(app.info) is (write_state != 'valid')


def test_invalid_read_identity_still_blocks_page(tmp_path, monkeypatch):
    import import_profit_intraday_runtime_page as entry
    from agri_research_agent.import_profit.runtime_store import RuntimeStoreError

    entry, _, script = _configured_page_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(entry, 'configured_operational_write',
        lambda: (_ for _ in ()).throw(RuntimeError('grant expired')))
    monkeypatch.setattr(entry, 'resolve_current_runtime_release',
        lambda _: (_ for _ in ()).throw(RuntimeStoreError('history invalid')))
    app = AppTest.from_string(script, default_timeout=20).run(timeout=20)
    assert not app.exception
    assert any('本地资产校验失败' in item.value for item in app.error)
    assert '大豆早间榨利' not in '\n'.join(item.proto.body for item in app.get('html'))


def _assets(tmp_path: Path) -> tuple[Path, Path]:
    cnf_path = tmp_path / "cnf.parquet"
    business_key = key()
    upsert_cnf_quotes(
        cnf_path,
        (
            CnfQuoteUpdate(
                business_key,
                150.0,
                "manual_ui",
                datetime(2026, 8, 28, 1, 30, tzinfo=timezone.utc),
                "page-fixture",
            ),
        ),
        allowed_origins=CONFIG.origin_codes,
        expected_store_sha256=None,
    )
    cnf_identity = load_cnf_store(
        cnf_path, allowed_origins=CONFIG.origin_codes
    ).store_sha256
    assert cnf_identity is not None
    result_root = tmp_path / "results"
    for session, delta in ((MarketSession.AM, 0), (MarketSession.PM, 10)):
        result = calculated(
            session,
            business_key,
            delta=delta,
            cnf_identity=cnf_identity,
        )
        seal_intraday_profit_batch(
            result_root,
            SoybeanIntradayResultBatch(
                DAY,
                session,
                result.market_snapshot_release_id,
                result.market_snapshot_sha256,
                result.market_captured_at,
                result.cnf_identity,
                result.calculated_at,
                (result,),
            ),
        )
    return result_root, cnf_path


def _app_script(
    result_root: Path, cnf_path: Path, snapshot_root: Path | None = None
) -> str:
    return f"""
from pathlib import Path
from datetime import date
from agri_research_agent.import_profit.config import load_soybean_config
from import_profit_intraday_page import IntradayPageDataPaths, render_import_profit_intraday_page
config = load_soybean_config({str(ROOT / '02_configs/import_profit_soybean.yaml')!r})
render_import_profit_intraday_page(
    IntradayPageDataPaths(
        Path({str(result_root)!r}),
        Path({str(cnf_path)!r}),
        {f'Path({str(snapshot_root)!r})' if snapshot_root is not None else 'None'},
        business_date=date(2026, 8, 28),
    ),
    config=config,
)
"""


def test_page_distinguishes_sealed_am_waiting_for_cnf_from_missing_pm(
    tmp_path: Path,
) -> None:
    result_root, cnf_path = _assets(tmp_path)
    empty_results = tmp_path / "empty-results"
    empty_results.mkdir()
    snapshot_root = tmp_path / "snapshots"
    snapshot = mixed_snapshot(
        MarketSession.AM, key(year=2026, month=12), key(year=2027, month=8)
    )
    seal_fixture_snapshot(snapshot_root, snapshot)

    app = AppTest.from_string(
        _app_script(empty_results, cnf_path, snapshot_root), default_timeout=20
    ).run(timeout=20)

    assert not app.exception
    status = "\n".join(item.value for item in app.markdown)
    assert "AM：尚未封存" in status
    assert "PM：尚未封存" in status
    html = "\n".join(item.proto.body for item in app.get("html"))
    assert "<span>AM — 尚未封存</span>" in html
    assert "<span>PM — 尚未封存</span>" in html


def test_page_has_only_required_am_pm_cnf_and_profit_chart(tmp_path: Path) -> None:
    result_root, cnf_path = _assets(tmp_path)
    app = AppTest.from_string(
        _app_script(result_root, cnf_path), default_timeout=20
    ).run(timeout=20)
    assert not app.exception
    html = "\n".join(item.proto.body for item in app.get("html"))
    assert "soy-title" in html
    assert "中国进口大豆盘面榨利" in html
    assert "大豆早间榨利" in html
    assert "大豆下午榨利" in html
    assert "soy-profit-table" in html
    assert "关税%" in html and "3%" in html
    assert "增值税%" in html and "9%" in html
    assert "粕价值" not in html and "油价值" not in html
    assert not app.selectbox
    assert len(app.dataframe) == 1  # the accepted CNF editor only


def test_page_module_has_no_database_or_producer_boundary() -> None:
    source = (ROOT / "05_apps/import_profit_intraday_page.py").read_text(
        encoding="utf-8"
    )
    forbidden = (
        "TankanClient",
        "capture_public_intraday",
        "materialize_soybean_intraday",
        "data_sources.tankan",
        "refresh",
    )
    assert all(token not in source for token in forbidden)
    assert "CNF 折线图" not in source
    assert "完税成本折线图" not in source


def test_page_renders_unavailable_row_and_history_ignores_it(tmp_path: Path) -> None:
    available_key = key(year=2026, month=12)
    unavailable_key = key(year=2027, month=8)
    cnf_path = tmp_path / "cnf.parquet"
    upsert_cnf_quotes(
        cnf_path,
        tuple(
            CnfQuoteUpdate(
                business_key,
                value,
                "manual_ui",
                datetime(2026, 8, 28, 1, 30, tzinfo=timezone.utc),
                "page-partial-fixture",
            )
            for business_key, value in (
                (available_key, 150.0),
                (unavailable_key, 151.0),
            )
        ),
        allowed_origins=CONFIG.origin_codes,
        expected_store_sha256=None,
    )
    cnf = load_cnf_store(cnf_path, allowed_origins=CONFIG.origin_codes)
    assert cnf.store_sha256 is not None
    result_root = tmp_path / "results"
    for session in (MarketSession.AM, MarketSession.PM):
        snapshot = mixed_snapshot(session, available_key, unavailable_key)
        results = tuple(
            calculate_soybean_intraday_profit(
                select_soybean_intraday_market_inputs(
                    snapshot, business_key=business_key, config=CONFIG
                ),
                cnf_store=cnf,
                config=CONFIG,
                calculated_at=datetime(2026, 8, 28, 7, 30, tzinfo=timezone.utc),
            )
            for business_key in (available_key, unavailable_key)
        )
        seal_intraday_profit_batch(
            result_root,
            SoybeanIntradayResultBatch(
                DAY,
                session,
                snapshot.release_id,
                snapshot.content_sha256,
                snapshot.captured_at,
                cnf.store_sha256,
                datetime(2026, 8, 28, 7, 30, tzinfo=timezone.utc),
                results,
            ),
        )

    app = AppTest.from_string(
        _app_script(result_root, cnf_path), default_timeout=20
    ).run(timeout=20)
    assert not app.exception
    html = "\n".join(item.proto.body for item in app.get("html"))
    assert html.count("2026-12") >= 2
    assert html.count("2027-08") >= 2
    assert html.count("M2705") >= 2
    assert html.count("Y2705") >= 2
    assert "margin-null" in html
    assert not app.selectbox


def _profit_tables(app):
    assert not app.exception
    tables = [item.proto.body for item in app.get("html")
              if 'soy-profit-table' in item.proto.body and '<tbody>' in item.proto.body]
    assert len(tables) == 2
    for table in tables:
        body = table.split('<tbody>', 1)[1].split('</tbody>', 1)[0]
        assert body.count('<tr>') == 12
        periods = re.findall(r'class="group-key shipment"><span[^>]*>([^<]+)</span>', body)
        assert [int(value[-2:]) for value in periods] == list(range(1, 13))
    return tables


@pytest.mark.parametrize('sessions', [(), (MarketSession.AM,), (MarketSession.PM,),
                                     (MarketSession.AM, MarketSession.PM)])
def test_fixed_month_tables_with_missing_and_partial_releases(tmp_path, sessions):
    # Real sealed one-month releases exercise loading, view-model and HTML together.
    cnf = tmp_path / 'cnf.parquet'
    results = tmp_path / 'results'
    for session in sessions:
        result = calculated(session, key(year=2026, month=12))
        seal_intraday_profit_batch(results, SoybeanIntradayResultBatch(
            DAY, session, result.market_snapshot_release_id,
            result.market_snapshot_sha256, result.market_captured_at,
            result.cnf_identity, result.calculated_at, (result,)))
    app = AppTest.from_string(_app_script(results, cnf), default_timeout=20).run()
    tables = _profit_tables(app)
    html = '\n'.join(item.proto.body for item in app.get('html'))
    for session, table in zip(MarketSession, tables, strict=True):
        assert table.count('margin-null') == (11 if session in sessions else 12)
        if session in sessions:
            assert re.search(
                fr'<span class="sealed">{session.value} [^<]+ 已封存</span>', html
            )
        else:
            assert f'<span>{session.value} — 尚未封存</span>' in html


def _presentation_release(session=MarketSession.AM, **changes):
    row = dict(business_date=DAY.isoformat(), session=session.value,
               commodity='soybean', origin='brazil', shipment_year=2026,
               shipment_month=12, shipment_period='2026-12',
               cnf_cents_per_bushel=0.0, cbot_price_cents_per_bushel=1200.0,
               soymeal_price_cny_per_tonne=3200.0, soyoil_price_cny_per_tonne=8000.0,
               fx_value=7.2, net_crush_margin_cny_per_tonne=None,
               availability_status='UNAVAILABLE', calculation_status='unavailable')
    row.update(changes)
    return SimpleNamespace(business_date=DAY, session=session, rows=(row,))


@pytest.mark.parametrize('field,value', [
    ('cnf_cents_per_bushel', None), ('cnf_cents_per_bushel', 0.0),
    ('cbot_price_cents_per_bushel', None), ('soymeal_price_cny_per_tonne', None),
    ('soyoil_price_cny_per_tonne', None), ('fx_value', None),
    ('net_crush_margin_cny_per_tonne', None),
])
@pytest.mark.parametrize('session', list(MarketSession))
def test_missing_business_values_never_remove_months(field, value, session):
    from import_profit_intraday_page import _result_profit_rows
    release = _presentation_release(session, **{field: value})
    rows = _result_profit_rows(release, business_date=DAY, session=session,
                               origin='brazil', config=CONFIG)
    assert len(rows) == 12
    assert [row['船期'] for row in rows] == [
        *(f'2027-{month:02d}' for month in range(1, 9)),
        *(f'2026-{month:02d}' for month in range(9, 13))]
    display = {'cnf_cents_per_bushel': 'CNF（美分/蒲）',
               'cbot_price_cents_per_bushel': 'CBOT价格',
               'soymeal_price_cny_per_tonne': '豆粕盘面',
               'soyoil_price_cny_per_tonne': '豆油盘面', 'fx_value': '汇率',
               'net_crush_margin_cny_per_tonne': '盘面榨利（元/吨）'}
    assert rows[11][display[field]] == value
    assert all(row['盘面榨利（元/吨）'] is None for row in rows)
    assert all(row['CNF（美分/蒲）'] is None for row in rows[:11])


@pytest.mark.parametrize('changes', [
    {'shipment_year': 2027, 'shipment_period': '2027-12'},
    {'business_date': '2026-08-27'}, {'origin': 'argentina'},
    {'session': 'PM'}, {'commodity': 'canola'},
])
def test_month_left_join_excludes_other_identities(changes):
    from import_profit_intraday_page import _result_profit_rows
    release = _presentation_release()
    release.rows[0].update(changes)
    rows = _result_profit_rows(release, business_date=DAY, session=MarketSession.AM,
                               origin='brazil', config=CONFIG)
    assert len(rows) == 12
    assert all(row['CNF（美分/蒲）'] is None for row in rows)


def test_origin_switch_and_other_session_cannot_borrow_results():
    from import_profit_intraday_page import _result_profit_rows
    release = _presentation_release()
    for origin in CONFIG.origin_codes:
        rows = _result_profit_rows(release, business_date=DAY, session=MarketSession.AM,
                                   origin=origin, config=CONFIG)
        assert len(rows) == 12
        assert rows[11]['CNF（美分/蒲）'] == (0.0 if origin == 'brazil' else None)
    for day, session in [(date(2026, 8, 31), MarketSession.AM), (DAY, MarketSession.PM)]:
        rows = _result_profit_rows(release, business_date=day, session=session,
                                   origin='brazil', config=CONFIG)
        assert len(rows) == 12
        assert all(row['CNF（美分/蒲）'] is None for row in rows)


def test_empty_result_rows_and_december_rollover():
    from import_profit_intraday_page import _result_profit_rows
    release = _presentation_release()
    release.rows = ()
    assert len(_result_profit_rows(release, business_date=DAY, session=MarketSession.AM,
                                  origin='brazil', config=CONFIG)) == 12
    rows = _result_profit_rows(None, business_date=date(2026, 12, 31),
                              session=MarketSession.PM, origin='brazil', config=CONFIG)
    assert [row['船期'] for row in rows] == [f'2027-{month:02d}' for month in range(1, 13)]


def test_current_date_does_not_fall_back_to_old_releases(tmp_path):
    results, cnf = _assets(tmp_path)
    script = _app_script(results, cnf)
    script = script.replace('business_date=date(2026, 8, 28)', 'business_date=date(2027, 1, 5)')
    app = AppTest.from_string(script, default_timeout=20).run()
    html = '\n'.join(item.proto.body for item in app.get('html'))
    assert '<span>AM — 尚未封存</span>' in html
    assert '<span>PM — 尚未封存</span>' in html
    for table in _profit_tables(app):
        assert table.count('margin-null') == 12
        assert '2026-12' not in table


def test_no_date_and_no_assets_still_render_twelve_months(tmp_path):
    script = _app_script(tmp_path / 'results', tmp_path / 'cnf.parquet')
    script = script.replace('business_date=date(2026, 8, 28)', 'business_date=None')
    app = AppTest.from_string(script, default_timeout=20).run()
    for table in _profit_tables(app):
        assert table.count('margin-null') == 12


@pytest.mark.parametrize('session', list(MarketSession))
def test_compact_release_status_requires_exact_business_date_and_session(session):
    from import_profit_intraday_page import _release_status

    release = _presentation_release(session)
    assert _release_status(
        release, business_date=DAY, session=session, compact=True
    ) == '已封存'
    assert _release_status(
        None, business_date=DAY, session=session, compact=True
    ) == '尚未封存'
    assert _release_status(
        release, business_date=date(2026, 8, 29), session=session, compact=True
    ) == '尚未封存'
    other = MarketSession.PM if session is MarketSession.AM else MarketSession.AM
    assert _release_status(
        release, business_date=DAY, session=other, compact=True
    ) == '尚未封存'
