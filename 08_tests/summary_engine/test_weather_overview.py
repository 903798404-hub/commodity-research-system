from __future__ import annotations

from copy import deepcopy
import inspect
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
from streamlit.testing.v1 import AppTest

import research_overview_page
from summary_panel import (
    _focus_current_state_html,
    _focus_reason_html,
    _focus_recent_change_html,
    _focus_table_html,
    _status_cell_style,
    _weather_comprehensive_markdown,
)


def _overview_summary(module: str, headline: str, short_text: str) -> dict:
    return {
        "module": module,
        "headline": headline,
        "source_date": "2026-08-12",
        "freshness_status": "fresh",
        "rule_version": "summary-rules-v1",
        "facts": {},
        "short_text": short_text,
        "detail_text": short_text,
        "missing_reason": None,
    }


def _export_overview_payload() -> dict:
    return {
        "fgis": {"summary": {
            "latest_week": "2026-07-30", "market_year_label": "2025/26",
            "my_week": 48, "weekly_world_mt": 344_000,
            "previous_week_world_mt": 366_000, "cumulative_world_mt": 39_349_000,
            "cumulative_yoy_pct": -17.8, "weekly_china_mt": 0,
            "china_share_pct": 0.0, "previous_week_initial_mt": None,
            "previous_week_initial_observed": False,
        }},
        "fas": {
            "current_summary": {
                "latest_week": "2026-07-30", "report_market_year_label": "2025/26",
                "report_week": 48, "world_weekly_net_sales_mt": 32_000,
                "world_total_commitments_mt": 41_715_000,
                "world_accumulated_exports_mt": 39_317_000,
                "world_outstanding_sales_mt": 2_398_000,
                "sales_progress_pct": None,
                "sales_progress_null_reason": "年度出口预测暂不可用",
            },
            "next_summary": {
                "report_market_year_label": "2025/26", "report_week": 48,
                "target_market_year_end": 2027,
                "world_weekly_net_sales_mt": 904_000,
                "world_total_presales_mt": 8_373_000,
                "china_weekly_net_sales_mt": 330_000,
                "non_china_weekly_net_sales_mt": 574_000,
                "china_total_presales_mt": 3_111_000,
            },
        },
    }


def _overview_app_source(
    *, weather: str = "天气短摘要正文", basis: str = "基差短摘要正文",
    crop: str = "美豆种植短摘要正文", missing: tuple[str, ...] = (),
) -> str:
    weather_value = "{}" if "weather" in missing else repr({
        "大豆天气": [_overview_summary("weather", "美国大豆天气", weather)]
    })
    basis_value = "None" if "basis" in missing else repr(
        _overview_summary("basis", "今日主要基差变化", basis)
    )
    crop_value = "None" if "crop" in missing else repr(
        _overview_summary("soybean_crop", "美豆种植生长最新变化", crop)
    )
    export_value = (
        repr({"fgis": None, "fas": None})
        if "export" in missing
        else repr(_export_overview_payload())
    )
    return f"""
from research_overview_page import render_research_overview
render_research_overview(
    weather_groups={weather_value},
    basis_summary={basis_value},
    crop_summary={crop_value},
    export_payload={export_value},
)
"""


def _complete_weather_groups() -> dict[str, list[dict]]:
    cases = {
        "大豆天气": ("美国大豆", "巴西大豆", "阿根廷大豆"),
        "菜籽天气": ("加拿大菜籽", "澳大利亚菜籽", "欧盟菜籽", "俄罗斯菜籽", "乌克兰菜籽"),
        "棕榈油天气": ("马来西亚", "印度尼西亚"),
        "印度作物天气": ("印度棉花", "印度甘蔗"),
    }
    return {
        group: [
            _overview_summary("weather", name, f"{name} short_text")
            for name in names
        ]
        for group, names in cases.items()
    }


