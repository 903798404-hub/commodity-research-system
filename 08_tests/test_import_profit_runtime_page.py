from __future__ import annotations

from datetime import date, datetime, timezone
import importlib
import inspect
from pathlib import Path
import sys

import pandas as pd
import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest


ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT / "03_src", ROOT / "05_apps"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

import import_profit_components as components
import import_profit_runtime_page as runtime_page
from agri_research_agent.import_profit.runtime_store import (
    RuntimeConcurrentUpdateError,
    RuntimeLockedError,
    RuntimeWriteError,
    resolve_current_runtime_release,
)
from agri_research_agent.pipelines.import_profit_runtime import (
    RuntimePipelineError,
)
from test_import_profit_components import CONFIG, configured_rows
from test_import_profit_runtime_store import CONFIG_PATH, bootstrap_fixture


FIXED_TIME = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)


def runtime_records():
    records = []
    for business_date in (date(2026, 6, 10), date(2026, 6, 25)):
        for month in range(1, 13):
            shipment_year = 2026 if month >= 7 else 2027
            cnf = 100.0 + month
            if business_date == date(2026, 6, 10) and month == 12:
                cnf = None
            records.append(
                configured_rows(
                    business_date,
                    "brazil",
                    shipment_year,
                    month,
                    cnf=cnf,
                    cbot=1152.25 if month == 12 else 1100.0 + month,
                    fx=6.691957 if month == 12 else 6.8,
                    soymeal=2995.0,
                    soyoil=8301.0,
                )
            )
    return records


def runtime_fixture(tmp_path):
    return bootstrap_fixture(
        tmp_path,
        release_id="page-base-001",
        records=runtime_records(),
    )[0]


def app_script(runtime_root, *, allow_save=True):
    runtime_expression = (
        "None" if runtime_root is None else repr(str(runtime_root))
    )
    return f"""
from import_profit_runtime_page import render_import_profit_runtime_page
render_import_profit_runtime_page(
    {runtime_expression},
    config_path={str(CONFIG_PATH)!r},
    allow_cnf_save={allow_save!r},
)
"""


def identity(runtime_root):
    resolved = resolve_current_runtime_release(runtime_root)
    return components.PageRuntimeIdentity(
        release_id=resolved.release_id,
        generation=resolved.generation,
        previous_release_id=resolved.previous_release_id,
        index_sha256=resolved.identity.index_sha256,
        manual_cnf_sha256=resolved.identity.manual_cnf_sha256,
        release_created_at=str(resolved.manifest["created_at"]),
        manual_cnf_record_count=int(
            resolved.manifest["manual_cnf_record_count"]
        ),
    )


def context_for(runtime_root, *, business_date=date(2026, 6, 10)):
    resolved = resolve_current_runtime_release(runtime_root)
    from agri_research_agent.import_profit.query import (
        load_soybean_query_dataset,
    )

    dataset = load_soybean_query_dataset(
        resolved.business_keys_path,
        resolved.snapshots_path,
        resolved.results_path,
    )
    records = components.records_for_date(
        dataset, origin="brazil", business_date=business_date
    )
    return components.build_runtime_editor_context(
        records,
        identity=identity(runtime_root),
        config=CONFIG,
        origin="brazil",
        business_date=business_date,
    )


def editor_for(context, values=None):
    values = values or dict(
        zip(context.shipment_periods, context.original_cnf, strict=True)
    )
    return pd.DataFrame(
        {
            "船期": context.shipment_periods,
            "CNF升贴水": [
                values[period] for period in context.shipment_periods
            ],
        }
    )


def test_runtime_module_import_has_no_render_or_environment_side_effect(
    monkeypatch,
):
    monkeypatch.setenv("IMPORT_PROFIT_RUNTIME_ROOT", "must-not-be-read")

    def unexpected(*_args, **_kwargs):
        raise AssertionError("module import must not render or resolve")

    monkeypatch.setattr(st, "title", unexpected)
    monkeypatch.setattr(
        runtime_page, "resolve_current_runtime_release", unexpected
    )
    importlib.reload(runtime_page)


