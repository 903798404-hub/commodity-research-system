from __future__ import annotations
from html import escape
import re
from typing import Any
import streamlit as st


def _status_cell_style(value: str) -> str:
    return {
        "风险": "background-color: #fdecec; color: #7f1d1d",
        "关注": "background-color: #fff7d6; color: #6b4f00",
        "平稳": "background-color: #f1f4f1; color: #374151",
    }.get(value, "")


_WEATHER_CLASSIFICATIONS = {
    "接近正常", "偏少", "明显偏少", "显著偏少", "偏多", "明显偏多", "显著偏多",
    "中性", "偏高", "明显偏高", "显著偏高", "偏低", "明显偏低", "显著偏低",
    "方向一致偏湿", "方向一致偏干", "一致显著偏湿", "一致显著偏干", "模型方向分歧",
}
_DRY_CLASSIFICATIONS = {"方向一致偏干", "一致显著偏干"}


def _focus_current_state_html(value: str) -> str:
    if value.startswith("土墒较5年同期高"):
        suffix = value.removeprefix("土墒较5年同期高")
        return "土墒较5年同期 <strong>高</strong> " + escape(suffix)
    if value.startswith("土墒较5年同期低"):
        suffix = value.removeprefix("土墒较5年同期低")
        return "土墒较5年同期 <strong>低</strong> " + escape(suffix)
    if value.startswith("土墒接近5年同期"):
        suffix = value.removeprefix("土墒接近5年同期")
        return "土墒 <strong>接近</strong> 5年同期" + escape(suffix)
    return escape(value)


def _focus_trend_parts(value: str) -> tuple[str | None, str | None]:
    for direction in ("继续下降", "继续上升", "下降", "上升", "回升", "改善", "转弱", "持平"):
        prefix = f"7日{direction}"
        if value.startswith(prefix):
            return direction, value.removeprefix(prefix)
    return None, None


def _focus_recent_change_html(value: str) -> str:
    direction, suffix = _focus_trend_parts(value)
    if direction is None or suffix is None:
        return escape(value)
    return "7日 <strong>" + escape(direction) + "</strong>" + (" " + escape(suffix) if suffix else "")


def _focus_classification_html(value: str) -> str:
    return f"<strong>{escape(value)}</strong>" if value in _WEATHER_CLASSIFICATIONS else escape(value)


def _focus_reason_html(row: dict[str, Any]) -> str:
    reason = str(row["reason"])
    recent = str(row["recent_change"])
    direction, trend_value = _focus_trend_parts(recent)
    short = str(row["short_term"])
    medium = str(row["medium_term"])
    windows = (("短期", short), ("中期", medium))
    if reason == "当前水分仍有缓冲，但近7日土墒走弱" and direction is not None and trend_value is not None:
        suffix = " " + escape(trend_value) if trend_value else ""
        return "当前水分仍有缓冲，但近7日 <strong>土墒" + escape(direction) + "</strong>" + suffix
    if reason == "土墒低于同期且近7日继续下降，未来至少一段EC/GFS均明显偏干":
        dry = next(((window, value) for window, value in windows if value in _DRY_CLASSIFICATIONS), None)
        tail = "" if dry is None else f"，{dry[0]}降雨 {_focus_classification_html(dry[1])}"
        return "当前 <strong>土墒低于同期</strong>，且近7日 <strong>土墒继续下降</strong>" + tail
    if reason == "土墒低于同期，并叠加持续一致偏干及明显偏热信号":
        dry_windows = [(window, value) for window, value in windows if value in _DRY_CLASSIFICATIONS]
        tail = "" if not dry_windows else "，未来降雨 " + "、".join(_focus_classification_html(value) for _, value in dry_windows)
        return "当前 <strong>土墒低于同期</strong>，并叠加持续一致偏干及明显偏热信号" + tail
    if reason == "EC/GFS方向分歧，预报不确定性较高":
        divergent = next(((window, value) for window, value in windows if value == "模型方向分歧"), None)
        return "预报不确定性较高" if divergent is None else f"{divergent[0]}EC/GFS {_focus_classification_html(divergent[1])}"
    if reason == "未来存在一致偏干信号，需跟踪水分变化":
        dry = next(((window, value) for window, value in windows if value in _DRY_CLASSIFICATIONS), None)
        return escape(reason) if dry is None else f"{dry[0]}降雨 {_focus_classification_html(dry[1])}，需跟踪水分变化"
    if reason == "高权重核心产区出现明确天气变化":
        return "核心产区近期天气 <strong>发生明显变化</strong>"
    return escape(reason)


_COMPREHENSIVE_EMPHASIS = (
    "EC/GFS方向分歧", "EC冷信号更强", "GFS冷信号更强", "土墒低于同期", "继续下降",
    "土墒走弱", "略高于", "差异较小", "高于", "低于",
)
_COMPREHENSIVE_EMPHASIS_RE = re.compile("|".join(map(re.escape, _COMPREHENSIVE_EMPHASIS)))