def test_research_overview_has_three_nonempty_independent_module_slots() -> None:
    app = AppTest.from_string(_overview_app_source(), default_timeout=20).run()
    assert not app.exception
    assert [item.value for item in app.subheader] == [
        "天气", "国内基差", "美豆种植生长", "美豆出口销售与装船",
    ]
    visible = "\n".join(str(item.value) for item in (*app.markdown, *app.info))
    assert "天气短摘要正文" in visible
    assert "基差短摘要正文" in visible
    assert "美豆种植短摘要正文" in visible
    assert "出口检验" in visible
    assert "本年度销售" in visible
    assert "下一年度销售" in visible
    assert "summary-rules-v1" not in visible
    assert "source_identity" not in visible
    assert "calculation version" not in visible
    assert len(app.subheader) == 4


def test_weather_groups_are_four_independent_collapsed_expanders_with_counts() -> None:
    app = AppTest.from_string(
        f"""
from research_overview_page import render_research_overview
render_research_overview(
    weather_groups={_complete_weather_groups()!r},
    basis_summary={_overview_summary('basis', '今日主要基差变化', '基差短摘要正文')!r},
    crop_summary={_overview_summary('soybean_crop', '美豆种植生长最新变化', '美豆种植短摘要正文')!r},
    export_payload={_export_overview_payload()!r},
)
""",
        default_timeout=20,
    ).run()

    assert not app.exception
    assert [item.label for item in app.expander] == [
        "大豆天气（3）",
        "菜籽天气（5）",
        "棕榈油天气（2）",
        "印度作物天气（2）",
    ]
    assert all(item.proto.expanded is False for item in app.expander)
    visible = "\n".join(str(item.value) for item in app.markdown)
    for name in (
        "美国大豆", "巴西大豆", "阿根廷大豆",
        "加拿大菜籽", "澳大利亚菜籽", "欧盟菜籽", "俄罗斯菜籽", "乌克兰菜籽",
        "马来西亚", "印度尼西亚", "印度棉花", "印度甘蔗",
    ):
        assert f"{name} short_text" in visible
    assert "基差短摘要正文" in visible
    assert "美豆种植短摘要正文" in visible
    assert "FAS" not in visible
    assert "FGIS" not in visible
    assert "USDA Summary" not in visible


def test_one_missing_overview_module_does_not_hide_the_other_two() -> None:
    app = AppTest.from_string(
        _overview_app_source(missing=("basis",)), default_timeout=20
    ).run()
    assert not app.exception
    visible = "\n".join(str(item.value) for item in (*app.markdown, *app.info))
    assert "基差摘要暂不可用" in visible
    assert "天气短摘要正文" in visible
    assert "美豆种植短摘要正文" in visible


def test_basis_overview_uses_shared_public_current_reader(monkeypatch) -> None:
    identity = SimpleNamespace(
        release_id="formal-release", manifest_sha256="a" * 64,
    )
    records = pd.DataFrame([{
        "date": "2026-08-19", "commodity": "一豆", "region": "华东",
        "quote_type": "基差报价", "delivery_month": "现货",
        "futures_contract": "2609", "cash_price": None,
        "futures_price": None, "basis": -10, "source_sheet": "basis_price:一豆",
    }])
    snapshot = SimpleNamespace(
        records=records,
        source_identity={"release_id": "formal-release", "manifest_sha256": "a" * 64},
    )
    calls: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        research_overview_page, "resolve_public_basis_current_identity",
        lambda _root: identity,
    )
    monkeypatch.setattr(
        research_overview_page, "load_public_basis_current",
        lambda root, *, expected_release_id, expected_manifest_sha256: (
            calls.append((root, expected_release_id, expected_manifest_sha256)) or snapshot
        ),
    )
    research_overview_page._cached_basis_overview_summary.clear()
    summary = research_overview_page.load_basis_overview_summary()
    assert calls and calls[0][1:] == ("formal-release", "a" * 64)
    assert summary.source_dataset == "public_domestic_basis_current"
    assert summary.source_identity["release_id"] == "formal-release"
    assert summary.source_date == "2026-08-19"