def test_runtime_cache_key_contains_release_file_and_manual_identities():
    names = tuple(
        inspect.signature(
            runtime_page._load_runtime_dataset_cached
        ).parameters
    )
    assert names == (
        "runtime_root",
        "release_id",
        "generation",
        "index_sha256",
        "business_keys_sha256",
        "business_keys_size",
        "business_keys_mtime_ns",
        "snapshots_sha256",
        "snapshots_size",
        "snapshots_mtime_ns",
        "results_sha256",
        "results_size",
        "results_mtime_ns",
        "manual_cnf_sha256",
    )


def test_missing_unconfigured_and_corrupt_runtime_degrade_without_path(
    tmp_path,
):
    unconfigured = AppTest.from_string(
        app_script(None), default_timeout=10
    ).run(timeout=10)
    assert not unconfigured.exception
    assert any(
        "运行数据尚未配置" in item.value for item in unconfigured.info
    )

    missing = tmp_path / "secret-user-path" / "runtime"
    absent = AppTest.from_string(
        app_script(missing), default_timeout=10
    ).run(timeout=10)
    assert not absent.exception
    assert str(missing) not in "\n".join(
        item.value for item in absent.error
    )

    corrupt = tmp_path / "corrupt"
    corrupt.mkdir()
    broken = AppTest.from_string(
        app_script(corrupt), default_timeout=10
    ).run(timeout=10)
    assert not broken.exception
    assert any("未自动回退Previous" in item.value for item in broken.error)


def test_formal_runtime_page_shows_identity_and_save_only_in_formal_mode(
    tmp_path,
):
    runtime_root = runtime_fixture(tmp_path)
    app = AppTest.from_string(
        app_script(runtime_root), default_timeout=40
    ).run(timeout=40)
    assert not app.exception
    assert app.selectbox[0].value == "brazil"
    assert app.date_input[0].value == date(2026, 6, 25)
    assert len(app.dataframe[0].value) == 12
    assert len(app.get("plotly_chart")) == 36
    assert {
        button.label for button in app.button
    } >= {
        "重新计算预览",
        "恢复原始CNF",
        "保存CNF并更新正式结果",
    }
    assert next(
        button
        for button in app.button
        if button.label == "保存CNF并更新正式结果"
    ).disabled
    metric_values = {item.label: item.value for item in app.metric}
    assert metric_values["当前Release"] == "page-base-001"
    assert metric_values["generation"] == "1"
    visible = "\n".join(
        str(item.value)
        for collection in (
            app.title,
            app.info,
            app.caption,
            app.error,
        )
        for item in collection
    )
    assert "正式运行模式" in visible
    assert str(runtime_root) not in visible


@pytest.mark.parametrize(
    ("previous", "new", "expected_type"),
    [
        (None, 100.0, "新增人工覆盖"),
        (100.0, 0.0, "覆盖历史值"),
        (0.0, None, "清空为NULL"),
        (-2.5, -2.5, None),
    ],
)
def test_exact_difference_rules_preserve_full_business_key(
    tmp_path, previous, new, expected_type
):
    runtime_root = runtime_fixture(tmp_path)
    context = context_for(runtime_root)
    index = 11
    originals = list(context.original_cnf)
    originals[index] = previous
    context = components.RuntimeEditorContext(
        context.loaded_release_id,
        context.loaded_generation,
        context.loaded_index_sha256,
        context.loaded_manual_cnf_sha256,
        context.business_date,
        context.origin,
        context.business_keys,
        context.shipment_periods,
        tuple(originals),
        context.original_sources,
    )
    values = dict(
        zip(context.shipment_periods, context.original_cnf, strict=True)
    )
    values[context.shipment_periods[index]] = new
    differences = components.cnf_differences_from_editor(
        context, editor_for(context, values)
    )
    if expected_type is None:
        assert differences == ()
        return
    assert len(differences) == 1
    difference = differences[0]
    assert difference.change_type == expected_type
    assert difference.business_key.business_date == date(2026, 6, 10)
    assert difference.business_key.shipment_year == 2026
    assert difference.business_key.shipment_month == 12


