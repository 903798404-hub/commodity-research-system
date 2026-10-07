from __future__ import annotations

import json
import sys
import zipfile
from datetime import date
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from agri_research_agent.pipelines.canada_canola import (
    SOURCE_URLS, STABLE_RELATIVE_PATH, compare_metric, comparison_table, import_workbook,
    load_bundle, match_history, merge_observations, observations_frame, seasonal_figure,
    sha256_file, strict_json, validate_bundle,
)
from agri_research_agent.pipelines.canada_canola_update import activate_local, prepare_update, save_candidate
from agri_research_agent.shared.atomic_storage import atomic_write_json
from agri_research_agent.shared.runtime_context import RuntimeContext, RuntimeMode

ROOT = Path(__file__).resolve().parents[2]


def observation(day="2026-09-08", value=15, province="MB", metric="HARVESTED"):
    return dict(province=province, metric=metric, date=day, value=value,
                source_url=SOURCE_URLS[province], source_sha256="a" * 64,
                source_locator="table 1, canola provincial", date_basis="workbook_date",
                published_at=None, retrieved_at="2026-10-07T00:00:00+00:00", status="reported")


def bundle(*items):
    return dict(schema_version="canada-canola/1", generated_at="2026-10-07T00:00:00+00:00",
                records=list(items), import_notes=[])


def context(tmp_path):
    atomic_write_json(tmp_path / ".market-data-runtime.json", {
        "schema_version": 1, "runtime_id": "test-canola", "classification": "fixture",
        "module_id": "canada-canola", "created_at": "2026-10-07T00:00:00+00:00",
    })
    return RuntimeContext(RuntimeMode.FIXTURE, "canada-canola", tmp_path)


@pytest.mark.parametrize("value", [None, True, float("nan"), float("inf"), -1, 101])
def test_invalid_percentage_is_rejected(value):
    with pytest.raises(ValueError):
        validate_bundle(bundle(observation(value=value)))


def test_duplicate_unknown_future_and_wrong_source_rejected():
    item = observation()
    with pytest.raises(ValueError, match="duplicate"):
        validate_bundle(bundle(item, item))
    for replacement in [dict(date="2099-01-01"), dict(metric="EXPORT_SALES"),
                        dict(source_url="https://example.com/report"), dict(published_at="2026-09-01"),
                        dict(province=[]), dict(retrieved_at=None), dict(published_at=123)]:
        with pytest.raises(ValueError):
            validate_bundle(bundle({**item, **replacement}))


def test_missing_current_year_is_not_replaced_by_old_data():
    frame = observations_frame(bundle(observation("2021-08-24", 35, metric="GOOD_EXCELLENT")))
    item = compare_metric(frame, "MB", "GOOD_EXCELLENT", 2026)
    assert item["current"] is None
    assert item["latest_historical"]["date"].year == 2021
    table = comparison_table(frame, "MB", 2026)
    assert table.iloc[2]["状态"] == "本年度暂无数据"
    assert table.iloc[2]["最新（%）"] is None


def test_history_alignment_excludes_future_and_stale_dates_and_keeps_zero():
    frame = observations_frame(bundle(observation("2025-09-01", 0), observation("2025-09-09", 80),
                                     observation("2024-08-31", 90)))
    matched = match_history(frame, date(2026, 9, 8), 2025)
    assert matched["value"] == 0
    assert match_history(frame, date(2026, 9, 8), 2024) is None


def test_mean_excludes_current_year_and_reports_partial_samples():
    frame = observations_frame(bundle(observation("2026-09-08", 15), observation("2026-08-25", 5),
                                     observation("2025-09-02", 20), observation("2024-09-03", 40)))
    item = compare_metric(frame, "MB", "HARVESTED", 2026)
    assert item["mean"] == 30
    assert len(item["samples"]) == 2
    assert item["previous_change"] == 10
    assert item["previous_days"] == 14
    table = comparison_table(frame, "MB", 2026)
    assert table.iloc[1]["有效样本"] == "2/5"
    assert table.iloc[1]["较均值（百分点）"] == -15