def _weather_comprehensive_markdown(value: str) -> str:
    parts = value.split("**")
    for index in range(0, len(parts), 2):
        parts[index] = _COMPREHENSIVE_EMPHASIS_RE.sub(lambda match: f"**{match.group(0)}**", parts[index])
    return "**".join(parts)


def _focus_table_html(focus: list[dict[str, Any]]) -> str:
    columns = (
        ("region", "地区"), ("status", "状态"), ("current_state", "当前状态"),
        ("recent_change", "近7日变化"), ("short_term", "短期1—7天"),
        ("medium_term", "中期8—14天"), ("reason", "关注原因"),
    )
    headers = "".join(f'<th style="padding:.45rem .5rem;text-align:left;white-space:nowrap;border-bottom:1px solid #dfe3e8">{label}</th>' for _, label in columns)
    body = []
    for row in focus:
        values = {
            "region": escape(str(row["region"])),
            "status": escape(str(row["status"])),
            "current_state": _focus_current_state_html(str(row["current_state"])),
            "recent_change": _focus_recent_change_html(str(row["recent_change"])),
            "short_term": _focus_classification_html(str(row["short_term"])),
            "medium_term": _focus_classification_html(str(row["medium_term"])),
            "reason": _focus_reason_html(row),
        }
        cells = []
        for key, _ in columns:
            status_style = _status_cell_style(str(row["status"])) if key == "status" else ""
            wrap = "white-space:normal;min-width:15rem" if key == "reason" else "white-space:nowrap"
            cells.append(f'<td style="padding:.45rem .5rem;vertical-align:top;border-bottom:1px solid #edf0f2;{wrap};{status_style}">{values[key]}</td>')
        body.append("<tr>" + "".join(cells) + "</tr>")
    return '<div style="overflow-x:auto"><table style="width:100%;border-collapse:collapse;font-size:.9rem"><thead><tr>' + headers + "</tr></thead><tbody>" + "".join(body) + "</tbody></table></div>"


def _render_weather_detail(facts: dict[str, Any]) -> None:
    render = facts.get("weather_render") or {}
    if not render:
        return
    st.markdown("### 综合")
    st.markdown(_weather_comprehensive_markdown(str(render["comprehensive"])))
    st.markdown("### 重点关注")
    focus = render.get("focus_regions") or []
    if focus:
        st.markdown(_focus_table_html(focus), unsafe_allow_html=True)
    else:
        st.caption("当前暂无需要特别关注的地区。")
    for label, key in (
        ("完整降水数据", "rain_markdown"),
        ("完整最高气温数据", "temperature_markdown"),
        ("完整土墒数据", "soil_markdown"),
    ):
        with st.expander(label, expanded=False):
            st.markdown(render[key])


def _crop_progress_history_text(item: dict[str, Any], *, five_year: bool) -> str:
    suffix = "5年平均" if five_year else "去年同期"
    direction_key = "direction_vs_five_year" if five_year else "direction_vs_last_year"
    difference_key = "difference_vs_five_year_pct_points" if five_year else "difference_vs_last_year_pct_points"
    direction = str(item.get(direction_key, "样本不足"))
    difference = item.get(difference_key)
    if direction == "样本不足" or difference is None:
        return f"相对{suffix}历史样本不足"
    if direction == "接近":
        return f"较{suffix} **接近**"
    return f"较{suffix} **{direction}** {abs(float(difference)):g}个百分点"


def _crop_condition_history_text(item: dict[str, Any], *, five_year: bool) -> str:
    suffix = "5年平均" if five_year else "去年同期"
    direction_key = "direction_vs_five_year" if five_year else "direction_vs_last_year"
    difference_key = "difference_vs_five_year_pct_points" if five_year else "difference_vs_last_year_pct_points"
    direction = str(item.get(direction_key, "样本不足"))
    difference = item.get(difference_key)
    if direction == "样本不足" or difference is None:
        return f"相对{suffix}历史样本不足"
    if direction == "接近":
        return f"与{suffix} **接近**"
    display_direction = "高" if direction == "高于" else "低"
    return f"较{suffix} **{display_direction}** {abs(float(difference)):g}个百分点"


def _crop_week_change_text(item: dict[str, Any]) -> tuple[str, str]:
    change = item.get("week_change_pct_points")
    if change is None:
        return "较上周暂无可比值", "unknown"
    numeric = float(change)
    if numeric > 0:
        return f"较上周 **回升** {numeric:g}个百分点", "improving"
    if numeric < 0:
        return f"较上周 **下降** {abs(numeric):g}个百分点", "weakening"
    return "较上周持平", "stable"


