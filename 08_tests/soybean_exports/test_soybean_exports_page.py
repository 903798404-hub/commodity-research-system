from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pandas as pd
from streamlit.testing.v1 import AppTest

from agri_research_agent.soybean_exports.research import (
    SALES_PROGRESS_NULL_REASON,
    _same_week_comparison,
    build_soybean_export_page_payload,
    load_soybean_export_page_payload,
)
from test_fas_data_layer import fas_row, normalize as normalize_fas
from test_fgis_data_layer import normalize as normalize_fgis
from test_fgis_data_layer import raw_row
from test_research_and_isolation import make_usda


PROJECT_ROOT = Path(__file__).resolve().parents[2]
APPS_DIR = PROJECT_ROOT / "05_apps"
FORMAL_ENTRY = APPS_DIR / "streamlit_app.py"
if str(APPS_DIR) not in sys.path:
    sys.path.insert(0, str(APPS_DIR))

from soybean_exports_page import (  # noqa: E402
    HISTORY_YEAR_COLORS,
    _year_style,
    build_fas_figures,
    build_fgis_figures,
)


def page_payload(
    tmp_path: Path, *, usda_project_root: Path | None = None
) -> dict[str, object]:
    fgis_rows = []
    for start_year in range(2019, 2026):
        end_year = start_year + 1
        fgis_rows.extend(
            [
                raw_row(
                    cert_date=f"{start_year}-09-01",
                    week_ending=f"{start_year}-09-05",
                    destination="CHINA",
                    mt=(end_year - 2018) * 10,
                ),
                raw_row(
                    cert_date=f"{start_year}-09-01",
                    week_ending=f"{start_year}-09-05",
                    destination="JAPAN",
                    mt=(end_year - 2018) * 20,
                ),
            ]
        )
    fgis_rows.extend(
        [
            raw_row(cert_date="2025-09-08", week_ending="2025-09-11", destination="CHINA", mt=40),
            raw_row(cert_date="2025-09-08", week_ending="2025-09-11", destination="JAPAN", mt=80),
        ]
    )
    fas_rows = []
    for report_year in range(2020, 2027):
        current_net = -50 if report_year == 2026 else report_year - 2010
        for country in (5700, 9990):
            fas_rows.append(
                fas_row(
                    country=country,
                    week=f"{report_year}-07-30T00:00:00",
                    report_year=report_year,
                    current_net=current_net,
                    next_net=20 if country == 5700 else 30,
                    accumulated=25_000_000,
                    outstanding=2_000_000,
                )
            )
    return build_soybean_export_page_payload(
        fgis_stable=normalize_fgis(fgis_rows),
        fas_stable=normalize_fas(fas_rows),
        usda_project_root=(
            make_usda(tmp_path) if usda_project_root is None else usda_project_root
        ),
        fgis_manifest={"batch_id": "fgis-fixture", "source_latest_week": "2025-09-11"},
        fas_manifest={
            "batch_id": "fas-fixture",
            "source_latest_week": "2026-07-30",
            "source_release_time_raw": "2026-08-06T08:30:06.973",
            "source_release_timezone": "America/New_York",
        },
        fgis_status={"status": "updated"},
        fas_status={"status": "updated"},
    )


def render_payload(payload: dict[str, object]) -> AppTest:
    serialized = json.dumps(payload, ensure_ascii=False)
    script = f"""
import json, sys
sys.path.insert(0, {str(APPS_DIR)!r})
from soybean_exports_page import render_soybean_exports_page
render_soybean_exports_page(json.loads({serialized!r}))
"""
    return AppTest.from_string(script, default_timeout=30).run()


def test_formal_workspace_has_two_first_level_tabs_and_preserves_crop_content() -> None:
    app = AppTest.from_file(str(FORMAL_ENTRY), default_timeout=30).run()
    app.session_state["selected_workspace_page"] = "美豆种植生长"
    app.run(timeout=30)
    labels = [tab.label for tab in app.tabs]
    assert labels[0] == "种植生长"
    assert labels[-1] == "出口销售与装船"
    assert labels[1:-1] == ["播种率", "出苗率", "开花率", "结荚率", "收割率", "优良率"]
    assert any(title.value == "美豆种植生长" for title in app.title)
    assert any(title.value == "出口销售与装船" for title in app.title)
    assert not app.exception