def test_chart_has_no_forecast_or_extrapolated_current_points():
    frame = observations_frame(bundle(observation("2026-09-08", 15), observation("2025-09-02", 20),
                                     observation("2025-09-16", 60)))
    figure = seasonal_figure(frame, "MB", "HARVESTED", 2026, [])
    trace = next(x for x in figure.data if x.name == "2026年")
    assert len(trace.x) == 1
    assert trace.customdata[0] == "2026-09-08"
    mean = next(x for x in figure.data if x.name == "前五年同期均值")
    assert list(mean.customdata) == [1, 1]


def test_merge_is_idempotent_and_revision_requires_explicit_choice():
    baseline = bundle(observation())
    candidate, stats = merge_observations(baseline, [{**observation(), "retrieved_at": "2026-10-07T01:00:00+00:00"}])
    assert candidate == baseline
    assert stats == dict(added=0, revised=0, unchanged=1)
    with pytest.raises(ValueError, match="explicit revision"):
        merge_observations(baseline, [observation(value=16)])
    updated, stats = merge_observations(baseline, [observation(value=16)], allow_revisions=True)
    assert updated["records"][0]["value"] == 16
    assert stats["revised"] == 1


def test_atomic_local_activation_backup_and_stale_candidate(tmp_path):
    ctx = context(tmp_path)
    first = save_candidate(ctx, bundle(observation()), baseline=None, changes={})
    stable = activate_local(ctx, first)
    assert activate_local(ctx, first) == stable
    baseline_hash = sha256_file(stable)
    second = save_candidate(ctx, bundle(observation(), observation("2026-09-15", 50)),
                            baseline=baseline_hash, changes={})
    stale = save_candidate(ctx, bundle(observation(), observation("2026-09-16", 60)),
                           baseline=baseline_hash, changes={})
    activate_local(ctx, second)
    backups = list((tmp_path / "backups/canada_canola").glob("*.json"))
    assert len(backups) == 1 and sha256_file(backups[0]) == baseline_hash
    current_hash = sha256_file(stable)
    with pytest.raises(ValueError, match="stable data moved"):
        activate_local(ctx, stale)
    assert sha256_file(stable) == current_hash


def test_tampered_candidate_leaves_stable_unchanged(tmp_path):
    ctx = context(tmp_path)
    candidate = save_candidate(ctx, bundle(observation()), baseline=None, changes={})
    atomic_write_json(candidate, bundle(observation(value=99)))
    with pytest.raises(ValueError, match="identity mismatch"):
        activate_local(ctx, candidate)
    assert not (tmp_path / STABLE_RELATIVE_PATH).exists()


def test_official_update_requires_archived_source_and_separate_dates(tmp_path):
    ctx = context(tmp_path)
    stable = activate_local(ctx, save_candidate(ctx, bundle(observation()), baseline=None, changes={}))
    raw = tmp_path / "raw/canada_canola/example"
    raw.mkdir(parents=True)
    (raw / "report.bin").write_bytes(b"Canola harvested 50% at September 15, published September 17.")
    retrieved = "2026-10-07T01:00:00+00:00"
    atomic_write_json(raw / "source.json", dict(province="MB", source_url=SOURCE_URLS["MB"],
                                              sha256=sha256_file(raw / "report.bin"), retrieved_at=retrieved))
    update = {**observation("2026-09-15", 50), "source_sha256": sha256_file(raw / "report.bin"),
              "date_basis": "report_cutoff", "published_at": "2026-09-17", "retrieved_at": retrieved}
    observations = tmp_path / "observations.json"
    atomic_write_json(observations, bundle(update))
    candidate = prepare_update(ctx, stable, observations)
    assert len(load_bundle(candidate)["records"]) == 2
    (raw / "report.bin").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="archived"):
        prepare_update(ctx, stable, observations)