def test_all_missing_overview_modules_still_render_three_degraded_slots() -> None:
    app = AppTest.from_string(
        _overview_app_source(missing=("weather", "basis", "crop", "export")),
        default_timeout=20,
    ).run()
    assert not app.exception
    assert [item.value for item in app.subheader] == [
        "天气", "国内基差", "美豆种植生长", "美豆出口销售与装船",
    ]
    info = [item.value for item in app.info]
    assert "天气摘要暂不可用" in info
    assert "基差摘要暂不可用" in info
    assert "美豆种植生长摘要暂不可用" in info
    assert "美豆出口销售与装船摘要暂不可用" in info


def test_export_overview_uses_shared_payload_formatter_and_keeps_business_identities() -> None:
    app = AppTest.from_string(_overview_app_source(), default_timeout=20).run()
    assert not app.exception
    visible = "\n".join(str(item.value) for item in app.markdown)
    assert "**出口检验**" in visible
    assert "本周 World 34.4 万吨，前一周 36.6 万吨" in visible
    assert "累计 3,934.9 万吨，累计同比 -17.8%" in visible
    assert "中国本周 0.0 万吨，占比 0.0%" in visible
    assert "**本年度销售**" in visible
    assert "Report MY 2025/26 第48周" in visible
    assert "累计销售 4,171.5 万吨" in visible
    assert "累计出口 3,931.7 万吨" in visible
    assert "待执行销售 239.8 万吨" in visible
    assert "年度出口预测暂不可用" in visible
    assert "**下一年度销售**" in visible
    assert "目标 MY 2026/27" in visible
    assert "累计预售 837.3 万吨" in visible
    assert "中国本周净销售 33.0 万吨" in visible
    assert "非中国本周净销售 57.4 万吨" in visible
    assert "中国累计预售 311.1 万吨" in visible
    assert "出口检验" in visible and "本周净销售 34.4 万吨" not in visible

    from agri_research_agent.soybean_exports import research
    import soybean_exports_page

    assert (
        research_overview_page.format_soybean_export_weekly_observation
        is research.format_soybean_export_weekly_observation
    )
    assert (
        soybean_exports_page.format_soybean_export_weekly_observation
        is research.format_soybean_export_weekly_observation
    )
    assert (
        research_overview_page.load_current_export_page_payload
        is soybean_exports_page.load_current_export_page_payload
    )
    overview_source = inspect.getsource(research_overview_page)
    assert "world_total_commitments_mt" not in overview_source
    assert "world_next_my_outstanding_sales_mt" not in overview_source
    assert not (research_overview_page.PROJECT_ROOT / "03_src/agri_research_agent/summary_engine/exports.py").exists()


def test_missing_export_stable_only_degrades_export_slot() -> None:
    app = AppTest.from_string(
        _overview_app_source(missing=("export",)), default_timeout=20
    ).run()
    assert not app.exception
    visible = "\n".join(
        str(item.value) for item in (*app.markdown, *app.info)
    )
    assert "美豆出口销售与装船摘要暂不可用" in visible
    assert "天气短摘要正文" in visible
    assert "基差短摘要正文" in visible
    assert "美豆种植短摘要正文" in visible


def test_research_overview_does_not_restore_paused_summary_modules() -> None:
    app = AppTest.from_string(_overview_app_source(), default_timeout=20).run()
    assert not app.exception
    visible = "\n".join(
        str(item.value)
        for item in (*app.title, *app.subheader, *app.markdown, *app.info)
    )
    assert "FAS" not in visible
    assert "FGIS" not in visible
    assert "USDA Summary" not in visible
    assert visible.strip() != "研究快览 / 最新变化"


def test_weather_overview_loads_all_groups_and_countries() -> None:
    groups, warnings = research_overview_page.load_weather_overview_summaries()
    assert {name: len(items) for name, items in groups.items()} == {
        "大豆天气": 3,
        "菜籽天气": 5,
        "棕榈油天气": 2,
        "印度作物天气": 2,
    }
    assert not warnings