def _crop_comprehensive_markdown(payload: dict[str, Any]) -> str:
    facts = payload.get("facts", {})
    metrics = list(facts.get("metrics") or [])
    focus_ids = {
        str(item.get("metric")) for item in (facts.get("focus_metrics") or [])
    }
    progress = [
        item
        for item in metrics
        if item.get("metric_kind") == "progress" and str(item.get("metric")) in focus_ids
    ][:2]
    condition = next(
        (item for item in metrics if item.get("metric") == "GOOD_EXCELLENT"),
        None,
    )
    progress_direction = next(
        (
            str(value)
            for value in payload.get("classifications", [])
            if str(value) in {"偏快", "偏慢", "接近历史水平"}
        ),
        "接近历史水平",
    )
    if progress_direction == "偏快":
        opening = "当前美豆生育进度整体相对历史 **领先**。"
        closing_progress = "当前生育进度整体偏快"
    elif progress_direction == "偏慢":
        opening = "当前美豆生育进度整体相对历史 **落后**。"
        closing_progress = "当前生育进度整体偏慢"
    else:
        opening = "当前美豆生育进度整体 **接近** 历史水平。"
        closing_progress = "当前生育进度基本正常"

    progress_parts = []
    for item in progress:
        progress_parts.append(
            f'{item["display_name"]}{float(item["current_pct"]):g}%，'
            + _crop_progress_history_text(item, five_year=False)
            + "、"
            + _crop_progress_history_text(item, five_year=True)
        )
    progress_sentence = "；".join(progress_parts) + "。" if progress_parts else ""

    condition_sentence = ""
    condition_trend = "unknown"
    if condition is not None:
        week_text, condition_trend = _crop_week_change_text(condition)
        condition_sentence = (
            f'优良率{float(condition["current_pct"]):g}%，{week_text}，当前'
            + _crop_condition_history_text(condition, five_year=False)
            + "、"
            + _crop_condition_history_text(condition, five_year=True)
            + "。"
        )

    condition_last_year = None if condition is None else condition.get("direction_vs_last_year")
    trend_text = {
        "improving": "作物状况本周边际改善",
        "weakening": "作物状况本周边际转弱",
        "stable": "作物状况本周保持稳定",
        "unknown": "作物状况周度变化暂不可比",
    }[condition_trend]
    if condition_last_year == "低于":
        history_tail = "，但优良率仍**低**于去年同期"
    elif condition_last_year == "高于":
        history_tail = "，且优良率仍**高**于去年同期"
    elif condition_last_year == "接近":
        history_tail = "，优良率与去年同期 **接近**"
    else:
        history_tail = ""
    closing = f"整体来看，{closing_progress}，{trend_text}{history_tail}。"
    return opening + progress_sentence + condition_sentence + closing


def _render_crop_detail(payload: dict[str, Any]) -> None:
    st.markdown("### 综合")
    st.markdown(_crop_comprehensive_markdown(payload))


def _crop_short_markdown(value: str) -> str:
    return value.replace("**低于**", "**低**于").replace("**高于**", "**高**于")


def render_summary_panel(summary: Any, *, compact: bool = False) -> None:
    payload=summary.to_dict() if hasattr(summary,"to_dict") else dict(summary)
    with st.container(border=True):
        st.markdown(f'### {payload["headline"]}')
        if payload.get("module") == "weather":
            forecast = payload.get("facts", {}).get("forecast_end", {})
            parts = [f'数据更新至：{payload.get("source_date") or "—"}']
            if forecast.get("ECMWF"): parts.append(f'EC预测至：{forecast["ECMWF"]}')
            if forecast.get("GFS"): parts.append(f'GFS预测至：{forecast["GFS"]}')
            st.caption('｜'.join(parts))
        elif payload.get("module") == "soybean_crop":
            crop_time = str(payload.get("facts", {}).get("data_time_label", "数据更新至：—"))
            st.caption(crop_time.replace("（详见“当前有效指标”）", ""))
        elif compact:
            st.caption(f'数据更新至：{payload.get("source_date") or "—"}')
        else:
            st.caption(f'数据身份：{payload.get("source_date") or "—"} · {payload.get("freshness_status","unknown")} · 规则 {payload.get("rule_version","—")}')
        weather_render = payload.get("facts", {}).get("weather_render") if payload.get("module") == "weather" else None
        if compact:
            compact_text = str(payload["short_text"])
            st.markdown(_crop_short_markdown(compact_text) if payload.get("module") == "soybean_crop" else compact_text)
        elif weather_render:
            _render_weather_detail(payload["facts"])
        elif payload.get("module") == "soybean_crop":
            _render_crop_detail(payload)
        else:
            st.markdown(payload["detail_text"])
        if payload.get("missing_reason"): st.warning(payload["missing_reason"])