def test_batch_save_calls_one_transaction_with_expected_identity_and_ids(
    tmp_path, monkeypatch
):
    runtime_root = runtime_fixture(tmp_path)
    context = context_for(runtime_root)
    values = dict(
        zip(context.shipment_periods, context.original_cnf, strict=True)
    )
    values[context.shipment_periods[0]] = 0.0
    values[context.shipment_periods[11]] = 100.0
    differences = components.cnf_differences_from_editor(
        context, editor_for(context, values)
    )
    calls = []

    class Result:
        status = "no_change"

    def fake_update(runtime_root_arg, updates, **kwargs):
        calls.append((runtime_root_arg, tuple(updates), kwargs))
        return Result()

    monkeypatch.setattr(
        runtime_page, "update_runtime_cnf_quotes", fake_update
    )
    feedback = runtime_page.save_runtime_cnf_differences(
        runtime_root,
        config_path=CONFIG_PATH,
        config=CONFIG,
        context=context,
        differences=differences,
        previous_success_count=0,
        previous_incomplete_count=24,
        clock=lambda: FIXED_TIME,
        id_factory=lambda now, items: (
            "fixed-batch",
            "fixed-release-002",
        ),
    )

    assert feedback.status == "no_change"
    assert len(calls) == 1
    assert len(calls[0][1]) == 2
    assert calls[0][2]["expected_release_id"] == context.loaded_release_id
    assert (
        calls[0][2]["expected_index_sha256"]
        == context.loaded_index_sha256
    )
    assert (
        calls[0][2]["expected_manual_cnf_sha256"]
        == context.loaded_manual_cnf_sha256
    )
    assert calls[0][2]["batch_id"] == "fixed-batch"
    assert calls[0][2]["release_id"] == "fixed-release-002"
    assert all(item.updated_at == FIXED_TIME for item in calls[0][1])


def test_real_page_save_reloads_new_release_and_preserves_other_rows(
    tmp_path,
):
    runtime_root = runtime_fixture(tmp_path)
    before = resolve_current_runtime_release(runtime_root)
    context = context_for(runtime_root)
    values = dict(
        zip(context.shipment_periods, context.original_cnf, strict=True)
    )
    values["2026-12"] = 100.0
    differences = components.cnf_differences_from_editor(
        context, editor_for(context, values)
    )
    feedback = runtime_page.save_runtime_cnf_differences(
        runtime_root,
        config_path=CONFIG_PATH,
        config=CONFIG,
        context=context,
        differences=differences,
        previous_success_count=23,
        previous_incomplete_count=1,
        clock=lambda: FIXED_TIME,
        id_factory=lambda now, items: (
            "page-real-001",
            "page-release-002",
        ),
    )
    assert feedback.status == "success"
    app = AppTest.from_string(
        app_script(runtime_root), default_timeout=60
    ).run(timeout=60)
    app.date_input[0].set_value(date(2026, 6, 10)).run(timeout=60)

    assert not app.exception
    after = resolve_current_runtime_release(runtime_root)
    assert before.generation == 1
    assert after.generation == 2
    assert after.previous_release_id == before.release_id
    official = app.dataframe[0].value
    row = official.loc[official["船期"] == "2026-12"].iloc[0]
    assert row["CNF（美分/蒲）"] == "100.00"
    assert "CNF来源" not in official
    assert row["美元成本"] == "460.12"
    assert row["完税成本"] == "3456.93"
    assert row["盘面榨利"] == "179.83"
    assert app.session_state[
        components.STATE_RUNTIME_CONTEXT
    ].loaded_generation == 2


def test_external_release_identity_marks_context_stale(tmp_path):
    runtime_root = runtime_fixture(tmp_path)
    context = context_for(runtime_root)
    newer = components.PageRuntimeIdentity(
        release_id="newer-002",
        generation=2,
        previous_release_id=context.loaded_release_id,
        index_sha256="A" * 64,
        manual_cnf_sha256="B" * 64,
        release_created_at="2026-07-30T12:00:00Z",
        manual_cnf_record_count=1,
    )
    assert components.runtime_context_is_stale(context, newer)
    state = {
        components.STATE_CONTEXT: "brazil|2026-06-10",
        components.STATE_RUNTIME_CONTEXT: context,
        components.STATE_PREVIEWS: {"old": "preview"},
    }
    components.reset_runtime_editor_state(state)
    assert components.STATE_RUNTIME_CONTEXT not in state
    assert components.STATE_PREVIEWS not in state