def test_workbook_import_reads_raw_values_not_pivots_or_cumulative_stages(tmp_path):
    path = tmp_path / "历史.xlsx"
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    workbook = f'<workbook xmlns="{ns}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
    names = ["萨省（产量55%）", "阿尔伯塔（产量28%）", "曼省（产量16%）"]
    sheets = [dict(C2="日期", J2="Provincial", CU2="日期", CV2="Provincial", BR3="日期", ID2="Provincial", IK2="收割率",
                   C3=46280, J3=0.9, M3=50, BR4=46280, BS4=0.2, BT4=0.5, CB4=1.0),
              dict(E1="日期", K1="Alberta", AB2="日期", AH2="Alberta", AS2="日期", AY2="Alberta",
                   E2=46280, K2=0.8, AS3=46280, AY3=0.1, AS4=46287),
              dict(C1="日期", D1="曼省", N2="日期", T2="Provincial", V1="日期", W1="收割进度",
                   C2=46280, D2=0.5, N3=46280, T3=36, V2=46280, W2=0)]
    with zipfile.ZipFile(path, "w") as archive:
        for i, name in enumerate(names, 1):
            workbook += f'<sheet name="{name}" sheetId="{i}" r:id="rId{i}"/>'
            cells = "".join(f'<c r="{ref}" t="str"><v>{value}</v></c>' if isinstance(value, str)
                            else f'<c r="{ref}"><v>{value}</v></c>' for ref, value in sheets[i - 1].items())
            archive.writestr(f"xl/worksheets/sheet{i}.xml", f'<worksheet xmlns="{ns}"><sheetData><row r="3">{cells}</row></sheetData></worksheet>')
        archive.writestr("xl/workbook.xml", workbook + "</sheets></workbook>")
        archive.writestr("xl/_rels/workbook.xml.rels", '<Relationships>' + "".join(
            f'<Relationship Id="rId{i}" Target="worksheets/sheet{i}.xml"/>' for i in range(1, 4)) + '</Relationships>')
        archive.writestr("xl/sharedStrings.xml", f'<sst xmlns="{ns}"/>')
    original_hash = sha256_file(path)
    data = import_workbook(path)
    frame = observations_frame(data)
    assert len(frame) == 8
    assert set(frame.loc[frame.province == "SK", "value"]) == {90, 20, 50}
    assert frame.loc[(frame.province == "MB") & (frame.metric == "GOOD_EXCELLENT"), "value"].item() == 36
    assert frame.loc[(frame.province == "MB") & (frame.metric == "HARVESTED"), "value"].item() == 0
    assert sha256_file(path) == original_hash


def test_duplicate_json_keys_rejected(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text('{"records":[],"records":[]}', encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        strict_json(path)


def test_real_workspace_route_renders_canada_and_switches_province(tmp_path, monkeypatch):
    root = tmp_path / "01_data"
    stable = root / STABLE_RELATIVE_PATH
    stable.parent.mkdir(parents=True)
    atomic_write_json(stable, bundle(observation(), observation(value=9, province="SK"),
                                    observation(value=40, province="AB")))
    monkeypatch.setenv("PUBLIC_MARKET_DATA_RUNTIME_ROOT", str(root))
    apps = str(ROOT / "05_apps")
    if apps not in sys.path:
        sys.path.insert(0, apps)
    app = AppTest.from_file(str(ROOT / "05_apps/streamlit_app.py"), default_timeout=25)
    app.query_params["workspace_page"] = "加拿大菜籽种植与生长"
    app.run()
    assert not app.exception
    assert app.title[0].value == "加拿大菜籽种植与生长"
    assert app.radio[0].options == ["萨省（55%）", "阿尔伯塔省（28%）", "曼省（16%）"]
    app.radio[0].set_value("MB").run()
    assert not app.exception
    assert app.dataframe[0].value.iloc[1]["最新"] == "15.0"
    assert app.expander[0].label == "生长阶段" and not app.expander[0].proto.expanded