def test_one_missing_country_does_not_hide_other_weather(monkeypatch) -> None:
    original = research_overview_page.WEATHER_OVERVIEW_SOURCES
    original_loader = research_overview_page.load_weather_current_summary_cached
    monkeypatch.setattr(
        research_overview_page,
        "WEATHER_OVERVIEW_SOURCES",
        (("大豆天气", "MISSING", "missing.yaml"), *original[:1]),
    )
    monkeypatch.setattr(
        research_overview_page,
        "load_weather_current_summary_cached",
        lambda root, config: (
            (_ for _ in ()).throw(FileNotFoundError(config))
            if Path(config).name == "missing.yaml"
            else original_loader(root, config)
        ),
    )
    groups, warnings = research_overview_page.load_weather_overview_summaries()
    assert len(groups["大豆天气"]) == 1
    assert any("MISSING" in warning for warning in warnings)


def test_weather_summary_ui_hides_technical_traceability() -> None:
    summary = {
        "module": "weather",
        "headline": "美国大豆天气",
        "source_date": "2026-08-17",
        "facts": {
            "forecast_end": {"ECMWF": "2026-09-01", "GFS": "2026-08-31"},
            "source_identity": {"manifest_sha256": "must-not-render"},
            "weather_render": {
                "comprehensive": "当前主要产区天气整体平稳。",
                "focus_regions": [{
                    "region": "Iowa（25%）",
                    "status": "平稳",
                    "current_state": "土墒接近5年同期（+0.5个百分点）",
                    "recent_change": "7日持平",
                    "short_term": "接近正常",
                    "medium_term": "接近正常",
                    "reason": "当前暂无复合异常，研究优先级较低",
                }],
                "rain_markdown": "| 地区 | 近7日 |\n|---|---:|\n| Iowa | 12 mm |",
                "temperature_markdown": "| 地区 | 最高气温 |\n|---|---:|\n| Iowa | 30℃ |",
                "soil_markdown": "| 地区 | 0—100cm土壤含水率 |\n|---|---:|\n| Iowa | 23.6% |",
            },
        },
        "short_text": "天气摘要。",
        "detail_text": "天气详情。",
        "missing_reason": None,
        "rule_version": "summary-rules-v1",
    }
    app = AppTest.from_string(f"""
from summary_panel import render_summary_panel
payload = {summary!r}
render_summary_panel(payload)
""", default_timeout=20)
    app.run()
    assert not app.exception
    text = " ".join(str(item.value) for item in (*app.caption, *app.markdown))
    assert "数据更新至" in text and "EC预测至" in text and "GFS预测至" in text
    assert "summary-rules" not in text
    assert "source_identity" not in text
    assert "查看可追溯事实" not in [item.label for item in app.expander]
    assert [item.label for item in app.expander] == ["完整降水数据", "完整最高气温数据", "完整土墒数据"]
    assert len(app.dataframe) == 0
    assert any("<table" in str(item.value) and "<strong>" in str(item.value) for item in app.markdown)


def test_weather_focus_status_colors_are_scoped_to_status_values() -> None:
    assert "#fdecec" in _status_cell_style("风险")
    assert "#fff7d6" in _status_cell_style("关注")
    assert "#f1f4f1" in _status_cell_style("平稳")
    assert _status_cell_style("方向一致偏干") == ""


def _focus_row(**overrides):
    row = {
        "region": "测试区（31%）", "status": "关注",
        "current_state": "土墒接近5年同期（+0.5个百分点）",
        "recent_change": "7日下降0.3个百分点",
        "short_term": "接近正常", "medium_term": "一致显著偏干",
        "reason": "当前水分仍有缓冲，但近7日土墒走弱",
    }
    row.update(overrides)
    return row


def test_focus_current_state_emphasizes_only_directional_semantics() -> None:
    assert _focus_current_state_html("土墒较5年同期高3.3个百分点") == "土墒较5年同期 <strong>高</strong> 3.3个百分点"
    assert _focus_current_state_html("土墒较5年同期低4.4个百分点") == "土墒较5年同期 <strong>低</strong> 4.4个百分点"
    assert _focus_current_state_html("土墒接近5年同期（+0.5个百分点）") == "土墒 <strong>接近</strong> 5年同期（+0.5个百分点）"