def test_runtime_context_is_preserved_for_same_selection_and_rebuilt_on_switch(
    tmp_path,
):
    runtime_root = runtime_fixture(tmp_path)
    first = context_for(runtime_root)
    second = context_for(
        runtime_root, business_date=date(2026, 6, 25)
    )
    state = {}
    components.initialize_preview_state(
        state,
        origin="brazil",
        business_date=date(2026, 6, 10),
        records=(None,) * 12,
        runtime_context=first,
    )
    components.initialize_preview_state(
        state,
        origin="brazil",
        business_date=date(2026, 6, 10),
        records=(None,) * 12,
        runtime_context=second,
    )
    assert state[components.STATE_RUNTIME_CONTEXT] is first
    state[components.STATE_PREVIEWS] = {"old": "preview"}
    components.initialize_preview_state(
        state,
        origin="brazil",
        business_date=date(2026, 6, 25),
        records=(None,) * 12,
        runtime_context=second,
    )
    assert state[components.STATE_RUNTIME_CONTEXT] is second
    assert state[components.STATE_PREVIEWS] == {}


@pytest.mark.parametrize(
    ("exception", "status"),
    [
        (RuntimeConcurrentUpdateError("stale"), "concurrent_conflict"),
        (RuntimeLockedError("locked"), "locked"),
        (RuntimePipelineError("invalid"), "validation_failed"),
        (RuntimeWriteError("write"), "failed"),
    ],
)
def test_save_errors_are_bounded_and_never_retried(
    tmp_path, monkeypatch, exception, status
):
    runtime_root = runtime_fixture(tmp_path)
    context = context_for(runtime_root)
    values = dict(
        zip(context.shipment_periods, context.original_cnf, strict=True)
    )
    values["2026-12"] = 100.0
    differences = components.cnf_differences_from_editor(
        context, editor_for(context, values)
    )
    calls = 0

    def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise exception

    monkeypatch.setattr(
        runtime_page, "update_runtime_cnf_quotes", fail_once
    )
    feedback = runtime_page.save_runtime_cnf_differences(
        runtime_root,
        config_path=CONFIG_PATH,
        config=CONFIG,
        context=context,
        differences=differences,
        previous_success_count=23,
        previous_incomplete_count=1,
        clock=lambda: FIXED_TIME,
        id_factory=lambda now, items: ("batch", "release-002"),
    )
    assert feedback.status == status
    assert calls == 1
    assert str(tmp_path) not in feedback.message


def test_empty_difference_is_page_no_change_without_transaction(
    tmp_path, monkeypatch
):
    runtime_root = runtime_fixture(tmp_path)
    context = context_for(runtime_root)
    monkeypatch.setattr(
        runtime_page,
        "update_runtime_cnf_quotes",
        lambda *args, **kwargs: pytest.fail("must not call transaction"),
    )
    before = resolve_current_runtime_release(runtime_root)
    feedback = runtime_page.save_runtime_cnf_differences(
        runtime_root,
        config_path=CONFIG_PATH,
        config=CONFIG,
        context=context,
        differences=(),
        previous_success_count=23,
        previous_incomplete_count=1,
    )
    after = resolve_current_runtime_release(runtime_root)
    assert feedback.status == "no_change"
    assert after.generation == before.generation == 1
    assert after.identity.index_sha256 == before.identity.index_sha256


def test_runtime_page_has_no_direct_store_parquet_network_or_sql_writes():
    source = (
        ROOT / "05_apps" / "import_profit_runtime_page.py"
    ).read_text(encoding="utf-8")
    for forbidden in (
        "upsert_cnf_quotes",
        "cnf_store",
        "write_parquet",
        "pyarrow",
        "akshare",
        "execute(",
        "requests.",
        "socket.",
        "bootstrap_import_profit_runtime",
    ):
        assert forbidden not in source.lower()