def test_page_renders_independent_status_four_kpis_observation_and_thirteen_charts(tmp_path: Path) -> None:
    payload = page_payload(tmp_path)
    app = render_payload(payload)
    assert not app.exception
    assert len(app.metric) == 8
    assert [metric.label for metric in app.metric[:4]] == [
        "累计出口检验同比",
        "本年度销售完成率",
        "下一年度累计销售",
        "中国下一年度累计采购",
    ]
    assert float(str(app.metric[1].value).replace("%", "").replace("+", "")) > 100
    assert app.metric[2].value.endswith("万吨")
    assert len(app.get("plotly_chart")) == 13
    markdown = "\n".join(item.value for item in app.markdown)
    assert "出口检验" in markdown and "本年度销售" in markdown and "下一年度销售" in markdown
    captions = "\n".join(item.value for item in app.caption)
    assert "2026-08-06 08:30:06.973 ET" in captions


def test_page_remains_complete_when_entire_psd_directory_is_absent(tmp_path: Path) -> None:
    available = page_payload(tmp_path / "available")
    payload = page_payload(
        tmp_path / "missing",
        usda_project_root=tmp_path / "missing" / "absent-usda",
    )
    assert payload["fas"]["current_summary"]["sales_progress_pct"] is None
    assert payload["fas"]["current_summary"]["sales_progress_denominator"] is None
    assert (
        payload["fas"]["current_summary"]["sales_progress_null_reason"]
        == SALES_PROGRESS_NULL_REASON
    )
    assert payload["kpis"]["current_my_sales_progress_pct"] is None
    assert payload["kpi_reasons"]["current_my_sales_progress_pct"] == SALES_PROGRESS_NULL_REASON
    for key in (
        "cumulative_export_inspections_yoy_pct",
        "next_my_total_sales_mt",
        "china_next_my_total_purchases_mt",
    ):
        assert payload["kpis"][key] == available["kpis"][key]

    app = render_payload(payload)
    assert not app.exception
    assert app.metric[1].value == "—"
    assert app.metric[1].help == SALES_PROGRESS_NULL_REASON
    assert len(app.get("plotly_chart")) == 13
    markdown = "\n".join(item.value for item in app.markdown)
    assert "本周净销售" in markdown
    assert "累计销售" in markdown
    assert "累计出口" in markdown
    assert "待执行销售" in markdown
    assert "下一年度销售" in markdown
    assert SALES_PROGRESS_NULL_REASON in markdown
    assert "/app/11_" not in markdown