def test_focus_recent_change_emphasizes_only_direction() -> None:
    assert _focus_recent_change_html("7日下降0.3个百分点") == "7日 <strong>下降</strong> 0.3个百分点"
    assert _focus_recent_change_html("7日上升1.2个百分点") == "7日 <strong>上升</strong> 1.2个百分点"
    assert _focus_recent_change_html("7日回升0.4个百分点") == "7日 <strong>回升</strong> 0.4个百分点"


def test_focus_reason_emphasizes_trigger_but_not_background_sentence() -> None:
    html = _focus_reason_html(_focus_row())
    assert html == "当前水分仍有缓冲，但近7日 <strong>土墒下降</strong> 0.3个百分点"
    assert "<strong>当前水分仍有缓冲" not in html


def test_focus_risk_row_emphasizes_level_trend_and_dry_forecast() -> None:
    row = _focus_row(
        status="风险", current_state="土墒较5年同期低4.8个百分点",
        recent_change="7日下降2.1个百分点", short_term="接近正常",
        medium_term="一致显著偏干",
        reason="土墒低于同期且近7日继续下降，未来至少一段EC/GFS均明显偏干",
    )
    html = _focus_table_html([row])
    assert "<strong>低</strong> 4.8个百分点" in html
    assert "<strong>下降</strong> 2.1个百分点" in html
    assert "<strong>一致显著偏干</strong>" in html
    assert "当前 <strong>土墒低于同期</strong>，且近7日 <strong>土墒继续下降</strong>" in html


def test_weather_comprehensive_emphasizes_semantics_without_numbers_or_regions() -> None:
    source = (
        "当前主要关注Córdoba（29%）：当前水分仍有缓冲，但近7日土墒走弱。"
        "当前主要产区0—100cm土壤含水率均略高于5年同期，EC冷信号更强。"
    )
    rendered = _weather_comprehensive_markdown(source)
    assert "Córdoba（29%）" in rendered
    assert "**土墒走弱**" in rendered
    assert "均**略高于**5年同期" in rendered
    assert "**EC冷信号更强**" in rendered
    assert "**29%**" not in rendered


def test_weather_comprehensive_preserves_existing_direction_emphasis() -> None:
    source = "未来两周主要区域整体**方向一致偏湿**，部分区域土壤含水率低于5年同期，EC/GFS方向分歧，近7日继续下降。"
    rendered = _weather_comprehensive_markdown(source)
    assert rendered.count("**方向一致偏湿**") == 1
    assert "含水率**低于**5年同期" in rendered
    assert "**EC/GFS方向分歧**" in rendered
    assert "近7日**继续下降**" in rendered
    assert "**近7日" not in rendered


def test_focus_html_preserves_status_and_facts_without_raw_markdown() -> None:
    row = _focus_row(status="平稳", reason="当前暂无复合异常，研究优先级较低")
    original = deepcopy(row)
    html = _focus_table_html([row])
    assert row == original
    assert row["status"] == "平稳"
    assert html.count("background-color:") == 1
    assert "#f1f4f1" in html
    assert "<strong>接近正常</strong>" in html and "<strong>一致显著偏干</strong>" in html
    assert "当前暂无复合异常，研究优先级较低" in html
    assert "**" not in html


def test_empty_weather_focus_table_renders_a_calm_message() -> None:
    app = AppTest.from_string("""
from summary_panel import _render_weather_detail
_render_weather_detail({"weather_render": {
    "comprehensive": "综合事实。", "focus_regions": [],
    "rain_markdown": "降水事实", "temperature_markdown": "气温事实", "soil_markdown": "土墒事实",
}})
""", default_timeout=20)
    app.run()
    assert not app.exception
    assert "当前暂无需要特别关注的地区。" in [item.value for item in app.caption]