def test_twelve_seasonal_charts_use_history_palette_axes_labels_and_single_hover(tmp_path: Path) -> None:
    payload = page_payload(tmp_path)
    fgis = build_fgis_figures(payload["fgis"])
    fas_current, fas_next, execution = build_fas_figures(payload["fas"])
    core = [*fgis, *fas_current, *fas_next]
    assert len(fgis) == 4
    assert len(fas_current) == 4
    assert len(fas_next) == 4
    assert len(core) == 12
    assert [figure.layout.title.text.split("<br>", maxsplit=1)[0] for figure in core] == [
        "周度出口检验",
        "累计出口检验",
        "周度出口检验至中国",
        "累计出口检验至中国",
        "本年度周度净销售",
        "本年度累计销售",
        "本年度当周对中国净销售",
        "本年度累计对中国销售",
        "下一年度周度净销售",
        "下一年度累计销售",
        "下一年度当周对中国净销售",
        "下一年度累计对中国销售",
    ]
    assert all(figure.layout.meta["x_field"] == "my_week" for figure in fgis)
    assert all(figure.layout.meta["x_field"] == "report_week" for figure in [*fas_current, *fas_next])
    assert all(len(figure.layout.meta["history_years"]) == 7 for figure in core)
    assert all(figure.layout.meta["core_seasonal"] is True for figure in core)
    assert all(figure.layout.meta["target_my_week"] is False for figure in fas_next)
    assert min(float(value) for trace in fas_current[0].data for value in trace.y) < 0
    assert min(float(value) for trace in fas_current[2].data for value in trace.y) < 0
    assert all(figure.layout.meta["latest_point_label"] for figure in core)
    assert all(figure.layout.hovermode == "closest" for figure in core)
    assert all(figure.layout.hoverlabel.font.size == 14 for figure in core)
    assert all(figure.layout.legend.font.size == 13 for figure in core)
    assert all(figure.layout.xaxis.tickfont.size == 12 for figure in core)
    assert all(figure.layout.yaxis.tickfont.size == 12 for figure in core)
    assert all(figure.layout.title.font.size == 20 for figure in core)
    palette_sequences = [[trace.line.color for trace in figure.data] for figure in core]
    assert all(colors == palette_sequences[0] for colors in palette_sequences)
    assert palette_sequences[0] == list(reversed(HISTORY_YEAR_COLORS))
    assert all(len(set(colors)) == 7 for colors in palette_sequences)
    assert HISTORY_YEAR_COLORS == (
        "#B3473A",
        "#62788F",
        "#5D8E92",
        "#7E9A73",
        "#B29A5B",
        "#C08267",
        "#8D8098",
    )
    assert _year_style(2031, 2031, 2030) == ("#B3473A", 3.9, "solid", 1.0)
    assert _year_style(2030, 2031, 2030) == ("#62788F", 2.4, "dash", 0.95)
    assert _year_style(2029, 2031, 2030) == ("#5D8E92", 1.9, "solid", 0.90)
    for figure in core:
        assert figure.data[-1].line.width == 3.9
        assert figure.data[-1].line.color == "#B3473A"
        assert figure.data[-1].opacity == 1.0
        assert figure.data[-2].line.width == 2.4
        assert figure.data[-2].line.color == "#62788F"
        assert figure.data[-2].line.dash == "dash"
        assert figure.data[-2].opacity == 0.95
        assert all(trace.line.width == 1.9 and trace.opacity == 0.90 for trace in figure.data[:-2])
        assert all(trace.connectgaps is False for trace in figure.data)
        latest_labels = [annotation for annotation in figure.layout.annotations if annotation.xref != "paper"]
        assert len(latest_labels) == 1
        assert latest_labels[0].font.color == "#B3473A"
        assert latest_labels[0].font.size == 14
    assert "Week ending" in fgis[0].data[0].hovertemplate
    assert "Target MY" not in fas_current[0].data[0].hovertemplate
    assert all("Target MY" in figure.data[0].hovertemplate for figure in fas_next)
    assert all("Report MY" in figure.data[0].hovertemplate for figure in fas_next)
    assert all("Report week" in figure.data[0].hovertemplate for figure in fas_next)
    current_next_x = [
        list(trace.x)
        for trace in fas_next[1].data
        if trace.opacity == 1.0
    ]
    assert current_next_x == [[1]]
    cumulative = [fgis[1], fgis[3], fas_current[1], fas_current[3], fas_next[1], fas_next[3]]
    assert all(figure.layout.meta["comparison_callout"] is True for figure in cumulative)
    assert all(figure.layout.meta["same_week_rank"] is not None for figure in cumulative)
    assert all(figure.layout.meta["same_week_comparable_years"] >= 1 for figure in cumulative)
    callout_positions = set()
    for figure in cumulative:
        callout = next(annotation for annotation in figure.layout.annotations if annotation.xref == "paper")
        assert "同期排名" in callout.text
        assert "#B3473A" in callout.text
        assert "#62788F" in callout.text
        assert callout.y > 1
        assert callout.yanchor == "bottom"
        callout_positions.add((callout.x, callout.y, callout.xanchor, callout.yanchor))
        assert figure.layout.legend.y == 0.98
    assert callout_positions == {(0.99, 1.21, "right", "bottom")}
    assert execution.layout.meta["auxiliary_execution"] is True
    assert execution.layout.meta["core_seasonal"] is False
    assert execution.layout.meta["official_outstanding_field"] == "world_outstanding_sales_mt"
    assert not any(trace.line.color in HISTORY_YEAR_COLORS for trace in execution.data)
    chart_ids = {figure.layout.meta["chart_id"] for figure in core}
    assert "fgis_weekly_structure" not in chart_ids
    assert "fgis_cumulative_structure" not in chart_ids


def test_research_payload_exposes_exact_week_seasonal_comparisons(tmp_path: Path) -> None:
    payload = page_payload(tmp_path)
    assert set(payload["fgis"]["seasonal_comparisons"]) == {
        "world_cumulative_mt",
        "china_cumulative_mt",
    }
    assert payload["fgis"]["seasonal_comparisons"]["world_cumulative_mt"]["comparison_week"] == 2
    assert payload["fgis"]["seasonal_comparisons"]["world_cumulative_mt"]["previous_same_week_mt"] is None
    assert payload["fgis"]["seasonal_comparisons"]["world_cumulative_mt"]["same_week_rank"] == 1
    assert payload["fgis"]["seasonal_comparisons"]["world_cumulative_mt"]["same_week_comparable_years"] == 1
    assert set(payload["fas"]["seasonal_comparisons"]) == {
        "world_current_my_total_commitment_mt",
        "china_current_my_total_commitment_mt",
        "world_next_my_outstanding_sales_mt",
        "china_next_my_outstanding_sales_mt",
    }
    assert all(
        comparison["same_week_comparable_years"] == 7
        for comparison in payload["fas"]["seasonal_comparisons"].values()
    )


def test_same_week_rank_excludes_missing_and_other_week_values() -> None:
    frame = pd.DataFrame(
        [
            {"year": 2020, "week": 5, "value": 100},
            {"year": 2021, "week": 5, "value": None},
            {"year": 2022, "week": 4, "value": 500},
            {"year": 2023, "week": 5, "value": 300},
            {"year": 2024, "week": 5, "value": 200},
        ]
    )
    comparison = _same_week_comparison(
        frame,
        year_field="year",
        week_field="week",
        value_field="value",
        current_year=2024,
    )
    assert comparison["comparison_week"] == 5
    assert comparison["previous_same_week_mt"] == 300
    assert comparison["same_week_rank"] == 2
    assert comparison["same_week_comparable_years"] == 3


def test_missing_kpis_stay_missing_psd_failure_isolated_and_sources_degrade_independently(tmp_path: Path) -> None:
    payload = page_payload(tmp_path)
    no_fgis = build_soybean_export_page_payload(
        fgis_stable=None,
        fas_stable=normalize_fas(
            [fas_row(country=5700), fas_row(country=9990)]
        ),
        usda_project_root=tmp_path / "missing-usda",
        fgis_error="FGIS fixture unavailable",
        fas_status={"status": "updated"},
    )
    assert no_fgis["fgis"] is None
    assert no_fgis["fas"] is not None
    assert no_fgis["kpis"]["cumulative_export_inspections_yoy_pct"] is None
    assert no_fgis["kpis"]["current_my_sales_progress_pct"] is None
    app = render_payload(no_fgis)
    assert not app.exception
    assert [metric.value for metric in app.metric[:2]] == ["—", "—"]
    assert len(app.get("plotly_chart")) == 9
    assert any("出口检验数据暂不可用" in item.value for item in app.warning)

    no_fas = build_soybean_export_page_payload(
        fgis_stable=normalize_fgis([raw_row(destination="JAPAN")]),
        fas_stable=None,
        usda_project_root=tmp_path,
        fas_error="FAS fixture unavailable",
        fgis_status={"status": "updated"},
    )
    app = render_payload(no_fas)
    assert not app.exception
    assert len(app.get("plotly_chart")) == 4
    assert any("出口销售数据暂不可用" in item.value for item in app.warning)


def test_revision_evidence_is_not_invented_and_explicit_evidence_is_rendered(tmp_path: Path) -> None:
    payload = page_payload(tmp_path)
    app = render_payload(payload)
    markdown = "\n".join(item.value for item in app.markdown)
    assert "尚无由本系统观察到的前一周初值/修订证据" in markdown
    observed = copy.deepcopy(payload)
    observed["fgis"]["summary"]["previous_week_initial_observed"] = True
    observed["fgis"]["summary"]["previous_week_initial_mt"] = 88
    app = render_payload(observed)
    markdown = "\n".join(item.value for item in app.markdown)
    assert "前一周初值" in markdown
    assert "尚无由本系统观察到" not in markdown


def test_loader_rejects_missing_or_unsealed_sources_without_blocking_other_side(tmp_path: Path) -> None:
    payload = load_soybean_export_page_payload(
        tmp_path / "empty-runtime", usda_project_root=tmp_path / "missing-usda"
    )
    assert payload["fgis"] is None and payload["fas"] is None
    assert "stable" in payload["errors"]["fgis"]
    assert "stable" in payload["errors"]["fas"]


def test_page_source_remains_a_thin_consumer() -> None:
    source = (APPS_DIR / "soybean_exports_page.py").read_text(encoding="utf-8")
    assert "load_soybean_export_page_payload" in source
    assert "cert_date" not in source
    assert "country_code" not in source
    assert "current_my_total_commitment_mt -" not in source
    assert "target_my_week" not in source.replace('"target_my_week": False', "")
